import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone
from study.models import (Domain,Topic,Source,SourcePage,Chapter,Question,StudySession,SessionItem,
                          QuestionProgress,ApiBudget,ApiCall,GenerationJob)
from study.engine import (start_session,save_answer,finish_session,public_item,synchronize_clock,
                          balanced_selection,adaptive_selection)
from study.sources import validate_references,extract_pages
from study.generation import reserve_call,settle_call,BudgetError,ask_model,DraftBatch


@override_settings(ALLOWED_HOSTS=['testserver'],SECURE_SSL_REDIRECT=False,AI_GENERATION_ENABLED=False)
class StudyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user=get_user_model().objects.create_user('tomas',password='a-valid-private-test-password',is_staff=True,is_superuser=True)
        cls.other=get_user_model().objects.create_user('other',password='another-test-password')
        cls.domain=Domain.objects.create(key='primary',title='Primary prevention')
        cls.topic=Topic.objects.create(domain=cls.domain,slug='hypertension',title='Hypertension')
        cls.source=Source.objects.create(title='Test guideline',kind='guideline',year=2024,sha256='a'*64,
            original_name='guideline.pdf',page_count=2,file='test.pdf')
        cls.page=SourcePage.objects.create(source=cls.source,number=1,text='The recommended office systolic blood pressure target is 120–129 mmHg when treatment is tolerated.')
        cls.chapter=Chapter.objects.create(source=cls.source,topic=cls.topic,title='Treatment',first_page=1,last_page=2)
        cls.question=cls.make_question(0)

    @classmethod
    def make_question(cls,n,**kwargs):
        return Question.objects.create(chapter=cls.chapter,topic=cls.topic,stem=f'Original question {n}: which target is correct?',
            choices=[{'text':f'Option {i}','explanation':f'PRIVATE EXPLANATION for option {i}'} for i in range(5)],answer=2,
            explanation='PRIVATE OVERALL RATIONALE',learning_point='PRIVATE LEARNING POINT',
            references=[{'page_id':cls.page.id,'section':'8.5','quote':cls.page.text}],status='published',**kwargs)

    def setUp(self):
        self.client.force_login(self.user)

    def exam_bank(self):
        for i in range(1,140): self.make_question(i)

    def test_practice_reveals_only_after_submission(self):
        s=start_session(self.user,count=1)
        response=self.client.get(f'/sessions/{s.id}/')
        self.assertEqual(response.status_code,200)
        self.assertNotContains(response,'PRIVATE OVERALL')
        self.assertNotContains(response,'PRIVATE EXPLANATION')
        item=s.items.get()
        displayed=item.choice_order.index(2)
        save_answer(s.id,self.user,1,displayed,'sure')
        response=self.client.get(f'/sessions/{s.id}/')
        self.assertContains(response,'PRIVATE OVERALL')
        self.assertContains(response,'PRIVATE EXPLANATION')

    def test_shuffled_choices_grade_original_answer(self):
        s=start_session(self.user,count=1)
        item=s.items.get()
        saved=save_answer(s.id,self.user,1,item.choice_order.index(2),'sure')
        self.assertTrue(saved.correct)
        self.assertEqual(QuestionProgress.objects.get(user=self.user).right,1)

    def test_repeated_practice_post_is_idempotent(self):
        s=start_session(self.user,count=1)
        item=s.items.get()
        save_answer(s.id,self.user,1,item.choice_order.index(2),'sure')
        save_answer(s.id,self.user,1,item.choice_order.index(0),'guessed')
        p=QuestionProgress.objects.get(user=self.user)
        self.assertEqual((p.seen,p.right),(1,1))

    def test_exam_bank_has_no_duplicates(self):
        self.exam_bank();s=start_session(self.user,mode='exam')
        self.assertEqual(s.items.count(),140)
        self.assertEqual(s.items.values('question').distinct().count(),140)
        self.assertEqual(s.items.filter(part=1).count(),70)
        self.assertEqual(s.items.filter(part=2).count(),70)

    def test_exam_answers_and_progress_hidden_until_whole_exam(self):
        self.exam_bank();s=start_session(self.user,mode='exam')
        item=s.items.get(position=1)
        save_answer(s.id,self.user,1,item.choice_order.index(2),'sure')
        for path in [f'/sessions/{s.id}/',f'/sessions/{s.id}/results/']:
            r=self.client.get(path,follow=True)
            self.assertNotContains(r,'PRIVATE OVERALL')
            self.assertNotContains(r,'PRIVATE EXPLANATION')
        self.assertFalse(QuestionProgress.objects.filter(user=self.user).exists())
        finish_session(s.id,self.user)
        r=self.client.get(f'/sessions/{s.id}/',follow=True)
        self.assertContains(r,'Take a breath')
        self.assertNotContains(r,'PRIVATE OVERALL')

    def test_first_part_locks_and_break_has_no_skip(self):
        self.exam_bank();s=start_session(self.user,mode='exam');finish_session(s.id,self.user)
        with self.assertRaises(ValidationError):save_answer(s.id,self.user,1,0,'sure')
        s.refresh_from_db();self.assertEqual(s.status,'break')
        s.break_until=timezone.now()-timedelta(seconds=1);s.save()
        synchronize_clock(s)
        self.assertEqual(s.part,2)
        with self.assertRaises(ValidationError):save_answer(s.id,self.user,1,0,'sure')

    def test_expiry_advances_from_original_deadline(self):
        self.exam_bank();s=start_session(self.user,mode='exam')
        now=timezone.now();s.part_started_at=now-timedelta(minutes=200);s.save()
        synchronize_clock(s,now)
        self.assertEqual(s.status,'complete');self.assertEqual(s.score,0)
        self.assertEqual(s.completed_at,s.part_started_at+timedelta(minutes=90))

    def test_expired_exam_rejects_late_answer(self):
        self.exam_bank();s=start_session(self.user,mode='exam')
        s.part_started_at=timezone.now()-timedelta(minutes=91);s.save()
        with self.assertRaises(ValidationError):save_answer(s.id,self.user,1,0,'sure')
        self.assertIsNone(s.items.get(position=1).selected)

    def test_exam_requires_140_distinct_questions(self):
        with self.assertRaisesMessage(ValidationError,'140'):start_session(self.user,mode='exam')

    def test_question_edit_does_not_rewrite_attempt(self):
        s=start_session(self.user,count=1);item=s.items.get()
        self.question.answer=0;self.question.explanation='UPDATED';self.question.save()
        save_answer(s.id,self.user,1,item.choice_order.index(2),'sure');finish_session(s.id,self.user)
        item.refresh_from_db();self.assertTrue(item.correct)
        self.assertEqual(item.snapshot['explanation'],'PRIVATE OVERALL RATIONALE')

    def test_wrong_guessed_and_unsure_are_scheduled_earlier(self):
        s=start_session(self.user,count=1);item=s.items.get()
        save_answer(s.id,self.user,1,item.choice_order.index(2),'guessed')
        p=QuestionProgress.objects.get(user=self.user)
        self.assertEqual(p.streak,0);self.assertLess(p.due_at,timezone.now()+timedelta(days=2))

    def test_new_and_repeated_stats_are_separate(self):
        for repeat in (False,True):
            s=start_session(self.user,count=1);item=s.items.get()
            save_answer(s.id,self.user,1,item.choice_order.index(2),'sure')
            item.refresh_from_db();self.assertEqual(item.is_repeat,repeat)
            finish_session(s.id,self.user)

    def test_session_ownership_is_enforced(self):
        s=start_session(self.other,count=1)
        for path in [f'/sessions/{s.id}/',f'/sessions/{s.id}/results/']:
            self.assertEqual(self.client.get(path).status_code,404)
        for suffix in ['answer/1/','finish/','flag/1/']:
            self.assertEqual(self.client.post(f'/sessions/{s.id}/{suffix}',{'choice':0,'confidence':'sure'}).status_code,404)

    def test_source_file_requires_authentication(self):
        self.client.logout()
        self.assertEqual(self.client.get(f'/library/{self.source.id}/file/').status_code,302)

    def test_studio_requires_staff(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/studio/').status_code,302)

    def test_private_pages_no_store(self):
        r=self.client.get('/')
        self.assertEqual(r['Cache-Control'],'private, no-store')

    def test_all_main_routes_render(self):
        for path in ['/','/practice/','/exam/','/library/','/progress/','/studio/','/password/']:
            with self.subTest(path=path): self.assertEqual(self.client.get(path).status_code,200)

    def test_manifest_and_worker_contract(self):
        r=self.client.get('/manifest.webmanifest');m=r.json()
        self.assertEqual(m['display'],'standalone')
        self.assertEqual(len(m['icons']),2)
        r=self.client.get('/sw.js');self.assertEqual(r['Service-Worker-Allowed'],'/')
        js=b''.join(r.streaming_content).decode()
        self.assertNotIn('cache.put(event.request',js)
        self.assertIn('PUBLIC.includes',js)

    def test_reference_rejects_invented_quote(self):
        with self.assertRaises(ValidationError):
            validate_references([{'page_id':self.page.id,'section':'8','quote':'This is a fabricated quote that does not exist in the guideline.'}])

    def test_reference_rejects_notes_as_authority(self):
        self.source.kind='notes';self.source.save()
        with self.assertRaises(ValidationError): validate_references(self.question.references)

    def test_reference_must_be_from_supplied_context(self):
        with self.assertRaises(ValidationError): validate_references(self.question.references,{999})

    def test_reference_accepts_whitespace_normalization(self):
        refs=[{**self.question.references[0],'quote':self.page.text.replace(' ','\n')}]
        validate_references(refs)

    def test_five_distinct_options_required(self):
        self.question.choices=self.question.choices[:4]
        with self.assertRaises(ValidationError):self.question.clean()

    def test_incomplete_session_unanswered_count_as_wrong(self):
        self.make_question(1);s=start_session(self.user,count=2)
        i=s.items.first();save_answer(s.id,self.user,i.position,i.choice_order.index(2),'sure')
        finish_session(s.id,self.user);s.refresh_from_db()
        self.assertEqual(s.score,1);self.assertEqual(s.items.filter(correct=False).count(),1)

    def test_duplicate_finish_does_not_double_count(self):
        s=start_session(self.user,count=1);finish_session(s.id,self.user);finish_session(s.id,self.user)
        self.assertEqual(QuestionProgress.objects.get(user=self.user).seen,1)

    def test_model_type_is_recorded(self):
        self.question.question_type='direct';self.question.full_clean(exclude=['fingerprint'])

    def test_notes_imported_as_notes_even_if_they_cite_a_doi(self):
        from study.sources import import_source
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d,override_settings(MEDIA_ROOT=d):
            text=('Study notes cite 10.1093/eurheartj/ehae178. This is not the guideline itself. '*4).encode()
            source,created=import_source(text,'notes.md')
            self.assertEqual(source.kind,'notes')
            duplicate,created=import_source(text,'renamed.md')
            self.assertFalse(created);self.assertEqual(source.pk,duplicate.pk)

    def test_unsupported_source_rejected(self):
        with self.assertRaises(ValidationError):extract_pages(b'x'*200,'evil.html')

    def test_offline_page_never_contains_private_identity(self):
        authenticated=self.client.get('/offline/').content
        self.client.logout()
        self.assertEqual(authenticated,self.client.get('/offline/').content)
        self.assertNotIn(b'tomas',authenticated)

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_internal_health_works_without_https(self):
        self.assertEqual(self.client.get('/health/').status_code,200)
        self.assertEqual(self.client.get('/login/').status_code,301)

    def test_old_part_submission_cannot_finish_second_part(self):
        self.exam_bank();s=start_session(self.user,mode='exam')
        finish_session(s.id,self.user,expected_part=1)
        s.refresh_from_db();s.break_until=timezone.now()-timedelta(seconds=1);s.save()
        synchronize_clock(s)
        with self.assertRaises(ValidationError):
            finish_session(s.id,self.user,expected_part=1)
        s.refresh_from_db();self.assertEqual(s.status,'active');self.assertEqual(s.part,2)

    def test_retiring_source_preserves_snapshots_and_removes_new_questions(self):
        from study.sources import retire_source
        from study.engine import available_questions
        s=start_session(self.user,count=1)
        snapshot=s.items.get().snapshot.copy()
        self.assertEqual(retire_source(self.source,self.user),1)
        self.assertEqual(available_questions().count(),0)
        self.assertEqual(s.items.get().snapshot,snapshot)
        self.assertEqual(retire_source(self.source,self.user),0)

    def test_retiring_notes_follows_recorded_generation_provenance(self):
        from study.sources import retire_source
        notes=Source.objects.create(title='Notes',kind='notes',sha256='b'*64,page_count=1)
        self.question.verification={'provenance':{'note_source_ids':[notes.pk]}}
        self.question.save()
        self.assertEqual(retire_source(notes,self.user),1)
        self.source.refresh_from_db();self.assertTrue(self.source.active)

    def test_new_guideline_gets_complete_page_suggestions_and_notes_do_not(self):
        from study.sources import import_source
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d,override_settings(MEDIA_ROOT=d):
            source,_=import_source(('Guideline text. '*5000).encode(),'new.txt',kind='guideline',topic=self.topic)
            chapters=list(source.chapters.order_by('first_page'))
            covered=[p for c in chapters for p in range(c.first_page,c.last_page+1)]
            self.assertEqual(covered,list(range(1,source.page_count+1)))
            notes,_=import_source(('Teaching notes. '*100).encode(),'notes.txt',kind='notes',topic=self.topic)
            self.assertFalse(notes.chapters.exists())

    def test_source_setup_requires_staff_and_retirement_requires_post(self):
        path=f'/studio/sources/{self.source.pk}/'
        self.assertEqual(self.client.get(path).status_code,200)
        self.client.get(path+'?action=retire')
        self.source.refresh_from_db();self.assertTrue(self.source.active)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(path,{'action':'retire'}).status_code,302)
        self.source.refresh_from_db();self.assertTrue(self.source.active)

    def test_flagged_filter(self):
        QuestionProgress.objects.create(user=self.user,question=self.question,flagged=True)
        self.make_question(1)
        selected=adaptive_selection(self.user,list(Question.objects.select_related('topic__domain')),10,'flagged')
        self.assertEqual([q.id for q in selected],[self.question.id])

    def test_login_rate_limit_and_external_redirect(self):
        self.client.logout()
        for _ in range(10):self.client.post('/login/',{'username':'tomas','password':'wrong'})
        r=self.client.post('/login/',{'username':'tomas','password':'a-valid-private-test-password','next':'https://evil.test'})
        self.assertContains(r,'Too many sign-in attempts')


