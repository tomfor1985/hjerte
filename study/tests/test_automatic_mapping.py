import copy
import hashlib
import io
import json
import tempfile
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from study.tests import test_study as base_fixtures
from study.tests import test_coverage as coverage_fixtures
from study.tests import test_pdf_reading as pdf_fixtures
from study.models import CoverageSegment, LearningObjective, ApiBudget, ApiCall, GenerationJob
from study.mapping import mapping_context
from study.automatic_mapping import (AutomaticObjective, AutomaticMap, AutomaticCheck, AutomaticReview,
                                     save_checked_inventory, reuse_passage_draft, reusable_review)
from study.coverage import chapter_segments, coverage_report, plan_targets
from study.generation import run_job, ask_model, BudgetError, DraftBatch, IncompleteModelResponse
from study.inventory_recovery import recover_inventory, resume_paid_inventory_review, OUTPUT_GAP
from study.pdf_images import page_image, metadata, validate_images, reference_images
from study.sources import import_source


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False, AI_GENERATION_ENABLED=False,
                   AI_MAPPING_MODEL='gpt-5.6-sol', AI_REVIEWER_MODEL='gpt-5.6-sol')
class AutomaticInventoryTests(TestCase):
    setUpTestData=classmethod(base_fixtures.StudyTests.setUpTestData.__func__)
    make_question=classmethod(base_fixtures.StudyTests.make_question.__func__)
    setUp=coverage_fixtures.CoverageTests.setUp
    reading=pdf_fixtures.PDFCitationTests.reading

    def fixture(self, count=2, complete=False):
        self.reading();self.page.refresh_from_db();self.job.kind='map'
        specs=chapter_segments(self.chapter)
        context=mapping_context(self.job,specs)
        records=[CoverageSegment.objects.create(**{k:s[k] for k in ('page','start','end','digest')}) for s in specs]
        passage=context['parts'][0]['passages'][0]
        proposal=AutomaticMap(objectives=[AutomaticObjective(title=f'Identify supported clinical concept {i}',
            testing_angles=[f'Assess precise fact {i}'],citations=[{'passage_id':passage['id'],'section':'Example'}],
            part_ids=[0],visual_page_ids=[]) for i in range(count)],
            parts=[{'part_id':0,'disposition':'learning','reason':'Teaching content'}],cross_references=[],unresolved_content='')
        review=AutomaticReview(complete_inventory=complete,missing_points=[] if complete else ['One exception is missing.'],
            checks=[AutomaticCheck(index=i,supported=complete or i==0,useful_angles=True,correct_source_parts=True,
                    qualifications_complete=complete or i==0,visual_page_ids=[],reason='Supported' if complete or i==0 else 'Missing exception') for i in range(count)],
            parts=[{'part_id':0,'all_teaching_points_covered':complete,'exclusion_justified':True}],notes='',cross_references=[],image_page_ids=[])
        return records,proposal,review,context

    def test_partial_acceptance_is_idempotent_and_never_claims_complete_coverage(self):
        records,proposal,review,context=self.fixture()
        result=save_checked_inventory(self.job,records,proposal,review,context,[])
        self.assertEqual(result[1],[0]);self.assertFalse(result[0])
        self.assertEqual(LearningObjective.objects.count(),1)
        self.assertEqual(records[0].status,'partial')
        save_checked_inventory(self.job,records,proposal,review,context,[])
        self.assertEqual(LearningObjective.objects.count(),1)
        report=coverage_report();self.assertEqual(report['documents'][0]['partial'],1)
        self.assertFalse(report['mapping_complete'])
        self.assertEqual(len(report['documents'][0]['issues']),1)
        obj=LearningObjective.objects.get();obj.reconciliation_status='complete';obj.save()
        self.question.status='retired';self.question.save()
        self.assertEqual(plan_targets(self.chapter,'coverage',1),[obj])
        self.page.text+=' Changed source';self.page.save()
        with self.assertRaises(ValidationError):plan_targets(self.chapter,'coverage',1)

    def test_saved_manual_edits_are_neither_overwritten_nor_silently_reapproved(self):
        records,proposal,review,context=self.fixture(count=1,complete=True)
        save_checked_inventory(self.job,records,proposal,review,context,[])
        obj=LearningObjective.objects.get();obj.title='Changed clinical scope';obj.save()
        complete,accepted,held=save_checked_inventory(self.job,records,proposal,review,context,[])
        self.assertFalse(complete);self.assertEqual(accepted,[]);self.assertEqual(len(held),1)
        obj.refresh_from_db();self.assertEqual(obj.title,'Changed clinical scope')
        self.assertTrue(obj.blocked_reason);self.assertFalse(obj.active)
        from study.reconciliation import catalogue, validate_objectives
        self.assertEqual(catalogue(),[])
        with self.assertRaises(ValidationError):validate_objectives([obj.pk])
        obj.title=proposal.objectives[0].title;obj.save()
        save_checked_inventory(self.job,records,proposal,review,context,[])
        obj.refresh_from_db();self.assertTrue(obj.active);self.assertFalse(obj.blocked_reason)
        self.assertEqual(obj.reconciliation_status,'pending')

    def test_unknown_passage_is_held_without_losing_supported_objective(self):
        records,proposal,review,context=self.fixture(complete=True)
        proposal.objectives[1].citations[0].passage_id='invented'
        complete,accepted,held=save_checked_inventory(self.job,records,proposal,review,context,[])
        self.assertFalse(complete);self.assertEqual(accepted,[0]);self.assertEqual(held[0]['index'],1)
        self.assertEqual(LearningObjective.objects.count(),1)

    def interrupted(self, proposal):
        prefix=json.dumps({'parts':[p.model_dump() for p in proposal.parts],
                           'cross_references':[],'unresolved_content':''})[:-1]
        return prefix+',"objectives":['+json.dumps(proposal.objectives[-1].model_dump())+',{"title":"unfinished'

    def test_interrupted_inventory_recovers_only_whole_candidates_and_preserves_gap(self):
        records,proposal,review,context=self.fixture()
        recovered=recover_inventory(self.interrupted(proposal),context)
        self.assertEqual(recovered.objectives,[proposal.objectives[1]])
        self.assertEqual(recovered.unresolved_content,OUTPUT_GAP)
        self.assertFalse(LearningObjective.objects.exists())
        # Decoder boundaries, not string matching, handle JSON inside strings.
        proposal.objectives[1].title='A title containing } and "objectives" and [ characters'
        self.assertEqual(recover_inventory(self.interrupted(proposal),context).objectives,[proposal.objectives[1]])
        with self.assertRaises(ValidationError):recover_inventory('{"objectives":[{"title":"cut',context)
        with self.assertRaises(ValidationError):recover_inventory('{"objectives":[{}',context)
        with self.assertRaises(ValidationError):recover_inventory('{"unexpected":true,',context)

    def test_interrupted_repair_requires_complete_independent_review(self):
        records,proposal,review,context=self.fixture()
        response=SimpleNamespace(id='response',output_text=self.interrupted(proposal))
        interrupted=IncompleteModelResponse(response,SimpleNamespace(pk=1))
        final=review.model_copy(deep=True);final.complete_inventory=True;final.missing_points=[]
        final.parts[0].all_teaching_points_covered=True
        for check in final.checks:check.supported=True;check.qualifications_complete=True
        with patch('study.automatic_mapping.MAX_REPAIRS',1),patch('study.mapping.ask_model',side_effect=[proposal,review,interrupted,final]) as ai:
            run_job(self.job)
        self.assertEqual(ai.call_count,4)
        self.assertEqual(LearningObjective.objects.count(),2)
        self.assertEqual(CoverageSegment.objects.get().status,'partial')
        self.assertEqual(self.job.audit['recovered_outputs'][0]['complete_candidates'],1)
        self.assertEqual(ai.call_args_list[2].args[5].model_json_schema()['properties']['objectives']['maxItems'],8)

    def test_paid_resume_is_same_job_once_with_original_cap_and_review_only(self):
        records,proposal,review,context=self.fixture()
        self.job.status='failed';self.job.spend_limit_nok=Decimal('25');self.job.retry_blocked=True;self.job.save()
        save_checked_inventory(self.job,records,proposal,review,context,[])
        call=ApiCall.objects.create(job=self.job,model='gpt-6-astra',purpose='map-repair',state='uncertain',
            actual_nok=1,reserved_nok=1,provider_response_id='paid')
        error=IncompleteModelResponse(SimpleNamespace(id='paid',output_text=self.interrupted(proposal)),call)
        with self.assertRaises(ValidationError):resume_paid_inventory_review(self.job,error)
        call.state='settled';call.save()
        self.assertEqual(resume_paid_inventory_review(self.job,error),1)
        self.assertEqual(self.job.status,'queued');self.assertEqual(self.job.spend_limit_nok,Decimal('25'))
        with self.assertRaises(ValidationError):resume_paid_inventory_review(self.job,error)
        final=review.model_copy(deep=True)
        for check in final.checks:check.supported=True;check.qualifications_complete=True
        with patch('study.mapping.ask_model',return_value=final) as ai:run_job(self.job)
        self.assertEqual(ai.call_count,1);self.assertEqual(ai.call_args.args[2],'map-review')
        self.assertEqual(LearningObjective.objects.count(),2)
        self.assertEqual(self.job.audit['repair_rounds'],1)

    def test_recovery_command_reuses_verified_saved_response_without_provider_request(self):
        records,proposal,review,context=self.fixture()
        self.job.status='failed';self.job.spend_limit_nok=Decimal('25');self.job.save()
        save_checked_inventory(self.job,records,proposal,review,context,[])
        call=ApiCall.objects.create(job=self.job,model='gpt-6-astra',purpose='map-repair',state='settled',
            actual_nok=1,reserved_nok=1,provider_response_id='paid')
        text=self.interrupted(proposal)
        cached={'call_id':str(call.pk),'response_id':'paid','status':'incomplete','incomplete_reason':'max_output_tokens',
                'output_text':text,'output_sha256':'wrong'}
        self.job.audit={**self.job.audit,'interrupted_repair_response':cached};self.job.save()
        with patch('openai.OpenAI') as provider:
            with self.assertRaises(CommandError):call_command('recover_inventory_response',str(self.job.pk),stdout=io.StringIO())
            cached['output_sha256']=hashlib.sha256(text.encode()).hexdigest();self.job.save()
            call_command('recover_inventory_response',str(self.job.pk),stdout=io.StringIO())
            provider.assert_not_called()
        self.job.refresh_from_db();self.assertEqual(self.job.status,'queued')

    def test_unseen_visual_claim_and_omitted_qualification_never_pass(self):
        records,proposal,review,context=self.fixture(complete=True)
        review.checks[0].visual_page_ids=[self.page.pk]
        review.checks[1].qualifications_complete=False
        complete,accepted,held=save_checked_inventory(self.job,records,proposal,review,context,[])
        self.assertFalse(complete);self.assertEqual(accepted,[]);self.assertEqual(len(held),2)
        self.assertFalse(LearningObjective.objects.exists())

    def test_duplicate_or_omitted_review_ids_fail_before_any_acceptance(self):
        records,proposal,review,context=self.fixture(complete=True)
        review.checks[1].index=0
        with self.assertRaises(ValidationError):save_checked_inventory(self.job,records,proposal,review,context,[])
        self.assertFalse(LearningObjective.objects.exists())

    def test_repair_only_requests_rejected_points_and_retains_verified_ids(self):
        records,proposal,review,context=self.fixture()
        replacement=proposal.model_copy(deep=True);replacement.objectives=replacement.objectives[1:]
        final=review.model_copy(deep=True);final.complete_inventory=True;final.missing_points=[]
        final.parts[0].all_teaching_points_covered=True
        for check in final.checks:check.supported=True;check.qualifications_complete=True
        with patch('study.mapping.ask_model',side_effect=[proposal,review,replacement,final]) as ai:
            run_job(self.job)
        self.assertEqual([c.args[2] for c in ai.call_args_list],['map-objectives','map-review','map-repair','map-review'])
        self.assertEqual({c.args[1] for c in ai.call_args_list},{'gpt-5.6-sol'})
        body=json.loads(ai.call_args_list[2].args[4])
        self.assertEqual(len(body['retained_objectives']),1);self.assertEqual(len(body['rejected_objectives']),1)
        self.assertEqual(LearningObjective.objects.count(),2)
        self.assertEqual(CoverageSegment.objects.get().status,'mapped')
        source_text=context['parts'][0]['passages'][0]['text']
        self.assertEqual(ai.call_args_list[1].args[4].count(source_text),1)

    def test_two_repairs_stop_and_second_uses_astra(self):
        records,proposal,review,context=self.fixture()
        replacement=proposal.model_copy(deep=True);replacement.objectives=replacement.objectives[1:]
        with patch('study.mapping.ask_model',side_effect=[proposal,review,replacement,review,replacement,review]) as ai:
            run_job(self.job)
        self.assertEqual(ai.call_count,6)
        self.assertEqual([c.args[1] for c in ai.call_args_list if c.args[2]=='map-repair'],['gpt-5.6-sol','gpt-6-astra'])
        self.assertEqual(LearningObjective.objects.count(),1)
        self.assertEqual(self.job.audit['repair_rounds'],2)
        with patch('study.mapping.ask_model') as later:run_job(self.job);later.assert_not_called()

    def test_budget_stop_preserves_accepted_points_and_uncertain_calls_do_not_retry(self):
        records,proposal,review,context=self.fixture()
        with patch('study.mapping.ask_model',side_effect=[proposal,review,BudgetError('cap')]) as ai:run_job(self.job)
        self.assertEqual(ai.call_count,3);self.assertEqual(self.job.audit['repair_stop'],'budget')
        self.assertEqual(LearningObjective.objects.count(),1)
        self.job.retry_blocked=True
        ApiCall.objects.create(job=self.job,model='gpt-5.6-sol',purpose='map-review',reserved_nok=1,state='uncertain')
        with patch('study.mapping.ask_model',side_effect=[proposal,review,BudgetError('uncertain')]) as ai:
            with self.assertRaises(BudgetError):run_job(self.job)
        self.assertEqual(ai.call_count,3)
        self.assertEqual(LearningObjective.objects.count(),1)

    def test_figure_repair_uses_original_images_in_writer_and_independent_check(self):
        records,proposal,review,context=self.fixture()
        review.image_page_ids=[self.page.pk];review.checks[1].visual_page_ids=[self.page.pk]
        replacement=proposal.model_copy(deep=True);replacement.objectives=replacement.objectives[1:]
        replacement.objectives[0].visual_page_ids=[self.page.pk]
        final=review.model_copy(deep=True);final.complete_inventory=True;final.missing_points=[];final.image_page_ids=[]
        final.parts[0].all_teaching_points_covered=True
        for check in final.checks:check.supported=True;check.qualifications_complete=True
        image={'page_id':self.page.pk,'pdf_page':1,'source_sha256':self.source.sha256,'image_sha256':'1'*64,
               'renderer':'test','width':100,'height':200,'data_url':'data:image/png;base64,test'}
        with patch('study.automatic_mapping.page_image',return_value=image),patch('study.automatic_mapping.validate_images'),patch(
                'study.mapping.ask_model',side_effect=[proposal,review,replacement,final]) as ai:run_job(self.job)
        self.assertEqual(ai.call_args_list[2].args[1],'gpt-6-astra')
        self.assertEqual(ai.call_args_list[2].kwargs['images'],[image])
        self.assertEqual(ai.call_args_list[3].kwargs['images'],[image])
        refs=LearningObjective.objects.order_by('pk').last().evidence.get().references
        self.assertEqual(refs[0]['visual_evidence'],metadata([image]))

    def completed_partial(self):
        records, proposal, review, context = self.fixture()
        ApiCall.objects.create(job=self.job, model='gpt-5.6-sol', purpose='map-review', state='settled',
                               reserved_nok=1, actual_nok=Decimal('.2'), provider_response_id='saved-check')
        save_checked_inventory(self.job, records, proposal, review, context, [])
        self.job.status='complete'; self.job.audit={'verified_source_points': 1}; self.job.save()
        retry=GenerationJob.objects.create(chapter=self.chapter, requested_by=self.user, kind='map',
                                            count=1, retry_blocked=True, spend_limit_nok=25)
        return retry, records, proposal, review, context

    def test_retry_uses_saved_gaps_directly_with_astra_and_requested_original_image(self):
        retry, records, proposal, review, context = self.completed_partial()
        # Save a completed check requesting a figure; it was absent from that check.
        review.image_page_ids=[self.page.pk]
        save_checked_inventory(self.job, records, proposal, review, context, [])
        original_id=LearningObjective.objects.get().pk
        replacement=proposal.model_copy(deep=True); replacement.objectives=replacement.objectives[1:]
        final=review.model_copy(deep=True); final.complete_inventory=True; final.missing_points=[]
        final.parts[0].all_teaching_points_covered=True
        for check in final.checks: check.supported=True; check.qualifications_complete=True
        image={'page_id':self.page.pk,'pdf_page':1,'source_sha256':self.source.sha256,'image_sha256':'1'*64,
               'renderer':'test','width':100,'height':200,'data_url':'data:image/png;base64,test'}
        with patch('study.automatic_mapping.page_image',return_value=image), patch('study.automatic_mapping.validate_images'), patch(
                'study.mapping.ask_model',side_effect=[replacement,final]) as ai:
            run_job(retry)
        self.assertEqual([c.args[2] for c in ai.call_args_list],['map-repair','map-review'])
        self.assertEqual(ai.call_args_list[0].args[1],'gpt-6-astra')
        self.assertTrue(all(c.kwargs['images']==[image] for c in ai.call_args_list))
        self.assertEqual(retry.audit['reused_review_job_id'],str(self.job.pk))
        self.assertEqual(LearningObjective.objects.count(),2)
        self.assertTrue(LearningObjective.objects.filter(pk=original_id,active=True).exists())
        self.assertEqual(CoverageSegment.objects.get().status,'mapped')

    def test_retry_budget_stop_keeps_prior_checked_state_and_review_provenance(self):
        retry, records, proposal, review, context = self.completed_partial()
        old=copy.deepcopy(records[0].audit)
        with patch('study.mapping.ask_model',side_effect=BudgetError('cap')) as ai:
            run_job(retry)
        self.assertEqual(ai.call_args.args[2],'map-repair')
        record=CoverageSegment.objects.get()
        self.assertEqual(record.status,'partial')
        self.assertEqual(record.audit['review_checkpoint'],old['review_checkpoint'])
        self.assertEqual(record.audit['job_id'],str(self.job.pk))
        self.assertEqual(retry.audit['verified_source_points'],1)
        self.assertEqual(retry.audit['repair_stop'],'budget')

    def test_saved_review_reuse_rejects_changed_proposal_objective_or_uncertain_provenance(self):
        retry, records, proposal, review, context = self.completed_partial()
        def reuse(): return reusable_review(retry,records,proposal,context,[],'gpt-5.6-sol')
        self.assertIsNotNone(reuse())
        old=copy.deepcopy(records[0].audit)
        records[0].audit['review']['missing_points']=['Changed after the check']
        self.assertIsNone(reuse())
        records[0].audit=old
        obj=LearningObjective.objects.get(); obj.title='Manually changed clinical scope'; obj.save()
        self.assertIsNone(reuse())
        obj.title=proposal.objectives[0].title; obj.save()
        self.assertIsNotNone(reuse())
        self.job.calls.update(state='uncertain')
        self.assertIsNone(reuse())

    def test_old_review_requires_explicit_adoption_and_completed_last_review(self):
        retry, records, proposal, review, context = self.completed_partial()
        records[0].audit.pop('review_checkpoint'); records[0].audit.pop('review_call_id')
        def reuse(): return reusable_review(retry,records,proposal,context,[],'gpt-5.6-sol')
        self.assertIsNone(reuse())
        retry.audit={'reuse_review_from':str(self.job.pk)}
        self.assertIsNotNone(reuse())
        ApiCall.objects.create(job=self.job,model='gpt-6-astra',purpose='map-repair',state='settled',
                               reserved_nok=1,actual_nok=Decimal('.2'),provider_response_id='unchecked-repair')
        self.assertIsNone(reuse())

    def test_cached_review_checks_current_source_and_previous_image_version(self):
        retry, records, proposal, review, context = self.completed_partial()
        with patch('study.automatic_mapping.validate_images',side_effect=ValidationError('Changed image')):
            with self.assertRaises(ValidationError):
                reusable_review(retry,records,proposal,context,[],'gpt-5.6-sol')
        self.source.active=False; self.source.save()
        with self.assertRaises(ValidationError):
            reusable_review(retry,records,proposal,context,[],'gpt-5.6-sol')

    def test_changed_source_and_retired_guideline_cannot_accept_anything(self):
        records,proposal,review,context=self.fixture(complete=True)
        self.source.active=False;self.source.save()
        with self.assertRaises(ValidationError):save_checked_inventory(self.job,records,proposal,review,context,[])
        self.assertFalse(LearningObjective.objects.exists())

    def test_paid_previous_version_is_reused_only_with_identical_evidence(self):
        import hashlib
        from study.mapping import resolve_citations
        records,proposal,review,context=self.fixture(complete=True)
        draft=resolve_citations(proposal,context)
        audit={'prompt_version':'inventory-3-passages','source_sha256':self.source.sha256,
               'segment_ids':[r.pk for r in records],'job_id':'old-paid-job','draft':draft.model_dump(),
               'evidence_pages':[{'page_id':p['page_id'],'text_sha256':hashlib.sha256(p['text'].encode()).hexdigest()} for p in context['parts']]}
        self.assertIsNotNone(reuse_passage_draft(audit,records,context))
        wrong=copy.deepcopy(audit);wrong['evidence_pages'][0]['text_sha256']='changed'
        self.assertIsNone(reuse_passage_draft(wrong,records,context))
        records[0].audit=audit;records[0].status='blocked';records[0].save();self.job.retry_blocked=True
        with patch('study.mapping.ask_model',return_value=review) as ai:run_job(self.job)
        self.assertEqual(ai.call_count,1);self.assertEqual(ai.call_args.args[2],'map-review')
        record=CoverageSegment.objects.get()
        self.assertEqual(record.audit['reused_draft_job_id'],'old-paid-job')
        self.assertEqual(record.status,'mapped')

    def test_visual_evidence_reaches_all_three_mcq_checks(self):
        from study.cited_questions import CitedQuestionBatch,CitedQuestion
        records,proposal,review,context=self.fixture(count=1,complete=True)
        image={'page_id':self.page.pk,'pdf_page':1,'data_url':'data:image/png;base64,test',
               'source_sha256':self.source.sha256,'image_sha256':'1'*64,'renderer':'test','width':100,'height':200}
        proposal.objectives[0].visual_page_ids=[self.page.pk]
        with patch('study.automatic_mapping.validate_images'):
            save_checked_inventory(self.job,records,proposal,review,context,[image])
        obj=LearningObjective.objects.get();obj.reconciliation_status='complete';obj.save()
        self.question.status='retired';self.question.save();self.job.kind='questions'
        draft,blind,rationale=coverage_fixtures.CoverageTests.draft_and_reviews(self,obj)
        item=draft.questions[0].model_dump()
        item['references']=[{'passage_id':context['parts'][0]['passages'][0]['id'],'section':'Example'}]
        batch=CitedQuestionBatch(questions=[CitedQuestion(**item)])
        with patch('study.pdf_images.reference_images',return_value=[image]),patch('study.generation.ask_model',side_effect=[batch,blind,rationale]) as ai:
            run_job(self.job)
        self.assertEqual(ai.call_count,3)
        self.assertTrue(all(c.kwargs['images']==[image] for c in ai.call_args_list))
        self.assertEqual(obj.questions.get().status,'published')
        self.assertEqual(obj.questions.get().references[0]['visual_evidence'],metadata([image]))


