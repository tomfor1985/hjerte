from copy import deepcopy
from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from study.tests import test_question_import as fixtures
from study.models import Question, ApiCall, ApiBudget
from study.question_import import import_bundle
from study.codex_review import export_review, apply_review
from study.question_resume import queue_saved_questions
from study.studio_summary import cost_summary


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False, AI_GENERATION_ENABLED=False)
class CodexReviewTests(TestCase):
    setUpTestData=classmethod(fixtures.ImportedQuestionTests.setUpTestData.__func__)
    make_question=classmethod(fixtures.ImportedQuestionTests.make_question.__func__)
    setUp=fixtures.ImportedQuestionTests.setUp
    verdict=fixtures.ImportedQuestionTests.verdict

    def prepare(self):
        self.job,_=import_bundle(self.payload,self.user)
        self.packet=export_review(self.job.pk,self.user,'author-session')
        self.result={'version':1,'job_id':str(self.job.pk),'snapshot_sha256':self.packet['snapshot_sha256'],
            'reviewer_session':'separate-reviewer','blind_answers':[{'index':0,'best_answer':2,'reason':'Source supports this answer.'}],
            'verdicts':self.verdict().model_dump()['verdicts']}

    def test_blind_export_then_idempotent_publication_without_api(self):
        self.prepare()
        q=self.packet['blind']['questions'][0]
        self.assertNotIn('answer',q);self.assertNotIn('explanation',q);self.assertNotIn('learning_point',q)
        self.assertTrue(all(isinstance(c,str) for c in q['choices']))
        with self.assertRaises(ValidationError):queue_saved_questions(self.job.pk,self.user)
        job,changed=apply_review(self.job.pk,self.user,self.result)
        self.assertTrue(changed);self.assertEqual(job.published,1)
        again,changed=apply_review(self.job.pk,self.user,self.result)
        self.assertFalse(changed);self.assertEqual(job.pk,again.pk)
        self.assertFalse(ApiCall.objects.exists());self.assertEqual(ApiBudget.objects.get().accounted_nok,0)
        summary=cost_summary(ApiBudget.objects.get());self.assertEqual(summary['codex_reviewed'],1)
        self.assertEqual(summary['imported']['published'],0)

    def test_changed_answer_or_source_snapshot_cannot_reuse_review(self):
        self.prepare()
        q=Question.objects.get(verification__provenance__job_id=str(self.job.pk))
        q.answer=1;q.save()
        with self.assertRaises(ValidationError):apply_review(self.job.pk,self.user,self.result)
        q.refresh_from_db();self.assertEqual(q.status,'quarantined')

    def test_author_cannot_be_reviewer_and_nonstaff_cannot_apply(self):
        self.prepare()
        with self.assertRaises(ValidationError):apply_review(self.job.pk,self.other,self.result)
        self.result['reviewer_session']='author-session'
        with self.assertRaises(ValidationError):apply_review(self.job.pk,self.user,self.result)

    def test_wrong_digest_missing_blind_and_disagreement_rejected(self):
        self.prepare()
        for key,value in [('snapshot_sha256','wrong'),('blind_answers',[]),
            ('blind_answers',[{'index':0,'best_answer':1,'reason':'Another answer'}])]:
            broken=deepcopy(self.result);broken[key]=value
            with self.assertRaises(ValidationError):apply_review(self.job.pk,self.user,broken)

    def test_failed_source_check_is_saved_but_never_published(self):
        self.prepare();self.result['verdicts'][0]['evidence_supports_answer']=False
        job,changed=apply_review(self.job.pk,self.user,self.result)
        self.assertEqual(job.published,0);self.assertEqual(job.quarantined,1)
        q=Question.objects.get(verification__provenance__job_id=str(job.pk))
        self.assertFalse(q.verification['publication_passed']);self.assertIn('codex_review',q.verification)

    def test_revised_held_draft_preserves_previously_published_review_provenance(self):
        second=deepcopy(self.payload['questions'][0])
        second['stem']='Select the appropriate clinical action for a patient whose treatment causes marked adverse effects.'
        second['objective_title']='Select individualized treatment when blood pressure therapy causes symptoms'
        self.payload['questions'].append(second)
        self.prepare()
        v=deepcopy(self.result['verdicts'][0]);v.update(index=1,evidence_supports_answer=False)
        self.result['verdicts'].append(v)
        self.result['blind_answers'].append({'index':1,'best_answer':2,'reason':'Source evidence check needed.'})
        job,_=apply_review(self.job.pk,self.user,self.result)
        own=Question.objects.filter(verification__provenance__job_id=str(job.pk))
        published=own.get(status='published');original=deepcopy(published.verification)
        held=own.get(status='quarantined');held.explanation+=' Corrected qualifier.'
        held.verification={**held.verification,'state':'awaiting_independent_review'};held.save()
        job.status='failed';job.save(update_fields=['status'])
        packet=export_review(job.pk,self.user,'author-session')
        result={**self.result,'snapshot_sha256':packet['snapshot_sha256'],
            'reviewer_session':'another-reviewer','blind_answers':self.result['blind_answers'][:1],
            'verdicts':self.verdict().model_dump()['verdicts']}
        job,_=apply_review(job.pk,self.user,result)
        published.refresh_from_db();self.assertEqual(published.verification,original)
        self.assertEqual(len(job.audit['codex_review_history']),1)
        self.assertEqual(job.audit['codex_review_history'][0]['reviewer_session'],'separate-reviewer')
