from django import forms
from django.conf import settings
from .models import Chapter, Topic, Source


class PracticeForm(forms.Form):
    count = forms.TypedChoiceField(choices=[(n,str(n)) for n in (5,10,20,30,50,70,100)], coerce=int, initial=10)
    topic = forms.ModelChoiceField(queryset=Topic.objects.all(), required=False, empty_label='All topics')
    chapter = forms.ModelChoiceField(queryset=Chapter.objects.select_related('source').all(), required=False, empty_label='All chapters')
    focus = forms.ChoiceField(choices=[('adaptive','Adaptive mix'),('new','New questions'),('weak','Previous mistakes and uncertainty'),('flagged','My flagged questions')])


class ImportForm(forms.Form):
    file = forms.FileField()
    title = forms.CharField(max_length=300, required=False)
    kind = forms.ChoiceField(choices=Source.KIND, initial='notes')
    year = forms.IntegerField(min_value=1990,max_value=2100,required=False)
    doi = forms.CharField(max_length=160,required=False)
    topic = forms.ModelChoiceField(queryset=Topic.objects.all(),required=False,
        help_text='For a guideline: the default topic for automatically suggested chapters.')
    supporting_guidelines = forms.ModelMultipleChoiceField(queryset=Source.objects.for_study().filter(kind='guideline'),required=False,help_text='For notes: select the guidelines that must support their teaching points.')

    def clean(self):
        data = super().clean()
        if data.get('kind') == 'guideline' and not data.get('topic'):
            self.add_error('topic','Choose a topic for the chapter suggestions.')
        return data


class NotesSourcesForm(forms.ModelForm):
    class Meta:
        model = Source
        fields = ['supporting_guidelines']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['supporting_guidelines'].queryset = Source.objects.for_study().filter(kind='guideline')


ChapterFormSet = forms.inlineformset_factory(Source,Chapter,
    fields=['title','topic','section','first_page','last_page'],extra=0,can_delete=False)


class ModelPairForm(forms.Form):
    generator_model = forms.ChoiceField(choices=[('gpt-5.6-terra','Terra · lower cost'),('gpt-5.6-sol','Sol'),('gpt-6-astra','Astra')],required=False,label='Question writer')
    reviewer_model = forms.ChoiceField(choices=[('gpt-5.6-sol','Sol'),('gpt-6-astra','Astra')],required=False,label='Verification model')

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['generator_model'].initial=settings.AI_GENERATOR_MODEL
        self.fields['reviewer_model'].initial=settings.AI_REVIEWER_MODEL

    def clean(self):
        data=super().clean()
        data['generator_model']=data.get('generator_model') or settings.AI_GENERATOR_MODEL
        data['reviewer_model']=data.get('reviewer_model') or settings.AI_REVIEWER_MODEL
        return data


class GenerateForm(ModelPairForm):
    spend_limit_nok = forms.DecimalField(min_value=1,max_value=200,decimal_places=2,required=False,initial=25,
        label='Maximum job spend (NOK)',help_text='Writing and the normal source check must both fit before a question group starts.')
    strategy = forms.ChoiceField(choices=[('coverage','Increase coverage'),('variants','Add useful variants')],initial='coverage',required=False,
        help_text='Uncovered objectives first. Variants require a distinct testing angle and stop at the objective ceiling.')
    chapter = forms.ModelChoiceField(queryset=Chapter.objects.filter(source__kind='guideline',source__active=True,source__duplicate_of__isnull=True).select_related('source'))
    notes_source = forms.ModelChoiceField(queryset=Source.objects.for_study().filter(kind='notes'),required=False,
        label='Focus on specific notes',empty_label='Use relevant linked notes',
        help_text='Optional. Questions use this file for teaching ideas and the selected guideline for evidence.')
    count = forms.TypedChoiceField(choices=[(n,str(n)) for n in (5,10,20,50)],coerce=int,initial=5,label='Questions to request')

    def clean(self):
        data=super().clean()
        from decimal import Decimal
        data['spend_limit_nok']=data.get('spend_limit_nok') or Decimal('25')
        data['strategy']=data.get('strategy') or 'coverage'
        chapter,notes=data.get('chapter'),data.get('notes_source')
        if chapter and notes and not notes.supporting_guidelines.filter(pk=chapter.source_id).exists():
            self.add_error('notes_source','Link these notes to the selected guideline in source setup first.')
        return data


class MappingForm(forms.Form):
    kind = forms.ChoiceField(choices=[('map','1. Map & verify source content'),('reconcile','2. Match shared learning objectives'),('link_questions','3. Link existing questions')],required=False,label='Next step')
    chapter = forms.ModelChoiceField(queryset=Chapter.objects.filter(source__kind='guideline', source__active=True,source__duplicate_of__isnull=True).select_related('source'))
    notes_source = forms.ModelChoiceField(queryset=Source.objects.for_study().filter(kind='notes'), required=False,
        label='Map study notes instead', empty_label='Map the guideline chapter',
        help_text='Notes are inventoried only when their teaching points can be verified against this guideline chapter.')
    notes_first_location = forms.IntegerField(min_value=1,required=False,label='Notes: first location (optional)')
    notes_last_location = forms.IntegerField(min_value=1,required=False,label='Notes: last location (optional)',help_text='Limit notes to the section supported by this chapter. PDF: page; slides: slide; Word: extracted section. Leave both empty to use the whole file.')
    count = forms.TypedChoiceField(choices=[(n, f'Up to {n} work batch'+('es' if n>1 else '')) for n in (1, 3, 5)], coerce=int, initial=1,
        help_text='A batch covers a source section or up to ten objectives/questions. The job stops at this limit.')
    generator_model = forms.ChoiceField(choices=[('gpt-5.6-sol','Sol · automatic source checks'),('gpt-5.6-terra','Terra · lower cost'),('gpt-6-astra','Astra · difficult material')],required=False,label='Inventory model')
    spend_limit_nok = forms.DecimalField(min_value=1,max_value=200,decimal_places=2,required=False,initial=25,label='Maximum job spend (NOK)',help_text='Also limited by the remaining approved allowance. Reservations must fit before a request starts.')
    retry_blocked = forms.BooleanField(required=False,label='Make another automatic attempt on unresolved items')
    retry_reason = forms.CharField(required=False,label='Reason for retry',help_text='Optional context for another bounded job. Source checks and up to two repair rounds run automatically.')

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['generator_model'].initial=settings.AI_MAPPING_MODEL

    def clean(self):
        data=super().clean()
        from decimal import Decimal
        data['kind']=data.get('kind') or 'map'
        data['generator_model']=data.get('generator_model') or settings.AI_MAPPING_MODEL
        data['spend_limit_nok']=data.get('spend_limit_nok') or Decimal('25')
        chapter,notes=data.get('chapter'),data.get('notes_source')
        if chapter and notes and not notes.supporting_guidelines.filter(pk=chapter.source_id).exists():
            self.add_error('notes_source','Link these notes to the selected guideline first.')
        if data['kind']!='map' and notes:
            self.add_error('notes_source','Leave notes unselected for objective matching or question linking; the chapter selects the scope.')
        first,last=data.get('notes_first_location'),data.get('notes_last_location')
        if first is not None or last is not None:
            if data['kind']!='map' or not notes or first is None or last is None or last<first or last>notes.page_count:
                self.add_error('notes_last_location','Choose a valid inclusive range within the selected notes file, for source mapping only.')
        if data.get('retry_blocked') and not data.get('retry_reason','').strip():
            data['retry_reason']='User requested another bounded automatic source check.'
        return data
