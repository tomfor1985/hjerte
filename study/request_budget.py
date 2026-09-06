"""Reserve a writer and its normal review atomically, before either is sent."""
from django.db import transaction
from django.core.exceptions import ValidationError
from .models import ApiBudget, ApiCall


@transaction.atomic
def reserve_pair(job, writer, reviewer):
    from .generation import reserve_call
    # reserve_call checks both the job cap and the shared allowance. The outer
    # transaction rolls back BOTH reservations when the second one cannot fit.
    first = reserve_call(job, **writer)
    second = reserve_call(job, **reviewer)
    second.state = 'planned'
    second.request_audit = {'input_bound': reviewer['input_bound'], 'output_bound': reviewer['output_bound'],
                            'original_reservation_nok': str(second.reserved_nok), 'writer_call_id': str(first.pk)}
    second.save(update_fields=['state', 'request_audit'])
    job.audit = {**job.audit, 'reserved_question_review': str(second.pk)}
    job.save(update_fields=['audit'])
    return first


@transaction.atomic
def activate_review(job, call_id, model, purpose, input_bound, output_bound):
    from .generation import BudgetError, cost_nok
    call = ApiCall.objects.select_for_update().get(pk=call_id, job=job)
    if call.state != 'planned' or call.model != model or call.purpose != purpose:
        raise ValidationError('This review reservation is no longer available for this request.')
    budget = ApiBudget.objects.select_for_update().get(pk=1)
    from django.utils import timezone
    if timezone.localdate()>budget.price_valid_until:
        raise BudgetError('Recheck API prices before using this saved reservation.')
    amount = cost_nok(model, 'default', input_bound, output_bound, budget)
    if input_bound > 250000 or amount > call.reserved_nok or output_bound > call.request_audit['output_bound']:
        raise BudgetError('The generated draft exceeds its reserved review envelope. It remains saved, unpublished.')
    call.state = 'reserved'
    call.save(update_fields=['state'])
    return call


@transaction.atomic
def release_unused_review(job):
    """Only a provably unsent review can be refunded; never a sent/uncertain call."""
    pk = job.audit.get('reserved_question_review')
    if not pk:
        return
    call = ApiCall.objects.select_for_update().filter(pk=pk, job=job, state='planned').first()
    if call:
        budget = ApiBudget.objects.select_for_update().get(pk=1)
        budget.accounted_nok -= call.reserved_nok
        budget.save(update_fields=['accounted_nok'])
        call.state = 'cancelled'
        call.reserved_nok = 0
        call.save(update_fields=['state', 'reserved_nok'])
    job.audit = {k: v for k, v in job.audit.items() if k != 'reserved_question_review'}
    job.save(update_fields=['audit'])
