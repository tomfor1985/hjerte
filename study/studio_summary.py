"""Read-only Studio guidance and cost reporting from the existing allowance ledger."""
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from .models import ApiCall, Chapter, GenerationJob, LearningObjective, Question, Source
from .coverage import plan_targets

ZERO = Decimal('0')
UNFINISHED = ('awaiting_independent_review', 'checked_pending_publication')
CATEGORIES = (
    ('inventory', 'Source inventory'),
    ('matching', 'Objective matching & question linking'),
    ('writing', 'Question writing'),
    ('checking', 'Question checks'),
    ('extra', 'Extra checks & repairs'),
    ('other', 'Other recorded work'),
)


def category(call):
    if call.purpose in ('map-repair', 'question-extra-review'):
        return 'extra'
    if call.purpose in ('map-objectives', 'map-review'):
        return 'inventory'
    if call.purpose in ('reconcile', 'reconcile-review', 'link_questions', 'link_questions-review'):
        return 'matching'
    if call.purpose == 'generate':
        return 'writing'
    if call.purpose in ('blind-review', 'rationale-review', 'question-review'):
        return 'checking'
    return 'other'


def tally(calls):
    amounts = dict(spent=ZERO, reserved=ZERO, uncertain=ZERO, missing_usage=0)
    for call in calls:
        if call.state == 'settled' and call.actual_nok is not None:
            amounts['spent'] += call.actual_nok
        elif call.state in ('planned', 'reserved'):
            amounts['reserved'] += call.reserved_nok
        elif call.state != 'cancelled':
            # Missing usage and unknown states must never masquerade as free work.
            amounts['uncertain'] += call.reserved_nok
            amounts['missing_usage'] += 1
    amounts['committed'] = amounts['spent'] + amounts['reserved'] + amounts['uncertain']
    return amounts


def review_credit(job, calls):
    return sum((c.reserved_nok for c in calls if c.state == 'planned' and c.purpose == 'question-review'
                and str(c.pk) == job.audit.get('reserved_question_review')), ZERO)


def cost_summary(budget):
    calls = list(ApiCall.objects.only('job_id', 'purpose', 'state', 'actual_nok', 'reserved_nok'))
    jobs = list(GenerationJob.objects.select_related('chapter__source').order_by('-created_at'))
    by_job = defaultdict(list)
    for call in calls:
        by_job[call.job_id].append(call)
    unfinished = defaultdict(int)
    for provenance in Question.objects.filter(status='quarantined', verification__state__in=UNFINISHED).values_list('verification__provenance', flat=True):
        if provenance:
            unfinished[provenance.get('job_id')] += 1
    for job in jobs:
        job.cost = tally(by_job[job.pk])
        job.has_unsettled_call = any(c.state in ('reserved', 'uncertain') for c in by_job[job.pk])
        job.cost['categories'] = [dict(key=key, label=label, **tally(c for c in by_job[job.pk] if category(c) == key))
                                  for key, label in CATEGORIES if any(category(c) == key for c in by_job[job.pk])]
        job.unfinished_count = unfinished[str(job.pk)]
        job.cap_remaining = max(ZERO, job.spend_limit_nok - job.cost['committed']) if job.spend_limit_nok is not None else None
        job.review_credit = review_credit(job, by_job[job.pk])
        job.available = min(job.cap_remaining, budget.remaining) + job.review_credit if job.cap_remaining is not None else ZERO
        job.unit_cost = job.cost['spent'] / job.published if job.kind == 'questions' and job.published else None
        # Resuming an older run does not relabel its legacy calls as a new-flow pilot.
        job.current_flow = job.audit.get('question_pipeline') == 'compact-2' and not any(
            c.purpose in ('blind-review', 'rationale-review') for c in by_job[job.pk])
    totals = tally(calls)
    totals['difference'] = budget.accounted_nok - totals['committed']
    totals['categories'] = [dict(key=key, label=label, **tally(c for c in calls if category(c) == key))
                            for key, label in CATEGORIES if key != 'other' or any(category(c) == key for c in calls)]
    totals['jobs'] = jobs
    totals['unfinished'] = sum(unfinished.values())
    totals['flows'] = []
    for current, label in ((True, 'New question flow'), (False, 'Earlier or mixed question runs')):
        cohort = [job for job in jobs if job.kind == 'questions' and job.current_flow == current]
        cohort_calls = [call for job in cohort for call in by_job[job.pk]]
        published = sum(job.published for job in cohort)
        amounts = tally(cohort_calls)
        totals['flows'].append(dict(label=label, current=current, published=published,
            calls=len(cohort_calls), unit=amounts['spent'] / published if published else None, **amounts))
    totals['preparation'] = tally(call for job in jobs if job.kind in ('map', 'reconcile', 'link_questions') for call in by_job[job.pk])['spent']
    return totals


