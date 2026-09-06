import json
from decimal import Decimal
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand,CommandError
from django.core.exceptions import ValidationError
from study.question_import import import_bundle
from study.question_resume import queue_saved_questions


class Command(BaseCommand):
    help='Import at most five authored drafts per chapter, free. Only --queue-review authorizes a bounded API check.'
    def add_arguments(self,p):
        p.add_argument('file');p.add_argument('--user',default='tomas')
        p.add_argument('--cap',type=Decimal,default=Decimal('4'))
        p.add_argument('--queue-review',action='store_true')
    def handle(self,*args,**o):
        try:
            user=get_user_model().objects.get(username=o['user'])
            job,created=import_bundle(json.loads(Path(o['file']).read_text()),user,o['cap'])
            if o['queue_review']:queue_saved_questions(job.pk,user)
        except (ValidationError,ValueError) as e:raise CommandError(str(e)) from e
        self.stdout.write(json.dumps({'job_id':str(job.pk),'created':created,'queued':o['queue_review']}))
