"""Automatic, bounded source inventory with per-objective acceptance and repair."""
import hashlib
import json
import subprocess
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from .generation import BudgetError
from .mapping import (CitedMap, CitedObjective, ScopedMapReview, ObjectiveCheck, resolve_citations,
                      prompt_context, visible_references, PROMPT_VERSION)
from .models import LearningObjective, ObjectiveEvidence, SourcePage
from .pdf_images import page_image, metadata, validate_images, MAX_IMAGES

MAX_REPAIRS = 2


class AutomaticObjective(CitedObjective):
    visual_page_ids: list[int]


class AutomaticMap(CitedMap):
    objectives: list[AutomaticObjective]


class AutomaticCheck(ObjectiveCheck):
    qualifications_complete: bool
    visual_page_ids: list[int]
    reason: str


class AutomaticReview(ScopedMapReview):
    checks: list[AutomaticCheck]
    image_page_ids: list[int]


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def remember(records, **values):
    for record in records:
        record.audit = {**record.audit, **values}
        record.save(update_fields=['audit'])


def load_images(ids, context, existing):
    evidence = context.get('primary_evidence', context['parts'])
    allowed = {p['page_id'] for p in evidence}
    wanted = set(ids) | {i['page_id'] for i in existing}
    if not wanted.issubset(allowed) or len(wanted) > MAX_IMAGES:
        raise ValidationError('Visual evidence points outside the supplied primary pages or exceeds the image limit.')
    return [page_image(SourcePage.objects.select_related('source').get(pk=pk)) for pk in sorted(wanted)]


def resolve_objectives(proposal, context):
    resolved, errors = {}, {}
    for index, obj in enumerate(proposal.objectives):
        try:
            one = CitedMap(objectives=[obj], parts=[], cross_references=[], unresolved_content='')
            draft = resolve_citations(one, context)
            visible_references([r.model_dump() for r in draft.objectives[0].references],
                               context.get('primary_evidence', context['parts']))
            resolved[index] = draft.objectives[0]
        except ValidationError as error:
            errors[index] = ' '.join(error.messages)
    return resolved, errors


