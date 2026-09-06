import copy
import io
import json
import tempfile
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from study.models import PageReading, SourcePage, CoverageSegment, LearningObjective
from study.pdf_reading import parse_readings, page_text, evidence_part, prepare_source_reading, digest
from study.sources import import_source, validate_references, normalize
from study.coverage import chapter_segments
from study.tests.test_coverage import CoverageTests
from study.tests.test_study import StudyTests
from study.mapping import (CitedMap, CitedObjective, PassageCitation, PartInventory, ScopedMapReview,
    CrossReference, CrossReferenceCheck, mapping_context, resolve_citations, visible_references, save_map)
from study.generation import run_job


def two_column_pdf():
    writer=PdfWriter();page=writer.add_blank_page(width=600,height=800)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    # Deliberately interleaved drawing order, as in real typeset PDFs.
    lines=[(40,710,'First column: the original threshold is 120.'),(330,710,'Other column: an unrelated cutoff is 900.'),
           (40,695,'The exception applies only to the defined group.'),(330,695,'Never join these columns into one quotation.')]
    stream=DecodedStreamObject()
    stream.set_data(('\n'.join(f'BT /F1 10 Tf {x} {y} Td ({text}) Tj ET' for x,y,text in lines)).encode())
    page[NameObject('/Contents')]=writer._add_object(stream)
    output=io.BytesIO();writer.write(output);return output.getvalue()


class PDFExtractionTests(TestCase):
    def test_real_pdf_columns_and_legacy_text_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            source,_=import_source(two_column_pdf(),'columns.pdf',kind='guideline')
            page=source.pages.get()
            original=page.text
            reading=page.reading
            first='First column: the original threshold is 120. The exception applies only to the defined group.'
            self.assertNotIn(normalize(first),normalize(original))
            self.assertIn(normalize(first),normalize(reading.text))
            self.assertLess(reading.text.index('The exception'),reading.text.index('Other column'))
            self.assertEqual(prepare_source_reading(source),0)
            page.refresh_from_db();self.assertEqual(page.text,original)
            self.assertEqual(PageReading.objects.count(),1)
            self.assertTrue(all(len(reading.text[p['start']:p['end']])<=800 for p in reading.passages))
            legacy={'page_id':page.pk,'quote':'First column: the original threshold is 120.','section':'Example'}
            validate_references([legacy])
            source.sha256='0'*64;source.save()
            with self.assertRaisesMessage(ValidationError,'source hash'):prepare_source_reading(source)

    def test_xml_external_entities_are_not_expanded(self):
        xml=b'''<!DOCTYPE html [<!ENTITY leak SYSTEM "file:///etc/passwd">]><html xmlns="http://www.w3.org/1999/xhtml"><body><page width="600"><flow><block xMin="1" yMin="1" xMax="100" yMax="20"><line><word>&leak;</word></line></block></flow></page></body></html>'''
        result=parse_readings(xml)
        self.assertNotIn('root:',result[0]['text'])

    def test_invalid_font_glyph_is_visible_not_silently_omitted(self):
        xml=b'<html xmlns="http://www.w3.org/1999/xhtml"><body><page width="600"><flow><block xMin="1" yMin="1" xMax="100" yMax="20"><line><word>Threshold\x02value</word></line></block></flow></page></body></html>'
        result=parse_readings(xml)[0]
        self.assertIn('Threshold\ufffdvalue',result['text'])
        self.assertEqual(result['passages'][0]['unreadable_glyphs'],1)


