import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from .engine import (available_questions, coverage_summary, start_session, save_answer, finish_session,
                     synchronize_clock, public_item, PART_SECONDS)
from .forms import PracticeForm, ImportForm, GenerateForm, ChapterFormSet, NotesSourcesForm
from .models import (Topic, Source, SourcePage, Chapter, Question, StudySession, SessionItem,
                     QuestionProgress, GenerationJob, ApiBudget, LoginThrottle)
from .sources import import_source, retire_source


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form=AuthenticationForm(request,data=request.POST or None)
    if request.method=='POST':
        now=timezone.now()
        key=hashlib.sha256(request.POST.get('username','').strip().casefold().encode()).hexdigest()
        with transaction.atomic():
            throttle,_=LoginThrottle.objects.get_or_create(key=key)
            if now-throttle.window_started>timedelta(minutes=15):
                throttle.failures=0
                throttle.window_started=now
            blocked=throttle.failures>=10
            if not blocked:
                throttle.failures+=1
            throttle.save()
        if blocked:
            form.add_error(None,'Too many sign-in attempts. Please try again in 15 minutes.')
        elif form.is_valid():
            LoginThrottle.objects.filter(key=key).delete()
            login(request,form.get_user())
            target=request.POST.get('next','/')
            if not url_has_allowed_host_and_scheme(target,allowed_hosts={request.get_host()},require_https=not settings.DEBUG):
                target='/'
            return redirect(target)
    return render(request,'registration/login.html',{'form':form,'next':request.GET.get('next','/')})


@login_required
def dashboard(request):
    progress=QuestionProgress.objects.filter(user=request.user,seen__gt=0)
    aggregates=progress.aggregate(seen=Sum('seen'),right=Sum('right'))
    stats={'seen':aggregates['seen'] or 0,'right':aggregates['right'] or 0,'unique':progress.count(),
           'due':progress.filter(due_at__lte=timezone.now()).count(),
           'flagged':QuestionProgress.objects.filter(user=request.user,flagged=True).count()}
    return render(request,'study/dashboard.html',{'stats':stats,'bank_count':available_questions().count(),
        'coverage':coverage_summary(),'active_sessions':StudySession.objects.filter(user=request.user).exclude(status='complete')[:5],
        'recent_sessions':StudySession.objects.filter(user=request.user,status='complete')[:5],
        'topics':Topic.objects.annotate(n=Count('questions',filter=Q(questions__status='published'))), 'nav':'study'})


@login_required
def practice(request):
    form=PracticeForm(request.POST or None,initial={'topic':request.GET.get('topic'),'chapter':request.GET.get('chapter'),'count':10})
    if request.method=='POST' and form.is_valid():
        try:
            data=form.cleaned_data
            session=start_session(request.user,count=data['count'],topic_id=data['topic'].pk if data['topic'] else None,
                chapter_id=data['chapter'].pk if data['chapter'] else None,focus=data['focus'])
            return redirect('session',session_id=session.id)
        except ValidationError as e:
            form.add_error(None,e)
    return render(request,'study/practice.html',{'form':form,'nav':'study'})


@login_required
@require_POST
def daily(request):
    try:
        session=start_session(request.user,count=10)
        return redirect('session',session_id=session.id)
    except ValidationError as e:
        messages.info(request,' '.join(e.messages))
        return redirect('practice')


@login_required
def exam(request):
    if request.method=='POST':
        try:
            session=start_session(request.user,mode='exam')
            return redirect('session',session_id=session.id)
        except ValidationError as e:
            messages.error(request,' '.join(e.messages))
    coverage=coverage_summary()
    return render(request,'study/exam.html',{'bank_count':available_questions().count(),'coverage':coverage,
         'missing_coverage':any(c['count']==0 for c in coverage),'nav':'study'})


@login_required
def session_view(request,session_id):
    with transaction.atomic():
        session=get_object_or_404(StudySession,pk=session_id,user=request.user)
        synchronize_clock(session)
    if session.status=='complete':
        return redirect('results',session_id=session.id)
    if session.status=='break':
        return render(request,'study/break.html',{'session':session,'deadline':session.break_until.isoformat(),'nav':'study'})
    try:
        position=int(request.GET.get('q',session.cursor))
    except (ValueError,TypeError):
        position=session.cursor
    item=session.items.filter(position=position).first()
    if not item or (session.mode=='exam' and item.part!=session.part):
        item=session.items.filter(part=session.part).first()
    session.cursor=item.position
    session.save(update_fields=['cursor'])
    feedback=session.mode=='practice' and item.answered_at is not None
    p=QuestionProgress.objects.filter(user=request.user,question=item.question).first()
    total=session.items.count()
    nav_items=list(session.items.filter(part=session.part).values('position','answered_at','review_flag'))
    return render(request,'study/session.html',{'session':session,'item':public_item(item,feedback),'feedback':feedback,
        'total':total,'next_position':min(item.position+1,total),'previous_position':max(item.position-1,1),
        'is_last':item.position==total,'part_last':session.mode=='exam' and item.position==70,
        'part_questions':nav_items,'answered_count':session.items.filter(answered_at__isnull=False).count(),
        'persistent_flag':p.flagged if p else False,'nav':'study',
        'deadline':(session.part_started_at+timedelta(seconds=PART_SECONDS)).isoformat() if session.mode=='exam' else ''})