@transaction.atomic
def save_checked_inventory(job, records, proposal, review, context, images):
    from .mapping import validate_context
    validate_context(job, records, context)
    validate_images(images)
    part_ids = set(range(len(records)))
    parts = {p.part_id: p for p in proposal.parts}
    part_checks = {p.part_id: p for p in review.parts}
    checks = {c.index: c for c in review.checks}
    if (set(parts) != part_ids or len(parts) != len(proposal.parts) or set(part_checks) != part_ids or
            len(part_checks) != len(review.parts) or set(checks) != set(range(len(proposal.objectives))) or
            len(checks) != len(review.checks)):
        raise ValidationError('Inventory review omitted or duplicated an objective or source part.')
    resolved, errors = resolve_objectives(proposal, context)
    image_index = {i['page_id']: i for i in metadata(images)}
    accepted, held, linked = [], [], {i: [] for i in part_ids}
    keys = {}
    for index, obj in enumerate(proposal.objectives):
        check = checks[index]
        visual = set(obj.visual_page_ids) | set(check.visual_page_ids)
        reason = errors.get(index, '')
        if not obj.part_ids or not set(obj.part_ids).issubset(part_ids) or len(set(obj.part_ids)) != len(obj.part_ids):
            reason = 'Invalid source-part assignment.'
        elif any(parts[i].disposition == 'nonlearning' for i in obj.part_ids):
            reason = 'Objective linked to an excluded source part.'
        elif not all((check.supported, check.useful_angles, check.correct_source_parts, check.qualifications_complete)):
            reason = check.reason or 'The independent source check did not pass.'
        elif not visual.issubset(image_index):
            reason = 'Necessary original figure pages were not supplied to the independent check.'
        if reason:
            held.append({'index': index, 'reason': reason})
            continue
        mapped = resolved[index]
        refs = [r.model_dump() for r in mapped.references]
        if visual:
            refs[0]['visual_evidence'] = [image_index[pk] for pk in sorted(visual)]
        key = signature({'chapter_id': job.chapter_id, 'source_sha256': job.chapter.source.sha256,
                         'title': obj.title, 'angles': obj.testing_angles, 'references': refs})
        objective, created = LearningObjective.objects.get_or_create(inventory_key=key, defaults={
            'title': obj.title, 'topic': job.chapter.topic, 'variant_limit': len(obj.testing_angles),
            'depth_reason': '\n'.join(obj.testing_angles), 'reconciliation_status': 'pending'})
        if not created and (objective.title != obj.title or
                objective.variant_limit != len(obj.testing_angles) or
                objective.depth_reason != '\n'.join(obj.testing_angles) or
                not objective.evidence.filter(chapter=job.chapter, references=refs).exists()):
            held.append({'index': index, 'reason': 'The saved objective was edited; this check does not approve its changed content.'})
            continue
        if not created and objective.blocked_reason.startswith('A subsequent inventory check'):
            objective.blocked_reason = ''
            objective.save(update_fields=['blocked_reason'])
        if created:
            ObjectiveEvidence.objects.create(objective=objective, chapter=job.chapter, references=refs)
        accepted.append(index)
        keys[str(index)] = objective.pk
        for part_id in obj.part_ids:
            linked[part_id].append(objective.pk)
    cross = {c.index: c for c in review.cross_references}
    cross_ok = (len(cross) == len(review.cross_references) == len(proposal.cross_references) and
                set(cross) == set(range(len(proposal.cross_references))))
    if cross_ok:
        try:
            resolve_citations(CitedMap(objectives=[], parts=[], cross_references=proposal.cross_references,
                                      unresolved_content=''), context)
        except ValidationError:
            cross_ok = False
    cross_ok = cross_ok and all(c.target_is_outside_supplied_parts and c.supplied_claims_are_fully_covered for c in cross.values())
    complete = bool(not held and cross_ok and set(review.image_page_ids).issubset(image_index) and not proposal.unresolved_content and review.complete_inventory and
                    not review.missing_points and all(p.disposition != 'unresolved' and
                    part_checks[i].all_teaching_points_covered and
                    (bool(linked[i]) if p.disposition == 'learning' else bool(p.reason.strip()) and part_checks[i].exclusion_justified)
                    for i, p in parts.items()))
    # A negative later check must not leave the old candidate usable. Source and
    # question history remain intact; generation excludes blocked objectives.
    previous = records[0].audit.get('accepted_candidates', {})
    current = set(keys.values())
    for index, old_id in previous.items():
        if old_id not in current:
            LearningObjective.objects.filter(pk=old_id, merged_into__isnull=True).update(
                blocked_reason='A subsequent inventory check did not retain this candidate.')
    audit = {'job_id': str(job.pk), 'prompt_version': PROMPT_VERSION,
             'source_sha256': records[0].page.source.sha256, 'evidence_source_id': job.chapter.source_id,
             'evidence_sha256': job.chapter.source.sha256, 'context_key': signature(context),
             'current_proposal': proposal.model_dump(), 'review': review.model_dump(),
             'accepted_candidates': keys, 'held_candidates': held, 'visual_evidence': metadata(images),
             'segment_ids': [r.pk for r in records], 'complete': complete,
             'reason': '; '.join(review.missing_points) or proposal.unresolved_content or
                       ('Some objectives did not pass the source check.' if held else ''),
             'draft': {'cross_references': [c.model_dump() for c in proposal.cross_references]},
             'reused_draft_job_id': records[0].audit.get('reused_draft_job_id'),
             'reused_draft_version': records[0].audit.get('reused_draft_version')}
    for index, record in enumerate(records):
        previous_audit = record.audit
        history = previous_audit.get('previous_attempts', [])
        if previous_audit:
            history = history + [{k: v for k, v in previous_audit.items() if k != 'previous_attempts'}]
        record.status = 'mapped' if complete else ('partial' if linked[index] else 'blocked')
        record.audit = {**audit, 'objective_ids': linked[index], 'previous_attempts': history}
        record.save(update_fields=['status', 'audit'])
    return complete, accepted, held



