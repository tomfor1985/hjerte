import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from study.tests import test_study as base_fixtures
from study.tests import test_coverage as coverage_fixtures
from study.models import ApiBudget, ApiCall, GenerationJob, Question, PageReading
from study.generation import ask_model, reserve_call, settle_call, BudgetError, DraftBatch, NoveltyReviewBatch, run_job
from study.request_budget import reserve_pair, activate_review, release_unused_review
from study.question_resume import queue_saved_questions
from study.question_evidence import evidence_packet, validate_packet
from study.pdf_reading import digest
from study.question_checks import candidate, dump, QUESTION_BYTES


@override_settings(AI_GENERATION_ENABLED=True,OPENAI_API_KEY='fake-key',AI_SERVICE_TIER='flex')
class FundedReviewTests(TestCase):
    setUpTestData=classmethod(base_fixtures.StudyTests.setUpTestData.__func__)
    make_question=classmethod(base_fixtures.StudyTests.make_question.__func__)

    def setUp(self):
        self.job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,count=1,spend_limit_nok=10)
        self.budget=ApiBudget.objects.create(pk=1,allowance_nok=10)
        self.writer={'model':'gpt-5.6-terra','purpose':'generate','input_bound':1000,'output_bound':1000}
        self.review={'model':'gpt-5.6-sol','purpose':'question-review','input_bound':1000,'output_bound':6000}

    def test_neither_call_is_reserved_if_whole_group_does_not_fit(self):
        self.job.spend_limit_nok=Decimal('1')
        with self.assertRaises(BudgetError):reserve_pair(self.job,self.writer,self.review)
        self.assertFalse(ApiCall.objects.exists())
        self.budget.refresh_from_db();self.assertEqual(self.budget.accounted_nok,0)

    def test_review_money_cannot_be_spent_by_another_job(self):
        first=reserve_pair(self.job,self.writer,self.review)
        self.budget.refresh_from_db();reserved=self.budget.accounted_nok
        self.budget.allowance_nok=reserved;self.budget.save()
        other=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user)
        with self.assertRaises(BudgetError):reserve_call(other,**self.writer)
        planned=ApiCall.objects.get(state='planned')
        activate_review(self.job,planned.pk,**self.review)
        self.budget.refresh_from_db();self.assertEqual(self.budget.accounted_nok,reserved)
        planned.refresh_from_db();self.assertEqual(planned.state,'reserved')
        release_unused_review(self.job)
        planned.refresh_from_db();self.assertEqual(planned.state,'reserved')

    def test_only_unsent_review_is_released_once(self):
        writer=reserve_pair(self.job,self.writer,self.review)
        ApiCall.objects.filter(pk=writer.pk).update(state='uncertain')
        release_unused_review(self.job);release_unused_review(self.job)
        self.budget.refresh_from_db();self.assertEqual(self.budget.accounted_nok,writer.reserved_nok)
        self.assertEqual(ApiCall.objects.get(purpose='generate').state,'uncertain')
        cancelled=ApiCall.objects.get(purpose='question-review')
        self.assertEqual(cancelled.state,'cancelled');self.assertEqual(cancelled.reserved_nok,0)
        self.assertTrue(cancelled.request_audit['original_reservation_nok'])

    def test_real_request_cannot_start_writer_when_followup_exceeds_cap(self):
        self.job.spend_limit_nok=Decimal('1')
        future={'model':'gpt-5.6-sol','purpose':'question-review','instructions':'Review',
                'prompt':'Evidence','schema':NoveltyReviewBatch,'output_limit':6000,'extra_input_bound':6000}
        with patch('openai.OpenAI') as client:
            client.return_value.responses.input_tokens.count.return_value.input_tokens=100
            with self.assertRaises(BudgetError):
                ask_model(self.job,'gpt-5.6-terra','generate','Write','Evidence',DraftBatch,1000,reserve_followup=future)
            client.return_value.responses.create.assert_not_called()
        self.assertFalse(ApiCall.objects.exists())

    def test_funded_review_runs_even_if_unreserved_allowance_is_zero(self):
        self.review['input_bound']=3000
        writer=reserve_pair(self.job,self.writer,self.review)
        ApiCall.objects.filter(pk=writer.pk).update(state='settled',actual_nok=writer.reserved_nok)
        self.budget.refresh_from_db();self.budget.allowance_nok=self.budget.accounted_nok;self.budget.save()
        self.assertEqual(self.budget.remaining,0)
        response=SimpleNamespace(status='completed',output_text='{"verdicts":[]}',
            usage=SimpleNamespace(input_tokens=100,output_tokens=100),service_tier='flex',id='funded-review')
        with patch('openai.OpenAI') as client:
            client.return_value.responses.input_tokens.count.return_value.input_tokens=100
            client.return_value.responses.create.return_value=response
            ask_model(self.job,'gpt-5.6-sol','question-review','Check','Evidence',NoveltyReviewBatch,6000,
                      prepaid_call_id=self.job.audit['reserved_question_review'])
            client.return_value.responses.create.assert_called_once()
        self.assertEqual(ApiCall.objects.get(purpose='question-review').state,'settled')

    def test_writer_timeout_retains_uncertain_cost_but_releases_unsent_check(self):
        future={'model':'gpt-5.6-sol','purpose':'question-review','instructions':'Review',
            'prompt':'Evidence','schema':NoveltyReviewBatch,'output_limit':6000,'extra_input_bound':6000}
        with patch('openai.OpenAI') as client:
            client.return_value.responses.input_tokens.count.return_value.input_tokens=100
            client.return_value.responses.create.side_effect=TimeoutError()
            with self.assertRaises(TimeoutError):
                ask_model(self.job,'gpt-5.6-terra','generate','Write','Evidence',DraftBatch,1000,reserve_followup=future)
        self.assertEqual(ApiCall.objects.get(purpose='generate').state,'uncertain')
        self.assertEqual(ApiCall.objects.get(purpose='question-review').state,'cancelled')

    def test_prepaid_review_settles_once_and_exact_result_replays_without_network(self):
        reserve_pair(self.job,self.writer,self.review)
        # Simulate the writer finishing before its already funded review starts.
        writer=ApiCall.objects.get(purpose='generate')
        response=SimpleNamespace(usage=SimpleNamespace(input_tokens=100,output_tokens=100),service_tier='flex',id='writer')
        settle_call(writer,response)
        review_id=self.job.audit['reserved_question_review']
        response=SimpleNamespace(status='completed',output_text='{"verdicts":[]}',usage=SimpleNamespace(input_tokens=100,output_tokens=100),service_tier='flex',id='review')
        with patch('openai.OpenAI') as client:
            client.return_value.responses.input_tokens.count.return_value.input_tokens=100
            client.return_value.responses.create.return_value=response
            # The reserved input bound includes the regular fixed request margin.
            planned=ApiCall.objects.get(pk=review_id)
            self.review['input_bound']=3000
            # Reserve a sufficient input envelope in the fixture.
            from study.generation import cost_nok
            extra=cost_nok('gpt-5.6-sol','default',3000,6000,self.budget)-planned.reserved_nok
            planned.reserved_nok+=extra;planned.save()
            self.budget.refresh_from_db();self.budget.accounted_nok+=extra;self.budget.save()
            ask_model(self.job,'gpt-5.6-sol','question-review','Review','Evidence',NoveltyReviewBatch,6000,
                      prepaid_call_id=review_id,reuse_result=True)
        self.assertEqual(ApiCall.objects.get(pk=review_id).result,{'verdicts':[]})
        with patch('openai.OpenAI') as client:
            result=ask_model(self.job,'gpt-5.6-sol','question-review','Review','Evidence',NoveltyReviewBatch,6000,reuse_result=True)
            self.assertEqual(result.verdicts,[]);client.assert_not_called()
        self.assertEqual(ApiCall.objects.count(),2)


