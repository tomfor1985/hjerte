"""Source-version coverage and finite generation planning. Reading a plan is free."""
import hashlib
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Sum
from .models import (Source, SourcePage, Chapter, Question, LearningObjective,
                     CoverageSegment, ApiCall, GenerationJob, ApiBudget)

SEGMENT_SIZE = 12000
FAILURE_LIMIT = 2


def segments_for(page):
    from .pdf_reading import page_text, reading_for
    content = page_text(page)
    reading = reading_for(page)
    if reading and reading.passages:
        start, end = 0, 0
        for passage in reading.passages:
            if passage['end'] - start > SEGMENT_SIZE and end > start:
                text = content[start:end]
                yield {'page': page, 'start': start, 'end': end, 'digest': hashlib.sha256(text.encode()).hexdigest(), 'text': text}
                start = end
            end = passage['end']
        text = content[start:]
        yield {'page': page, 'start': start, 'end': len(content), 'digest': hashlib.sha256(text.encode()).hexdigest(), 'text': text}
        return
    # Never silently skip long pages or blank/OCR-failed pages.
    for start in range(0, max(1, len(content)), SEGMENT_SIZE):
        text = content[start:start + SEGMENT_SIZE]
        yield {'page': page, 'start': start, 'end': start + len(text),
               'digest': hashlib.sha256(text.encode()).hexdigest(), 'text': text}


def chapter_segments(chapter):
    return [s for p in chapter.source.pages.select_related('source', 'reading').filter(number__gte=chapter.first_page,
            number__lte=chapter.last_page) for s in segments_for(p)]


def pack_segments(segments,limit=48000):
    """Amortize API overhead for short note paragraphs without dropping pages."""
    group, size = [], 0
    for segment in segments:
        length=len(segment['text'])
        if group and (size+length>limit or not segment['text'].strip()):
            yield group
            group,size=[],0
        if not segment['text'].strip():
            yield [segment]
        else:
            group.append(segment)
            size+=length
    if group:
        yield group


def mapping_state(segments):
    ids = {s['page'].pk for s in segments}
    return {(s.page_id, s.start, s.digest): s for s in CoverageSegment.objects.filter(page_id__in=ids)}


def prior_blocked_parts(segments, states):
    keys={(s['page'].pk,s['start'],s['digest']) for s in segments}
    pending_pages={key[0] for key in keys if key not in states or states[key].status!='mapped'}
    return [r for key,r in states.items() if r.status=='blocked' and key not in keys and r.page_id in pending_pages]


def objective_rows(chapter=None):
    objectives = LearningObjective.objects.filter(active=True, evidence__chapter__source__active=True).distinct()
    if chapter:
        objectives = objectives.filter(evidence__chapter=chapter)
    rows = []
    for obj in objectives.prefetch_related('questions__chapter__source').order_by('id'):
        count = sum(q.status == 'published' and q.chapter.source.active for q in obj.questions.all())
        blocked = bool(obj.blocked_reason) or obj.failed_attempts >= FAILURE_LIMIT or obj.reconciliation_status!='complete'
        rows.append({'objective': obj, 'count': count, 'missing': int(count == 0),
                     'remaining': max(0, obj.variant_limit - count),
                     'state': 'Awaiting matching' if obj.reconciliation_status=='pending' else ('Needs review' if blocked else ('Target reached' if count >= obj.variant_limit else ('Uncovered' if count == 0 else 'Covered'))),
                     'blocked': blocked})
    return rows


