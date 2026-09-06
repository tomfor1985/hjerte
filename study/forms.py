from django import forms
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
    supporting_guidelines = forms.ModelMultipleChoiceField(queryset=Source.objects.filter(kind='guideline',active=True),required=False,help_text='For notes: select the guidelines that must support their teaching points.')

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
        self.fields['supporting_guidelines'].queryset = Source.objects.filter(kind='guideline',active=True)


ChapterFormSet = forms.inlineformset_factory(Source,Chapter,
    fields=['title','topic','section','first_page','last_page'],extra=0,can_delete=False)


class GenerateForm(forms.Form):
    chapter = forms.ModelChoiceField(queryset=Chapter.objects.filter(source__kind='guideline',source__active=True).select_related('source'))
    notes_source = forms.ModelChoiceField(queryset=Source.objects.filter(kind='notes',active=True),required=False,
        label='Focus on specific notes',empty_label='Use relevant linked notes',
        help_text='Optional. Questions use this file for teaching ideas and the selected guideline for evidence.')
    count = forms.TypedChoiceField(choices=[(n,str(n)) for n in (5,10,20,50)],coerce=int,initial=5)

    def clean(self):
        data=super().clean()
        chapter,notes=data.get('chapter'),data.get('notes_source')
        if chapter and notes and not notes.supporting_guidelines.filter(pk=chapter.source_id).exists():
            self.add_error('notes_source','Link these notes to the selected guideline in source setup first.')
        return data
