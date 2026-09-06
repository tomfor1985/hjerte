"""Source-first inventory; objective matching and question linking are separate jobs."""
import hashlib
import json
import re
from typing import Literal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from pydantic import Field
from .generation import StrictModel, Reference, ask_model
from .coverage import segments_for, chapter_segments, mapping_state, pack_segments, prior_blocked_parts
from .models import LearningObjective, ObjectiveEvidence, CoverageSegment, Source
from .sources import validate_references, normalize
from .pdf_reading import page_text, reading_for, evidence_part, prompt_part, prepare_source_reading

PROMPT_VERSION = 'inventory-4-automatic'


class InventoryReference(Reference):
    reading_sha256: str | None = None
    passage_start: int | None = None
    passage_end: int | None = None


class PassageCitation(StrictModel):
    passage_id: str
    section: str


class CrossReference(StrictModel):
    part_id: int
    passage_id: str
    target: str
    reason: str


class CitedObjective(StrictModel):
    title: str = Field(min_length=10, max_length=400)
    testing_angles: list[str] = Field(min_length=1, max_length=3)
    citations: list[PassageCitation] = Field(min_length=1)
    part_ids: list[int] = Field(min_length=1)


class CitedMap(StrictModel):
    objectives: list[CitedObjective]
    parts: list['PartInventory']
    cross_references: list[CrossReference]
    unresolved_content: str


class MappedObjective(StrictModel):
    title: str = Field(min_length=10, max_length=400)
    testing_angles: list[str] = Field(min_length=1, max_length=3)
    references: list[InventoryReference] = Field(min_length=1)
    part_ids: list[int] = Field(min_length=1)


class PartInventory(StrictModel):
    part_id: int
    disposition: Literal['learning', 'nonlearning', 'unresolved']
    reason: str


class ObjectiveMap(StrictModel):
    objectives: list[MappedObjective]
    parts: list[PartInventory]
    unresolved_content: str
    cross_references: list[CrossReference] = Field(default_factory=list)


class ObjectiveCheck(StrictModel):
    index: int
    supported: bool
    useful_angles: bool
    correct_source_parts: bool


class PartCheck(StrictModel):
    part_id: int
    all_teaching_points_covered: bool
    exclusion_justified: bool


class MapReview(StrictModel):
    complete_inventory: bool
    missing_points: list[str]
    checks: list[ObjectiveCheck]
    parts: list[PartCheck]
    notes: str


class CrossReferenceCheck(StrictModel):
    index: int
    target_is_outside_supplied_parts: bool
    supplied_claims_are_fully_covered: bool


class ScopedMapReview(MapReview):
    cross_references: list[CrossReferenceCheck]


CitedMap.model_rebuild()


from .mapping_prompts import MAP_INSTRUCTIONS, MAP_REVIEW


def resolve_citations(proposal, context):
    evidence = context.get('primary_evidence', context['parts'])
    available = {p['id']: (part, p) for part in evidence for p in part['passages']}
    objectives = []
    for obj in proposal.objectives:
        references = []
        seen = set()
        for citation in obj.citations:
            if citation.passage_id not in available:
                raise ValidationError('The model selected an unknown or unsupplied primary passage.')
            if citation.passage_id in seen:
                continue
            seen.add(citation.passage_id)
            part, passage = available[citation.passage_id]
            references.append(InventoryReference(page_id=part['page_id'], section=citation.section,
                quote=passage['text'], reading_sha256=part.get('reading_sha256'),
                passage_start=passage['start'],passage_end=passage['end']))
        objectives.append(MappedObjective(title=obj.title, testing_angles=obj.testing_angles,
                                          references=references, part_ids=obj.part_ids))
    for link in proposal.cross_references:
        if context['kind']=='notes':
            raise ValidationError('Notes cannot defer unsupported claims to absent primary evidence.')
        if not any(part['part_id']==link.part_id and any(p['id']==link.passage_id for p in part['passages']) for part in context['parts']):
            raise ValidationError('A cross-reference points outside its supplied source part.')
    return ObjectiveMap(objectives=objectives, parts=proposal.parts,
                        cross_references=proposal.cross_references, unresolved_content=proposal.unresolved_content)


def prompt_context(context):
    return {**context, 'parts': [prompt_part(p) for p in context['parts']],
            **({'primary_evidence': [prompt_part(p) for p in context['primary_evidence']]} if 'primary_evidence' in context else {})}


def map_segments(job):
    if job.notes_source_id:
        if not Source.objects.for_study().filter(pk=job.notes_source_id, kind='notes', supporting_guidelines=job.chapter.source_id).exists():
            raise ValidationError('Choose active notes linked to the selected guideline.')
        pages=job.notes_source.pages.all()
        first,last=job.notes_first_location,job.notes_last_location
        if first is not None or last is not None:
            if first is None or last is None or first<1 or last<first or last>job.notes_source.page_count:
                raise ValidationError('Choose a valid inclusive range of notes locations.')
            pages=pages.filter(number__gte=first,number__lte=last)
            if pages.count()!=last-first+1:raise ValidationError('The selected notes range has missing extracted locations.')
        return [s for p in pages for s in segments_for(p)]
    return chapter_segments(job.chapter)


