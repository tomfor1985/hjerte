from decimal import Decimal
from django.test import TestCase, override_settings
from study.tests import test_study as base
from study.models import ApiBudget,ApiCall,GenerationJob,LearningObjective,ObjectiveEvidence,CoverageSegment,Source,SourcePage
from study.coverage import segments_for
from study.studio_summary import cost_summary
from study.completion_estimate import completion_estimate


@override_settings(ALLOWED_HOSTS=['testserver'],SECURE_SSL_REDIRECT=False,AI_GENERATION_ENABLED=False)
class CompletionEstimateTests(TestCase):
    setUpTestData=classmethod(base.StudyTests.setUpTestData.__func__)
    make_question=classmethod(base.StudyTests.make_question.__func__)

    def setUp(self):
        self.budget=ApiBudget.objects.create(pk=1,allowance_nok=200,accounted_nok=Decimal('3.5'))
        self.second=SourcePage.objects.create(source=self.source,number=2,text='Uninspected clinical content. '*50)
        self.objective=LearningObjective.objects.create(title='A precise source-supported clinical decision',topic=self.topic)
        ObjectiveEvidence.objects.create(objective=self.objective,chapter=self.chapter,references=self.question.references)
        self.question.objective=self.objective;self.question.save()
        self.record=CoverageSegment.objects.create(**{k:s for k,s in next(segments_for(self.page)).items() if k in ('page','start','end','digest')},
            status='partial',audit={'objective_ids':[self.objective.pk]})
        for kind,purpose,amount in [('questions','generate','1'),('map','map-objectives','2'),('reconcile','reconcile','0.5')]:
            job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,kind=kind,status='complete',published=1 if kind=='questions' else 0)
            ApiCall.objects.create(job=job,model='gpt-5.6-terra',purpose=purpose,state='settled',reserved_nok=5,actual_nok=Decimal(amount))

    def estimate(self):
        return completion_estimate(cost_summary(self.budget))

    def test_full_scenario_includes_preparation_and_never_counts_it_twice(self):
        result=self.estimate();self.assertTrue(result['available']);self.assertFalse(result['complete'])
        self.assertGreater(result['objectives_high'],result['objectives_low'])
        for row in result['scenarios']:
            self.assertEqual(row['additional_low'],result['preparation_low']+row['question_cost_low'])
            self.assertEqual(row['additional_high'],result['preparation_high']+row['question_cost_high'])
            self.assertEqual(row['total_low'],Decimal('3.5')+row['additional_low'])
            self.assertGreaterEqual(row['questions_low'],result['known_missing'])
        self.assertFalse(result['new_flow_measured'])

    def test_notes_add_checking_work_not_another_copy_of_objectives(self):
        before=self.estimate()
        notes=Source.objects.create(title='Study notes',kind='notes',sha256='e'*64,page_count=1)
        notes.supporting_guidelines.add(self.source)
        SourcePage.objects.create(source=notes,number=1,text=self.second.text*10)
        after=self.estimate()
        self.assertEqual((after['objectives_low'],after['objectives_high']),(before['objectives_low'],before['objectives_high']))
        self.assertGreater(after['remaining_batches'],before['remaining_batches'])
        self.assertGreater(after['preparation_low'],before['preparation_low'])

    def test_alternate_format_does_not_inflate_the_library(self):
        before=self.estimate()
        duplicate=Source.objects.create(title='Alternate copy',kind='guideline',sha256='e'*64,page_count=1,duplicate_of=self.source)
        SourcePage.objects.create(source=duplicate,number=1,text=self.second.text*10)
        after=self.estimate()
        self.assertEqual(after['objectives_high'],before['objectives_high'])
        self.assertEqual(after['guideline_pages'],2)

    def test_pdf_column_padding_does_not_inflate_objective_density(self):
        before=self.estimate()
        self.second.text=self.second.text.replace(' ',' '*40);self.second.save()
        after=self.estimate()
        self.assertEqual(after['objectives_low'],before['objectives_low'])
        self.assertEqual(after['objectives_high'],before['objectives_high'])
        self.assertEqual(after['total_characters'],before['total_characters'])

    def test_complete_covered_inventory_has_zero_additional_cost(self):
        self.record.status='mapped';self.record.save()
        CoverageSegment.objects.create(**{k:s for k,s in next(segments_for(self.second)).items() if k in ('page','start','end','digest')},
            status='mapped',audit={'objective_ids':[]})
        result=self.estimate()
        self.assertTrue(result['complete']);self.assertTrue(result['available'])
        self.assertEqual(result['objectives_low'],1)
        self.assertEqual(result['scenarios'][0]['additional_high'],0)
        self.assertEqual(result['scenarios'][0]['total_high'],Decimal('3.5'))

    def test_missing_pages_prevent_a_falsely_complete_price(self):
        self.second.delete()
        result=self.estimate()
        self.assertFalse(result['available']);self.assertEqual(result['missing_pages'],1)
        self.assertIn('no extracted text',result['reason'])

    def test_changed_source_invalidates_the_density_sample(self):
        self.page.text+=' Changed source.';self.page.save()
        result=self.estimate()
        self.assertFalse(result['available']);self.assertEqual(result['sampled_characters'],0)

    def test_missing_matching_rate_does_not_silently_price_that_stage_at_zero(self):
        ApiCall.objects.filter(purpose='reconcile').delete()
        result=self.estimate()
        self.assertFalse(result['available']);self.assertIn('every remaining stage',result['reason'])
        self.assertIsNotNone(result['known_cost_low'])

    def test_depth_is_an_alternative_total_using_only_useful_angles(self):
        self.objective.variant_limit=2;self.objective.depth_reason='Recall a criterion; apply the criterion';self.objective.save()
        result=self.estimate();basic,depth=result['scenarios']
        self.assertEqual(depth['target_low'],basic['target_low']*2)
        self.assertEqual(depth['additional_low'],result['preparation_low']+depth['question_cost_low'])
        self.assertEqual(result['depth_ratio'],2)

    def test_preview_is_private_and_does_not_spend_or_queue_anything(self):
        self.client.force_login(self.user)
        before=(ApiCall.objects.count(),GenerationJob.objects.count(),self.budget.accounted_nok)
        response=self.client.get('/studio/')
        self.assertContains(response,'What could adequate coverage cost?')
        self.assertContains(response,'Early planning scenario')
        self.assertContains(response,'This is not a guaranteed maximum')
        self.assertIn('no-store',response['Cache-Control'])
        self.budget.refresh_from_db()
        self.assertEqual(before,(ApiCall.objects.count(),GenerationJob.objects.count(),self.budget.accounted_nok))
