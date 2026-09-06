import time
from django.core.management.base import BaseCommand
from django.db import transaction,close_old_connections
from django.utils import timezone
from study.models import GenerationJob
from study.generation import run_job,BudgetError
from django.core.exceptions import ValidationError

class Command(BaseCommand):
    help='Process generation jobs serially; never automatically retry uncertain API calls.'
    def add_arguments(self,parser):
        parser.add_argument('--once',action='store_true')
        parser.add_argument('--drain',action='store_true',help='Exit when no queued jobs remain.')
    def handle(self,*args,**options):
        while True:
            close_old_connections()
            with transaction.atomic():
                job=GenerationJob.objects.filter(status='queued').select_related('chapter__source','chapter__topic').order_by('created_at').first()
                if job:
                    job.status='running'
                    job.started_at=timezone.now()
                    job.save()
            if job:
                try:
                    run_job(job)
                    job.status='complete'
                    job.message='Generation and independent checks complete.'
                except (BudgetError,ValidationError) as e:
                    job.status='failed'
                    job.message=' '.join(e.messages) if isinstance(e,ValidationError) else str(e)
                except Exception as e:
                    job.status='failed'
                    # Provider exceptions can contain request bodies. Do not log them.
                    chain=[]
                    cause=e
                    while cause is not None and len(chain)<4:
                        chain.append(type(cause).__name__)
                        cause=cause.__cause__
                    job.message=f'Generation stopped ({" / ".join(chain)}). No automatic retry; inspect the API audit before another job.'
                job.finished_at=timezone.now()
                job.save()
                self.stdout.write(f'{job.id}: {job.status}; {job.published} published, {job.quarantined} held.')
            if options['once'] or (options['drain'] and not job):
                break
            time.sleep(5)
