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
    count = forms.TypedChoiceField(choices=[(n,str(n)) for n in (5,10,20,50)],coerce=int,initial=5)