@override_settings(ALLOWED_HOSTS=['testserver'],SECURE_SSL_REDIRECT=False,AI_GENERATION_ENABLED=False)
class CompactQuestionTests(TestCase):
    setUpTestData=classmethod(base_fixtures.StudyTests.setUpTestData.__func__)
    make_question=classmethod(base_fixtures.StudyTests.make_question.__func__)
    setUp=coverage_fixtures.CoverageTests.setUp
    prepare=coverage_fixtures.CoverageTests.prepare
    fresh_target=coverage_fixtures.CoverageTests.fresh_target
    draft_and_reviews=coverage_fixtures.CoverageTests.draft_and_reviews

    def test_one_combined_check_covers_answer_rationale_and_novelty(self):
        obj=self.fresh_target()
        with patch('study.generation.ask_model',side_effect=self.draft_and_reviews(obj)) as ai:run_job(self.job)
        self.assertEqual([c.args[2] for c in ai.call_args_list],['generate','question-review'])
        self.assertIn('reserve_followup',ai.call_args_list[0].kwargs)
        self.assertNotIn('Each quote should',ai.call_args_list[0].args[3])
        self.assertIn('Do not write quotations',ai.call_args_list[0].args[3])
        q=Question.objects.exclude(pk=self.question.pk).get()
        self.assertEqual(q.status,'published')
        self.assertIn('combined_review',q.verification)
        self.assertNotIn('blind_review',q.verification)
        self.assertLessEqual(len(dump(candidate(q,0)).encode()),QUESTION_BYTES)
        self.assertNotIn('answer',json.loads(ai.call_args_list[1].args[4])['questions'][0])

    def test_saved_drafts_resume_without_regenerating_and_keep_job_cap(self):
        obj=self.fresh_target();draft,review=self.draft_and_reviews(obj)
        self.job.spend_limit_nok=Decimal('25');self.job.save()
        with patch('study.generation.ask_model',side_effect=[draft,BudgetError('stop')]):
            with self.assertRaises(BudgetError):run_job(self.job)
        q=Question.objects.exclude(pk=self.question.pk).get()
        self.assertEqual(q.status,'quarantined')
        self.job.status='failed';self.job.save()
        queue_saved_questions(self.job.pk,self.user)
        self.job.refresh_from_db()
        with patch('study.generation.ask_model',return_value=review) as ai:run_job(self.job)
        self.assertEqual(ai.call_count,1);self.assertEqual(ai.call_args.args[2],'question-review')
        q.refresh_from_db();self.assertEqual(q.status,'published')
        self.assertEqual(self.job.spend_limit_nok,25)
        self.assertEqual(Question.objects.exclude(pk=self.question.pk).count(),1)

    def test_completed_check_survives_a_failure_before_publication(self):
        obj=self.fresh_target()
        with patch('study.generation.ask_model',side_effect=self.draft_and_reviews(obj)),patch(
            'study.question_pipeline.publish_checked',side_effect=ValidationError('interrupted')):
            with self.assertRaises(ValidationError):run_job(self.job)
        q=Question.objects.exclude(pk=self.question.pk).get()
        self.assertEqual(q.verification['state'],'checked_pending_publication')
        self.assertTrue(q.verification['combined_review']['explanations_accurate'])
        self.assertEqual(q.status,'quarantined')

    def test_invalid_review_indices_cannot_publish_anything(self):
        obj=self.fresh_target();draft,review=self.draft_and_reviews(obj)
        review.verdicts[0].index=1
        with patch('study.generation.ask_model',side_effect=[draft,review]):
            with self.assertRaises(ValidationError):run_job(self.job)
        self.assertFalse(Question.objects.exclude(pk=self.question.pk).filter(status='published').exists())

    def test_uncertain_full_page_answer_is_held_without_paying_to_repeat_identical_evidence(self):
        obj=self.fresh_target();draft,review=self.draft_and_reviews(obj)
        review.verdicts[0].evidence_supports_answer=False
        with patch('study.generation.ask_model',side_effect=[draft,review]) as ai:run_job(self.job)
        self.assertEqual(ai.call_count,2)
        self.assertEqual(self.job.published,0)

    def test_uncertainty_gets_only_one_full_context_followup(self):
        self.page.text+='\n\n'+('Additional primary-source context. '*700);self.page.save()
        obj=self.fresh_target();draft,good=self.draft_and_reviews(obj)
        uncertain=good.model_copy(deep=True);uncertain.verdicts[0].evidence_supports_answer=False
        with patch('study.generation.ask_model',side_effect=[draft,uncertain,good]) as ai:run_job(self.job)
        self.assertEqual([c.args[2] for c in ai.call_args_list],['generate','question-review','question-extra-review'])
        q=Question.objects.exclude(pk=self.question.pk).get()
        self.assertFalse(q.verification['combined_review']['evidence_supports_answer'])
        self.assertTrue(q.verification['full_context_review']['evidence_supports_answer'])
        self.assertEqual(q.status,'published')

    def test_optional_check_budget_stop_preserves_negative_result(self):
        self.page.text+='\n\n'+('Additional primary-source context. '*700);self.page.save()
        obj=self.fresh_target();draft,review=self.draft_and_reviews(obj)
        review.verdicts[0].evidence_supports_answer=False
        with patch('study.generation.ask_model',side_effect=[draft,review,BudgetError('cap')]) as ai:run_job(self.job)
        self.assertEqual(ai.call_count,3)
        q=Question.objects.exclude(pk=self.question.pk).get()
        self.assertEqual(q.status,'quarantined');self.assertEqual(q.verification['followup_stop'],'budget')
        self.assertFalse(q.verification['combined_review']['evidence_supports_answer'])

    def test_extra_check_still_compares_other_candidates_in_the_group(self):
        from study.question_checks import verify_candidates
        self.page.text+='\n\n'+('Additional primary-source context. '*700);self.page.save()
        obj=self.fresh_target();_,good=self.draft_and_reviews(obj)
        questions=[self.make_question(n,objective=obj) for n in (10,11)]
        for q in questions:
            q.status='quarantined';q.references=self.question.references;q.save()
        normal=good.model_copy(deep=True)
        normal.verdicts[0].evidence_supports_answer=False
        normal.verdicts.append(good.verdicts[0].model_copy(update={'index':1}))
        context,_,_=evidence_packet(self.chapter,self.question.references,[])
        with patch('study.generation.ask_model',side_effect=[normal,good]) as ai:
            verify_candidates(self.job,questions,[obj],context,[],[],ai,'gpt-5.6-sol')
        followup=json.loads(ai.call_args_list[1].args[4])
        self.assertEqual(len(followup['questions']),1)
        self.assertEqual(followup['existing_questions'][0]['id'],str(questions[1].pk))
        self.assertEqual(followup['existing_questions'][0]['stem'],questions[1].stem)

    def test_changed_additional_context_cannot_approve_a_question(self):
        self.page.text+='\n\n'+('Additional primary-source context. '*700);self.page.save()
        obj=self.fresh_target();draft,good=self.draft_and_reviews(obj)
        uncertain=good.model_copy(deep=True);uncertain.verdicts[0].evidence_supports_answer=False
        def answer(*args,**kwargs):
            if args[2]=='generate':return draft
            if args[2]=='question-review':return uncertain
            self.page.text+=' Changed final qualification.';self.page.save()
            return good
        with patch('study.generation.ask_model',side_effect=answer):
            with self.assertRaises(ValidationError):run_job(self.job)
        self.assertFalse(Question.objects.exclude(pk=self.question.pk).filter(status='published').exists())

    def test_manual_edit_during_review_cannot_be_approved_by_stale_result(self):
        obj=self.fresh_target();draft,review=self.draft_and_reviews(obj)
        def answer(*args,**kwargs):
            if args[2]=='generate':return draft
            Question.objects.exclude(pk=self.question.pk).update(answer=1)
            return review
        with patch('study.generation.ask_model',side_effect=answer):
            with self.assertRaises(ValidationError):run_job(self.job)
        self.assertFalse(Question.objects.exclude(pk=self.question.pk).filter(status='published').exists())

    def test_source_changes_during_review_cannot_publish(self):
        obj=self.fresh_target();draft,review=self.draft_and_reviews(obj)
        def answer(*args,**kwargs):
            if args[2]=='generate':return draft
            self.page.text+=' Changed qualification';self.page.save()
            return review
        with patch('study.generation.ask_model',side_effect=answer):
            with self.assertRaises(ValidationError):run_job(self.job)
        self.assertFalse(Question.objects.exclude(pk=self.question.pk).filter(status='published').exists())

    def test_resume_is_staff_owned_and_never_retries_uncertain_calls(self):
        obj=self.fresh_target();draft,review=self.draft_and_reviews(obj)
        with patch('study.generation.ask_model',side_effect=[draft,BudgetError('stop')]):
            with self.assertRaises(BudgetError):run_job(self.job)
        self.job.status='failed';self.job.spend_limit_nok=25;self.job.save()
        ApiCall.objects.create(job=self.job,model='gpt-5.6-sol',purpose='question-review',state='uncertain',reserved_nok=1)
        with self.assertRaises(ValidationError):queue_saved_questions(self.job.pk,self.user)
        self.client.force_login(self.user)
        response=self.client.post('/studio/',{'action':'resume_questions','job_id':str(self.job.pk)})
        self.assertEqual(response.status_code,302)
        self.job.refresh_from_db();self.assertEqual(self.job.status,'failed')

    def test_excerpt_keeps_whole_cited_block_and_neighbouring_qualifications(self):
        text='';spans=[]
        for i in range(12):
            if text:text+='\n\n'
            start=len(text);text+=f'Clinical paragraph {i}: '+('Evidence and necessary qualifications. '*10)
            spans.append({'start':start,'end':len(text),'bbox':[0,i*20,100,i*20+15],'block':i})
        PageReading.objects.create(page=self.page,source_sha256=self.source.sha256,layout_sha256=digest(self.page.text),
            text_sha256=digest(text),text=text,extractor='test',passages=spans)
        self.page.refresh_from_db()
        ref={'page_id':self.page.pk,'section':'Example','quote':text[spans[5]['start']:spans[5]['end']],
            'passage_start':spans[5]['start'],'passage_end':spans[5]['end'],'reading_sha256':digest(text)}
        context,parts,stats=evidence_packet(self.chapter,[ref],[])
        blocks={p['block'] for p in parts[0]['passages']}
        self.assertTrue({0,1,3,4,5,6,7}.issubset(blocks));self.assertNotIn(11,blocks)
        self.assertLess(stats['sent_text_characters'],stats['full_text_characters'])
        validate_packet(self.chapter,context)
        changed=json.loads(json.dumps(context));changed['pages'][0]['passages'][0][2]='Changed qualifier'
        with self.assertRaises(ValidationError):validate_packet(self.chapter,changed)
        full,_,_=evidence_packet(self.chapter,[ref],[],full=True)
        self.assertEqual(len(full['pages'][0]['passages']),12)