def require_mapped_chapter(chapter):
    segments = chapter_segments(chapter)
    state = mapping_state(segments)
    if not any(state.get((s['page'].pk,s['start'],s['digest'])) and
               state[(s['page'].pk,s['start'],s['digest'])].status in ('mapped','partial') for s in segments):
        raise ValidationError('Map and verify the supporting guideline chapter before mapping its notes.')



def note_evidence(chapter, group):
    """Bounded retrieval within the chosen primary chapter, never a silent claim of support."""
    words=set(re.findall(r'\w{4,}', ' '.join(s['text'] for s in group).casefold()))
    words -= {'this','that','with','from','have','which','these','should','patients','patient'}
    candidates=chapter_segments(chapter)
    candidates.sort(key=lambda s:(-len(words.intersection(re.findall(r'\w{4,}',s['text'].casefold()))),s['page'].number,s['start']))
    selected=[];size=0
    for s in candidates:
        if s['text'].strip() and size+len(s['text']) <= 36000:
            selected.append(s);size+=len(s['text'])
    if not selected:
        raise ValidationError('No readable primary evidence is available in this chapter.')
    return [evidence_part(s['page'],s['start'],s['end']) for s in sorted(selected,key=lambda s:(s['page'].number,s['start']))]


def mapping_context(job, group):
    parts=[evidence_part(s['page'],s['start'],s['end'],part_id=i) for i,s in enumerate(group)]
    context={'guideline':{'title':job.chapter.source.title,'year':job.chapter.source.year,'doi':job.chapter.source.doi},
             'kind':'notes' if job.notes_source_id else 'guideline','parts':parts}
    if job.notes_source_id:
        context['primary_evidence']=note_evidence(job.chapter,group)
    return context


def visible_references(refs, evidence):
    validate_references(refs,{p['page_id'] for p in evidence})
    for ref in refs:
        if not any(p['page_id']==ref['page_id'] and p.get('reading_sha256')==ref.get('reading_sha256') and normalize(ref['quote']) in normalize(p['text']) for p in evidence):
            raise ValidationError('A reference quotes text outside the supplied evidence excerpt.')


def block_records(records, reason, audit):
    for record in records:
        old=record.audit
        history=old.get('previous_attempts',[])
        if old:
            history=history+[{k:v for k,v in old.items() if k!='previous_attempts'}]
        record.status='blocked'
        record.audit={**audit,'reason':reason,'previous_attempts':history}
        record.save(update_fields=['status','audit'])


def validate_context(job, records, context):
    evidence=context.get('primary_evidence',context['parts'])
    source_ids={job.chapter.source_id,records[0].page.source_id}
    if Source.objects.for_study().filter(pk__in=source_ids).count()!=len(source_ids):
        raise ValidationError('A source was retired or replaced during mapping. No inventory was applied.')
    # Content edits during a request invalidate the result even if page IDs survive.
    for record,part in zip(records,context['parts'],strict=True):
        record.page.refresh_from_db()
        if hashlib.sha256(page_text(record.page)[record.start:record.end].encode()).hexdigest()!=record.digest:
            raise ValidationError('Source text changed during mapping.')
    for p in evidence:
        from .models import SourcePage
        current=SourcePage.objects.get(pk=p['page_id'])
        reading=reading_for(current)
        if (p.get('reading_sha256')!=(reading.text_sha256 if reading else None) or p['text']!=page_text(current)[p['start']:p['end']] or
                p['passages']!=evidence_part(current,p['start'],p['end'])['passages']):
            raise ValidationError('Primary evidence changed during mapping.')


