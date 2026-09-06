import hashlib
from types import SimpleNamespace
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from study.models import GenerationJob
from study.generation import IncompleteModelResponse
from study.inventory_recovery import resume_paid_inventory_review


class Command(BaseCommand):
    help = 'Recover complete candidates from a settled, token-limited repair for one check under its original budget.'

    def add_arguments(self, parser):
        parser.add_argument('job_id')

    def handle(self, *args, **options):
        from openai import OpenAI
        job = GenerationJob.objects.get(pk=options['job_id'])
        call = job.calls.order_by('-created_at').first()
        if job.status != 'failed' or not call or call.state != 'settled' or call.purpose != 'map-repair':
            raise CommandError('Only a failed job with a settled last repair can be recovered.')
        cached = job.audit.get('interrupted_repair_response')
        if cached:
            if (cached['call_id'] != str(call.pk) or cached['response_id'] != call.provider_response_id or
                    hashlib.sha256(cached['output_text'].encode()).hexdigest() != cached['output_sha256']):
                raise CommandError('The saved response does not match this paid call.')
            response = SimpleNamespace(id=cached['response_id'], status=cached['status'],
                output_text=cached['output_text'], incomplete_details=SimpleNamespace(reason=cached['incomplete_reason']))
        else:
            try:
                response = OpenAI(api_key=settings.OPENAI_API_KEY, max_retries=0, timeout=60).responses.retrieve(call.provider_response_id)
            except Exception as error:
                raise CommandError(f'Could not read the paid response ({type(error).__name__}). No work queued.') from None
        if response.status != 'incomplete' or getattr(response.incomplete_details, 'reason', None) != 'max_output_tokens':
            raise CommandError('This response was not interrupted by its output limit.')
        count = resume_paid_inventory_review(job, IncompleteModelResponse(response, call))
        self.stdout.write(f'{count} complete candidates queued for one independent check; original budget unchanged. No new writing pass.')
