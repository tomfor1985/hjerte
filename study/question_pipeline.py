"""Generate from saved objectives, with a funded normal check and resumable drafts."""
import json
from difflib import SequenceMatcher
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError
from .models import Question, Source, LearningObjective
from .coverage import plan_targets, record_failed_objective, question_catalog
from .sources import validate_references, normalize
from . import pdf_images
from .pdf_images import metadata
from .cited_questions import CitedQuestion, CitedQuestionBatch, question_references
from .question_evidence import evidence_packet, validate_packet
from .question_checks import (REVIEW_INSTRUCTIONS, REVIEW_LIMIT, QUESTION_BYTES, dump, candidate,
                              review_base, verify_candidates, passes, fingerprint)
from .reconciliation import objective_details, signature
from .request_budget import release_unused_review
from .generation import BudgetError, GENERATOR_INSTRUCTIONS, NoveltyReviewBatch


def catalogue_without(candidates):
    excluded={str(q.pk) for q in candidates}
    return [q for q in question_catalog() if q['id'] not in excluded]


def write_group(job,targets,ask,writer,reviewer):
    """Shrink a group only before any generation call has been purchased."""
    while targets:
        refs=[r for obj in targets for ev in obj.evidence.filter(chapter=job.chapter) for r in ev.references]
        images=pdf_images.reference_images(refs)
        context,parts,stats=evidence_packet(job.chapter,refs,images)
        catalog=question_catalog()
        base=review_base(context,catalog,targets)
        body={**base,'count':len(targets)}
        body.pop('questions')
        instructions=GENERATOR_INSTRUCTIONS.replace(
            'and at least one exact supporting quote from a supplied page. Each quote should be a short sentence or passage of 30-400 characters that appears verbatim in the source. Reference the actual printed guideline section/table and the provided database page_id.',
            'and at least one supporting reference selecting a supplied passage_id. Do not write quotations; the server copies the selected primary passage verbatim.')+'''
Use ONLY the supplied planned objective IDs, at most one question per objective. These objectives were already mapped and checked against the original guideline. Do not invent a new objective or rerun source inventory. Give a precise testing_angle; skip cosmetic variants of existing questions.
Select existing passage_id values for citations; the server copies their exact text. A truthful descriptive section/table locator is acceptable; never claim an unverified formal heading. Keep each question compact (at most 6000 UTF-8 bytes of question, options, explanations and citation IDs).'''
        followup={'model':reviewer,'purpose':'question-review','instructions':REVIEW_INSTRUCTIONS,
            'prompt':dump(base),'schema':NoveltyReviewBatch,'output_limit':REVIEW_LIMIT,'images':images,
            # Every accepted candidate is checked against this byte envelope.
            # UTF-8 bytes upper-bound its tokens; include the counter's 10% margin.
            'extra_input_bound':len(targets)*(QUESTION_BYTES*11//10+128)}
        before=set(job.calls.values_list('pk',flat=True))
        try:
            batch=ask(job,writer,'generate',instructions,dump(body),CitedQuestionBatch,10000,
                      images=images,reserve_followup=followup)
            job.audit={**job.audit,'question_evidence_version':'compact-passages-2',
                'last_proposal':batch.model_dump(),'evidence_packets':job.audit.get('evidence_packets',[])+[stats]}
            job.save(update_fields=['audit'])
            return targets,batch,context,parts,images,catalog
        except BudgetError:
            if len(targets)==1 or set(job.calls.values_list('pk',flat=True))!=before:
                raise
            targets=targets[:-1]


@transaction.atomic
def publish_checked(job,candidates,targets,verdicts,objective_snapshot,catalog_snapshot):
    if signature(objective_details([o.pk for o in targets]))!=signature(objective_snapshot):
        raise ValidationError('The learning objectives changed during verification. Saved drafts remain unpublished.')
    if signature(catalogue_without(candidates))!=signature(catalog_snapshot):
        raise ValidationError('The question bank changed during verification. Recheck novelty before publication.')
    current_source=Source.objects.get(pk=job.chapter.source_id)
    for i,q in enumerate(candidates):
        current=Question.objects.select_for_update().get(pk=q.pk)
        if current.status!='quarantined' or fingerprint(current)!=fingerprint(q):
            raise ValidationError('A checked question was edited before publication.')
        if current.verification.get('checked_question_hash')!=fingerprint(current):
            raise ValidationError('The saved check does not describe this question.')
        q.verification=current.verification
        if not current_source.active or current_source.sha256!=q.verification['provenance']['sha256']:
            raise ValidationError('The primary source changed; saved checks cannot publish against a different edition.')
        validate_references(q.references);pdf_images.reference_images(q.references)
        obj=LearningObjective.objects.select_for_update().get(pk=q.objective_id)
        ok=passes(verdicts[i],q.answer) and obj.active and not obj.blocked_reason and obj.reconciliation_status=='complete'
        ok=ok and obj.questions.filter(status='published',chapter__source__active=True).count()<obj.variant_limit
        if Source.objects.filter(pk__in=q.verification['provenance'].get('note_source_ids',[]),active=False).exists():ok=False
        q.status='published' if ok else 'quarantined'
        q.verification={**q.verification,'state':'reviewed','source_checked':True}
        q.full_clean(exclude=['fingerprint']);q.save()
        if ok:
            obj.failed_attempts=0;obj.save(update_fields=['failed_attempts'])
        else:
            record_failed_objective(obj,'The independent source, rationale or novelty check did not pass.')


def check_group(job,candidates,targets,context,catalog,images,ask,reviewer):
    validate_packet(job.chapter,context)
    objective_snapshot=objective_details([o.pk for o in targets])
    verdicts=verify_candidates(job,candidates,targets,context,catalog,images,ask,reviewer)
    validate_packet(job.chapter,context)
    publish_checked(job,candidates,targets,verdicts,objective_snapshot,catalog)


def resume_saved(job,ask,reviewer):
    candidates=list(Question.objects.filter(verification__provenance__job_id=str(job.pk),status='quarantined',
        verification__state__in=['awaiting_independent_review','checked_pending_publication']).order_by('created_at','pk'))
    if not candidates:
        raise ValidationError('This job has no unfinished question drafts to resume.')
    for q in candidates:
        if q.chapter_id!=job.chapter_id or q.verification['provenance']['sha256']!=job.chapter.source.sha256:
            raise ValidationError('The saved drafts belong to a different source version.')
    targets=list(LearningObjective.objects.filter(pk__in={q.objective_id for q in candidates},active=True,
        reconciliation_status='complete').order_by('pk'))
    if {o.pk for o in targets}!={q.objective_id for q in candidates}:
        raise ValidationError('A saved draft has an inactive or unresolved learning objective.')
    refs=[r for q in candidates for r in q.references]
    images=pdf_images.reference_images(refs)
    # The same source packet and candidate order allow exact saved API results
    # to be reused; changed inputs always require a fresh independent check.
    saved=job.audit.get('pending_check')
    if saved and saved['ids']==[str(q.pk) for q in candidates] and saved['hashes']==[fingerprint(q) for q in candidates]:
        context=saved['context'];catalog=catalogue_without(candidates)
        if catalog==saved['catalog']:
            saved_targets={o.pk:o for o in LearningObjective.objects.filter(pk__in=saved['target_ids'])}
            if set(saved_targets)!=set(saved['target_ids']):raise ValidationError('A saved objective is missing.')
            targets=[saved_targets[pk] for pk in saved['target_ids']]
            images=pdf_images.reference_images([{'visual_evidence':saved['images']}])
        else:
            context,_,_=evidence_packet(job.chapter,refs,images)
    else:
        context,_,_=evidence_packet(job.chapter,refs,images);catalog=catalogue_without(candidates)
    # A changed source page, even with the same file name, invalidates the draft.
    validate_references(refs)
    check_group(job,candidates,targets,context,catalog,images,ask,reviewer)
    job.message=f'Finished the saved question drafts. {len(candidates)} checked; no new questions were written.'
    job.save(update_fields=['message'])


def run_questions(job,ask):
    writer=job.generator_model or settings.AI_GENERATOR_MODEL
    reviewer=job.reviewer_model or settings.AI_REVIEWER_MODEL
    if not Source.objects.for_study().filter(pk=job.chapter.source_id,kind='guideline').exists():
        raise ValidationError('Generate from an active primary guideline.')
    if job.calls.filter(state__in=['reserved','uncertain']).exists():
        raise BudgetError('An unsettled API call blocks automatic continuation. Its cost remains reserved.')
    try:
        if job.audit.get('resume_saved_questions'):
            return resume_saved(job,ask,reviewer)
        job.audit={**job.audit,'question_pipeline':'compact-2'}
        job.save(update_fields=['audit'])
        remaining=job.count
        while remaining>0:
            targets=plan_targets(job.chapter,job.strategy,min(5,remaining),job.notes_source)
            if not targets:
                job.message='No eligible learning objectives remain. No further API call was made.'
                job.save(update_fields=['message']);break
            targets,batch,context,parts,images,catalog=write_group(job,targets,ask,writer,reviewer)
            if len(batch.questions)>len(targets):raise ValidationError('The writer exceeded the requested question count.')
            target_map={o.pk:o for o in targets};attempted=set();candidates=[]
            old=list(Question.objects.exclude(status='retired').values_list('stem',flat=True))
            for draft in batch.questions:
                if draft.objective_id not in target_map or draft.objective_id in attempted:
                    raise ValidationError('The writer returned an unplanned or repeated objective.')
                attempted.add(draft.objective_id)
                payload=draft.model_dump()
                if isinstance(draft,CitedQuestion):payload['references']=question_references(draft.references,parts)
                visual_ids={i['page_id'] for r in target_map[draft.objective_id].evidence.get(chapter=job.chapter).references
                            for i in r.get('visual_evidence',[])}
                visual_ids|={r['page_id'] for r in payload['references']}
                selected=[i for i in images if i['page_id'] in visual_ids]
                if selected and payload['references']:payload['references'][0]['visual_evidence']=metadata(selected)
                q=Question(chapter=job.chapter,topic=job.chapter.topic,**payload,generated_by=writer,
                    verification={'state':'awaiting_independent_review','provenance':{
                        'source_id':job.chapter.source_id,'sha256':job.chapter.source.sha256,'year':job.chapter.source.year,
                        'doi':job.chapter.source.doi,'chapter_id':job.chapter_id,'context_page_ids':[p['page_id'] for p in parts],
                        'note_source_ids':[job.notes_source_id] if job.notes_source_id else [],
                        'job_id':str(job.pk),'prompt_version':'questions-2-combined'}})
                try:
                    q.clean();validate_references(q.references,{p['page_id'] for p in parts})
                    if len(dump(candidate(q,len(candidates))).encode())>QUESTION_BYTES:
                        raise ValidationError('The draft exceeds the reserved compact review envelope.')
                    if any(SequenceMatcher(None,normalize(q.stem),normalize(s)).ratio()>.9 for s in old):
                        raise ValidationError('Near-duplicate question.')
                    q.save();candidates.append(q);old.append(q.stem)
                except ValidationError as e:
                    q.verification={**q.verification,'state':'rejected_structure','reason':' '.join(e.messages)}
                    try:q.clean();q.save()
                    except (ValidationError,IntegrityError):pass
                    record_failed_objective(target_map[draft.objective_id],'The source/structure/novelty check did not pass.')
            for obj in targets:
                if obj.pk not in attempted:record_failed_objective(obj,'No supported new testing angle was returned.')
            remaining-=len(targets)
            if candidates:
                catalog=catalogue_without(candidates)
                job.audit={**job.audit,'pending_check':{'ids':[str(q.pk) for q in candidates],
                    'hashes':[fingerprint(q) for q in candidates],'target_ids':[o.pk for o in targets],
                    'context':context,'catalog':catalog,'images':metadata(images)}}
                job.save(update_fields=['audit'])
                check_group(job,candidates,targets,context,catalog,images,ask,reviewer)
            release_unused_review(job)
    finally:
        release_unused_review(job)
        own=Question.objects.filter(verification__provenance__job_id=str(job.pk))
        job.published=own.filter(status='published').count()
        job.quarantined=own.filter(status='quarantined').count()
        job.save(update_fields=['published','quarantined'])
