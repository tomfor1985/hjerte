import json
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase,override_settings
from study.tests.test_study import StudyTests
from study.models import Source,SourcePage,GenerationJob,CoverageSegment,Question
from study.source_copies import format_pairs,prefer_pdf,restore_separate_copy
from study.sources import retire_source
from study.forms import GenerateForm,MappingForm
from study.coverage import coverage_report,chapter_segments
from study.engine import start_session,available_questions
from study.generation import run_job


@override_settings(ALLOWED_HOSTS=['testserver'],SECURE_SSL_REDIRECT=False,AI_GENERATION_ENABLED=False)
class SourceCopyTests(TestCase):
    setUpTestData=classmethod(StudyTests.setUpTestData.__func__)
    make_question=classmethod(StudyTests.make_question.__func__)

    def setUp(self):
        self.word=Source.objects.create(title='Shared notes',original_name='Shared notes.docx',kind='notes',sha256='b'*64,page_count=1,file='word.docx')
        self.pdf=Source.objects.create(title='Shared notes',original_name='Shared notes.pdf',kind='notes',sha256='c'*64,page_count=1,file='pdf.pdf')
        self.unique=Source.objects.create(title='Unique presentation',original_name='Unique presentation.pptx',kind='notes',sha256='d'*64,page_count=1,file='slides.pptx')
        for source in (self.word,self.pdf,self.unique):
            SourcePage.objects.create(source=source,number=1,text='A substantive teaching point. '*20)
        self.word.supporting_guidelines.add(self.source)
        self.question.verification={'provenance':{'note_source_ids':[self.word.pk],'note_pages':[{'source_id':self.word.pk,'page_id':self.word.pages.get().pk,'sha256':self.word.sha256}]}}
        self.question.save()
        self.client.force_login(self.user)

    def test_link_removes_double_counting_without_touching_questions_or_snapshots(self):
        session=start_session(self.user,count=1)
        snapshot=json.dumps(session.items.get().snapshot,sort_keys=True)
        question_data=json.dumps(list(Question.objects.values()),default=str,sort_keys=True)
        before=coverage_report()
        self.assertTrue(prefer_pdf(self.word,self.pdf,self.user))
        self.word.refresh_from_db();self.pdf.refresh_from_db()
        self.assertEqual(self.word.duplicate_of_id,self.pdf.pk)
        self.assertTrue(self.word.active)
        self.assertTrue(self.pdf.supporting_guidelines.filter(pk=self.source.pk).exists())
        self.assertEqual(available_questions().count(),1)
        self.assertEqual(json.dumps(session.items.get().snapshot,sort_keys=True),snapshot)
        self.assertEqual(json.dumps(list(Question.objects.values()),default=str,sort_keys=True),question_data)
        after=coverage_report()
        self.assertEqual(len(after['documents']),len(before['documents'])-1)
        self.assertEqual(after['forecast']['segments_remaining'],before['forecast']['segments_remaining']-1)
        self.assertIn(self.unique.pk,[d['source'].pk for d in after['documents']])

    def test_copy_is_excluded_from_both_dropdowns_and_relevant_note_retrieval(self):
        prefer_pdf(self.word,self.pdf,self.user)
        for form in (GenerateForm(),MappingForm()):
            self.assertNotIn(self.word,form.fields['notes_source'].queryset)
            self.assertIn(self.pdf,form.fields['notes_source'].queryset)
            self.assertIn(self.unique,form.fields['notes_source'].queryset)
        linked=list(self.source.study_notes.for_study().filter(kind='notes'))
        self.assertNotIn(self.word,linked);self.assertIn(self.pdf,linked)
        self.assertIn('PDF',str(self.pdf));self.assertIn('PPTX',str(self.unique))

    def test_direct_mapping_job_for_copy_is_blocked_before_api(self):
        prefer_pdf(self.word,self.pdf,self.user)
        job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,notes_source=self.word,kind='map',count=1)
        with patch('study.mapping.ask_model') as ai:
            with self.assertRaises(ValidationError):run_job(job)
            ai.assert_not_called()

    def test_direct_generation_job_for_copy_is_blocked_before_api(self):
        prefer_pdf(self.word,self.pdf,self.user)
        job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,notes_source=self.word,count=1)
        with patch('study.generation.ask_model') as ai:
            with self.assertRaises(ValidationError):run_job(job)
            ai.assert_not_called()

    def test_active_work_must_finish_before_linking(self):
        job=GenerationJob.objects.create(chapter=self.chapter,requested_by=self.user,notes_source=self.word,kind='map',count=1)
        with self.assertRaisesMessage(ValidationError,'Let the mapping'):
            prefer_pdf(self.word,self.pdf,self.user)
        job.notes_source=None;job.kind='questions';job.save()
        with self.assertRaisesMessage(ValidationError,'Let the mapping'):
            prefer_pdf(self.word,self.pdf,self.user)
        job.status='complete';job.save()
        self.assertTrue(prefer_pdf(self.word,self.pdf,self.user))
        self.assertFalse(prefer_pdf(self.word,self.pdf,self.user))

    def test_existing_mapping_retained_without_relabelling_word_sections_as_pdf_pages(self):
        page=self.word.pages.get()
        segment=CoverageSegment.objects.create(page=page,start=0,end=len(page.text),digest='e'*64,status='mapped',audit={'objective_ids':[1]})
        prefer_pdf(self.word,self.pdf,self.user)
        segment.refresh_from_db()
        self.assertEqual(segment.status,'mapped')
        self.assertEqual(segment.page.source_id,self.word.pk)
        self.assertFalse(CoverageSegment.objects.filter(page__source=self.pdf).exists())

    def test_same_title_different_filename_or_edition_is_not_linked(self):
        self.assertEqual(len(format_pairs(self.word)),1)
        self.pdf.original_name='Other edition.pdf';self.pdf.save()
        self.assertEqual(format_pairs(self.word),[])
        with self.assertRaises(ValidationError):prefer_pdf(self.word,self.pdf,self.user)
        self.pdf.original_name='Shared notes.pdf';self.pdf.year=2027;self.pdf.save()
        self.assertEqual(format_pairs(self.word),[])

    def test_no_automatic_merging_merely_from_matching_names(self):
        self.assertIsNone(self.word.duplicate_of_id)
        self.assertIn(self.word,Source.objects.for_study())
        self.assertIn(self.pdf,Source.objects.for_study())

    def test_copy_can_be_restored_without_reimport_or_answer_changes(self):
        prefer_pdf(self.word,self.pdf,self.user)
        restore_separate_copy(self.word,self.user)
        self.word.refresh_from_db()
        self.assertIsNone(self.word.duplicate_of_id)
        self.assertIn(self.word,Source.objects.for_study())
        self.assertEqual(available_questions().count(),1)
        self.assertIn('Restored as a separate',self.word.duplicate_note)

    def test_link_requires_staff_post_and_valid_pair(self):
        path=f'/studio/sources/{self.word.pk}/'
        data={'action':'prefer_pdf','copy_id':self.word.pk,'main_id':self.pdf.pk}
        self.client.get(path,data)
        self.word.refresh_from_db();self.assertIsNone(self.word.duplicate_of_id)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(path,data).status_code,302)
        self.word.refresh_from_db();self.assertIsNone(self.word.duplicate_of_id)
        self.client.force_login(self.user)
        self.client.post(path,{**data,'main_id':self.unique.pk})
        self.word.refresh_from_db();self.assertIsNone(self.word.duplicate_of_id)
        self.assertRedirects(self.client.post(path,data),f'/studio/sources/{self.pdf.pk}/')
        self.word.refresh_from_db();self.assertEqual(self.word.duplicate_of_id,self.pdf.pk)
        response=self.client.get(path)
        self.assertContains(response,'Alternate format · saved for history')
        self.assertNotContains(response,'<button class="button primary">Generate & verify</button>')

    def test_library_shows_saved_copy_separately(self):
        prefer_pdf(self.word,self.pdf,self.user)
        r=self.client.get('/library/')
        self.assertContains(r,'Alternate formats retained for history (1)')
        self.assertContains(r,'Shared notes.pdf')
        self.assertContains(r,f'/library/{self.word.pk}/file/')

    def test_retiring_main_copy_also_follows_alternate_file_provenance(self):
        session=start_session(self.user,count=1)
        snapshot=json.dumps(session.items.get().snapshot,sort_keys=True)
        prefer_pdf(self.word,self.pdf,self.user)
        self.assertEqual(retire_source(self.pdf,self.user),1)
        self.word.refresh_from_db();self.question.refresh_from_db()
        self.assertFalse(self.word.active)
        self.assertEqual(self.question.status,'retired')
        self.assertEqual(json.dumps(session.items.get().snapshot,sort_keys=True),snapshot)
        self.assertEqual(retire_source(self.pdf,self.user),0)

    def test_self_link_and_copy_chains_are_invalid(self):
        self.word.duplicate_of=self.word
        with self.assertRaises(ValidationError):self.word.clean()
        self.word.duplicate_of=None
        prefer_pdf(self.word,self.pdf,self.user)
        self.pdf.duplicate_of=self.word
        with self.assertRaises(ValidationError):self.pdf.clean()