def plan_targets(chapter, strategy, count, notes_source=None):
    if not Source.objects.for_study().filter(pk=chapter.source_id,kind='guideline').exists():
        raise ValidationError('Choose an active main guideline copy.')
    if LearningObjective.objects.filter(active=True,evidence__chapter__source__active=True).exclude(reconciliation_status='complete').exists():
        raise ValidationError('Match the newly inventoried learning objectives before generating questions.')
    if strategy not in ('coverage', 'variants') or not 1 <= count <= 50:
        raise ValidationError('Choose a valid generation plan and a limit of 1–50 questions.')
    segments = chapter_segments(chapter)
    state = mapping_state(segments)
    if len({s['page'].number for s in segments}) != chapter.last_page - chapter.first_page + 1:
        raise ValidationError('This chapter has missing extracted pages. Check the source import before generating questions.')
    if not segments or any(state.get((s['page'].pk, s['start'], s['digest'])) is None or
            state[(s['page'].pk, s['start'], s['digest'])].status != 'mapped' for s in segments):
        raise ValidationError('Map every part of this chapter before generating questions; unresolved text stays visible in Coverage.')
    if chapter.questions.filter(status='published', objective__isnull=True).exists():
        raise ValidationError('Link the existing published questions to learning objectives before generating more for this chapter.')
    note_ids=None
    if notes_source:
        if not Source.objects.for_study().filter(pk=notes_source.pk,kind='notes',supporting_guidelines=chapter.source_id).exists():
            raise ValidationError('Choose active notes linked to this guideline.')
        note_segments=[s for p in notes_source.pages.all() for s in segments_for(p)]
        note_states=mapping_state(note_segments)
        if not note_segments or any((s['page'].pk,s['start'],s['digest']) not in note_states or
                note_states[(s['page'].pk,s['start'],s['digest'])].status!='mapped' for s in note_segments):
            raise ValidationError('Map and verify the selected notes before generating their teaching points.')
        from .reconciliation import resolved_ids
        note_ids=resolved_ids({oid for s in note_states.values() if s.status=='mapped' for oid in s.audit.get('objective_ids',[])})
    all_rows = objective_rows()
    if strategy == 'variants' and any(r['count'] == 0 and not r['blocked'] for r in all_rows):
        raise ValidationError('Cover the remaining mapped learning objectives before adding variants.')
    rows = objective_rows(chapter)
    eligible = [r for r in rows if not r['blocked'] and r['remaining'] > 0 and
                (note_ids is None or r['objective'].pk in note_ids) and
                (r['count'] == 0 if strategy == 'coverage' else r['count'] > 0)]
    eligible.sort(key=lambda r: (r['count'], r['objective'].pk))
    return [r['objective'] for r in eligible[:count]]


def record_failed_objective(obj, reason):
    obj.refresh_from_db()
    obj.failed_attempts += 1
    if obj.failed_attempts >= FAILURE_LIMIT:
        obj.blocked_reason = reason or 'Two unsuccessful attempts. Review the source and testing angle before trying again.'
    obj.save(update_fields=['failed_attempts', 'blocked_reason'])


def cost_forecast(rows, mapping_complete, generator=None, reviewer=None):
    # Count each job once. Include rejected candidates and failed jobs' settled
    # calls, but keep uncertain reservations separate from measured unit cost.
    question_jobs = GenerationJob.objects.filter(kind='questions')
    published = question_jobs.aggregate(n=Sum('published'))['n'] or 0
    calls = ApiCall.objects.filter(job__kind='questions')
    measured = calls.filter(state='settled').aggregate(n=Sum('actual_nok'))['n'] or Decimal('0')
    uncertain = ApiCall.objects.filter(state__in=['reserved', 'uncertain']).aggregate(n=Sum('reserved_nok'))['n'] or Decimal('0')
    unit = measured / published if published and measured else None
    # Reprice the observed token mix for the configured models. This remains a
    # scenario: another model can use different tokens and have a different yield.
    from .generation import cost_nok, PRICES
    budget=ApiBudget.objects.filter(pk=1).first() or ApiBudget()
    generator=generator or settings.AI_GENERATOR_MODEL
    reviewer=reviewer or settings.AI_REVIEWER_MODEL
    configured={'generate':generator,'blind-review':reviewer,'rationale-review':reviewer}
    settled=list(calls.filter(state='settled'))
    can_reprice=bool(settled and published) and all(c.input_tokens>0 and c.purpose in configured and
        configured[c.purpose] in PRICES for c in settled) and settings.AI_SERVICE_TIER in ('flex','default')
    repriced=sum((cost_nok(configured[c.purpose],settings.AI_SERVICE_TIER,c.input_tokens,c.output_tokens,budget)
                  for c in settled),Decimal('0')) if can_reprice else None
    planning_unit=repriced/published if repriced is not None else unit
    basic = sum(r['missing'] for r in rows)
    extended = sum(r['remaining'] for r in rows)
    from .mapping import PROMPT_VERSION
    map_calls = ApiCall.objects.filter(job__kind='map', state='settled',job__audit__inventory_prompt=PROMPT_VERSION)
    map_spend = map_calls.aggregate(n=Sum('actual_nok'))['n'] or Decimal('0')
    map_attempts = map_calls.filter(purpose='map-objectives').count()
    matching_spend=ApiCall.objects.filter(job__kind__in=['reconcile','link_questions'],state='settled').aggregate(n=Sum('actual_nok'))['n'] or Decimal('0')
    inventory_calls=ApiCall.objects.filter(job__audit__pipeline='inventory-2',state='settled')
    inventory_usage=inventory_calls.aggregate(cost=Sum('actual_nok'),inputs=Sum('input_tokens'),cached=Sum('cached_tokens'),reasoning=Sum('reasoning_tokens'))
    inventory_cost=inventory_usage['cost'] or Decimal('0')
    return {'sample': published, 'measured': measured, 'unit': unit, 'uncertain': uncertain,
            'planning_unit':planning_unit,'repriced':repriced,'generator':generator,
            'reviewer':reviewer,'mapper':settings.AI_MAPPING_MODEL,
            'map_spend': map_spend, 'map_unit': map_spend / map_attempts if map_attempts else None,
            'matching_spend':matching_spend,
            'inventory_cost':inventory_cost,'inventory_usage':inventory_usage,
            'cost_per_matched_objective':inventory_cost/len(rows) if rows and map_attempts else None,
            'basic_missing': basic, 'extended_missing': extended,
            'basic': planning_unit * basic if planning_unit is not None else None,
            'extended': planning_unit * extended if planning_unit is not None else None,
            # Explicit planning reserve, not a statistical confidence interval.
            'basic_with_reserve': planning_unit * basic * Decimal('1.5') if planning_unit is not None else None,
            'extended_with_reserve': planning_unit * extended * Decimal('1.5') if planning_unit is not None else None,
            'complete': mapping_complete}


