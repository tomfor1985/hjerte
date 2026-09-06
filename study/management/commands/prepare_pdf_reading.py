from django.core.management.base import BaseCommand, CommandError
from study.models import Source, GenerationJob
from study.pdf_reading import prepare_source_reading


class Command(BaseCommand):
    help = 'Build and save PDF reading order locally; no API calls or original-text edits.'

    def add_arguments(self, parser):
        parser.add_argument('--source', type=int, required=True)

    def handle(self, *args, **options):
        if GenerationJob.objects.filter(status__in=['queued','running']).exists():
            raise CommandError('Wait for active AI jobs before preparing reading views.')
        source=Source.objects.for_study().get(pk=options['source'])
        count=prepare_source_reading(source)
        self.stdout.write(f'{count} PDF page reading views saved. Existing original text, questions and audits preserved. No API use.')
