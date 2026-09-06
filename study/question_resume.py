"""Explicitly continue saved drafts within the same approved job cap."""
from django.db import transaction
from django.core.exceptions import ValidationError
from .models import GenerationJob, Question


@transaction.atomic
def queue_saved_questions(job_id,user):
    if not user.is_staff:raise ValidationError('Only an administrator can resume generation.')
    try:
        job=GenerationJob.objects.select_for_update().get(pk=job_id,requested_by=user,kind='questions',status='failed')
    except (GenerationJob.DoesNotExist,ValidationError,ValueError):
        raise ValidationError('Choose one of your stopped question jobs.')
    if job.calls.filter(state__in=['reserved','uncertain']).exists():
        raise ValidationError('An unsettled API call must be reconciled before continuing; it will not be retried automatically.')
    if job.spend_limit_nok is None:
        raise ValidationError('This older job has no explicit cost cap. Set a cap before continuing.')
    if not Question.objects.filter(verification__provenance__job_id=str(job.pk),status='quarantined',
        verification__state__in=['awaiting_independent_review','checked_pending_publication']).exists():
        raise ValidationError('This job has no unfinished saved drafts. Rejected questions are not automatically retried.')
    if GenerationJob.objects.filter(status__in=['queued','running']).exists():
        raise ValidationError('Let the active generation job finish first.')
    job.audit={**job.audit,'resume_saved_questions':True}
    job.status='queued';job.message='Continue saved drafts only; the original job spending limit remains in force.'
    job.finished_at=None
    job.save(update_fields=['audit','status','message','finished_at'])
    return job
