import base64
import json
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from pydantic import ValidationError as SchemaError
from study.codex_review import export_review, apply_review


class Command(BaseCommand):
    help = 'Export blinded evidence or apply an actual separately recorded Codex review. Never calls an API.'
    def add_arguments(self, p):
        p.add_argument('action', choices=['export', 'apply'])
        p.add_argument('job_id'); p.add_argument('path')
        p.add_argument('--user', default='tomas'); p.add_argument('--author-session')
    def handle(self, *args, **o):
        user = get_user_model().objects.get(username=o['user'])
        path = Path(o['path'])
        try:
            if o['action'] == 'export':
                result = export_review(o['job_id'], user, o['author_session'] or '')
                path.mkdir(parents=True, exist_ok=True)
                for key in ['blind', 'rationale']:
                    (path / (key + '.json')).write_text(json.dumps(result[key], ensure_ascii=False, indent=2))
                images = result.pop('images')
                for im in images:
                    (path / f"page-{im['page_id']}.png").write_bytes(base64.b64decode(im['data_url'].split(',', 1)[1]))
                (path / 'manifest.json').write_text(json.dumps({k: v for k, v in result.items()
                    if k not in ('blind', 'rationale')}, indent=2))
                self.stdout.write(str(path))
            else:
                job, changed = apply_review(o['job_id'], user, json.loads(path.read_text()))
                self.stdout.write(json.dumps({'job_id': str(job.pk), 'changed': changed,
                                             'published': job.published, 'held': job.quarantined}))
        except (ValidationError, SchemaError, ValueError) as e:
            raise CommandError(str(e)) from e