def reuse_passage_draft(audit, records, context):
    """An old paid draft is reusable evidence, never an automatic approval."""
    if (audit.get('prompt_version') != 'inventory-3-passages' or
            audit.get('segment_ids') != [r.pk for r in records] or
            audit.get('source_sha256') != records[0].page.source.sha256):
        return None
    evidence = context.get('primary_evidence', context['parts'])
    snapshots = [{'page_id': p['page_id'], 'text_sha256': hashlib.sha256(p['text'].encode()).hexdigest()} for p in evidence]
    if audit.get('evidence_pages') != snapshots:
        return None
    available = {(p['page_id'], passage['start'], passage['end']): passage['id'] for p in evidence for passage in p['passages']}
    draft = audit.get('draft', {})
    try:
        objectives = []
        for obj in draft['objectives']:
            visible_references(obj['references'], evidence)
            citations = [{'passage_id': available[(r['page_id'], r['passage_start'], r['passage_end'])], 'section': r['section']} for r in obj['references']]
            objectives.append(AutomaticObjective(title=obj['title'], testing_angles=obj['testing_angles'],
                                                part_ids=obj['part_ids'], citations=citations, visual_page_ids=[]))
        return AutomaticMap(objectives=objectives, parts=draft['parts'],
                            cross_references=draft.get('cross_references', []), unresolved_content=draft['unresolved_content'])
    except (KeyError, ValidationError, ValueError):
        return None

