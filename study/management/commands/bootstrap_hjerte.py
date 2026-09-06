import os
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from study.models import Domain,Topic,ApiBudget

DOMAINS=[
('primary-care','Primary care & risk factor management',[
 ('risk-assessment','Risk assessment'),('hypertension','Hypertension'),('lipids','Lipids & lipoproteins'),
 ('diabetes','Diabetes & metabolic risk'),('smoking','Smoking cessation'),('lifestyle','Physical activity & nutrition'),
 ('psychosocial','Psychosocial health')]),
('secondary-prevention','Secondary prevention & rehabilitation',[
 ('rehabilitation','Cardiac rehabilitation'),('secondary-prevention','Secondary prevention'),('cardio-oncology','Cardio-oncology')]),
('sports-exercise','Sports cardiology & exercise',[
 ('sports-cardiology','Sports cardiology'),('exercise-testing','Exercise testing & CPET'),('exercise-prescription','Exercise prescription')]),
('population-health','Population science & public health',[
 ('public-health','Population prevention'),('epidemiology','Epidemiology & prevention policy')])]

class Command(BaseCommand):
    help='Idempotently create the study taxonomy and initial private user. Does not make API calls.'
    def handle(self,*args,**options):
        for order,(key,title,topics) in enumerate(DOMAINS):
            domain,_=Domain.objects.get_or_create(key=key,defaults={'title':title,'order':order})
            for slug,name in topics:
                Topic.objects.get_or_create(slug=slug,defaults={'title':name,'domain':domain})
        user,created=get_user_model().objects.get_or_create(username='tomas',defaults={'is_staff':True,'is_superuser':True})
        if created:
            password=os.environ.get('HJERTE_BOOTSTRAP_PASSWORD','')
            if not password:
                user.set_unusable_password()
                self.stdout.write('User created with no usable password. Set one with changepassword tomas.')
            else:
                if len(password)<12:
                    raise CommandError('Bootstrap password must contain at least 12 characters.')
                user.set_password(password)
            user.save()
        ApiBudget.objects.get_or_create(pk=1)
        self.stdout.write('Taxonomy and user are ready. Existing credentials and budget were preserved.')
