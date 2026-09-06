import json
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from study.tests.test_study import StudyTests
from study.models import Question,GenerationJob,ApiCall,ApiBudget,LearningObjective,Source
from study.pdf_reading import page_text,evidence_part
from study.question_import import import_bundle,run_imported,AuthoredReview
from study.generation import BudgetError,run_job
from study.source_generation import SourceBatch


@override_settings(ALLOWED_HOSTS=['testserver'],SECURE_SSL_REDIRECT=False,AI_GENERATION_ENABLED=True,OPENAI_API_KEY='test')
class ImportedQuestionTests(TestCase):
    setUpTestData=classmethod(StudyTests.setUpTestData.__func__)
    make_question=classmethod(StudyTests.make_question.__func__)

    def setUp(self):
        self.source.original_name='source.txt';self.source.save()
        part=evidence_part(self.page,0,len(page_text(self.page)))
        self.payload={'version':1,'chapter_id':self.chapter.pk,'source_sha256':self.source.sha256,'questions':[{
            'objective_title':'Recognize the tolerated systolic treatment target',
            'testing_angle':'Select the guideline systolic target for a treated patient',
            'stem':'Which systolic target should be selected when treatment is tolerated?',
            'choices':[{'text':str(i),'explanation':'This option is supported or excluded by the guideline.'} for i in range(5)],
            'answer':2,'explanation':'The target must follow the source and treatment tolerability.',
            'learning_point':'Use the tolerated guideline target.',
            'references':[{'passage_id':part['passages'][0]['id'],'section':'Treatment target'}],
            'difficulty':'basic','question_type':'direct'}]}
        ApiBudget.objects.create(pk=1,allowance_nok=20)

    def verdict(self,**kw):
        v=dict(index=0,best_answer=2,single_best_answer=True,evidence_supports_answer=True,
            reference_section_accurate=True,explanations_accurate=True,within_source_scope=True,
            objective_matches=True,adds_distinct_testing_angle=True,canonical_objective_id=None,notes='Supported.')
        return AuthoredReview(verdicts=[{**v,**kw}])

    def test_free_atomic_idempotent_import_does_not_claim_coverage(self):
        job,created=import_bundle(self.payload,self.user)
        again,created_again=import_bundle(self.payload,self.user)
        self.assertTrue(created);self.assertFalse(created_again);self.assertEqual(job,again)
        self.assertFalse(ApiCall.objects.exists());self.assertFalse(LearningObjective.objects.exists())
        self.assertEqual(job.status,'failed');self.assertEqual(Question.objects.filter(status='quarantined').count(),1)
        self.assertEqual(ApiBudget.objects.get().accounted_nok,0)

    def test_invalid_reference_rolls_back_all_drafts_and_job(self):
        self.payload['questions'][0]['references'][0]['passage_id']='999999:0'
        with self.assertRaises(ValidationError):import_bundle(self.payload,self.user)
        self.assertFalse(GenerationJob.objects.exists());self.assertEqual(Question.objects.count(),1)

    def test_wrong_hash_and_non_admin_rejected(self):
        with self.assertRaises(ValidationError):import_bundle(self.payload,self.other)
        self.payload['source_sha256']='b'*64
        with self.assertRaises(ValidationError):import_bundle(self.payload,self.user)

    def test_one_check_publishes_and_saves_source_backed_objective(self):
        job,_=import_bundle(self.payload,self.user)
        calls=[]
        def ask(*a,**kw):calls.append((a,kw));return self.verdict()
        run_imported(job,ask)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][0][2],'question-review')
        self.assertNotIn('answer',json.loads(calls[0][0][4])['questions'][0])
        q=Question.objects.get(verification__provenance__job_id=str(job.pk))
        self.assertEqual(q.status,'published');self.assertEqual(q.objective.variant_limit,1)
        self.assertTrue(q.objective.evidence.exists());self.assertTrue(q.objective_link_audit['review']['supported'])

    def test_negative_check_stays_held_without_automatic_retry(self):
        job,_=import_bundle(self.payload,self.user)
        run_imported(job,lambda *a,**kw:self.verdict(evidence_supports_answer=False))
        self.assertFalse(LearningObjective.objects.exists())
        self.assertEqual(job.published,0)
        with self.assertRaises(ValidationError):run_imported(job,lambda *a,**kw:self.verdict())

    def test_retirement_and_changed_question_during_review_block_publication(self):
        job,_=import_bundle(self.payload,self.user)
        def ask(*a,**kw):
            Question.objects.filter(verification__provenance__job_id=str(job.pk)).update(answer=1)
            return self.verdict()
        with self.assertRaises(ValidationError):run_imported(job,ask)
        self.source.active=False;self.source.save()
        with self.assertRaises(ValidationError):run_imported(job,ask)
        self.assertFalse(LearningObjective.objects.exists())

    def test_budget_stop_preserves_resumable_draft(self):
        job,_=import_bundle(self.payload,self.user)
        def ask(*a,**kw):raise BudgetError('cap')
        with self.assertRaises(BudgetError):run_imported(job,ask)
        self.assertEqual(Question.objects.get(status='quarantined').verification['state'],'awaiting_independent_review')
        self.assertFalse(LearningObjective.objects.exists())

    def test_import_ui_is_staff_only_and_does_not_queue(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/studio/').status_code,302)
        self.client.force_login(self.user)
        f=SimpleUploadedFile('drafts.json',json.dumps(self.payload).encode())
        r=self.client.post('/studio/',{'action':'import_questions','file':f,'spend_limit_nok':'4'})
        self.assertEqual(r.status_code,302)
        self.assertEqual(GenerationJob.objects.get().status,'failed');self.assertFalse(ApiCall.objects.exists())
        self.assertContains(self.client.get('/studio/'),'Import authored questions')

    def test_simple_source_flow_needs_no_inventory_and_funds_review(self):
        job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,count=1,
            spend_limit_nok=20,reviewer_model='gpt-5.6-sol',audit={'question_pipeline':'source-1'})
        purposes=[]
        def ask(*a,**kw):
            purposes.append(a[2])
            if a[2]=='generate':
                self.assertIn('reserve_followup',kw)
                return SourceBatch(questions=self.payload['questions'])
            return self.verdict()
        with patch('study.generation.ask_model',side_effect=ask):run_job(job)
        self.assertEqual(purposes,['generate','question-review'])
        self.assertEqual(GenerationJob.objects.count(),1);self.assertEqual(job.published,1)

    def test_existing_canonical_objective_ceiling_prevents_variant_inflation(self):
        job,_=import_bundle(self.payload,self.user);run_imported(job,lambda *a,**k:self.verdict())
        obj=LearningObjective.objects.get()
        self.payload['questions'][0]['stem']='For a different clinical scenario, select the recommended treatment level.'
        second,_=import_bundle(self.payload,self.user)
        run_imported(second,lambda *a,**k:self.verdict(canonical_objective_id=obj.pk))
        self.assertEqual(LearningObjective.objects.count(),1);self.assertEqual(second.published,0)

    def test_proposed_objective_edit_during_review_cannot_publish(self):
        job,_=import_bundle(self.payload,self.user)
        def ask(*a,**kw):
            q=Question.objects.get(status='quarantined')
            q.verification['proposed_objective']='An unrelated learning objective';q.save(update_fields=['verification'])
            return self.verdict()
        with self.assertRaises(ValidationError):run_imported(job,ask)
        self.assertFalse(LearningObjective.objects.exists())

    def test_bounded_readable_excerpt_keeps_parent_coordinates_and_source_hash(self):
        from study.models import PageReading
        from study.pdf_reading import digest
        from study.sources import validate_references
        text=self.page.text+' Unreadable reference \ufffd.'
        PageReading.objects.create(page=self.page,source_sha256=self.source.sha256,layout_sha256=digest(self.page.text),
            text_sha256=digest(text),text=text,extractor='test',passages=[{'start':0,'end':len(text),'block':0,'bbox':[0,0,10,10]}])
        self.payload['questions'][0]['references'][0]={'passage_id':f'{self.page.pk}:0','section':'Target','quote':self.page.text}
        job,_=import_bundle(self.payload,self.user)
        q=Question.objects.get(status='quarantined');ref=q.references[0]
        self.assertEqual(ref['parent_passage_end'],len(text));self.assertEqual(ref['passage_end'],len(self.page.text))
        validate_references(q.references)
        ref['passage_end']+=1
        with self.assertRaises(ValidationError):validate_references([ref])
        ref['passage_end']=len(self.page.text);ref['parent_passage_end']=9999
        with self.assertRaises(ValidationError):validate_references([ref])

    def test_imported_review_cost_is_not_mixed_into_legacy_production_rates(self):
        from study.studio_summary import cost_summary
        job,_=import_bundle(self.payload,self.user)
        ApiCall.objects.create(job=job,model='gpt-5.6-sol',purpose='question-review',state='settled',reserved_nok=1,actual_nok=Decimal('.2'))
        job.published=1;job.save(update_fields=['published'])
        costs=cost_summary(ApiBudget.objects.get())
        self.assertEqual(costs['imported']['unit'],Decimal('.2'))
        self.assertEqual(sum(f['calls'] for f in costs['flows']),0)