@override_settings(ALLOWED_HOSTS=['testserver'],SECURE_SSL_REDIRECT=False,AI_GENERATION_ENABLED=False)
class PDFCitationTests(TestCase):
    setUpTestData=classmethod(StudyTests.setUpTestData.__func__)
    make_question=classmethod(StudyTests.make_question.__func__)
    setUp=CoverageTests.setUp
    map_fixture=CoverageTests.map_fixture

    def reading(self,text=None):
        text=text or 'The verified source paragraph preserves its threshold of 120 and all exceptions.'
        return PageReading.objects.create(page=self.page,source_sha256=self.source.sha256,
            layout_sha256=digest(self.page.text),text_sha256=digest(text),text=text,extractor='test',
            passages=[{'start':0,'end':len(text),'bbox':[10,20,100,40],'block':0}])

    def fixture(self):
        self.reading();self.page.refresh_from_db()
        specs=chapter_segments(self.chapter)
        self.job.kind='map'
        context=mapping_context(self.job,specs)
        passage=context['parts'][0]['passages'][0]
        proposal=CitedMap(objectives=[CitedObjective(title='Identify the exact threshold in the source',testing_angles=['Recall the supported threshold'],
            citations=[PassageCitation(passage_id=passage['id'],section='Example')],part_ids=[0])],
            parts=[PartInventory(part_id=0,disposition='learning',reason='Substantive source')],cross_references=[],unresolved_content='')
        _,_,old_review=self.map_fixture()
        review=ScopedMapReview(**old_review.model_dump(),cross_references=[])
        return proposal,review,context

    def test_ids_resolve_to_exact_versioned_passages_without_model_quotes(self):
        proposal,review,context=self.fixture()
        draft=resolve_citations(proposal,context)
        ref=draft.objectives[0].references[0].model_dump()
        self.assertEqual(ref['quote'],page_text(self.page))
        validate_references([ref]);visible_references([ref],context['parts'])
        for key,value in (('quote',ref['quote'].replace('120','130')),('reading_sha256','f'*64),('passage_start',1)):
            bad={**ref,key:value}
            with self.subTest(key=key), self.assertRaises(ValidationError):validate_references([bad])
        proposal.objectives[0].citations[0].passage_id+='unknown'
        with self.assertRaisesMessage(ValidationError,'unsupplied'):resolve_citations(proposal,context)

    def test_successful_mapping_and_unknown_id_audit(self):
        proposal,review,context=self.fixture()
        with patch('study.mapping.ask_model',side_effect=[proposal,review]) as ai:run_job(self.job)
        from study.automatic_mapping import AutomaticMap
        self.assertEqual(ai.call_args_list[0].args[5],AutomaticMap)
        self.assertEqual(ai.call_args_list[0].args[4].count(page_text(self.page)),1)
        self.assertEqual(LearningObjective.objects.count(),1)
        stored=LearningObjective.objects.get().evidence.get().references
        validate_references(stored)
        CoverageSegment.objects.all().delete();LearningObjective.objects.all().delete()
        proposal.objectives[0].citations[0].passage_id='invented'
        with patch('study.mapping.ask_model',return_value=proposal) as ai:
            with self.assertRaises(ValidationError):run_job(self.job)
        self.assertEqual(ai.call_count,1)
        segment=CoverageSegment.objects.get()
        self.assertEqual(segment.status,'blocked')
        self.assertEqual(segment.audit['raw_proposal'],proposal.model_dump())
        self.assertNotIn('pending_draft',segment.audit)

    def test_notes_cannot_cite_themselves_or_defer_missing_primary_support(self):
        proposal,_,context=self.fixture()
        context['kind']='notes';context['primary_evidence']=[]
        with self.assertRaisesMessage(ValidationError,'primary passage'):resolve_citations(proposal,context)
        context['primary_evidence']=context['parts']
        proposal.cross_references=[CrossReference(part_id=0,passage_id=context['parts'][0]['passages'][0]['id'],target='Missing primary source',reason='Unsupported note')]
        with self.assertRaisesMessage(ValidationError,'Notes cannot defer'):resolve_citations(proposal,context)

    def test_cross_reference_requires_independent_scope_check(self):
        proposal,review,context=self.fixture()
        proposal.cross_references=[CrossReference(part_id=0,passage_id=context['parts'][0]['passages'][0]['id'],target='Section 7',reason='The original text points to another section')]
        draft=resolve_citations(proposal,context)
        records=list(CoverageSegment.objects.all())
        self.assertFalse(save_map(self.job,records,draft,review,context))
        review.cross_references=[CrossReferenceCheck(index=0,target_is_outside_supplied_parts=True,supplied_claims_are_fully_covered=False)]
        self.assertFalse(save_map(self.job,records,draft,review,context))
        review.cross_references[0].supplied_claims_are_fully_covered=True
        self.assertTrue(save_map(self.job,records,draft,review,context))
        self.assertEqual(CoverageSegment.objects.filter(status='mapped').count(),1)

    def test_long_reading_segments_never_cut_citation_passages(self):
        text=('A source paragraph retaining its exact clinical condition. '*10+'\n\n')*30
        reading=self.reading(text)
        # Same bounded passage representation produced by the PDF extractor.
        reading.passages=[{'start':s,'end':min(s+590,len(text)),'bbox':[0,0,100,100],'block':s} for s in range(0,len(text),590)]
        reading.save();self.page.refresh_from_db()
        specs=chapter_segments(self.chapter)
        self.assertGreater(len(specs),1)
        self.assertEqual(''.join(s['text'] for s in specs),text)
        for s in specs:
            part=evidence_part(s['page'],s['start'],s['end'])
            for p in part['passages']:
                self.assertTrue(any(p['start']==span['start'] and p['end']==span['end'] for span in reading.passages))

    def test_stale_reading_or_changed_coordinates_cannot_be_accepted(self):
        proposal,review,context=self.fixture();draft=resolve_citations(proposal,context)
        reading=self.page.reading;reading.passages[0]['bbox']=[20,20,100,40];reading.save()
        with self.assertRaisesMessage(ValidationError,'Primary evidence changed'):
            save_map(self.job,list(CoverageSegment.objects.all()),draft,review,context)
        self.page.text+=' The original source was edited.';self.page.save()
        with self.assertRaises(ValidationError):validate_references([r.model_dump() for r in draft.objectives[0].references])

    def test_mcq_generation_reuses_passages_and_preserves_attempt_snapshots(self):
        from study.cited_questions import CitedQuestion, CitedQuestionBatch
        from study.engine import start_session
        session=start_session(self.user,count=1);snapshot=session.items.get().snapshot
        proposal,review,context=self.fixture()
        with patch('study.mapping.ask_model',side_effect=[proposal,review]):run_job(self.job)
        objective=LearningObjective.objects.get()
        objective.reconciliation_status='complete';objective.save()
        self.question.status='retired';self.question.save()
        original,blind,rationale=CoverageTests.draft_and_reviews(self,objective)
        payload=original.questions[0].model_dump()
        payload['references']=[{'passage_id':context['parts'][0]['passages'][0]['id'],'section':'Example'}]
        batch=CitedQuestionBatch(questions=[CitedQuestion(**payload)])
        self.job.kind='questions'
        with patch('study.generation.ask_model',side_effect=[batch,blind,rationale]) as ai:run_job(self.job)
        self.assertEqual(ai.call_args_list[0].args[5],CitedQuestionBatch)
        from study.models import Question
        created=Question.objects.exclude(pk=self.question.pk).get()
        self.assertEqual(created.status,'published')
        validate_references(created.references)
        self.assertEqual(created.references[0]['reading_sha256'],self.page.reading.text_sha256)
        self.assertEqual(session.items.get().snapshot,snapshot)

    def test_changed_reading_view_does_not_auto_retry_a_blocked_inventory(self):
        old=list(chapter_segments(self.chapter))[0]
        CoverageSegment.objects.create(page=self.page,start=old['start'],end=old['end'],digest=old['digest'],status='blocked')
        self.reading();self.page.refresh_from_db();self.job.kind='map'
        with patch('study.mapping.ask_model') as ai:
            with self.assertRaisesMessage(ValidationError,'explicitly select retry'):run_job(self.job)
        ai.assert_not_called()

    def test_unreadable_glyph_cannot_be_accepted_as_citation(self):
        self.reading('The source threshold is \ufffd120 for this population.')
        self.page.refresh_from_db()
        part=evidence_part(self.page,0,len(page_text(self.page)))
        ref={'page_id':self.page.pk,'quote':part['passages'][0]['text'],'section':'Example',
            'reading_sha256':part['reading_sha256'],'passage_start':0,'passage_end':part['end']}
        with self.assertRaisesMessage(ValidationError,'unreadable PDF glyphs'):validate_references([ref])

    def test_successful_explicit_retry_leaves_old_audit_but_clears_stop(self):
        old=chapter_segments(self.chapter)[0]
        old_record=CoverageSegment.objects.create(page=self.page,start=old['start'],end=old['end'],digest=old['digest'],status='blocked',audit={'reason':'Old rejected quote'})
        proposal,review,_=self.fixture();self.job.retry_blocked=True
        with patch('study.mapping.ask_model',side_effect=[proposal,review]):run_job(self.job)
        self.job.retry_blocked=False
        with patch('study.mapping.ask_model') as again:run_job(self.job);again.assert_not_called()
        old_record.refresh_from_db()
        self.assertEqual(old_record.audit,{'reason':'Old rejected quote'})