@override_settings(AI_GENERATION_ENABLED=True,OPENAI_API_KEY='not-a-real-key',AI_SERVICE_TIER='flex')
class BudgetTests(TestCase):
    setUpTestData=classmethod(StudyTests.setUpTestData.__func__)
    make_question=classmethod(StudyTests.make_question.__func__)
    def setUp(self):
        super().setUp()
        self.job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,count=5)
        self.budget=ApiBudget.objects.create(pk=1,allowance_nok=Decimal('200'))

    def test_zero_allowance_blocks_before_any_network(self):
        self.budget.allowance_nok=0;self.budget.save()
        with patch('openai.OpenAI') as client:
            with self.assertRaises(BudgetError):ask_model(self.job,'gpt-6-astra','test','instruction','prompt',DraftBatch,1000)
            client.assert_not_called()

    def test_reservation_enforces_total_allowance(self):
        self.budget.allowance_nok=Decimal('1');self.budget.save()
        with self.assertRaises(BudgetError):reserve_call(self.job,'gpt-6-astra','test',50000,16000)
        self.assertFalse(ApiCall.objects.exists())

    def test_settlement_releases_unused_reservation_once(self):
        call=reserve_call(self.job,'gpt-6-astra','test',10000,16000)
        self.budget.refresh_from_db();reserved=self.budget.accounted_nok
        response=SimpleNamespace(usage=SimpleNamespace(input_tokens=1000,output_tokens=500),service_tier='flex',id='resp_test')
        settle_call(call,response);self.budget.refresh_from_db();settled=self.budget.accounted_nok
        self.assertLess(settled,reserved)
        settle_call(call,response);self.budget.refresh_from_db();self.assertEqual(self.budget.accounted_nok,settled)

    def test_timeout_keeps_reservation_and_never_retries(self):
        with patch('openai.OpenAI') as client:
            client.return_value.responses.create.side_effect=TimeoutError()
            with self.assertRaises(TimeoutError):ask_model(self.job,'gpt-6-astra','test','i','p',DraftBatch,1000)
            self.assertEqual(client.call_args.kwargs['max_retries'],0)
            self.assertEqual(client.return_value.responses.create.call_count,1)
        self.budget.refresh_from_db();self.assertGreater(self.budget.accounted_nok,0)
        self.assertEqual(ApiCall.objects.get().state,'uncertain')

    def test_unpriced_model_and_expired_prices_block(self):
        with self.assertRaises(BudgetError):reserve_call(self.job,'unpriced','test',1000,1000)
        self.budget.price_valid_until=timezone.localdate()-timedelta(days=1);self.budget.save()
        with self.assertRaises(BudgetError):reserve_call(self.job,'gpt-6-astra','test',1000,1000)

    def test_generation_requires_blind_and_rationale_agreement_and_keeps_provenance(self):
        from study.generation import run_job,DraftQuestion,ReviewBatch,Verdict
        self.job.count=1
        draft=DraftQuestion(stem='A new independently checked question?',choices=self.question.choices,
            answer=2,explanation='A source-grounded explanation.',learning_point='Distinct objective',
            references=self.question.references,difficulty='basic',question_type='direct')
        good=Verdict(index=0,best_answer=2,single_best_answer=True,evidence_supports_answer=True,
            reference_section_accurate=True,explanations_accurate=True,within_source_scope=True,notes='Supported')
        bad=good.model_copy(update={'explanations_accurate':False})
        with patch('study.generation.ask_model',side_effect=[DraftBatch(questions=[draft]),ReviewBatch(verdicts=[good]),ReviewBatch(verdicts=[bad])]) as ai:
            run_job(self.job)
        q=Question.objects.get(stem=draft.stem)
        self.assertEqual(q.status,'quarantined')
        self.assertEqual(q.verification['provenance']['source_id'],self.source.id)
        self.assertEqual(q.verification['provenance']['sha256'],self.source.sha256)
        blind=json.loads(ai.call_args_list[1].args[4])['questions'][0]
        self.assertNotIn('answer',blind);self.assertNotIn('explanation',blind)
        self.assertTrue(all(isinstance(c,str) for c in blind['choices']))
