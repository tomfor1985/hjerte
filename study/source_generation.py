"""Write questions and their objectives together, then independently check both."""
from django.conf import settings
from django.core.exceptions import ValidationError
from .generation import StrictModel, GENERATOR_INSTRUCTIONS
from .question_import import (AuthoredQuestion, AuthoredReview, import_bundle, run_imported,
    objective_catalog, IMPORT_REVIEW_INSTRUCTIONS)
from .question_checks import dump, REVIEW_LIMIT, QUESTION_BYTES
from .coverage import question_catalog
from .pdf_reading import page_text, evidence_part, prompt_part
from .models import Source, Question
from . import pdf_images
from .request_budget import release_unused_review


class SourceBatch(StrictModel):
    questions: list[AuthoredQuestion]


def run_source(job,ask):
    if job.audit.get('resume_saved_questions'):
        try:return run_imported(job,ask)
        finally:release_unused_review(job)
    if not Source.objects.for_study().filter(pk=job.chapter.source_id,kind='guideline').exists():
        raise ValidationError('Choose an active primary guideline.')
    if job.calls.filter(state__in=['reserved','uncertain']).exists():
        raise ValidationError('An unsettled request prevents retry.')
    remaining=job.count
    try:
        while remaining>0:
            pages=list(job.chapter.source.pages.filter(number__range=(job.chapter.first_page,job.chapter.last_page)).select_related('source','reading'))
            counts={p.pk:0 for p in pages}
            for refs in Question.objects.exclude(status='retired').values_list('references',flat=True):
                for ref in refs:
                    if ref.get('page_id') in counts:counts[ref['page_id']]+=1
            visited=set(job.audit.get('visited_pages',[]))
            pages.sort(key=lambda p:(p.pk in visited,counts[p.pk],p.number))
            selected=[];size=0
            for page in pages:
                text=page_text(page)
                if not text.strip() or size+len(text)>35000:continue
                selected.append(page);size+=len(text)
                if len(selected)==3:break
            if not selected:raise ValidationError('No bounded, readable source pages remain.')
            selected.sort(key=lambda p:p.number)
            images=[pdf_images.page_image(p) for p in selected if p.source.original_name.lower().endswith('.pdf')]
            source={'guideline':job.chapter.source.title,'year':job.chapter.source.year,'doi':job.chapter.source.doi,
                'chapter':job.chapter.title,'source_sha256':job.chapter.source.sha256,
                'pages':[prompt_part(evidence_part(p,0,len(page_text(p)))) for p in selected]}
            base={'source':source,'existing_questions':question_catalog(),'existing_objectives':objective_catalog(),'questions':[]}
            body={**base,'count':min(5,remaining)}
            if job.notes_source_id:
                if not Source.objects.for_study().filter(pk=job.notes_source_id,kind='notes',supporting_guidelines=job.chapter.source_id).exists():
                    raise ValidationError('Link active notes to the supporting guideline first.')
                # Notes only suggest ideas; primary-source review is still mandatory.
                note_parts=[];note_size=0
                for p in job.notes_source.pages.all():
                    if note_size+len(p.text)>16000:break
                    note_parts.append({'location':p.number,'text':p.text});note_size+=len(p.text)
                body['untrusted_note_ideas']=note_parts
                body['notes_partial']=note_size<sum(len(p.text) for p in job.notes_source.pages.all())
            instructions=GENERATOR_INSTRUCTIONS+'''
Return questions with a precise objective_title, not an objective_id. Produce questions and their source-supported learning objectives together; no separate inventory is required. Reuse the concept of an existing objective when appropriate, and reject cosmetic overlap. Select exact supplied passage_id values plus accurate descriptive section locators for references. The server supplies quotations. If an otherwise usable passage contains unreadable glyphs in an unrelated sentence or citation, you may supply an optional quote containing an exact readable excerpt of at least 30 characters; never repair or guess characters. The check still receives the full page and original image. Do not write page_id/quote fields. Keep each question under 6000 UTF-8 bytes. Do not claim the selected pages or chapter are completely covered.'''
            followup={'model':job.reviewer_model or settings.AI_REVIEWER_MODEL,'purpose':'question-review',
                'instructions':IMPORT_REVIEW_INSTRUCTIONS,'prompt':dump(base),'schema':AuthoredReview,
                'output_limit':REVIEW_LIMIT,'images':images,'extra_input_bound':min(5,remaining)*(QUESTION_BYTES*11//10+2000)}
            batch=ask(job,job.generator_model or settings.AI_GENERATOR_MODEL,'generate',instructions,dump(body),
                SourceBatch,10000,images=images,reserve_followup=followup)
            job.audit={**job.audit,'last_source_proposal':batch.model_dump(),
                'visited_pages':job.audit.get('visited_pages',[])+[p.pk for p in selected]}
            job.save(update_fields=['audit'])
            available={p[0] for page in source['pages'] for p in page['passages']}
            if any(r.passage_id not in available for q in batch.questions for r in q.references):
                raise ValidationError('The writer cited a passage outside its supplied source window.')
            if len(batch.questions)>min(5,remaining):raise ValidationError('The writer exceeded the group limit.')
            if not batch.questions:
                job.message='No new supported question was found in the selected source pages.';job.save(update_fields=['message']);break
            payload={'version':1,'chapter_id':job.chapter_id,'source_sha256':source['source_sha256'],
                'note_source_ids':[job.notes_source_id] if job.notes_source_id else [],
                'questions':[q.model_dump() for q in batch.questions]}
            import_bundle(payload,job.requested_by,job.spend_limit_nok,job=job)
            run_imported(job,ask)
            release_unused_review(job)
            remaining-=min(5,remaining)
    finally:
        release_unused_review(job)
