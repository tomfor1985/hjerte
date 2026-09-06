from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand,CommandError
from study.models import Source
from study.source_copies import prefer_pdf


class Command(BaseCommand):
    help='Link an explicitly identified alternate-format copy to its main PDF, preserving history.'
    def add_arguments(self,parser):
        parser.add_argument('--copy',type=int,required=True)
        parser.add_argument('--main',type=int,required=True)
        parser.add_argument('--user',required=True)

    def handle(self,*args,**options):
        try:
            copy=Source.objects.get(pk=options['copy'])
            main=Source.objects.get(pk=options['main'])
            user=get_user_model().objects.get(username=options['user'],is_staff=True)
            changed=prefer_pdf(copy,main,user)
        except (Source.DoesNotExist,get_user_model().DoesNotExist,ValidationError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f'{"Linked" if changed else "Already linked"}: {copy.original_name} → {main.original_name}. Historical questions and files retained.')
