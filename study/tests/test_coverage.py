import json
from decimal import Decimal
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from study.tests.test_study import StudyTests
from study.models import (LearningObjective, ObjectiveEvidence, CoverageSegment, Question,
                          Source, SourcePage, Chapter, GenerationJob, ApiBudget, ApiCall)
from study.coverage import (chapter_segments, plan_targets, objective_rows, coverage_report,
                            cost_forecast, record_failed_objective)
from study.generation import (run_job, DraftQuestion, DraftBatch, ReviewBatch, Verdict,
                              NoveltyVerdict, NoveltyReviewBatch)
from study.mapping import (save_map, MappedObjective, ObjectiveMap, ObjectiveCheck, MapReview, PartInventory, PartCheck, mapping_context)
from study.engine import start_session, save_answer, public_item


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False, AI_GENERATION_ENABLED=False)
class CoverageTests(TestCase):
    setUpTestData=classmethod(StudyTests.setUpTestData.__func__)
    make_question=classmethod(StudyTests.make_question.__func__)

    def setUp(self):
        self.chapter.last_page=1; self.chapter.save()
        self.source.page_count=1; self.source.save()
        self.job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,count=1)

    def prepare(self, limit=1, classify=True):
        objective=LearningObjective.objects.create(title='Apply the supported target to the stated population',
            topic=self.topic,variant_limit=limit,depth_reason='Identify the target; interpret tolerance' if limit>1 else 'Identify the target')
        ObjectiveEvidence.objects.create(objective=objective,chapter=self.chapter,references=self.question.references)
        for s in chapter_segments(self.chapter):
            CoverageSegment.objects.get_or_create(**{k:s[k] for k in ('page','start','digest')},defaults={'end':s['end'],'status':'mapped'})
        if classify:
            self.question.objective=objective;self.question.save()
        return objective

    def fresh_target(self, limit=1):
        obj=self.prepare(limit)
        self.question.status='retired';self.question.save()
        return obj

    def test_unmapped_chapter_blocks_before_network(self):
        with patch('study.generation.ask_model') as ai:
            with self.assertRaises(ValidationError):run_job(self.job)
            ai.assert_not_called()

    def test_existing_questions_must_be_classified(self):
        self.prepare(classify=False)
        with self.assertRaisesMessage(ValidationError,'Link the existing'):
            plan_targets(self.chapter,'coverage',5)

    def test_ceiling_stops_queued_job_without_network(self):
        self.prepare()
        with patch('study.generation.ask_model') as ai:
            run_job(self.job)
            ai.assert_not_called()
        self.assertIn('No eligible',self.job.message)

    def test_two_failures_pause_without_marking_covered(self):
        obj=self.fresh_target()
        self.job.count=50
        with patch('study.generation.ask_model',return_value=DraftBatch(questions=[])) as ai:
            run_job(self.job)
            self.assertEqual(ai.call_count,2)
        obj.refresh_from_db()
        self.assertEqual(obj.failed_attempts,2)
        row=objective_rows()[0]
        self.assertEqual(row['state'],'Needs review');self.assertEqual(row['missing'],1)

    def test_global_breadth_precedes_variants_and_counts_across_documents(self):
        obj=self.prepare(limit=2)
        other=LearningObjective.objects.create(title='Another important uncovered decision',topic=self.topic)
        ObjectiveEvidence.objects.create(objective=other,chapter=self.chapter,references=self.question.references)
        self.assertEqual(plan_targets(self.chapter,'coverage',5),[other])
        with self.assertRaisesMessage(ValidationError,'Cover the remaining'):
            plan_targets(self.chapter,'variants',5)
        other.active=False;other.save()
        self.assertEqual(plan_targets(self.chapter,'variants',5),[obj])
        other_source=Source.objects.create(title='Another primary guideline',kind='guideline',sha256='b'*64,page_count=1)
        chapter=Chapter.objects.create(source=other_source,topic=self.topic,title='Same concept')
        q=self.make_question(100,objective=obj);q.chapter=chapter;q.save()
        self.assertEqual(plan_targets(self.chapter,'variants',5),[])
        other_source.active=False;other_source.save()
        self.assertEqual(plan_targets(self.chapter,'variants',5),[obj])

    def test_maximum_three_and_reason_for_depth(self):
        obj=self.prepare()
        obj.variant_limit=2
        obj.depth_reason=''
        with self.assertRaises(ValidationError):obj.full_clean()
        with self.assertRaises(IntegrityError),transaction.atomic():
            LearningObjective.objects.filter(pk=obj.pk).update(variant_limit=4)

    def draft_and_reviews(self,obj,novel=True):
        draft=DraftQuestion(objective_id=obj.pk,testing_angle='Select the appropriate treatment goal',
            stem='Which systolic goal is recommended when the treatment is tolerated?',choices=self.question.choices,
            answer=2,explanation='This is supported by the source.',learning_point=obj.title,
            references=self.question.references,difficulty='basic',question_type='direct')
        verdict=Verdict(index=0,best_answer=2,single_best_answer=True,evidence_supports_answer=True,
            reference_section_accurate=True,explanations_accurate=True,within_source_scope=True,notes='Checked')
        novelty=NoveltyVerdict(**verdict.model_dump(),objective_matches=True,adds_distinct_testing_angle=novel)
        return [DraftBatch(questions=[draft]),ReviewBatch(verdicts=[verdict]),NoveltyReviewBatch(verdicts=[novelty])]

    def test_semantic_duplicate_is_held_even_when_medically_correct(self):
        obj=self.fresh_target()
        with patch('study.generation.ask_model',side_effect=self.draft_and_reviews(obj,False)):
            run_job(self.job)
        q=Question.objects.exclude(pk=self.question.pk).get()
        self.assertEqual(q.status,'quarantined')
        self.assertFalse(q.verification['rationale_review']['adds_distinct_testing_angle'])
        self.assertEqual(self.job.published,0)

    def test_generation_uses_exact_objective_and_global_question_catalogue(self):
        obj=self.fresh_target()
        other=self.make_question(10)
        other_source=Source.objects.create(title='Other guideline',kind='guideline',sha256='d'*64,page_count=1)
        other_chapter=Chapter.objects.create(source=other_source,topic=self.topic,title='Other chapter')
        other.chapter=other_chapter;other.save()
        with patch('study.generation.ask_model',side_effect=self.draft_and_reviews(obj)) as ai:
            run_job(self.job)
        prompt=json.loads(ai.call_args_list[0].args[4])
        self.assertEqual(prompt['planned_objectives'][0]['id'],obj.pk)
        self.assertIn(str(other.pk),[q['id'] for q in prompt['existing_questions']])
        self.assertEqual(Question.objects.exclude(pk__in=[self.question.pk,other.pk]).get().objective_id,obj.pk)
        self.assertEqual(self.job.published,1)

    def map_fixture(self):
        spec=chapter_segments(self.chapter)[0]
        segment=CoverageSegment.objects.create(**{k:spec[k] for k in ('page','start','end','digest')})
        objective=MappedObjective(title='Identify the recommended systolic target',
            testing_angles=['Recall the target'],references=self.question.references,part_ids=[0])
        draft=ObjectiveMap(objectives=[objective],parts=[PartInventory(part_id=0,disposition='learning',reason='Teaching point')],unresolved_content='')
        check=ObjectiveCheck(index=0,supported=True,useful_angles=True,correct_source_parts=True)
        review=MapReview(complete_inventory=True,missing_points=[],checks=[check],parts=[PartCheck(part_id=0,all_teaching_points_covered=True,exclusion_justified=True)],notes='Complete for this segment')
        return segment,draft,review

    def save_fixture(self,segment,draft,review):
        group=[{'page':segment.page,'start':segment.start,'end':segment.end,'digest':segment.digest,'text':segment.page.text[segment.start:segment.end]}]
        return save_map(self.job,[segment],draft,review,mapping_context(self.job,group))

    def test_verified_mapping_classifies_without_rewriting_answers_or_attempts(self):
        session=start_session(self.user,count=1)
        snapshot=session.items.get().snapshot
        segment,draft,review=self.map_fixture()
        self.save_fixture(segment,draft,review)
        self.question.refresh_from_db();segment.refresh_from_db()
        self.assertEqual(segment.status,'mapped')
        self.assertIsNone(self.question.objective_id)
        obj=LearningObjective.objects.get()
        self.assertEqual(obj.reconciliation_status,'pending')
        self.assertFalse(coverage_report()['mapping_complete'])
        from study.reconciliation import MatchItem,MatchProposal,MatchCheck,MatchReview,apply_objective_matches,apply_question_links,catalogue
        draft_match=MatchProposal(items=[MatchItem(id=str(obj.pk),target_id=None,uncertain=False,reason='Distinct concept')])
        check_match=MatchReview(checks=[MatchCheck(id=str(obj.pk),target_id=None,supported=True,reason='Checked all concepts')])
        apply_objective_matches([obj],draft_match,check_match,catalogue())
        link=MatchProposal(items=[MatchItem(id=str(self.question.pk),target_id=obj.pk,uncertain=False,reason='Tests this objective')])
        checked=MatchReview(checks=[MatchCheck(id=str(self.question.pk),target_id=obj.pk,supported=True,reason='Matches the source')])
        apply_question_links([self.question],link,checked,catalogue())
        self.question.refresh_from_db()
        self.assertEqual(self.question.objective_id,obj.pk)
        self.assertEqual(self.question.answer,2)
        self.assertEqual(session.items.get().snapshot,snapshot)
        self.assertTrue(coverage_report()['mapping_complete'])

    def test_rejected_inventory_cannot_claim_coverage(self):
        segment,draft,review=self.map_fixture()
        review.complete_inventory=False
        self.save_fixture(segment,draft,review)
        self.assertFalse(LearningObjective.objects.exists())
        segment.refresh_from_db();self.assertEqual(segment.status,'blocked')
        self.assertFalse(coverage_report()['mapping_complete'])

    def test_unsupported_notes_remain_unresolved(self):
        segment,draft,review=self.map_fixture()
        notes=Source.objects.create(title='AI notes',kind='notes',sha256='c'*64,page_count=1)
        note_page=SourcePage.objects.create(source=notes,number=1,text='An unsupported teaching claim.')
        import hashlib
        segment.page=note_page;segment.end=len(note_page.text);segment.digest=hashlib.sha256(note_page.text.encode()).hexdigest();segment.save()
        self.job.notes_source=notes
        draft.unresolved_content='The claim has no primary guideline evidence.'
        self.save_fixture(segment,draft,review)
        self.assertFalse(LearningObjective.objects.exists())
        self.assertEqual(segment.status,'blocked')
        self.assertFalse(coverage_report()['mapping_complete'])

    def test_canonical_objective_reused_without_raising_ceiling(self):
        obj=LearningObjective.objects.create(title='Canonical treatment objective',topic=self.topic,variant_limit=1)
        segment,draft,review=self.map_fixture()
        ObjectiveEvidence.objects.create(objective=obj,chapter=self.chapter,references=self.question.references)
        draft.objectives[0].testing_angles=['Recall','Apply','Interpret']
        self.save_fixture(segment,draft,review)
        obj.refresh_from_db()
        from study.reconciliation import MatchItem,MatchProposal,MatchCheck,MatchReview,apply_objective_matches,catalogue
        pending=LearningObjective.objects.get(reconciliation_status='pending')
        proposal=MatchProposal(items=[MatchItem(id=str(pending.pk),target_id=obj.pk,uncertain=False,reason='Same concept')])
        checked=MatchReview(checks=[MatchCheck(id=str(pending.pk),target_id=obj.pk,supported=True,reason='Same scope')])
        apply_objective_matches([pending],proposal,checked,catalogue())
        self.assertEqual(LearningObjective.objects.filter(active=True).count(),1)
        self.assertEqual(obj.variant_limit,1)

    def test_changed_text_and_unmapped_notes_invalidate_complete_inventory(self):
        self.prepare()
        self.assertTrue(coverage_report()['mapping_complete'])
        self.page.text+=' A new source paragraph.';self.page.save()
        self.assertFalse(coverage_report()['mapping_complete'])
        with self.assertRaises(ValidationError):plan_targets(self.chapter,'coverage',1)

    def test_long_pages_are_split_and_no_unconfigured_pages_disappear(self):
        self.page.text='x'*25000;self.page.save()
        self.assertEqual(len(chapter_segments(self.chapter)),3)
        SourcePage.objects.create(source=self.source,number=2,text='Page outside all chapters')
        self.source.page_count=2;self.source.save()
        report=coverage_report()
        self.assertEqual(report['documents'][0]['gaps'],1)
        self.assertEqual(report['documents'][0]['total'],4)

    def test_short_segments_share_calls_without_creating_duplicate_objectives(self):
        page2=SourcePage.objects.create(source=self.source,number=2,text=self.page.text)
        self.chapter.last_page=2;self.chapter.save()
        segment,draft,review=self.map_fixture()
        self.job.kind='map'
        draft.objectives[0].part_ids=[0,1]
        draft.parts.append(PartInventory(part_id=1,disposition='learning',reason='Repeated teaching point'))
        review.parts.append(PartCheck(part_id=1,all_teaching_points_covered=True,exclusion_justified=True))
        with patch('study.mapping.ask_model',side_effect=[draft,review]) as ai:
            run_job(self.job)
        self.assertEqual(ai.call_count,2)
        self.assertEqual(CoverageSegment.objects.filter(status='mapped').count(),2)
        self.assertEqual(LearningObjective.objects.count(),1)
        prompt=json.loads(ai.call_args_list[0].args[4])
        self.assertEqual({p['page_id'] for p in prompt['parts']},{self.page.pk,page2.pk})

    def test_empty_page_is_blocked_without_api(self):
        self.page.text='';self.page.save()
        self.job.kind='map'
        with patch('study.mapping.ask_model') as ai:
            run_job(self.job)
            ai.assert_not_called()
        self.assertEqual(CoverageSegment.objects.get().status,'blocked')

    def test_retiring_evidence_invalidates_note_inventory(self):
        from study.sources import retire_source
        notes=Source.objects.create(title='Notes',kind='notes',sha256='e'*64,page_count=1)
        page=SourcePage.objects.create(source=notes,number=1,text='A learning point to verify.')
        segment=CoverageSegment.objects.create(page=page,start=0,end=len(page.text),digest='f'*64,
            status='mapped',audit={'evidence_source_id':self.source.pk})
        retire_source(self.source,self.user)
        segment.refresh_from_db();self.assertEqual(segment.status,'blocked')
        notes.refresh_from_db();self.assertTrue(notes.active)

    def test_selected_notes_restrict_generation_to_their_verified_objectives(self):
        first=self.fresh_target()
        second=LearningObjective.objects.create(title='A different note-specific learning objective',topic=self.topic)
        ObjectiveEvidence.objects.create(objective=second,chapter=self.chapter,references=self.question.references)
        notes=Source.objects.create(title='Selected notes',kind='notes',sha256='e'*64,page_count=1)
        notes.supporting_guidelines.add(self.source)
        page=SourcePage.objects.create(source=notes,number=1,text='A note-specific teaching point.')
        from study.coverage import segments_for
        spec=list(segments_for(page))[0]
        CoverageSegment.objects.create(**{k:spec[k] for k in ('page','start','end','digest')},
            status='mapped',audit={'objective_ids':[second.pk]})
        self.assertEqual(plan_targets(self.chapter,'coverage',5,notes),[second])

    @override_settings(AI_GENERATOR_MODEL='gpt-5.6-sol',AI_REVIEWER_MODEL='gpt-5.6-sol',AI_SERVICE_TIER='flex')
    def test_price_scenario_reprices_generation_without_discounting_reviews_twice(self):
        from study.generation import cost_nok
        self.fresh_target()
        self.job.published=1;self.job.save()
        budget=ApiBudget.objects.create(pk=1,allowance_nok=200)
        for model,purpose in [('gpt-6-astra','generate'),('gpt-5.6-sol','blind-review')]:
            ApiCall.objects.create(job=self.job,model=model,purpose=purpose,state='settled',reserved_nok=10,
                actual_nok=cost_nok(model,'flex',10000,1000,budget),input_tokens=10000,output_tokens=1000)
        f=cost_forecast(objective_rows(),False)
        self.assertEqual(f['planning_unit'],2*cost_nok('gpt-5.6-sol','flex',10000,1000,budget))
        self.assertLess(f['planning_unit'],f['unit'])

    def test_job_uses_saved_writer_and_reviewer_even_when_defaults_change(self):
        obj=self.fresh_target()
        self.job.generator_model='gpt-5.6-terra'
        self.job.reviewer_model='gpt-6-astra'
        with override_settings(AI_GENERATOR_MODEL='gpt-6-astra',AI_REVIEWER_MODEL='gpt-5.6-sol'),patch(
                'study.generation.ask_model',side_effect=self.draft_and_reviews(obj)) as ai:
            run_job(self.job)
        self.assertEqual([c.args[1] for c in ai.call_args_list],['gpt-5.6-terra','gpt-6-astra','gpt-6-astra'])
        q=Question.objects.exclude(pk=self.question.pk).get()
        self.assertEqual(q.generated_by,'gpt-5.6-terra')
        self.assertEqual(q.verification['reviewer'],'gpt-6-astra')

    def test_model_comparison_is_read_only_and_unknown_model_is_rejected(self):
        self.client.force_login(self.user)
        before=GenerationJob.objects.count()
        response=self.client.get('/studio/coverage/',{'generator_model':'gpt-5.6-terra','reviewer_model':'gpt-6-astra'})
        self.assertContains(response,'gpt-5.6-terra for questions, gpt-6-astra for blind')
        self.assertEqual(GenerationJob.objects.count(),before)
        response=self.client.get('/studio/coverage/',{'generator_model':'unpriced','reviewer_model':'gpt-5.6-sol'})
        self.assertContains(response,'Select a valid choice')

    def test_terra_pricing_reservation_uses_verified_standard_rates(self):
        from study.generation import cost_nok
        budget=ApiBudget()
        self.assertEqual(cost_nok('gpt-5.6-terra','default',10000,1000,budget),Decimal('0.5550'))

    def test_forecast_uses_job_totals_once_and_keeps_uncertain_cost_separate(self):
        obj=self.fresh_target(limit=3)
        self.job.published=2;self.job.save()
        for amount in (Decimal('2'),Decimal('4')):
            ApiCall.objects.create(job=self.job,model='gpt-6-astra',purpose='generate',state='settled',reserved_nok=10,actual_nok=amount)
        ApiCall.objects.create(job=self.job,model='gpt-6-astra',purpose='generate',state='uncertain',reserved_nok=20)
        f=cost_forecast(objective_rows(),False)
        self.assertEqual(f['unit'],Decimal('3'))
        self.assertEqual(f['basic'],Decimal('3'));self.assertEqual(f['extended'],Decimal('9'))
        self.assertEqual(f['uncertain'],Decimal('20'));self.assertFalse(f['complete'])

    def test_coverage_page_is_staff_only_and_get_never_starts_jobs(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/studio/coverage/').status_code,302)
        self.client.force_login(self.user)
        with patch('study.generation.ask_model') as ai:
            response=self.client.get('/studio/coverage/')
            self.assertContains(response,'total library cost is not yet known')
            self.assertContains(response,'does not mean that completing the library is free')
            ai.assert_not_called()
        self.assertEqual(GenerationJob.objects.count(),1)

    def test_admin_status_changes_require_a_review_reason(self):
        from study.admin import SegmentReviewForm
        segment,_,_=self.map_fixture()
        self.assertFalse(SegmentReviewForm({'status':'mapped'},instance=segment).is_valid())
        self.assertTrue(SegmentReviewForm({'status':'mapped','review_reason':'Checked original: administrative page only.'},instance=segment).is_valid())

    def test_shuffle_is_new_per_session_and_stable_on_resume_and_feedback(self):
        orders=iter([[4,3,2,1,0],[1,2,3,4,0]])
        def shuffle(order):order[:]=next(orders)
        with patch('study.engine.RNG.shuffle',side_effect=shuffle):
            first=start_session(self.user,count=1)
            second=start_session(self.user,count=1)
        first_item=first.items.get();second_item=second.items.get()
        self.assertNotEqual(first_item.choice_order.index(2),second_item.choice_order.index(2))
        self.client.force_login(self.user)
        for _ in range(2):self.client.get(f'/sessions/{first.pk}/')
        first_item.refresh_from_db();self.assertEqual(first_item.choice_order,[4,3,2,1,0])
        save_answer(first.pk,self.user,1,2,'sure')
        first_item.refresh_from_db();self.assertEqual(first_item.choice_order,[4,3,2,1,0])
        self.assertTrue(public_item(first_item,True)['choices'][2]['correct'])
