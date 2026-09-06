"""Free authored drafts; one independent source, objective and novelty review."""
import hashlib
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Literal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from pydantic import Field, ValidationError as SchemaError
from .models import Chapter, Source, Question, GenerationJob, LearningObjective, ObjectiveEvidence
from .generation import StrictModel, Choice, NoveltyVerdict, BudgetError
from .mapping import PassageCitation
from .cited_questions import question_references
from .pdf_reading import evidence_part, page_text
from .sources import validate_references, normalize
from .question_checks import (REVIEW_INSTRUCTIONS, REVIEW_LIMIT, candidate, dump, fingerprint,
                              store_check, passes, QUESTION_BYTES)
from .question_evidence import evidence_packet, validate_packet
from .question_pipeline import catalogue_without
from . import pdf_images

PIPELINE = 'authored-1'

IMPORT_REVIEW_INSTRUCTIONS=REVIEW_INSTRUCTIONS+'''
Also verify the proposed learning objective against the primary source. Return canonical_objective_id from existing_objectives when the same concept already exists, even with different wording or in another document; otherwise null. Do not invent IDs. objective_matches must be false if the proposed objective is unsupported or too broad for this question. Match shared concepts before deciding novelty. These drafts have not had any earlier clinical or inventory approval.'''


class AuthoredCitation(PassageCitation):
    quote: str | None = None


class AuthoredQuestion(StrictModel):
    objective_title: str = Field(min_length=10, max_length=400)
    testing_angle: str = Field(min_length=10)
    stem: str = Field(min_length=20)
    choices: list[Choice] = Field(min_length=5, max_length=5)
    answer: int = Field(ge=0, le=4)
    explanation: str = Field(min_length=20)
    learning_point: str = Field(min_length=10)
    references: list[AuthoredCitation] = Field(min_length=1, max_length=8)
    difficulty: Literal['basic', 'applied', 'advanced']
    question_type: Literal['case', 'direct', 'interpretation']


class AuthoredBundle(StrictModel):
    version: Literal[1]
    chapter_id: int
    source_sha256: str
    note_source_ids: list[int] = Field(default_factory=list, max_length=10)
    questions: list[AuthoredQuestion] = Field(min_length=1, max_length=5)


class AuthoredVerdict(NoveltyVerdict):
    canonical_objective_id: int | None


class AuthoredReview(StrictModel):
    verdicts: list[AuthoredVerdict]


def objective_catalog():
    return list(LearningObjective.objects.filter(active=True,reconciliation_status='complete',
        evidence__chapter__source__active=True).distinct().order_by('pk').values('id','title','variant_limit'))


def active_source(job):
    if not Source.objects.for_study().filter(pk=job.chapter.source_id,kind='guideline',
            sha256=job.audit['source_sha256']).exists():
        raise ValidationError('The imported draft source is no longer the active edition.')
    for note in job.audit.get('notes',[]):
        if not Source.objects.for_study().filter(pk=note['id'],kind='notes',sha256=note['sha256'],
                supporting_guidelines=job.chapter.source_id).exists():
            raise ValidationError('An originating note changed or lost its supporting guideline.')


