import math
import random
from collections import Counter, defaultdict
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from .models import Domain, Question, QuestionProgress, SessionItem, StudySession
from .sources import reference_snapshot

RNG = random.SystemRandom()
PART_SIZE = 70
PART_SECONDS = 90 * 60
BREAK_SECONDS = 10 * 60


def available_questions():
    return Question.objects.filter(status='published', chapter__source__active=True).select_related('topic__domain', 'chapter__source')


def question_snapshot(q):
    return {'stem':q.stem, 'choices':q.choices, 'answer':q.answer, 'explanation':q.explanation,
            'learning_point':q.learning_point, 'references':reference_snapshot(q.references),
            'topic_id':q.topic_id, 'topic':q.topic.title, 'domain':q.topic.domain.title,
            'chapter':q.chapter.title, 'version':q.version, 'difficulty':q.difficulty}


def coverage_summary():
    counts = dict(available_questions().values_list('topic__domain_id').annotate(n=Count('id')))
    return [{'name':d.title, 'count':counts.get(d.id, 0)} for d in Domain.objects.all()]


def balanced_selection(questions, count):
    groups = defaultdict(list)
    for q in questions:
        groups[q.topic.domain_id].append(q)
    for group in groups.values():
        RNG.shuffle(group)
    chosen = []
    while len(chosen) < count and any(groups.values()):
        keys = list(groups)
        RNG.shuffle(keys)
        for key in keys:
            if groups[key] and len(chosen) < count:
                chosen.append(groups[key].pop())
    return chosen


def adaptive_selection(user, questions, count, focus):
    progress = {p.question_id:p for p in QuestionProgress.objects.filter(user=user)}
    now = timezone.now()
    topic_scores = defaultdict(lambda: [0,0])
    for q in questions:
        p = progress.get(q.id)
        if p:
            topic_scores[q.topic_id][0] += p.right
            topic_scores[q.topic_id][1] += p.seen
    if focus == 'flagged':
        questions = [q for q in questions if (p:=progress.get(q.id)) and p.flagged]
    elif focus == 'weak':
        questions = [q for q in questions if (p:=progress.get(q.id)) and p.seen and (not p.last_correct or p.last_confidence != 'sure')]
    elif focus == 'new':
        questions = [q for q in questions if not (p:=progress.get(q.id)) or not p.seen]
    ranked = []
    for q in questions:
        p = progress.get(q.id)
        weight = 3.0 if not p or not p.seen else 1.0
        if p:
            weight += 5 * (p.flagged or p.last_correct is False)
            weight += 3 * (p.due_at <= now)
            weight += 2 * (p.last_confidence in ('unsure', 'guessed'))
            if p.due_at > now and p.last_correct and p.last_confidence == 'sure':
                weight *= 0.15
        right, seen = topic_scores[q.topic_id]
        if seen >= 3:
            weight *= 1 + (1-right/seen)
        ranked.append((-math.log(max(RNG.random(), 1e-10))/weight, q))
    ranked.sort(key=lambda pair:pair[0])
    return [q for _,q in ranked[:count]]


@transaction.atomic
def start_session(user, *, mode='practice', count=10, chapter_id=None, topic_id=None, focus='adaptive'):
    if StudySession.objects.filter(user=user).exclude(status='complete').count() >= 8:
        raise ValidationError('Finish one of your current sessions before starting another.')
    if mode not in ('practice','exam'):
        raise ValidationError('Choose practice or exam.')
    qs = available_questions()
    if mode == 'practice':
        if chapter_id:
            qs = qs.filter(chapter_id=chapter_id)
        if topic_id:
            qs = qs.filter(topic_id=topic_id)
        if not 1 <= count <= 100:
            raise ValidationError('Choose between 1 and 100 practice questions.')
    questions = list(qs)
    coverage = coverage_summary()
    if mode == 'exam':
        if len(questions) < 140:
            raise ValidationError(f'Exam simulation needs 140 distinct published questions. The bank currently has {len(questions)}.')
        questions = balanced_selection(questions, 140)
        title = 'EAPC exam simulation'
    else:
        questions = adaptive_selection(user, questions, count, focus)
        if not questions:
            raise ValidationError('No questions match this selection yet.')
        title = questions[0].chapter.title if chapter_id else ('Daily practice' if focus == 'adaptive' else f'{focus.title()} practice')
    now = timezone.now()
    session = StudySession.objects.create(user=user, mode=mode, title=title, started_at=now, part_started_at=now,
        coverage={'domains':coverage, 'complete':all(c['count']>0 for c in coverage),
                  'sampling':'Balanced across available domains; this is not an official exam blueprint.'})
    items = []
    for i,q in enumerate(questions, 1):
        order = list(range(5))
        RNG.shuffle(order)
        items.append(SessionItem(session=session,question=q,position=i,part=1 if mode=='practice' else (i-1)//70+1,
                                 snapshot=question_snapshot(q),choice_order=order))
    SessionItem.objects.bulk_create(items)
    return session