def resume_reason(job, user, active=False):
    if job.requested_by_id != user.pk:
        return 'Only the administrator who started this run can continue it.'
    if job.kind != 'questions' or job.status != 'failed' or not job.unfinished_count:
        return 'No unfinished drafts to continue.'
    if job.has_unsettled_call or job.cost['missing_usage']:
        return 'An earlier API request is unsettled. Its reservation is retained; it will not be retried automatically.'
    if not job.chapter.source.active or job.chapter.source.duplicate_of_id:
        return 'This source is no longer the active main copy.'
    if job.spend_limit_nok is None:
        return 'This older run has no explicit cost cap. A cap must be set before continuing.'
    if job.available <= 0 or job.cost['committed'] > job.spend_limit_nok:
        return 'No allowance is available within this run’s original cap.'
    if active:
        return 'Wait for the active run to finish.'
    return ''


def inventory_link(chapter, kind, retry=False):
    return '/studio/coverage/?' + urlencode({'chapter': chapter.pk, 'kind': kind, **({'retry': '1'} if retry else {})}) + '#inventory-step'


def next_step(summary, user):
    jobs = summary['jobs']
    active = next((job for job in jobs if job.status in ('queued', 'running')), None)
    for job in jobs:
        job.resume_reason = resume_reason(job, user, bool(active))
        job.can_resume = not job.resume_reason
    if active:
        return dict(kind='wait', title='A run is already in progress',
            description='Its results and costs will appear here when it finishes. Refresh this page to check progress.',
            job=active, label='View this run', url=f'#job-{active.pk}')
    saved = next((job for job in jobs if job.can_resume), None)
    if saved:
        return dict(kind='resume', title='Finish your saved questions', job=saved,
            description='These drafts are already written. Continue their source checks before paying to write more.',
            label='Finish saved questions')
    held = next((job for job in jobs if job.status == 'failed' and job.unfinished_count and job.requested_by_id == user.pk), None)
    if held:
        return dict(kind='held', title='Saved questions need attention', job=held,
            description=held.resume_reason, label='View this run', url=f'#job-{held.pk}')
    chapters = Chapter.objects.filter(source__in=Source.objects.for_study().filter(kind='guideline')).select_related('source').order_by('source_id', 'first_page')
    pending = LearningObjective.objects.filter(active=True, evidence__chapter__source__active=True).exclude(reconciliation_status='complete').order_by('pk').first()
    if pending:
        chapter = chapters.filter(objective_evidence__objective=pending).first()
        if chapter:
            return dict(kind='inventory', title='Match the saved learning objectives',
                description='Check which concepts are shared across sources before generating questions. The existing inventory is reused.',
                chapter=chapter, label='Prepare objective matching', url=inventory_link(chapter, 'reconcile', pending.reconciliation_status == 'blocked'))
    # Consider only chapters with mapped objectives. Planning is local and makes no API requests.
    candidates = chapters.filter(objective_evidence__objective__active=True).distinct()
    linking = None
    for chapter in candidates:
        if chapter.questions.filter(status='published', objective__isnull=True).exists():
            linking = linking or chapter
            continue
        try:
            targets = plan_targets(chapter, 'coverage', 5)
        except ValidationError:
            continue
        if targets:
            return dict(kind='generate', title='Cover the next learning objectives', chapter=chapter,
                description='This chapter has checked learning objectives without a published question. Reuse them to broaden the bank.',
                label='Prepare new questions', url=f'/studio/?chapter={chapter.pk}#generate-questions')
    if linking:
        return dict(kind='inventory', title='Link the questions you already have', chapter=linking,
            description='Connect published questions to saved learning objectives before creating more for this chapter.',
            label='Prepare question linking', url=inventory_link(linking, 'link_questions'))
    chapter = chapters.first()
    if chapter:
        return dict(kind='inventory', title='Choose the next source section',
            description='Review the remaining source gaps, then map a section. Saved objectives and questions are retained.',
            label='Open coverage planner', url='/studio/coverage/#inventory-step')
    return dict(kind='import', title='Add your first guideline', description='Import a primary guideline to start building your question bank.',
        label='Add a source', url='#add-source')
