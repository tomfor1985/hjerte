"""Recover complete JSON candidates, never approval, from a token-limited map."""
import hashlib
import json
from django.core.exceptions import ValidationError
from django.db import transaction

OUTPUT_GAP = 'The output limit interrupted this inventory. Its remaining teaching points have not been accounted for.'


def recover_inventory(text, context):
    from .automatic_mapping import AutomaticMap, AutomaticObjective
    decoder = json.JSONDecoder()
    fields, objectives = {}, []
    pos = 0

    def whitespace(index):
        while index < len(text) and text[index].isspace():
            index += 1
        return index

    pos = whitespace(pos)
    if pos >= len(text) or text[pos] != '{':
        raise ValidationError('The interrupted inventory is not a JSON object.')
    pos += 1
    while pos < len(text):
        pos = whitespace(pos)
        try:
            key, pos = decoder.raw_decode(text, pos)
        except ValueError:
            break
        if key not in ('objectives', 'parts', 'cross_references', 'unresolved_content') or key in fields:
            raise ValidationError('Unexpected or duplicate inventory field.')
        pos = whitespace(pos)
        if pos >= len(text) or text[pos] != ':':
            break
        pos = whitespace(pos + 1)
        if key == 'objectives':
            fields[key] = objectives
            if pos >= len(text) or text[pos] != '[':
                raise ValidationError('Interrupted objectives must be a JSON array.')
            pos += 1
            while pos < len(text):
                pos = whitespace(pos)
                if pos < len(text) and text[pos] == ']':
                    pos += 1
                    break
                try:
                    item, end = decoder.raw_decode(text, pos)
                except ValueError:
                    pos = len(text)
                    break
                try:
                    objective = AutomaticObjective.model_validate(item)
                except ValueError as error:
                    raise ValidationError('A complete recovered candidate has an invalid schema.') from error
                pos = whitespace(end)
                # A complete object must end at an array delimiter or EOF.
                if pos < len(text) and text[pos] not in ',]':
                    raise ValidationError('Malformed candidate boundary.')
                objectives.append(objective)
                if pos < len(text) and text[pos] == ',':
                    pos += 1
                elif pos < len(text) and text[pos] == ']':
                    pos += 1
                    break
                else:
                    break
        else:
            try:
                fields[key], pos = decoder.raw_decode(text, pos)
            except ValueError:
                break
        pos = whitespace(pos)
        if pos >= len(text) or text[pos] != ',':
            break
        pos += 1
    if not objectives:
        raise ValidationError('No complete inventory candidates could be recovered.')
    # Even a complete leading parts list cannot prove that a truncated tail was
    # nonessential. An independent review is required and coverage stays partial.
    return AutomaticMap(objectives=objectives,
        parts=fields.get('parts', [{'part_id': p['part_id'], 'disposition': 'unresolved',
                                  'reason': OUTPUT_GAP} for p in context['parts']]),
        cross_references=fields.get('cross_references', []), unresolved_content=OUTPUT_GAP)


def record_recovery(job, error, proposal, purpose):
    records = job.audit.get('recovered_outputs', [])
    if not any(r['call_id'] == str(error.call_id) for r in records):
        records = records + [{'call_id': str(error.call_id), 'response_id': error.response_id,
            'purpose': purpose, 'output_sha256': hashlib.sha256(error.output_text.encode()).hexdigest(),
            'complete_candidates': len(proposal.objectives), 'output_text': error.output_text}]
        job.audit = {**job.audit, 'recovered_outputs': records}
        job.save(update_fields=['audit'])


@transaction.atomic
def resume_paid_inventory_review(job, error):
    """Resume a settled pre-fix response once, under the original cap, review only."""
    from .models import GenerationJob, CoverageSegment
    from .coverage import chapter_segments
    from .mapping import mapping_context, validate_context
    from .automatic_mapping import AutomaticMap, signature, remember
    from .pdf_images import reference_images, metadata
    job.refresh_from_db()
    if job.kind != 'map' or job.notes_source_id or job.status != 'failed' or job.spend_limit_nok is None:
        raise ValidationError('Recovery needs a failed, capped guideline inventory job.')
    if GenerationJob.objects.filter(status__in=['queued', 'running']).exists():
        raise ValidationError('Wait for active jobs before recovering the paid response.')
    call = job.calls.order_by('-created_at').first()
    if (not call or str(call.pk) != str(error.call_id) or call.state != 'settled' or
            call.purpose != 'map-repair' or call.provider_response_id != error.response_id or
            job.calls.exclude(state='settled').exists() or job.audit.get('paid_review_resume')):
        raise ValidationError('Only the last settled repair can be recovered once; uncertain calls cannot be resumed.')
    found = list(CoverageSegment.objects.filter(audit__job_id=str(job.pk)).select_related('page__source'))
    if not found:
        raise ValidationError('The original inventory checkpoint is unavailable.')
    order = found[0].audit.get('segment_ids', [])
    by_id = {r.pk: r for r in found}
    if set(order) != set(by_id) or len(order) != len(by_id):
        raise ValidationError('The recovery checkpoint has changed.')
    records = [by_id[pk] for pk in order]
    specs = {(s['page'].pk, s['start'], s['digest']): s for s in chapter_segments(job.chapter)}
    try:
        group = [specs[(r.page_id, r.start, r.digest)] for r in records]
    except KeyError as cause:
        raise ValidationError('Source segments changed after the paid repair.') from cause
    context = mapping_context(job, group)
    if any(r.audit.get('context_key') != signature(context) for r in records):
        raise ValidationError('Source context changed after the paid repair.')
    validate_context(job, records, context)
    proposal = recover_inventory(error.output_text, context)
    old = AutomaticMap.model_validate(records[0].audit['current_proposal'])
    kept = [old.objectives[int(i)] for i in records[0].audit['accepted_candidates']]
    keys = {signature(o.model_dump()) for o in kept}
    combined = AutomaticMap(objectives=kept + [o for o in proposal.objectives if signature(o.model_dump()) not in keys],
        parts=proposal.parts, cross_references=proposal.cross_references, unresolved_content=proposal.unresolved_content)
    visual = [v for v in job.audit.get('visual_requests', []) if v['purpose'] == 'map-repair']
    images = reference_images([{'visual_evidence': visual[-1]['images']}]) if visual else []
    round_number = job.audit.get('repair_rounds', 0) + 1
    if not 1 <= round_number <= 2:
        raise ValidationError('The original automatic repair limit has been exhausted.')
    record_recovery(job, error, proposal, call.purpose)
    remember(records, current_proposal=combined.model_dump(), visual_evidence=metadata(images),
             resume_job_id=str(job.pk), resume_round=round_number, resume_review_only=True)
    job.audit = {**job.audit, 'paid_review_resume': {'call_id': str(call.pk),
        'original_started_at': str(job.started_at), 'original_finished_at': str(job.finished_at),
        'review_only': True, 'recovered_candidates': len(proposal.objectives)}}
    job.status = 'queued'
    job.message = ''
    job.finished_at = None
    job.save(update_fields=['audit', 'status', 'message', 'finished_at'])
    return len(proposal.objectives)
