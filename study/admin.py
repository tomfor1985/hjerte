from django.contrib import admin
from django import forms
from .models import (Domain,Topic,Source,SourcePage,Chapter,Question,StudySession,GenerationJob,
                     ApiBudget,ApiCall,LearningObjective,ObjectiveEvidence,CoverageSegment)

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display=['short_stem','topic','question_type','difficulty','status','version']
    list_filter=['status','topic','question_type','difficulty',('objective',admin.EmptyFieldListFilter)]
    search_fields=['stem','learning_point']
    readonly_fields=['fingerprint','verification','generated_by','created_at','updated_at','version']
    def short_stem(self,obj):
        return obj.stem[:110]
    def save_model(self,request,obj,form,change):
        if change:
            obj.version+=1
        obj.verification={**obj.verification,'edited_by':request.user.username,'manual_edit':True}
        super().save_model(request,obj,form,change)

@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display=['title','year','kind','page_count','active']
    readonly_fields=['sha256','original_name','file','page_count','imported_at','extraction_warning']
    def has_add_permission(self,request):
        return False

@admin.register(Chapter)
class ChapterAdmin(admin.ModelAdmin):
    list_display=['title','source','topic','first_page','last_page']
    list_filter=['source','topic']

@admin.register(ApiBudget)
class BudgetAdmin(admin.ModelAdmin):
    readonly_fields=[f.name for f in ApiBudget._meta.fields]
    def has_add_permission(self,request): return False
    def has_delete_permission(self,request,obj=None): return False

@admin.register(ApiCall,GenerationJob)
class AuditAdmin(admin.ModelAdmin):
    def get_readonly_fields(self,request,obj=None):
        return [f.name for f in self.model._meta.fields]
    def has_add_permission(self,request): return False
    def has_delete_permission(self,request,obj=None): return False

admin.site.register([Domain,Topic])


@admin.register(LearningObjective)
class ObjectiveAdmin(admin.ModelAdmin):
    list_display=['title','topic','variant_limit','failed_attempts','active']
    list_filter=['topic','active']
    search_fields=['title','depth_reason']


@admin.register(ObjectiveEvidence)
class EvidenceAdmin(admin.ModelAdmin):
    list_display=['objective','chapter']
    readonly_fields=['objective','chapter','references']
    def has_add_permission(self,request): return False
    def has_delete_permission(self,request,obj=None): return False


class SegmentReviewForm(forms.ModelForm):
    review_reason=forms.CharField(required=False,widget=forms.Textarea,
        help_text='Required when changing status. Record why the text is excluded, resolved, or safe to retry. Pending does not itself start an API call.')
    class Meta:
        model=CoverageSegment
        fields=['status']
    def clean(self):
        data=super().clean()
        if 'status' in self.changed_data and len(data.get('review_reason','').strip())<15:
            self.add_error('review_reason','Explain the source review or retry decision in at least 15 characters.')
        return data


@admin.register(CoverageSegment)
class SegmentAdmin(admin.ModelAdmin):
    form=SegmentReviewForm
    list_display=['page','start','end','status']
    list_filter=['status','page__source']
    readonly_fields=['page','start','end','digest','audit']
    def has_add_permission(self,request): return False
    def has_delete_permission(self,request,obj=None): return False
    def save_model(self,request,obj,form,change):
        if 'status' in form.changed_data:
            obj.audit={**obj.audit,'manual_review':{'user':request.user.username,
                'reason':form.cleaned_data['review_reason'],'status':obj.status}}
        super().save_model(request,obj,form,change)
