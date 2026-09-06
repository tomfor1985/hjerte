import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase,override_settings
from study.tests.test_coverage import CoverageTests
from study.tests.test_study import StudyTests
from study.models import Source,SourcePage,LearningObjective,ObjectiveEvidence,Question,CoverageSegment,GenerationJob,ApiBudget,ApiCall
from study.mapping import PartInventory,PartCheck,save_map,mapping_context,visible_references
from study.coverage import chapter_segments,pack_segments,coverage_report,plan_targets
from study.generation import run_job,ask_model,reserve_call,settle_call,cost_nok,BudgetError,DraftBatch
from study.reconciliation import (catalogue,MatchProposal,MatchItem,MatchReview,MatchCheck,
    apply_objective_matches,apply_question_links,resolved_ids,run_reconciliation_job)
from study.forms import MappingForm
from study.engine import start_session


@override_settings(ALLOWED_HOSTS=['testserver'],SECURE_SSL_REDIRECT=False,AI_GENERATION_ENABLED=False,AI_MAPPING_MODEL='gpt-5.6-terra')
class InventoryTests(TestCase):
    setUpTestData=classmethod(StudyTests.setUpTestData.__func__)
    make_question=classmethod(StudyTests.make_question.__func__)
    setUp=CoverageTests.setUp
    map_fixture=CoverageTests.map_fixture
    save_fixture=CoverageTests.save_fixture
    prepare=CoverageTests.prepare

    def proposal(self,pk,target=None,accept=True,uncertain=False):
        return (MatchProposal(items=[MatchItem(id=str(pk),target_id=target,uncertain=uncertain,reason='Compared exact scope')]),
                MatchReview(checks=[MatchCheck(id=str(pk),target_id=target,supported=accept,reason='Independently checked')]))

    def pending(self,title='Identify the recommended systolic target'):
        obj=LearningObjective.objects.create(title=title,topic=self.topic,reconciliation_status='pending',depth_reason='Recall the threshold')
        ObjectiveEvidence.objects.create(objective=obj,chapter=self.chapter,references=self.question.references)
        return obj

    def test_larger_batches_preserve_all_source_offsets(self):
        self.page.text='x'*99000;self.page.save()
        specs=chapter_segments(self.chapter);groups=list(pack_segments(specs))
        self.assertEqual([sum(len(s['text']) for s in g) for g in groups],[48000,48000,3000])
        self.assertEqual(''.join(s['text'] for g in groups for s in g),self.page.text)
        self.assertEqual([s['start'] for g in groups for s in g],list(range(0,99000,12000)))

    def test_mapping_omits_question_bank_and_sends_source_once(self):
        segment,draft,review=self.map_fixture();self.job.kind='map'
        with patch('study.mapping.ask_model',side_effect=[draft,review]) as ai:run_job(self.job)
        payload=json.loads(ai.call_args_list[0].args[4])
        self.assertNotIn('existing_questions',payload);self.assertNotIn('catalogue',payload)
        self.assertNotIn('pages',payload)
        self.assertEqual(ai.call_args_list[0].args[4].count(self.page.text),1)
        self.assertEqual([c.args[1] for c in ai.call_args_list],['gpt-5.6-terra','gpt-5.6-sol'])
        self.assertEqual(LearningObjective.objects.get().reconciliation_status,'pending')
        with patch('study.mapping.ask_model') as again:run_job(self.job);again.assert_not_called()

    def test_omitted_part_or_objective_never_marks_source_complete(self):
        for case in ('draft_part','review_part','review_objective','missing_fact'):
            with self.subTest(case=case):
                CoverageSegment.objects.all().delete()
                segment,draft,review=self.map_fixture()
                if case=='draft_part':draft.parts=[]
                elif case=='review_part':review.parts=[]
                elif case=='review_objective':review.checks=[]
                else:review.missing_points=['The treatment exception is missing.']
                self.assertFalse(self.save_fixture(segment,draft,review))
                self.assertEqual(segment.status,'blocked')
                self.assertFalse(LearningObjective.objects.exists())

    def test_nonlearning_exclusion_requires_independent_justification(self):
        segment,draft,review=self.map_fixture()
        draft.objectives=[];draft.parts[0].disposition='nonlearning';draft.parts[0].reason='Reference list'
        review.checks=[];review.parts[0].exclusion_justified=False
        self.assertFalse(self.save_fixture(segment,draft,review))
        review.parts[0].exclusion_justified=True
        self.assertTrue(self.save_fixture(segment,draft,review))
        self.assertFalse(LearningObjective.objects.exists())

    def test_quote_on_same_page_but_outside_supplied_excerpt_is_rejected(self):
        with self.assertRaises(ValidationError):
            visible_references(self.question.references,[{'page_id':self.page.pk,'text':'Unrelated visible excerpt'}])

    def test_source_edit_during_model_call_invalidates_inventory(self):
        segment,draft,review=self.map_fixture()
        context=mapping_context(self.job,chapter_segments(self.chapter))
        SourcePage.objects.filter(pk=self.page.pk).update(text='Changed clinical text')
        with self.assertRaises(ValidationError):save_map(self.job,[segment],draft,review,context)
        self.assertFalse(LearningObjective.objects.exists())

    def test_notes_wait_for_primary_mapping_without_spending(self):
        notes=Source.objects.create(title='Notes',kind='notes',sha256='b'*64)
        notes.supporting_guidelines.add(self.source)
        SourcePage.objects.create(source=notes,number=1,text=self.page.text)
        self.job.kind='map';self.job.notes_source=notes
        with patch('study.mapping.ask_model') as ai:
            with self.assertRaisesMessage(ValidationError,'Map and verify the supporting'):run_job(self.job)
            ai.assert_not_called()

    def test_note_subsection_keeps_other_locations_unmapped(self):
        from study.mapping import map_segments
        notes=Source.objects.create(title='Notes',kind='notes',sha256='c'*64,page_count=3)
        notes.supporting_guidelines.add(self.source)
        for n in range(1,4):SourcePage.objects.create(source=notes,number=n,text=f'Source section {n} contains material.')
        self.job.notes_source=notes;self.job.notes_first_location=2;self.job.notes_last_location=2
        self.assertEqual([s['page'].number for s in map_segments(self.job)],[2])
        self.assertEqual(coverage_report()['documents'][1]['mapped'],0)
        self.job.notes_last_location=4
        with self.assertRaises(ValidationError):map_segments(self.job)

    def test_unresolved_source_requires_explicit_retry(self):
        segment,draft,review=self.map_fixture();self.job.kind='map';review.missing_points=['Missing exception']
        with patch('study.mapping.ask_model',side_effect=[draft,review]):run_job(self.job)
        with patch('study.mapping.ask_model') as ai:run_job(self.job);ai.assert_not_called()
        self.job.retry_blocked=True;self.job.generator_model='gpt-6-astra';review.missing_points=[]
        with patch('study.mapping.ask_model',side_effect=[draft,review]) as ai:run_job(self.job)
        self.assertEqual(ai.call_args_list[0].args[1],'gpt-6-astra')
        segment.refresh_from_db();self.assertTrue(segment.audit['previous_attempts'])

    def test_paid_draft_survives_failed_review_and_is_reused_on_explicit_retry(self):
        segment,draft,review=self.map_fixture();self.job.kind='map'
        with patch('study.mapping.ask_model',side_effect=[draft,BudgetError('Job cap')]):
            with self.assertRaises(BudgetError):run_job(self.job)
        segment.refresh_from_db();self.assertIn('pending_draft',segment.audit)
        self.assertEqual(segment.status,'blocked')
        self.job.retry_blocked=True
        with patch('study.mapping.ask_model',side_effect=[review]) as ai:run_job(self.job)
        self.assertEqual(ai.call_count,1);self.assertEqual(ai.call_args.args[2],'map-review')
        segment.refresh_from_db();self.assertEqual(segment.status,'mapped')

    def test_matching_uses_complete_compact_catalogue_without_mcqs(self):
        existing=self.pending();existing.reconciliation_status='complete';existing.save()
        pending=self.pending();self.job.kind='reconcile'
        with patch('study.reconciliation.ask_model',side_effect=self.proposal(pending.pk,existing.pk)) as ai:run_job(self.job)
        body=json.loads(ai.call_args_list[0].args[4]);prefix=json.loads(ai.call_args_list[0].kwargs['cache_context'])
        self.assertNotIn('questions',body)
        self.assertEqual({i['id'] for i in prefix['catalogue']},{existing.pk,pending.pk})
        self.assertEqual(resolved_ids([pending.pk]),{existing.pk})
        self.assertEqual(LearningObjective.objects.filter(active=True).count(),1)

    def test_source_audits_and_question_snapshots_survive_merge_and_link(self):
        session=start_session(self.user,count=1);snapshot=session.items.get().snapshot
        segment,draft,review=self.map_fixture();self.save_fixture(segment,draft,review)
        pending=LearningObjective.objects.get()
        canonical=self.pending('A canonical title');canonical.reconciliation_status='complete';canonical.save()
        # The earlier source objective becomes the canonical one, never a cycle.
        apply_objective_matches([pending],*self.proposal(pending.pk),catalogue())
        apply_objective_matches([canonical],*self.proposal(canonical.pk,pending.pk),catalogue())
        refs_before=self.question.references;stem_before=self.question.stem
        apply_question_links([self.question],*self.proposal(self.question.pk,pending.pk),catalogue())
        self.question.refresh_from_db();segment.refresh_from_db()
        self.assertEqual(segment.audit['objective_ids'],[pending.pk])
        self.assertEqual(self.question.references,refs_before);self.assertEqual(self.question.stem,stem_before)
        self.assertEqual(session.items.get().snapshot,snapshot)
        self.assertTrue(coverage_report()['mapping_complete'])

    def test_uncertain_match_cannot_enable_generation(self):
        self.prepare();pending=self.pending()
        apply_objective_matches([pending],*self.proposal(pending.pk,accept=False),catalogue())
        pending.refresh_from_db();self.assertEqual(pending.reconciliation_status,'blocked')
        with self.assertRaisesMessage(ValidationError,'Match the newly'):plan_targets(self.chapter,'coverage',1)

    def test_forward_or_unknown_target_cannot_partially_merge(self):
        first=self.pending();second=self.pending()
        for target in (second.pk,99999):
            with self.assertRaises(ValidationError):apply_objective_matches([first],*self.proposal(first.pk,target),catalogue())
        self.assertEqual(LearningObjective.objects.filter(active=True).count(),2)

    def test_rejected_question_link_remains_unclassified_and_is_not_repeated(self):
        obj=self.pending();obj.reconciliation_status='complete';obj.save();self.job.kind='link_questions'
        with patch('study.reconciliation.ask_model',side_effect=self.proposal(self.question.pk,obj.pk,False)):run_job(self.job)
        self.question.refresh_from_db();self.assertIsNone(self.question.objective_id)
        another=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,kind='link_questions',count=1)
        with patch('study.reconciliation.ask_model') as ai:run_job(another);ai.assert_not_called()

    def test_changed_catalogue_or_question_invalidates_pending_classification(self):
        obj=self.pending();obj.reconciliation_status='complete';obj.save();index=catalogue()
        self.pending('Another distinct objective')
        with self.assertRaises(ValidationError):apply_question_links([self.question],*self.proposal(self.question.pk,obj.pk),index)
        index=catalogue();Question.objects.filter(pk=self.question.pk).update(stem='An edited question')
        with self.assertRaises(ValidationError):apply_question_links([self.question],*self.proposal(self.question.pk,obj.pk),index)

    @override_settings(AI_GENERATION_ENABLED=True,OPENAI_API_KEY='not-real')
    def test_staff_form_queues_requested_stage_and_hard_cost_limit(self):
        ApiBudget.objects.create(pk=1,allowance_nok=200)
        self.client.force_login(self.user)
        response=self.client.post('/studio/coverage/',{'chapter':self.chapter.pk,'kind':'reconcile','count':1,'generator_model':'gpt-5.6-terra','spend_limit_nok':'15'})
        self.assertEqual(response.status_code,302)
        saved=GenerationJob.objects.exclude(pk=self.job.pk).get()
        self.assertEqual(saved.kind,'reconcile');self.assertEqual(saved.spend_limit_nok,Decimal('15'))
        self.assertEqual(saved.reviewer_model,'gpt-5.6-sol')
        form=MappingForm({'chapter':self.chapter.pk,'count':1,'retry_blocked':True,'spend_limit_nok':0})
        self.assertFalse(form.is_valid());self.assertIn('retry_reason',form.errors);self.assertIn('spend_limit_nok',form.errors)