@transaction.atomic
def import_bundle(payload, user, cap=Decimal('4'), *, job=None):
    if not user.is_staff:
        raise ValidationError('Only an administrator can import question drafts.')
    if not Decimal('1') <= cap <= Decimal('200'):
        raise ValidationError('Set a review cap from 1 to 200 NOK within the existing allowance.')
    try:
        bundle=AuthoredBundle.model_validate(payload)
    except SchemaError as e:
        raise ValidationError('Invalid question bundle: '+str(e)) from e
    key=hashlib.sha256(dump(bundle.model_dump()).encode()).hexdigest()
    old=GenerationJob.objects.filter(audit__authored_bundle_sha256=key,requested_by=user).first()
    if old and job is None:
        return old,False
    try:
        chapter=Chapter.objects.select_related('source','topic').get(pk=bundle.chapter_id,
            source__active=True,source__kind='guideline',source__duplicate_of__isnull=True,
            source__sha256=bundle.source_sha256)
    except Chapter.DoesNotExist as e:
        raise ValidationError('The bundle must identify the active primary chapter and exact source hash.') from e
    notes=list(Source.objects.for_study().filter(pk__in=bundle.note_source_ids,kind='notes',
        supporting_guidelines=chapter.source_id).values('id','sha256'))
    if {n['id'] for n in notes}!=set(bundle.note_source_ids):
        raise ValidationError('Every originating note must be active and linked to this guideline.')
    pages=list(chapter.source.pages.filter(number__range=(chapter.first_page,chapter.last_page)).select_related('source','reading'))
    parts=[evidence_part(p,0,len(page_text(p))) for p in pages]
    audit={'authored_bundle_sha256':key,'source_sha256':chapter.source.sha256,'notes':notes}
    if job is None:
        job=GenerationJob.objects.create(requested_by=user,chapter=chapter,count=len(bundle.questions),
            generator_model='Codex / imported draft',reviewer_model=settings.AI_REVIEWER_MODEL,
            spend_limit_nok=cap,status='failed',message='Drafts saved without API use. Ready for independent checking.',
            audit={**audit,'question_pipeline':PIPELINE,'authored_outside_api':True})
    else:
        if job.chapter_id!=chapter.pk or job.requested_by_id!=user.pk:
            raise ValidationError('Source job and authored bundle do not match.')
        job.audit={**job.audit,**audit};job.save(update_fields=['audit'])
    old_stems=list(Question.objects.exclude(status='retired').values_list('stem',flat=True))
    for draft in bundle.questions:
        payload=draft.model_dump();title=payload.pop('objective_title')
        payload['references']=question_references(draft.references,parts)
        q=Question(chapter=chapter,topic=chapter.topic,**payload,generated_by=job.generator_model,
            verification={'state':'awaiting_independent_review','proposed_objective':title,
                'provenance':{'job_id':str(job.pk),'source_id':chapter.source_id,
                    'sha256':chapter.source.sha256,'year':chapter.source.year,'doi':chapter.source.doi,
                    'chapter_id':chapter.pk,'note_source_ids':bundle.note_source_ids,
                    'authored_bundle_sha256':key,'prompt_version':PIPELINE}})
        q.clean();validate_references(q.references,{p.pk for p in pages})
        if len(dump(candidate(q,0)).encode())>QUESTION_BYTES:
            raise ValidationError('Keep each draft within the 6000-byte review envelope.')
        if any(SequenceMatcher(None,normalize(q.stem),normalize(s)).ratio()>.9 for s in old_stems):
            raise ValidationError('This bundle contains a duplicate or near-duplicate question.')
        q.save();old_stems.append(q.stem)
    job.quarantined=len(bundle.questions);job.save(update_fields=['quarantined'])
    return job,True


@transaction.atomic
def publish_imports(job,questions,verdicts,catalog,objectives):
    active_source(job)
    if catalogue_without(questions)!=catalog or objective_catalog()!=objectives:
        raise ValidationError('The question bank or objective inventory changed during review.')
    canonical={o['id'] for o in objectives}
    for i,q in enumerate(questions):
        current=Question.objects.select_for_update().get(pk=q.pk)
        if (current.status!='quarantined' or fingerprint(current)!=fingerprint(q) or
                current.verification.get('checked_question_hash')!=fingerprint(current) or
                current.verification.get('proposed_objective')!=q.verification.get('proposed_objective')):
            raise ValidationError('The imported draft changed during review.')
        v=verdicts[i]
        if v.canonical_objective_id is not None and v.canonical_objective_id not in canonical:
            raise ValidationError('The reviewer selected an unknown canonical objective.')
        q.verification=current.verification
        validate_references(q.references)
        ok=passes(v,q.answer)
        if ok:
            title=q.verification['proposed_objective']
            if v.canonical_objective_id is not None:
                obj=LearningObjective.objects.select_for_update().get(pk=v.canonical_objective_id)
            else:
                key=hashlib.sha256(('authored:'+normalize(title)).encode()).hexdigest()
                obj,_=LearningObjective.objects.get_or_create(inventory_key=key,defaults={
                    'title':title,'topic':q.topic,'variant_limit':1,'reconciliation_status':'complete',
                    'reconciliation_audit':{'pipeline':PIPELINE,'job_id':str(job.pk),'review':v.model_dump()}})
            ok=(obj.active and not obj.blocked_reason and obj.reconciliation_status=='complete' and
                obj.questions.filter(status='published',chapter__source__active=True).count()<obj.variant_limit)
            if ok:
                q.objective=obj
                evidence,_=ObjectiveEvidence.objects.get_or_create(objective=obj,chapter=q.chapter,defaults={'references':q.references})
                if evidence.references!=q.references:
                    merged={dump(r):r for r in evidence.references+q.references}
                    evidence.references=list(merged.values());evidence.save(update_fields=['references'])
                q.objective_link_audit={'pipeline':PIPELINE,'review':{'supported':True,'verdict':v.model_dump()}}
        q.status='published' if ok else 'quarantined'
        q.verification={**q.verification,'state':'reviewed','source_checked':True,
            'checked_question_hash':fingerprint(q),'publication_passed':ok}
        q.full_clean(exclude=['fingerprint']);q.save()