@login_required
@require_POST
def answer(request,session_id,position):
    get_object_or_404(StudySession,pk=session_id,user=request.user)
    try:
        item=save_answer(session_id,request.user,position,int(request.POST.get('choice','-1')),request.POST.get('confidence',''))
        if item.session.mode=='exam':
            target=min(position+1,70 if item.part==1 else 140)
        else:
            target=position
    except (ValueError,ValidationError,SessionItem.DoesNotExist) as e:
        messages.error(request,' '.join(e.messages) if isinstance(e,ValidationError) else 'Select a valid answer.')
        target=position
    return redirect(f'/sessions/{session_id}/?q={target}')


@login_required
@require_POST
def finish(request,session_id):
    get_object_or_404(StudySession,pk=session_id,user=request.user)
    try:
        finish_session(session_id,request.user,expected_part=int(request.POST.get('part','1')))
    except (ValueError,ValidationError):
        messages.error(request,'This form belongs to a previous exam part. Refresh before submitting.')
    return redirect('session',session_id=session_id)


@login_required
@require_POST
def flag(request,session_id,position):
    with transaction.atomic():
        session=get_object_or_404(StudySession,pk=session_id,user=request.user)
        item=get_object_or_404(SessionItem,session=session,position=position)
        if request.POST.get('kind')=='review':
            item.review_flag=not item.review_flag
            item.save(update_fields=['review_flag'])
        else:
            p,_=QuestionProgress.objects.get_or_create(user=request.user,question=item.question)
            p.flagged=not p.flagged
            p.save(update_fields=['flagged'])
    return redirect(f'/sessions/{session_id}/?q={position}' if session.status!='complete' else f'/sessions/{session_id}/results/#question-{position}')


@login_required
def results(request,session_id):
    with transaction.atomic():
        session=get_object_or_404(StudySession,pk=session_id,user=request.user)
        synchronize_clock(session)
    if session.status!='complete':
        return redirect('session',session_id=session.id)
    items=list(session.items.select_related('question'))
    groups=defaultdict(lambda:{'right':0,'count':0})
    for i in items:
        g=groups[i.snapshot['topic']]
        g['count']+=1
        g['right']+=int(bool(i.correct))
    return render(request,'study/results.html',{'session':session,'items':[public_item(i,True) for i in items],
          'total':len(items),'percentage':round(100*session.score/len(items)) if items else 0,'topics':dict(groups),'nav':'progress'})


@login_required
def progress_view(request):
    items=SessionItem.objects.filter(session__user=request.user,progress_recorded=True)
    summary=[]
    for repeat,label in [(False,'First encounters'),(True,'Repeated questions')]:
        subset=items.filter(is_repeat=repeat)
        total=subset.count()
        right=subset.filter(correct=True).count()
        summary.append({'label':label,'total':total,'right':right,'percentage':round(100*right/total) if total else None})
    topic_stats=defaultdict(lambda:{'total':0,'right':0,'uncertain':0})
    for i in items:
        t=topic_stats[i.snapshot['topic']]
        t['total']+=1
        t['right']+=int(bool(i.correct))
        t['uncertain']+=int(i.confidence!='sure')
    for t in topic_stats.values():
        t['percentage']=round(100*t['right']/t['total'])
    weak=QuestionProgress.objects.filter(user=request.user,seen__gt=0).filter(Q(last_correct=False)|Q(last_confidence__in=['unsure','guessed'])|Q(flagged=True)).select_related('question__topic')[:30]
    history=StudySession.objects.filter(user=request.user,status='complete').annotate(total=Count('items'))[:30]
    return render(request,'study/progress.html',{'summary':summary,'topics':dict(topic_stats),'weak':weak,'history':history,'nav':'progress'})


@login_required
def library(request):
    sources=Source.objects.prefetch_related('chapters__topic').all().order_by('-year','title')
    chapters=Chapter.objects.select_related('source','topic').annotate(n=Count('questions',filter=Q(questions__status='published')))
    return render(request,'study/library.html',{'sources':sources,'chapters':chapters,'coverage':coverage_summary(),'nav':'library'})


