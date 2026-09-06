from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from study.models import Chapter,GenerationJob

class Command(BaseCommand):
    help='Queue a bounded generation job; the worker applies the approved API allowance.'
    def add_arguments(self,parser):
        parser.add_argument('--chapter',type=int,required=True)
        parser.add_argument('--count',type=int,default=5)
    def handle(self,*args,**options):
        if not 1<=options['count']<=50:
            raise CommandError('Use 1–50 questions per job.')
        chapter=Chapter.objects.get(pk=options['chapter'])
        job=GenerationJob.objects.create(chapter=chapter,count=options['count'],requested_by=get_user_model().objects.get(username='tomas'),
            generator_model=settings.AI_GENERATOR_MODEL,reviewer_model=settings.AI_REVIEWER_MODEL)
        self.stdout.write(f'Queued {job.id}: {job.count} questions for {chapter.title}.')
