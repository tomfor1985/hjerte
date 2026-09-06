from django.contrib import admin
from .models import Domain,Topic,Source,SourcePage,Chapter,Question,StudySession,GenerationJob,ApiBudget,ApiCall

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display=['short_stem','topic','question_type','difficulty','status','version']
    list_filter=['status','topic','question_type','difficulty']
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
