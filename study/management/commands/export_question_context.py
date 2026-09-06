import json
from pathlib import Path
from django.core.management.base import BaseCommand
from study.models import Chapter
from study.pdf_reading import page_text,evidence_part,prompt_part
from study.question_import import AuthoredBundle,objective_catalog
from study.coverage import question_catalog

class Command(BaseCommand):
    help='Export primary passages and existing questions for Codex authoring, without API use.'
    def add_arguments(self,p):
        p.add_argument('--chapter',type=int,required=True);p.add_argument('--output',required=True)
    def handle(self,*args,**o):
        c=Chapter.objects.select_related('source').get(pk=o['chapter'],source__active=True,source__duplicate_of__isnull=True,source__kind='guideline')
        result={'chapter_id':c.pk,'chapter':c.title,'source_sha256':c.source.sha256,'source_title':c.source.title,
            'year':c.source.year,'doi':c.source.doi,'private_pdf_url':f'/library/{c.source_id}/file/',
            'pages':[prompt_part(evidence_part(p,0,len(page_text(p)))) for p in c.source.pages.filter(number__range=(c.first_page,c.last_page)).select_related('source','reading')],
            'existing_questions':question_catalog(),'existing_objectives':objective_catalog(),
            'bundle_schema':AuthoredBundle.model_json_schema(),
            'instructions':'Write original English five-option MCQs with source-backed explanations. Mix cases and direct questions. Select passage IDs exactly. Avoid existing concepts unless a distinct useful angle exists. Do not claim complete coverage.'}
        Path(o['output']).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        self.stdout.write('Source context exported. No API use.')