@override_settings(AI_GENERATION_ENABLED=True,OPENAI_API_KEY='not-real',AI_SERVICE_TIER='flex')
class InventoryBudgetTests(TestCase):
    setUpTestData=classmethod(StudyTests.setUpTestData.__func__)
    make_question=classmethod(StudyTests.make_question.__func__)

    def setUp(self):
        self.job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,kind='map',spend_limit_nok=Decimal('1'))
        self.budget=ApiBudget.objects.create(pk=1,allowance_nok=200)

    def test_job_limit_blocks_before_network_even_with_global_allowance(self):
        with patch('openai.OpenAI') as client:
            with self.assertRaises(BudgetError):ask_model(self.job,'gpt-6-astra','map-objectives','instructions','prompt',DraftBatch,10000)
            client.assert_not_called()
        self.assertFalse(ApiCall.objects.exists())

    def test_job_limit_includes_uncertain_reservations(self):
        ApiCall.objects.create(job=self.job,model='gpt-5.6-terra',purpose='map-objectives',state='uncertain',reserved_nok=Decimal('.99'))
        with self.assertRaises(BudgetError):reserve_call(self.job,'gpt-5.6-terra','map-review',10000,1000)

    def test_settlement_records_cache_and_reasoning_and_charges_correct_rates(self):
        self.job.spend_limit_nok=None
        call=reserve_call(self.job,'gpt-5.6-terra','map-objectives',20000,3000)
        response=SimpleNamespace(id='r_test',service_tier='flex',usage=SimpleNamespace(input_tokens=10000,output_tokens=1000,
            input_tokens_details=SimpleNamespace(cached_tokens=8000,cache_write_tokens=1000),output_tokens_details=SimpleNamespace(reasoning_tokens=700)))
        settle_call(call,response);call.refresh_from_db();self.budget.refresh_from_db()
        self.assertEqual(call.actual_nok,Decimal('.1358'))
        self.assertEqual(call.cached_tokens,8000);self.assertEqual(call.cache_write_tokens,1000);self.assertEqual(call.reasoning_tokens,700)
        self.assertEqual(self.budget.accounted_nok,call.actual_nok)

    def test_missing_cache_breakdown_keeps_conservative_accounting(self):
        self.assertGreater(cost_nok('gpt-5.6-terra','flex',10000,1000,self.budget),cost_nok('gpt-5.6-terra','flex',10000,1000,self.budget,8000,1000))
        with self.assertRaises(BudgetError):cost_nok('gpt-5.6-terra','flex',10,1000,self.budget,20,0)

    def test_api_request_only_caches_stable_prefix(self):
        self.job.spend_limit_nok=None
        response=SimpleNamespace(id='r',status='completed',output_text='{"questions":[]}',service_tier='flex',usage=SimpleNamespace(input_tokens=100,output_tokens=20))
        with patch('openai.OpenAI') as client:
            client.return_value.responses.create.return_value=response
            ask_model(self.job,'gpt-5.6-terra','reconcile','Stable instructions','Changing task',DraftBatch,1000,cache_context='Stable catalogue')
            kwargs=client.return_value.responses.create.call_args.kwargs
        self.assertEqual(kwargs['prompt_cache_options']['mode'],'explicit')
        self.assertFalse(kwargs['store'])
        self.assertEqual(kwargs['input'][1]['content'][0]['text'],'Stable catalogue')
        self.assertIn('prompt_cache_breakpoint',kwargs['input'][1]['content'][0])
        self.assertNotIn('prompt_cache_breakpoint',kwargs['input'][-1]['content'][0])
