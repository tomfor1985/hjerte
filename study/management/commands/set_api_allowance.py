from decimal import Decimal,InvalidOperation
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from study.models import ApiBudget

class Command(BaseCommand):
    help='Set the total explicitly approved, non-recurring API allowance. Never call without user approval.'
    def add_arguments(self,parser):
        parser.add_argument('amount')
        parser.add_argument('--approval-note',required=True)
    @transaction.atomic
    def handle(self,*args,**options):
        try:
            amount=Decimal(options['amount'])
        except InvalidOperation:
            raise CommandError('Use an amount in NOK.')
        if not amount.is_finite() or amount<0:
            raise CommandError('Use a finite nonnegative amount.')
        budget,_=ApiBudget.objects.get_or_create(pk=1)
        if amount<budget.accounted_nok:
            raise CommandError('Allowance cannot be lower than already-accounted usage.')
        budget.allowance_nok=amount
        budget.approval_note=options['approval_note']
        budget.save()
        self.stdout.write(f'Total allowance: {amount:.2f} NOK; remaining: {budget.remaining:.2f} NOK.')