class SourceImageTests(TestCase):
    def test_original_pdf_render_is_reusable_and_source_and_image_bytes_are_checked(self):
        with tempfile.TemporaryDirectory() as directory,override_settings(MEDIA_ROOT=directory):
            source,_=import_source(pdf_fixtures.two_column_pdf(),'diagram.pdf',kind='guideline')
            page=source.pages.get();first=page_image(page);self.assertEqual(first,page_image(page))
            self.assertEqual(max(first['width'],first['height']),2000)
            self.assertEqual(reference_images([{'visual_evidence':metadata([first])}]),[first])
            altered={**first,'data_url':'data:image/png;base64,wrong'}
            with self.assertRaises(ValidationError):validate_images([altered])
            source.sha256='0'*64;source.save()
            with self.assertRaises(ValidationError):page_image(page)


@override_settings(AI_GENERATION_ENABLED=True,OPENAI_API_KEY='not-real',AI_SERVICE_TIER='flex')
class ImageBudgetTests(TestCase):
    setUpTestData=classmethod(base_fixtures.StudyTests.setUpTestData.__func__)
    make_question=classmethod(base_fixtures.StudyTests.make_question.__func__)

    def test_image_count_is_reserved_before_generation_and_sent_unchanged(self):
        job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,kind='map',spend_limit_nok=1)
        ApiBudget.objects.create(pk=1,allowance_nok=200)
        image={'page_id':1,'pdf_page':1,'data_url':'data:image/png;base64,test'}
        response=SimpleNamespace(id='r',status='completed',output_text='{"questions":[]}',service_tier='flex',
                                 usage=SimpleNamespace(input_tokens=300,output_tokens=20))
        with patch('study.pdf_images.validate_images'),patch('openai.OpenAI') as client:
            client.return_value.responses.input_tokens.count.return_value.input_tokens=100000
            with self.assertRaises(BudgetError):ask_model(job,'gpt-6-astra','map-review','Instruction','Text',DraftBatch,100,images=[image])
            client.return_value.responses.create.assert_not_called()
            self.assertFalse(ApiCall.objects.exists())
            client.return_value.responses.input_tokens.count.return_value.input_tokens=300
            client.return_value.responses.create.return_value=response
            ask_model(job,'gpt-6-astra','map-review','Instruction','Text',DraftBatch,100,images=[image])
            counted=client.return_value.responses.input_tokens.count.call_args.kwargs
            sent=client.return_value.responses.create.call_args.kwargs
            self.assertEqual(counted['input'],sent['input'])
            self.assertEqual(sent['input'][-1]['content'][-1]['detail'],'original')
        self.assertEqual(ApiCall.objects.get().state,'settled')

    def test_output_limited_response_stays_settled_and_is_not_a_network_retry(self):
        job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,kind='map',spend_limit_nok=25)
        ApiBudget.objects.create(pk=1,allowance_nok=200)
        response=SimpleNamespace(id='r',status='incomplete',output_text='{"objectives":[',
            incomplete_details=SimpleNamespace(reason='max_output_tokens'),service_tier='flex',
            usage=SimpleNamespace(input_tokens=300,output_tokens=100))
        with patch('openai.OpenAI') as client:
            client.return_value.responses.create.return_value=response
            with self.assertRaises(IncompleteModelResponse) as error:
                ask_model(job,'gpt-5.6-sol','map-repair','Instruction','Text',DraftBatch,100)
            self.assertEqual(error.exception.output_text,response.output_text)
            self.assertEqual(client.return_value.responses.create.call_count,1)
        self.assertEqual(ApiCall.objects.get().state,'settled')