def record_progress(item, now):
    if item.progress_recorded:
        return
    p,_ = QuestionProgress.objects.get_or_create(user=item.session.user,question=item.question)
    item.correct = item.selected == item.snapshot['answer'] if item.selected is not None else False
    item.is_repeat = p.seen > 0
    p.seen += 1
    p.right += int(item.correct)
    p.last_correct = item.correct
    p.last_confidence = item.confidence
    p.last_answered = now
    if not item.correct or item.confidence == 'guessed':
        p.streak = 0
        days = 1
    elif item.confidence == 'unsure':
        p.streak = 0
        days = 3
    else:
        p.streak += 1
        days = [1,3,7,14,30,60][min(p.streak-1,5)]
    p.due_at = now + timedelta(days=days)
    p.save()
    item.progress_recorded = True
    item.save(update_fields=['correct','is_repeat','progress_recorded'])


def complete_session(session, now):
    if session.status == 'complete':
        return
    for item in session.items.select_related('session','question'):
        record_progress(item, now)
    session.score = session.items.filter(correct=True).count()
    session.status = 'complete'
    session.completed_at = now
    session.save()


def end_part_one(session, ended_at):
    session.status = 'break'
    session.break_until = ended_at + timedelta(seconds=BREAK_SECONDS)
    session.cursor = 71
    session.save()


def synchronize_clock(session, now=None):
    now = now or timezone.now()
    if session.mode != 'exam' or session.status == 'complete':
        return
    deadline = session.part_started_at + timedelta(seconds=PART_SECONDS)
    if session.part == 1 and session.status == 'active' and now >= deadline:
        end_part_one(session, deadline)
    if session.status == 'break' and now >= session.break_until:
        session.part = 2
        session.status = 'active'
        session.part_started_at = session.break_until
        session.save()
    deadline = session.part_started_at + timedelta(seconds=PART_SECONDS)
    if session.part == 2 and session.status == 'active' and now >= deadline:
        complete_session(session, deadline)


@transaction.atomic
def save_answer(session_id, user, position, displayed_choice, confidence):
    session = StudySession.objects.get(pk=session_id,user=user)
    now = timezone.now()
    synchronize_clock(session, now)
    if session.status != 'active':
        raise ValidationError('This session is no longer accepting answers. Refresh to continue.')
    item = session.items.get(position=position)
    if session.mode == 'exam' and item.part != session.part:
        raise ValidationError('This exam part is closed.')
    if session.mode == 'practice' and item.answered_at:
        return item  # A repeated submit cannot award points twice or change the result.
    if confidence not in ('sure','unsure','guessed') or displayed_choice not in range(5):
        raise ValidationError('Choose an answer and how confident you are.')
    item.selected = item.choice_order[displayed_choice]
    item.confidence = confidence
    item.answered_at = now
    item.save(update_fields=['selected','confidence','answered_at'])
    if session.mode == 'practice':
        record_progress(item, now)
    session.cursor = min(position+1,session.items.count()) if session.mode=='exam' else position
    session.save(update_fields=['cursor'])
    return item


@transaction.atomic
def finish_session(session_id,user,expected_part=None):
    session = StudySession.objects.get(pk=session_id,user=user)
    now = timezone.now()
    synchronize_clock(session, now)
    if expected_part is not None and session.part!=expected_part:
        raise ValidationError('This form belongs to a previous exam part. Refresh before submitting.')
    if session.status == 'complete':
        return session
    if session.status == 'break':
        return session
    if session.mode == 'exam' and session.part == 1:
        end_part_one(session, now)
    else:
        complete_session(session, now)
    return session


def reference_links(references):
    """One displayed link per source page; retain every distinct section label."""
    groups={}
    for ref in references:
        key=(ref['source_id'],ref['page'])
        if key not in groups:
            groups[key]={**{field:ref.get(field) for field in ('source_id','page','title','year','doi')},
                         'sections':{}}
        section=' '.join(str(ref.get('section') or '').split())
        if section:
            groups[key]['sections'].setdefault(section.casefold(),section)
    return [{**{key:value for key,value in group.items() if key!='sections'},
             'section':'; '.join(group['sections'].values())} for group in groups.values()]


def public_item(item, feedback=False):
    # Explicit allowlist: do not serialize the snapshot before grading is allowed.
    s=item.snapshot
    options=[]
    for displayed,index in enumerate(item.choice_order):
        c=s['choices'][index]
        option={'number':displayed,'letter':'ABCDE'[displayed],'text':c['text'],'selected':index==item.selected}
        if feedback:
            option.update(correct=index==s['answer'],explanation=c['explanation'])
        options.append(option)
    result={'position':item.position,'topic':s['topic'],'stem':s['stem'],'choices':options,
            'confidence':item.confidence,'answered':item.answered_at is not None,'flagged':item.review_flag}
    if feedback:
        result.update(correct=item.correct,explanation=s['explanation'],learning_point=s['learning_point'],
                      references=s['references'],source_links=reference_links(s['references']),is_repeat=item.is_repeat)
    return result