@login_required
def source_file(request,source_id):
    source=get_object_or_404(Source,pk=source_id)
    try:
        response=FileResponse(source.file.open('rb'),as_attachment=not source.original_name.lower().endswith('.pdf'),filename=source.original_name)
    except FileNotFoundError:
        raise Http404('Source file is currently unavailable.')
    return response


@staff_member_required(login_url='/login/')
def studio(request):
    import_form=ImportForm()
    generate_form=GenerateForm()
    if request.method=='POST':
        if request.POST.get('action')=='import':
            import_form=ImportForm(request.POST,request.FILES)
            if import_form.is_valid():
                data=import_form.cleaned_data.copy()
                f=data.pop('file')
                linked=data.pop('supporting_guidelines')
                try:
                    source,created=import_source(f.read(),f.name,**data)
                    if source.kind=='notes' and linked:
                        source.supporting_guidelines.set(linked)
                    messages.success(request,'Source imported.' if created else 'This exact document is already in the library.')
                    return redirect('source_setup',source_id=source.pk)
                except (ValidationError,ValueError,UnicodeError) as e:
                    import_form.add_error(None,e)
        elif request.POST.get('action')=='generate':
            generate_form=GenerateForm(request.POST)
            if generate_form.is_valid():
                budget,_=ApiBudget.objects.get_or_create(pk=1)
                if not settings.AI_GENERATION_ENABLED or not settings.OPENAI_API_KEY:
                    generate_form.add_error(None,'Generation is not enabled yet. Configure the approved API key and allowance first.')
                elif budget.remaining<=0:
                    generate_form.add_error(None,'The approved API allowance is exhausted.')
                elif GenerationJob.objects.filter(status__in=['queued','running']).count()>=5:
                    generate_form.add_error(None,'Let the existing generation jobs finish before adding more.')
                else:
                    GenerationJob.objects.create(requested_by=request.user,**generate_form.cleaned_data)
                    messages.success(request,'Generation queued. Questions that pass source validation and an independent AI check are published automatically.')
                    return redirect('studio')
    budget,_=ApiBudget.objects.get_or_create(pk=1)
    return render(request,'study/studio.html',{'import_form':import_form,'generate_form':generate_form,
        'jobs':GenerationJob.objects.select_related('chapter').order_by('-created_at')[:15],
        'budget':budget,'enabled':settings.AI_GENERATION_ENABLED and bool(settings.OPENAI_API_KEY),
        'published':Question.objects.filter(status='published').count(),'quarantined':Question.objects.filter(status='quarantined').count(),'nav':'studio'})


@staff_member_required(login_url='/login/')
def source_setup(request,source_id):
    source=get_object_or_404(Source,pk=source_id)
    if request.method=='POST' and request.POST.get('action')=='retire':
        count=retire_source(source,request.user)
        messages.success(request,f'Source retired. {count} questions removed from future practice and exams. Past results are preserved.')
        return redirect('source_setup',source_id=source.pk)
    data=request.POST if request.method=='POST' else None
    chapters=ChapterFormSet(data,instance=source) if source.kind=='guideline' else None
    notes=NotesSourcesForm(data,instance=source) if source.kind=='notes' else None
    form=chapters if chapters is not None else notes
    if request.method=='POST' and form.is_valid():
        form.save()
        messages.success(request,'Source setup saved.')
        return redirect('source_setup',source_id=source.pk)
    generate=GenerateForm()
    eligible=source.chapters.all() if source.kind=='guideline' else Chapter.objects.filter(source__in=source.supporting_guidelines.filter(active=True))
    generate.fields['chapter'].queryset=eligible.filter(source__active=True).select_related('source')
    return render(request,'study/source_setup.html',{'source':source,'chapters':chapters,'notes':notes,
        'generate_form':generate,'can_generate':source.active and eligible.exists(),'nav':'studio'})


def health(request):
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
    return JsonResponse({'status':'ok'})


def manifest(request):
    return JsonResponse({'id':'/','name':'Hjerte · EAPC study','short_name':'Hjerte','start_url':'/',
        'scope':'/','display':'standalone','background_color':'#f3f6fb','theme_color':'#101e36',
        'description':'Your preventive cardiology study companion.','lang':'en',
        'icons':[{'src':f'/static/hjerte/icon-{n}.png','sizes':f'{n}x{n}','type':'image/png','purpose':'any maskable'} for n in (192,512)]},content_type='application/manifest+json')


def service_worker(request):
    response=FileResponse(open(settings.BASE_DIR/'static/hjerte/sw.js','rb'),content_type='application/javascript')
    response['Service-Worker-Allowed']='/'
    response['Cache-Control']='no-cache'
    return response


def offline(request):
    return render(request,'study/offline.html')
