import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.utils import timezone

class Command(BaseCommand):
    help='Create a consistent database and source-library backup, then check integrity.'
    def add_arguments(self,parser):
        parser.add_argument('--directory',default=os.environ.get('BACKUP_DIR',str(settings.BASE_DIR/'backups')))
    def handle(self,*args,**options):
        directory=Path(options['directory']);directory.mkdir(parents=True,exist_ok=True)
        os.umask(0o077)
        target=directory/('hjerte-'+timezone.now().strftime('%Y%m%dT%H%M%S%f')+'.tar.gz')
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'hjerte.sqlite3'
            with sqlite3.connect(f'file:{settings.DATABASES["default"]["NAME"]}?mode=ro',uri=True) as live:
                with sqlite3.connect(db) as copy:
                    live.backup(copy)
                    if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                        raise CommandError('Backup database integrity check failed.')
            with tarfile.open(target,'w:gz') as archive:
                archive.add(db,arcname='hjerte.sqlite3')
                if Path(settings.MEDIA_ROOT).exists():archive.add(settings.MEDIA_ROOT,arcname='sources')
        # Delete only backups created by this command and only after success.
        for old in sorted(directory.glob('hjerte-*.tar.gz'),reverse=True)[30:]:old.unlink()
        self.stdout.write(f'Backup verified: {target.name}')
