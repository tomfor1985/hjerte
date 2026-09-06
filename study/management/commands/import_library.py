from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand
from study.sources import ALLOWED,import_source

class Command(BaseCommand):
    help='Import source documents from the configured local library. Originals are never modified.'
    def add_arguments(self,parser):
        parser.add_argument('--path',default=settings.SOURCE_LIBRARY)
    def handle(self,*args,**options):
        root=Path(options['path']).resolve()
        if not root.is_dir():
            from django.core.management.base import CommandError
            raise CommandError('Source library directory does not exist.')
        groups={}
        for file in sorted(root.rglob('*')):
            if not file.is_file() or file.suffix.lower() not in ALLOWED or not file.resolve().is_relative_to(root):
                continue
            source,created=import_source(file.read_bytes(),file.name)
            groups.setdefault(file.parent,[]).append(source)
            self.stdout.write(f'{"Imported" if created else "Already present"}: {source.title} ({source.page_count} locations)')
        for sources in groups.values():
            guidelines=[s for s in sources if s.kind=='guideline']
            for source in sources:
                if source.kind=='notes' and guidelines:
                    source.supporting_guidelines.add(*guidelines)