def process_group(job, records, context, ask):
    from .mapping import MAP_INSTRUCTIONS, MAP_REVIEW, block_records, save_map
    from django.conf import settings
    from .mapping_prompts import AUTO_MAP, AUTO_REVIEW, AUTO_REPAIR
    if job.spend_limit_nok is None:
        job.spend_limit_nok = Decimal('25')
        job.save(update_fields=['spend_limit_nok'])
    writer = job.generator_model or settings.AI_MAPPING_MODEL
    reviewer = job.reviewer_model or settings.AI_REVIEWER_MODEL
    context_key = signature(context)
    old = records[0].audit
    proposal = None
    images = []
    if (old.get('context_key') == context_key and old.get('prompt_version') == PROMPT_VERSION and
            old.get('current_proposal') and not job.calls.filter(state='uncertain').exists()):
        proposal = AutomaticMap.model_validate(old['current_proposal'])
        images = load_images([i['page_id'] for i in old.get('visual_evidence', [])], context, [])
    if proposal is None and all(r.audit.get('pending_key') == context_key and r.audit.get('pending_model') == writer and
            r.audit.get('pending_group') == [item.pk for item in records] and r.audit.get('prompt_version') == PROMPT_VERSION for r in records):
        from .mapping import ObjectiveMap
        proposal = ObjectiveMap.model_validate(old['pending_draft'])
    if proposal is None:
        proposal = reuse_passage_draft(old, records, context)
        if proposal is not None:
            remember(records, reused_draft_job_id=old.get('job_id'), reused_draft_version=old.get('prompt_version'))
    reviewed = False
    try:
        if proposal is None:
            proposal = ask(job, writer, 'map-objectives', MAP_INSTRUCTIONS + AUTO_MAP,
                           json.dumps(prompt_context(context), ensure_ascii=False), AutomaticMap, 16000)
            remember(records, job_id=str(job.pk), prompt_version=PROMPT_VERSION, raw_proposal=proposal.model_dump())
        # Compatibility for previously stored/explicitly supplied old schemas.
        # New API calls are always constrained to AutomaticMap/AutomaticReview.
        if not isinstance(proposal, AutomaticMap):
            draft = resolve_citations(proposal, context) if isinstance(proposal, CitedMap) else proposal
            visible_references([r.model_dump() for o in draft.objectives for r in o.references], context.get('primary_evidence', context['parts'])) if draft.objectives else None
            remember(records, pending_key=context_key, pending_model=writer, pending_group=[r.pk for r in records], pending_draft=draft.model_dump())
            review = ask(job, reviewer, 'map-review', MAP_REVIEW,
                         json.dumps({**prompt_context(context), 'proposed_inventory': draft.model_dump()}, ensure_ascii=False), ScopedMapReview, 12000)
            done = save_map(job, records, draft, review, context)
            return done, len(draft.objectives) if done else 0
        for round_number in range(MAX_REPAIRS + 1):
            remember(records, current_proposal=proposal.model_dump(), context_key=context_key,
                     prompt_version=PROMPT_VERSION, job_id=str(job.pk))
            # Writer-requested visuals are included in the first independent check.
            requested = {pk for obj in proposal.objectives for pk in obj.visual_page_ids}
            image_error = ''
            try:
                images = load_images(requested, context, images)
            except (ValidationError, OSError, subprocess.SubprocessError) as error:
                image_error = 'Original page images unavailable; visual claims must stay unresolved.'
            body = {**prompt_context(context), 'proposed_inventory': proposal.model_dump(),
                    'supplied_images': metadata(images), 'image_issue': image_error}
            review = ask(job, reviewer, 'map-review', MAP_REVIEW + AUTO_REVIEW,
                         json.dumps(body, ensure_ascii=False), AutomaticReview, 12000, **({'images': images} if images else {}))
            if not isinstance(review, AutomaticReview):
                raise ValidationError('Automatic inventory requires an item-level source review.')
            done, accepted, held = save_checked_inventory(job, records, proposal, review, context, images)
            reviewed = True
            job.audit = {**job.audit, 'repair_rounds': round_number,
                         'accepted_objectives': len(accepted), 'held_objectives': len(held)}
            job.save(update_fields=['audit'])
            if done or round_number == MAX_REPAIRS:
                return done, len(accepted)
            # Retain approved candidates, ask only for additions and replacements.
            retained = [proposal.objectives[i] for i in accepted]
            wanted = set(review.image_page_ids) | {pk for c in review.checks for pk in c.visual_page_ids}
            try:
                images = load_images(wanted, context, images)
            except (ValidationError, OSError, subprocess.SubprocessError):
                image_error = 'Original page images unavailable. Do not infer visual relationships.'
            repair_model = 'gpt-6-astra' if wanted or images or round_number == 1 else writer
            repair_body = {**prompt_context(context), 'retained_objectives': [o.model_dump() for o in retained],
                           'rejected_objectives': [proposal.objectives[h['index']].model_dump() for h in held],
                           'source_review': review.model_dump(), 'mechanical_issues': held,
                           'unresolved_content': proposal.unresolved_content, 'supplied_images': metadata(images),
                           'image_issue': image_error}
            replacement = ask(job, repair_model, 'map-repair', MAP_INSTRUCTIONS + AUTO_MAP + AUTO_REPAIR,
                              json.dumps(repair_body, ensure_ascii=False), AutomaticMap, 16000,
                              **({'images': images} if images else {}))
            remember(records, raw_repair=replacement.model_dump(), repair_model=repair_model)
            retained_keys = {signature(o.model_dump()) for o in retained}
            added = [o for o in replacement.objectives if signature(o.model_dump()) not in retained_keys]
            proposal = AutomaticMap(objectives=retained + added, parts=replacement.parts,
                                    cross_references=replacement.cross_references, unresolved_content=replacement.unresolved_content)
    except BudgetError:
        if job.calls.filter(state='uncertain').exists():
            raise
        if reviewed:
            remember(records, stop_reason='Automatic repair stopped at the approved job budget. Verified objectives are retained.')
            job.audit = {**job.audit, 'repair_stop': 'budget'}
            job.save(update_fields=['audit'])
            return False, len(records[0].audit.get('accepted_candidates', {}))
        block_records(records, 'Stopped at the job budget; the paid proposal is saved.', dict(records[0].audit))
        raise
    except Exception:
        if not reviewed:
            block_records(records, 'Automatic mapping stopped. Saved proposals are retained; no uncertain API call is retried.',
                          {**records[0].audit, 'job_id': str(job.pk), 'prompt_version': PROMPT_VERSION})
        raise