@transaction.atomic
def save_map(job, records, draft, review, context):
    validate_context(job, records, context)
    evidence=context.get('primary_evidence',context['parts'])
    visible_references([r.model_dump() for o in draft.objectives for r in o.references],evidence) if draft.objectives else None
    ids=set(range(len(records)))
    parts={p.part_id:p for p in draft.parts};checks={p.part_id:p for p in review.parts}
    objectives={c.index:c for c in review.checks}
    audit={'job_id':str(job.pk),'prompt_version':PROMPT_VERSION,'source_sha256':records[0].page.source.sha256,
           'evidence_source_id':job.chapter.source_id,'evidence_sha256':job.chapter.source.sha256,
           'evidence_pages':[{'page_id':p['page_id'],'text_sha256':hashlib.sha256(p['text'].encode()).hexdigest()} for p in evidence],
           'draft':draft.model_dump(),'review':review.model_dump(),'segment_ids':[r.pk for r in records],'objective_ids':[]}
    reason=''
    cross_checks=getattr(review,'cross_references',[])
    if (len(cross_checks)!=len(draft.cross_references) or {c.index for c in cross_checks}!=set(range(len(draft.cross_references))) or
            any(not c.target_is_outside_supplied_parts or not c.supplied_claims_are_fully_covered for c in cross_checks)):
        reason='A cross-reference excludes unresolved teaching claims or was not independently checked.'
    elif set(parts)!=ids or len(parts)!=len(draft.parts) or set(checks)!=ids or len(checks)!=len(review.parts):
        reason='The inventory or independent review did not account for every source part.'
    elif set(objectives)!=set(range(len(draft.objectives))) or len(objectives)!=len(review.checks):
        reason='The independent review did not check every objective.'
    elif draft.unresolved_content or not review.complete_inventory or review.missing_points:
        reason=draft.unresolved_content or '; '.join(review.missing_points) or review.notes or 'Incomplete inventory.'
    else:
        assigned={i for obj in draft.objectives for i in obj.part_ids}
        if not assigned.issubset(ids) or any(len(set(o.part_ids))!=len(o.part_ids) for o in draft.objectives):
            reason='An objective has invalid source-part links.'
        elif any(not all((c.supported,c.useful_angles,c.correct_source_parts)) for c in review.checks):
            reason='An objective failed the primary-source review.'
        elif any(p.disposition=='unresolved' or not checks[i].all_teaching_points_covered or
                 (p.disposition=='nonlearning' and (not p.reason.strip() or not checks[i].exclusion_justified or i in assigned)) or
                 (p.disposition=='learning' and i not in assigned) for i,p in parts.items()):
            reason='A source part has missing objectives or an unjustified exclusion.'
    if reason:
        block_records(records,reason,audit);return False
    linked={i:[] for i in ids}
    for obj in draft.objectives:
        objective=LearningObjective.objects.create(title=obj.title,topic=job.chapter.topic,
            variant_limit=len(obj.testing_angles),depth_reason='\n'.join(obj.testing_angles),reconciliation_status='pending')
        ObjectiveEvidence.objects.create(objective=objective,chapter=job.chapter,references=[r.model_dump() for r in obj.references])
        for i in obj.part_ids:linked[i].append(objective.pk)
    for i,record in enumerate(records):
        previous=record.audit
        history=previous.get('previous_attempts',[])
        if previous:history=history+[{k:v for k,v in previous.items() if k!='previous_attempts'}]
        record.status='mapped';record.audit={**audit,'objective_ids':linked[i],'previous_attempts':history}
        record.save(update_fields=['status','audit'])
    return True


def run_mapping_job(job):
    if not 1<=job.count<=5:raise ValidationError('Choose 1–5 inventory batches.')
    job.audit={**job.audit,'inventory_prompt':PROMPT_VERSION}
    job.save(update_fields=['audit'])
    if not Source.objects.for_study().filter(pk=job.chapter.source_id,kind='guideline').exists():
        raise ValidationError('Mapping needs an active primary guideline.')
    if job.notes_source_id:map_segments(job)  # Validate selection before file work.
    prepare_source_reading(job.chapter.source)
    if job.notes_source_id:prepare_source_reading(job.notes_source)
    segments=map_segments(job)
    if not job.retry_blocked and prior_blocked_parts(segments,mapping_state(segments)):
        raise ValidationError('This source has a blocked earlier inventory. Review it and explicitly select retry after changing the reading view.')
    if job.notes_source_id:require_mapped_chapter(job.chapter)
    states=mapping_state(segments)
    pending=[s for s in segments if (s['page'].pk,s['start'],s['digest']) not in states or
        states[(s['page'].pk,s['start'],s['digest'])].status=='pending' or
        (job.retry_blocked and states[(s['page'].pk,s['start'],s['digest'])].status in ('blocked','partial'))]
    groups=list(pack_segments(pending));completed=blocked=0
    for group in groups[:job.count]:
        records=[CoverageSegment.objects.get_or_create(page=s['page'],start=s['start'],digest=s['digest'],defaults={'end':s['end']})[0] for s in group]
        if any(not s['text'].strip() for s in group):
            block_records(records,'No extractable text. Check the original file or OCR.',{'prompt_version':PROMPT_VERSION})
            blocked+=len(records);continue
        context=mapping_context(job,group)
        from .automatic_mapping import process_group
        done, accepted = process_group(job, records, context, ask_model)
        if done: completed += len(records)
        else: blocked += len(records)
        job.audit={**job.audit,'verified_source_parts':job.audit.get('verified_source_parts',0)+(len(records) if done else 0),
                   'verified_source_points':job.audit.get('verified_source_points',0)+accepted}
        job.save(update_fields=['audit'])
    job.message=f'{completed} source parts fully mapped; {blocked} remain incomplete. {job.audit.get("verified_source_points",0)} source-checked objectives retained. Automatic checks require no manual approval. Match objectives, then link existing questions.'
    job.save(update_fields=['message'])
