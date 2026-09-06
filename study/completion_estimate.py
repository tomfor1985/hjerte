"""Explicit planning scenarios for the existing library, never a spending approval."""
from decimal import Decimal, ROUND_CEILING
from .models import ApiCall, LearningObjective, Question, Source
from .coverage import coverage_report, mapping_state, segments_for
from .reconciliation import resolved_ids
from .pdf_reading import page_text

D = Decimal


def ceiling(value):
    return int(D(value).to_integral_value(rounding=ROUND_CEILING))


def completion_estimate(costs, report=None):
    report = report if report is not None else coverage_report()
    f = report['forecast']
    known = report['total']
    result = dict(available=False, complete=report['mapping_complete'], known=known, covered=report['covered'],
        known_missing=f['basic_missing'], unclassified=report['unclassified'],
        guideline_count=sum(not d['notes'] for d in report['documents']),
        guideline_pages=sum(d['source'].page_count for d in report['documents'] if not d['notes']),
        notes_count=sum(d['notes'] for d in report['documents']),
        unconfigured_pages=sum(d['gaps'] for d in report['documents']),
        missing_pages=sum(d['missing_pages'] for d in report['documents']),
        remaining_batches=f['batches_remaining'], scenarios=[], known_cost_low=None, known_cost_high=None)
    current = costs['flows'][0]
    # Until the new flow is measured, retain the observed token-mix scenario
    # at the selected models; never infer an API discount from fewer characters.
    rate = current['unit'] if current['unit'] is not None else f['planning_unit']
    if rate is None:
        result['reason'] = 'A measured question run is needed before a cost scenario can be calculated.'
        return result
    low_rate = rate
    high_rate = max(rate, f['unit'] or rate) * D('1.5')
    result.update(question_rate_low=low_rate, question_rate_high=high_rate,
        new_flow_measured=current['unit'] is not None,
        known_cost_low=f['basic_missing'] * low_rate, known_cost_high=f['basic_missing'] * high_rate)
    if result['missing_pages']:
        result['reason'] = 'Some source pages have no extracted text. A full-library estimate would omit them.'
        return result
    matched = set(LearningObjective.objects.filter(active=True, reconciliation_status='complete',
        evidence__chapter__source__active=True).values_list('pk', flat=True))
    total_chars = sampled_chars = 0
    sampled_ids = set()
    for source in Source.objects.for_study().filter(kind='guideline').prefetch_related('pages__reading'):
        pages = list(source.pages.all())
        text = {p.pk: page_text(p) for p in pages}
        segments = [s for p in pages for s in segments_for(p)]
        states = mapping_state(segments)
        for s in segments:
            # Raw layout extraction can contain huge runs of column-padding
            # spaces. Compare non-whitespace content across both PDF readers.
            characters = len(''.join(text[s['page'].pk][s['start']:s['end']].replace('\u00ad', '').split()))
            total_chars += characters
            record = states.get((s['page'].pk, s['start'], s['digest']))
            if record and record.status in ('mapped', 'partial'):
                sampled_chars += characters
                sampled_ids.update(resolved_ids(record.audit.get('objective_ids', [])) & matched)
    result.update(sampled_characters=sampled_chars, total_characters=total_chars, sample_objectives=len(sampled_ids),
        sampled_percent=100 * sampled_chars / total_chars if total_chars else 0)
    if report['mapping_complete']:
        objectives_low = objectives_high = known
    elif not sampled_chars or not sampled_ids:
        result['reason'] = 'Map a source section first to establish the density of distinct learning objectives.'
        return result
    else:
        # A deliberately broad scenario, not a confidence interval. Notes are
        # primary-source-checked ideas, not an extra copy of the same syllabus.
        unseen = max(0, total_chars - sampled_chars)
        density = D(len(sampled_ids)) / sampled_chars
        objectives_low = known + ceiling(unseen * density * D('.5'))
        objectives_high = known + ceiling(unseen * density * D('1.5'))
    calls = list(ApiCall.objects.filter(state='settled',actual_nok__isnull=False).select_related('job').only(
        'job_id', 'job__kind', 'purpose', 'actual_nok'))
    mapping = [c for c in calls if c.job.kind == 'map']
    mapping_jobs = {c.job_id for c in mapping}
    started_batches = sum(max(1, sum(c.purpose == 'map-objectives' for c in mapping if c.job_id == job_id))
                          for job_id in mapping_jobs)
    map_rate = sum((c.actual_nok for c in mapping), D(0)) / started_batches if started_batches else None
    matching_cost = sum((c.actual_nok for c in calls if c.job.kind == 'reconcile'), D(0))
    linking_cost = sum((c.actual_nok for c in calls if c.job.kind == 'link_questions'), D(0))
    match_rate = matching_cost / known if known and matching_cost else None
    linked = Question.objects.filter(objective__isnull=False, objective_link_audit__review__supported=True).count()
    link_rate = linking_cost / linked if linked and linking_cost else None
    if (result['remaining_batches'] and map_rate is None) or (
        (objectives_high > known or report['unmatched']) and match_rate is None) or (
        report['unclassified'] and link_rate is None):
        result['reason'] = 'More measured source mapping, matching or question linking is needed to price every remaining stage.'
        return result
    map_low = result['remaining_batches'] * (map_rate or D(0))
    match_low = (max(0, objectives_low - known) + report['unmatched']) * (match_rate or D(0))
    match_high = (max(0, objectives_high - known) + report['unmatched']) * (match_rate or D(0)) * D('1.5')
    link_low = report['unclassified'] * (link_rate or D(0))
    preparation_low = map_low + match_low + link_low
    preparation_high = map_low * D('1.5') + match_high + link_low * D('1.5')
    # Only active main copies supply usable question credit. Lower cost assumes
    # unclassified questions fill distinct gaps; upper cost credits known coverage only.
    usable = Question.objects.filter(status='published',chapter__source__in=Source.objects.for_study()).count()
    depth = D(sum(r['objective'].variant_limit for r in report['objectives']
                  if r['objective'].reconciliation_status == 'complete')) / known if known else D(1)
    for extended, label in ((False, 'Core coverage'), (True, 'Useful depth')):
        target_low = ceiling(objectives_low * (depth if extended else 1))
        target_high = ceiling(objectives_high * (depth if extended else 1))
        known_gap = f['extended_missing'] if extended else f['basic_missing']
        if report['mapping_complete']:
            missing_low = missing_high = known_gap
        else:
            missing_low = max(known_gap, target_low - usable)
            missing_high = max(known_gap, target_high - report['covered'])
        question_low, question_high = missing_low * low_rate, missing_high * high_rate
        additional_low, additional_high = preparation_low + question_low, preparation_high + question_high
        result['scenarios'].append(dict(label=label, extended=extended, target_low=target_low, target_high=target_high,
            questions_low=missing_low, questions_high=missing_high, question_cost_low=question_low, question_cost_high=question_high,
            additional_low=additional_low, additional_high=additional_high,
            total_low=costs['spent'] + additional_low, total_high=costs['spent'] + additional_high))
    result.update(available=True, objectives_low=objectives_low, objectives_high=objectives_high,
        preparation_low=preparation_low, preparation_high=preparation_high,
        mapping_low=map_low, mapping_high=map_low * D('1.5'),
        matching_low=match_low + link_low, matching_high=match_high + link_low * D('1.5'),
        depth_ratio=depth, map_sample_batches=started_batches)
    return result