def run_imported(job,ask):
    active_source(job)
    if job.calls.filter(state__in=['reserved','uncertain']).exists():
        raise BudgetError('An unsettled call blocks automatic retry; its cost remains reserved.')
    questions=list(Question.objects.filter(verification__provenance__job_id=str(job.pk),status='quarantined',
        verification__state__in=['awaiting_independent_review','checked_pending_publication']).order_by('created_at','pk'))
    if not questions:
        raise ValidationError('No unfinished imported drafts remain. Rejected drafts are not retried.')
    if len(questions)>5 or any(q.chapter_id!=job.chapter_id or q.verification['provenance']['sha256']!=job.audit['source_sha256'] for q in questions):
        raise ValidationError('Imported draft provenance does not match the bounded review job.')
    refs=[r for q in questions for r in q.references]
    pages=list(job.chapter.source.pages.filter(pk__in={r['page_id'] for r in refs}).select_related('source'))
    if len(pages)>pdf_images.MAX_IMAGES:
        raise ValidationError('Use at most eight cited source pages per review group.')
    # Images and full cited pages include table structure, footnotes and qualifiers.
    images=[pdf_images.page_image(p) for p in pages if p.source.original_name.lower().endswith('.pdf')]
    for q in questions:
        visual=pdf_images.metadata([im for im in images if im['page_id'] in {r['page_id'] for r in q.references}])
        if visual:
            q.references[0]['visual_evidence']=visual;q.save(update_fields=['references'])
    context,_,_=evidence_packet(job.chapter,refs,images,full=True)
    catalog=catalogue_without(questions);objectives=objective_catalog()
    body={'source':context,'existing_questions':catalog,'existing_objectives':objectives,
        'questions':[{**candidate(q,i),'proposed_objective':q.verification['proposed_objective']} for i,q in enumerate(questions)]}
    instructions=IMPORT_REVIEW_INSTRUCTIONS
    before={q.pk:(fingerprint(q),q.verification.get('proposed_objective')) for q in questions}
    validate_packet(job.chapter,context)
    kwargs={'images':images,'reuse_result':True}
    if job.audit.get('reserved_question_review'):kwargs['prepaid_call_id']=job.audit['reserved_question_review']
    review=ask(job,job.reviewer_model or settings.AI_REVIEWER_MODEL,'question-review',instructions,
        dump(body),AuthoredReview,REVIEW_LIMIT,**kwargs)
    validate_packet(job.chapter,context)
    pdf_images.validate_images(images)
    for current in Question.objects.filter(pk__in=before):
        if before[current.pk]!=(fingerprint(current),current.verification.get('proposed_objective')):
            raise ValidationError('The imported question or proposed objective changed during checking.')
    verdicts=store_check(questions,review,job.reviewer_model,'combined_review')
    publish_imports(job,questions,verdicts,catalog,objectives)
    own=Question.objects.filter(verification__provenance__job_id=str(job.pk))
    job.published=own.filter(status='published').count();job.quarantined=own.filter(status='quarantined').count()
    origin='No API writing or inventory.' if job.audit.get('authored_outside_api') else 'Writing and one independent source/objective check.'
    job.message=f'{job.published} published; {job.quarantined} held. {origin}'
    job.save(update_fields=['published','quarantined','message'])