def coverage_report(generator=None,reviewer=None):
    documents = []
    all_complete = True
    for source in Source.objects.for_study().prefetch_related('pages__reading', 'chapters').order_by('kind', 'title'):
        segments = [s for p in source.pages.all() for s in segments_for(p)]
        states = mapping_state(segments)
        from .pdf_reading import reading_for
        reading_pages=sum(reading_for(p) is not None for p in source.pages.all())
        unreadable_pages=sum(bool((reading:=reading_for(p)) and '\ufffd' in reading.text) for p in source.pages.all())
        keys={(s['page'].pk,s['start'],s['digest']) for s in segments}
        old_blocked=len(prior_blocked_parts(segments,states))
        cross_references={}
        for key,record in states.items():
            if key in keys and record.status=='mapped':
                for ref in record.audit.get('draft',{}).get('cross_references',[]):
                    cross_references[(ref['passage_id'],ref['target'])]=ref
        outstanding=[s for s in segments if (s['page'].pk,s['start'],s['digest']) not in states or
                     states[(s['page'].pk,s['start'],s['digest'])].status!='mapped']
        mapped = sum(bool(states.get((s['page'].pk, s['start'], s['digest'])) and
                     states[(s['page'].pk, s['start'], s['digest'])].status == 'mapped') for s in segments)
        blocked = sum(bool(states.get((s['page'].pk, s['start'], s['digest'])) and
                      states[(s['page'].pk, s['start'], s['digest'])].status == 'blocked') for s in segments)
        covered_pages = {n for c in source.chapters.all() for n in range(c.first_page, c.last_page + 1)}
        gaps = source.page_count - len(set(range(1, source.page_count + 1)) & covered_pages) if source.kind == 'guideline' else 0
        missing_pages = source.page_count - source.pages.count()
        complete = bool(segments) and mapped == len(segments) and not missing_pages
        all_complete = all_complete and complete
        documents.append({'source': source, 'notes': source.kind == 'notes', 'total': len(segments), 'mapped': mapped,
                          'reading_pages':reading_pages,'unreadable_pages':unreadable_pages,'old_blocked':old_blocked,'cross_references':list(cross_references.values()),
                          'blocked': blocked, 'gaps': gaps, 'missing_pages': missing_pages, 'complete': complete,
                          'batches_remaining':len(list(pack_segments(outstanding)))})
    rows = objective_rows()
    unclassified = Question.objects.filter(status='published', chapter__source__active=True, objective__isnull=True).count()
    unmatched=sum(r['objective'].reconciliation_status!='complete' for r in rows)
    confirmed=[r for r in rows if r['objective'].reconciliation_status=='complete']
    all_complete = all_complete and not unclassified and not unmatched and any(not d.get('notes') for d in documents)
    forecast = cost_forecast(confirmed, all_complete,generator,reviewer)
    forecast['segments_remaining'] = sum(d['total'] - d['mapped'] for d in documents)
    forecast['batches_remaining'] = sum(d['batches_remaining'] for d in documents)
    forecast['mapping_remaining'] = forecast['map_unit'] * forecast['batches_remaining'] if forecast['map_unit'] is not None else None
    return {'documents': documents, 'objectives': rows, 'total': len(confirmed),'unmatched':unmatched,
            'covered': sum(r['count'] > 0 for r in rows), 'unclassified': unclassified,
            'blocked': sum(r['blocked'] for r in rows), 'mapping_complete': all_complete,
            'forecast': forecast,
            'chapters': Chapter.objects.filter(source__kind='guideline', source__active=True,source__duplicate_of__isnull=True).select_related('source')}


def question_catalog():
    # All saved questions across documents, including quarantined drafts. No
    # last-N truncation that would allow an old idea to be generated again.
    return [{'id': str(q.pk), 'objective_id': q.objective_id, 'stem': q.stem,
             'learning_point': q.learning_point, 'testing_angle': q.testing_angle}
            for q in Question.objects.exclude(status='retired').order_by('id')]
