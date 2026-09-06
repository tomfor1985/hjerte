from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from study.tests import test_study as base
from study.tests import test_coverage as coverage
from study.models import ApiBudget, ApiCall, GenerationJob, Question, LearningObjective, ObjectiveEvidence
from study.studio_summary import cost_summary, next_step
from study.question_resume import queue_saved_questions


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False, AI_GENERATION_ENABLED=False)
class StudioSummaryTests(TestCase):
    setUpTestData=classmethod(base.StudyTests.setUpTestData.__func__)
    make_question=classmethod(base.StudyTests.make_question.__func__)
    prepare=coverage.CoverageTests.prepare
    fresh_target=coverage.CoverageTests.fresh_target

    def setUp(self):
        self.chapter.last_page=1;self.chapter.save()
        self.budget=ApiBudget.objects.create(pk=1,allowance_nok=200)
        self.client.force_login(self.user)

    def job(self,**kwargs):
        return GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,status='complete',**kwargs)

    def call(self,job,purpose,amount=None,state='settled',reserved='10'):
        return ApiCall.objects.create(job=job,purpose=purpose,model='gpt-5.6-sol',state=state,
            reserved_nok=Decimal(reserved),actual_nok=Decimal(amount) if amount is not None else None)

    def saved_job(self):
        job=self.job(spend_limit_nok=25)
        job.status='failed';job.save()
        q=self.make_question(11)
        q.status='quarantined';q.verification={'state':'awaiting_independent_review','provenance':{'job_id':str(job.pk)}};q.save()
        return job

    def test_costs_reconcile_without_counting_settled_reservations_twice(self):
        job=self.job(published=2)
        self.call(job,'generate','3',reserved='50')
        self.call(job,'question-review','1',reserved='40')
        self.call(job,'question-review',state='planned',reserved='2')
        self.call(job,'generate',state='uncertain',reserved='5')
        self.call(job,'question-review',state='cancelled',reserved='9')
        self.budget.accounted_nok=11;self.budget.save()
        result=cost_summary(self.budget)
        self.assertEqual((result['spent'],result['reserved'],result['uncertain'],result['difference']),
                         (Decimal('4'),Decimal('2'),Decimal('5'),Decimal('0')))
        self.assertEqual(result['flows'][1]['unit'],2)
        self.assertEqual(sum(r['spent'] for r in result['categories']),result['spent'])
        self.budget.refresh_from_db();self.assertEqual(self.budget.accounted_nok,11)

    def test_mapping_matching_and_extras_do_not_inflate_question_rate(self):
        mapping=self.job(kind='map')
        self.call(mapping,'map-objectives','2');self.call(mapping,'map-review','1');self.call(mapping,'map-repair','3')
        matching=self.job(kind='reconcile');self.call(matching,'reconcile-review','2')
        questions=self.job(published=2,audit={'question_pipeline':'compact-2'})
        self.call(questions,'generate','2');self.call(questions,'question-review','1');self.call(questions,'question-extra-review','1')
        result=cost_summary(self.budget)
        rows={r['key']:r['spent'] for r in result['categories']}
        self.assertEqual(rows,dict(inventory=3,matching=2,writing=2,checking=1,extra=4))
        self.assertEqual(result['preparation'],8)
        self.assertEqual(result['flows'][0]['unit'],2)
        self.assertEqual(result['flows'][0]['spent'],4)

    def test_new_flow_does_not_inherit_old_or_resumed_mixed_costs(self):
        old=self.job(published=4)
        self.call(old,'generate','15');self.call(old,'blind-review','5')
        self.call(old,'question-review','1') # An older run resumed on the new code.
        current=self.job(published=2,audit={'question_pipeline':'compact-2'})
        self.call(current,'generate','1');self.call(current,'question-review','1')
        result=cost_summary(self.budget)
        self.assertEqual(result['flows'][0]['unit'],1)
        self.assertEqual(result['flows'][1]['unit'],Decimal('5.25'))
        # Guard against wrongly tagged older jobs as well.
        old.audit={'question_pipeline':'compact-2'};old.save()
        self.assertEqual(cost_summary(self.budget)['flows'][0]['unit'],1)

    def test_empty_or_failed_run_never_reports_zero_per_published_question(self):
        self.assertIsNone(cost_summary(self.budget)['flows'][0]['unit'])
        job=self.job(audit={'question_pipeline':'compact-2'})
        self.call(job,'generate','2')
        result=cost_summary(self.budget)['flows'][0]
        self.assertIsNone(result['unit']);self.assertEqual(result['spent'],2)

    def test_missing_usage_unknown_state_and_ledger_difference_stay_visible(self):
        job=self.job()
        self.call(job,'new-purpose',state='settled',reserved='3')
        self.call(job,'future-purpose',state='unrecognized',reserved='2')
        self.budget.accounted_nok=7;self.budget.save()
        result=cost_summary(self.budget)
        self.assertEqual(result['uncertain'],5);self.assertEqual(result['missing_usage'],2)
        self.assertEqual(result['difference'],2)
        response=self.client.get('/studio/')
        self.assertContains(response,'Ledger difference: 2.0000 NOK')
        self.assertContains(response,'Other recorded work')

    def test_existing_saved_drafts_are_the_next_step_and_rejected_drafts_are_not(self):
        self.fresh_target()
        job=self.saved_job();self.call(job,'generate','2')
        result=cost_summary(self.budget);step=next_step(result,self.user)
        self.assertEqual(step['kind'],'resume');self.assertEqual(step['job'].pk,job.pk)
        self.assertEqual(step['job'].available,23)
        Question.objects.filter(verification__provenance__job_id=str(job.pk)).update(
            verification={'state':'reviewed','provenance':{'job_id':str(job.pk)}})
        self.assertEqual(next_step(cost_summary(self.budget),self.user)['kind'],'generate')

    def test_unsettled_saved_job_explains_stop_and_has_no_resume_button(self):
        job=self.saved_job();self.call(job,'generate',state='uncertain',reserved='4')
        response=self.client.get('/studio/')
        self.assertContains(response,'Saved questions need attention')
        self.assertNotContains(response,'name="action" value="resume_questions"')
        with self.assertRaises(ValidationError):queue_saved_questions(job.pk,self.user)
        job.refresh_from_db();self.assertEqual(job.status,'failed')

    def test_spent_cap_cannot_be_bypassed_by_posting_resume(self):
        job=self.saved_job();self.call(job,'generate','25')
        step=next_step(cost_summary(self.budget),self.user)
        self.assertEqual(step['kind'],'held')
        with self.assertRaises(ValidationError):queue_saved_questions(job.pk,self.user)
        self.client.post('/studio/',{'action':'resume_questions','job_id':str(job.pk)})
        job.refresh_from_db();self.assertEqual(job.status,'failed')

    def test_retired_source_cannot_be_resumed(self):
        job=self.saved_job();self.source.active=False;self.source.save()
        self.assertEqual(next_step(cost_summary(self.budget),self.user)['kind'],'held')
        with self.assertRaises(ValidationError):queue_saved_questions(job.pk,self.user)

    def test_already_funded_check_can_resume_with_no_free_allowance(self):
        job=self.saved_job();self.call(job,'generate','20')
        planned=self.call(job,'question-review',state='planned',reserved='5')
        job.audit={'reserved_question_review':str(planned.pk)};job.save()
        self.budget.allowance_nok=25;self.budget.accounted_nok=25;self.budget.save()
        step=next_step(cost_summary(self.budget),self.user)
        self.assertEqual(step['kind'],'resume');self.assertEqual(step['job'].available,5)
        queue_saved_questions(job.pk,self.user)
        job.refresh_from_db();self.assertEqual(job.status,'queued')
        self.budget.refresh_from_db();self.assertEqual(self.budget.accounted_nok,25)

    def test_active_job_takes_priority(self):
        self.saved_job()
        active=self.job();active.status='running';active.save()
        step=next_step(cost_summary(self.budget),self.user)
        self.assertEqual(step['kind'],'wait');self.assertEqual(step['job'].pk,active.pk)

    def test_another_admins_draft_does_not_offer_resume(self):
        self.fresh_target();job=self.saved_job();job.requested_by=self.other;job.save()
        result=cost_summary(self.budget);step=next_step(result,self.user)
        self.assertEqual(step['kind'],'generate')
        self.assertFalse(next(j for j in result['jobs'] if j.pk==job.pk).can_resume)

    def test_pending_objectives_link_to_the_correct_matching_step(self):
        obj=self.fresh_target();obj.reconciliation_status='pending';obj.save()
        step=next_step(cost_summary(self.budget),self.user)
        self.assertEqual(step['kind'],'inventory');self.assertIn('kind=reconcile',step['url'])
        response=self.client.get(step['url'])
        self.assertContains(response,'value="reconcile" selected')
        self.assertContains(response,'id="inventory-step"')

    def test_unclassified_questions_link_to_linking_instead_of_more_mapping(self):
        self.prepare(classify=False)
        step=next_step(cost_summary(self.budget),self.user)
        self.assertIn('kind=link_questions',step['url'])

    def test_ready_chapter_is_prefilled_without_starting_api_work(self):
        self.fresh_target()
        with patch('study.generation.ask_model') as ai:
            response=self.client.get('/studio/')
            self.assertContains(response,'Cover the next learning objectives')
            self.assertEqual(response.context['generate_form']['chapter'].value(),self.chapter.pk)
            ai.assert_not_called()
        self.assertEqual(GenerationJob.objects.count(),0)
        self.assertEqual(ApiCall.objects.count(),0)

    def test_generation_post_tags_new_cost_cohort_and_preserves_selected_cap(self):
        self.fresh_target()
        with override_settings(AI_GENERATION_ENABLED=True,OPENAI_API_KEY='fake-key'):
            response=self.client.post('/studio/',{'action':'generate','chapter':self.chapter.pk,'count':'5','spend_limit_nok':'12.50'})
        self.assertEqual(response.status_code,302)
        job=GenerationJob.objects.get()
        self.assertEqual(job.audit['question_pipeline'],'source-1')
        self.assertEqual(job.spend_limit_nok,Decimal('12.50'))
        self.assertEqual(ApiCall.objects.count(),0)

    def test_field_errors_stay_visible_and_do_not_start_a_job(self):
        response=self.client.post('/studio/',{'action':'generate','chapter':self.chapter.pk,'count':'5','spend_limit_nok':'-1'})
        self.assertContains(response,'Ensure this value is greater than or equal to 1')
        self.assertContains(response,'class="studio-options" open')
        self.assertEqual(GenerationJob.objects.count(),0)

    def test_studio_costs_remain_staff_only_and_private(self):
        response=self.client.get('/studio/')
        self.assertEqual(response.status_code,200);self.assertIn('no-store',response['Cache-Control'])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/studio/').status_code,302)
        self.client.logout()
        self.assertEqual(self.client.get('/studio/').status_code,302)
