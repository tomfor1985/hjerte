import hashlib
import uuid
from pathlib import Path
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone


class Domain(models.Model):
    key = models.SlugField(unique=True)
    title = models.CharField(max_length=160)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order', 'title']

    def __str__(self):
        return self.title


class Topic(models.Model):
    domain = models.ForeignKey(Domain, on_delete=models.PROTECT, related_name='topics')
    title = models.CharField(max_length=160)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ['domain__order', 'title']

    def __str__(self):
        return self.title


class SourceQuerySet(models.QuerySet):
    def for_study(self):
        return self.filter(active=True, duplicate_of__isnull=True)


class Source(models.Model):
    KIND = [('guideline', 'Guideline'), ('notes', 'Study notes')]
    title = models.CharField(max_length=300)
    kind = models.CharField(max_length=20, choices=KIND, default='guideline')
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    doi = models.CharField(max_length=160, blank=True)
    url = models.URLField(blank=True)
    sha256 = models.CharField(max_length=64, unique=True)
    original_name = models.CharField(max_length=300)
    file = models.FileField(upload_to='%Y/%m')
    page_count = models.PositiveIntegerField(default=0)
    imported_at = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True)
    duplicate_of = models.ForeignKey('self',on_delete=models.PROTECT,null=True,blank=True,related_name='format_copies')
    duplicate_note = models.TextField(blank=True)
    extraction_warning = models.TextField(blank=True)
    supporting_guidelines = models.ManyToManyField('self',symmetrical=False,blank=True,related_name='study_notes')
    objects = SourceQuerySet.as_manager()

    def clean(self):
        if self.duplicate_of_id:
            if self.duplicate_of_id==self.pk:
                raise ValidationError('A source cannot be its own main copy.')
            target=self.duplicate_of
            if target.duplicate_of_id or target.kind!=self.kind or not target.active:
                raise ValidationError('Choose an active main copy of the same source kind.')
            if self.pk and self.format_copies.exists():
                raise ValidationError('This source already has format copies. Resolve those links first.')

    def __str__(self):
        return f'{self.title} · {Path(self.original_name).suffix.lstrip(".").upper()}' if Path(self.original_name).suffix else self.title


class SourcePage(models.Model):
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name='pages')
    number = models.PositiveIntegerField()
    text = models.TextField()

    class Meta:
        ordering = ['number']
        constraints = [models.UniqueConstraint(fields=['source', 'number'], name='unique_source_page')]

    def __str__(self):
        return f'{self.source.title} — page {self.number}'


class PageReading(models.Model):
    """Versioned PDF reading order; the original SourcePage.text is untouched."""
    page = models.OneToOneField(SourcePage, on_delete=models.CASCADE, related_name='reading')
    source_sha256 = models.CharField(max_length=64)
    layout_sha256 = models.CharField(max_length=64)
    text_sha256 = models.CharField(max_length=64)
    extractor = models.CharField(max_length=80)
    text = models.TextField()
    passages = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)


class Chapter(models.Model):
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name='chapters')
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name='chapters')
    title = models.CharField(max_length=200)
    section = models.CharField(max_length=120, blank=True)
    first_page = models.PositiveIntegerField(default=1)
    last_page = models.PositiveIntegerField(default=1)

    def clean(self):
        if self.first_page < 1 or self.last_page < self.first_page:
            raise ValidationError('Use a valid inclusive PDF page range.')
        if self.source_id and self.last_page > self.source.page_count:
            raise ValidationError('Page range exceeds the imported document.')

    def __str__(self):
        return f'{self.source.year or ""} · {self.title}'


class LearningObjective(models.Model):
    title = models.CharField(max_length=400)
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT)
    variant_limit = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(3)])
    depth_reason = models.TextField(blank=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    blocked_reason = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    reconciliation_status = models.CharField(max_length=16,default='complete',choices=[('pending','Awaiting matching'),('complete','Matched'),('blocked','Needs review')])
    reconciliation_audit = models.JSONField(default=dict,blank=True)
    merged_into = models.ForeignKey('self',null=True,blank=True,on_delete=models.PROTECT,related_name='aliases')

    def clean(self):
        if self.variant_limit > 1 and not self.depth_reason.strip():
            raise ValidationError('Explain what additional testing angles are useful before allowing variants.')

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(variant_limit__gte=1, variant_limit__lte=3), name='objective_max_three')]

    def __str__(self):
        return self.title


class ObjectiveEvidence(models.Model):
    objective = models.ForeignKey(LearningObjective, on_delete=models.CASCADE, related_name='evidence')
    chapter = models.ForeignKey(Chapter, on_delete=models.CASCADE, related_name='objective_evidence')
    references = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['objective', 'chapter'], name='objective_chapter_evidence')]


class CoverageSegment(models.Model):
    page = models.ForeignKey(SourcePage, on_delete=models.CASCADE, related_name='coverage_segments')
    start = models.PositiveIntegerField()
    end = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default='pending', choices=[('pending', 'Not mapped'), ('mapped', 'Mapped'), ('blocked', 'Needs review')])
    audit = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['page', 'start', 'digest'], name='unique_coverage_segment')]


class Question(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chapter = models.ForeignKey(Chapter, on_delete=models.PROTECT, related_name='questions')
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name='questions')
    stem = models.TextField()
    choices = models.JSONField(help_text='Exactly five objects with text and explanation.')
    answer = models.PositiveSmallIntegerField(help_text='Correct option index, 0 to 4.')
    explanation = models.TextField()
    learning_point = models.TextField()
    objective = models.ForeignKey(LearningObjective, on_delete=models.PROTECT, null=True, blank=True, related_name='questions')
    objective_link_audit = models.JSONField(default=dict,blank=True)
    testing_angle = models.TextField(blank=True)
    references = models.JSONField(default=list, help_text='Page ID, section and supporting quote for each reference.')
    difficulty = models.CharField(max_length=12, choices=[('basic', 'Basic'), ('applied', 'Applied'), ('advanced', 'Advanced')], default='applied')
    question_type = models.CharField(max_length=16, choices=[('case', 'Clinical case'), ('direct', 'Direct knowledge'), ('interpretation', 'Interpretation')], default='case')
    status = models.CharField(max_length=16, choices=[('published', 'Published'), ('quarantined', 'Quarantined'), ('retired', 'Retired')], default='quarantined')
    fingerprint = models.CharField(max_length=64, unique=True, editable=False)
    version = models.PositiveIntegerField(default=1)
    generated_by = models.CharField(max_length=80, blank=True)
    verification = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not isinstance(self.choices, list) or len(self.choices) != 5:
            raise ValidationError('A question must have exactly five options.')
        for c in self.choices:
            if not isinstance(c, dict) or not str(c.get('text', '')).strip() or not str(c.get('explanation', '')).strip():
                raise ValidationError('Every option needs text and an explanation.')
        if len({c['text'].strip().casefold() for c in self.choices}) != 5:
            raise ValidationError('Options must be distinct.')
        if self.answer not in range(5):
            raise ValidationError('Correct option must be 0–4.')
        if self.chapter_id and self.topic_id != self.chapter.topic_id:
            raise ValidationError('Question topic must match its chapter.')
        if self.status == 'published':
            from .sources import validate_references
            validate_references(self.references)

    def save(self, *args, **kwargs):
        self.fingerprint = hashlib.sha256(' '.join(self.stem.casefold().split()).encode()).hexdigest()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.stem[:110]


class StudySession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    mode = models.CharField(max_length=12, choices=[('practice', 'Practice'), ('exam', 'Exam')])
    status = models.CharField(max_length=12, default='active', choices=[('active', 'Active'), ('break', 'Break'), ('complete', 'Complete')])
    title = models.CharField(max_length=200)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    part = models.PositiveSmallIntegerField(default=1)
    part_started_at = models.DateTimeField(default=timezone.now)
    break_until = models.DateTimeField(null=True, blank=True)
    cursor = models.PositiveIntegerField(default=1)
    coverage = models.JSONField(default=dict)
    score = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [models.Index(fields=['user', 'status'])]


class SessionItem(models.Model):
    session = models.ForeignKey(StudySession, on_delete=models.CASCADE, related_name='items')
    question = models.ForeignKey(Question, on_delete=models.PROTECT)
    position = models.PositiveIntegerField()
    part = models.PositiveSmallIntegerField(default=1)
    snapshot = models.JSONField()
    choice_order = models.JSONField()
    selected = models.PositiveSmallIntegerField(null=True, blank=True)
    confidence = models.CharField(max_length=12, choices=[('sure', 'Sure'), ('unsure', 'Unsure'), ('guessed', 'Guessed')], blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    correct = models.BooleanField(null=True, blank=True)
    is_repeat = models.BooleanField(default=False)
    review_flag = models.BooleanField(default=False)
    progress_recorded = models.BooleanField(default=False)

    class Meta:
        ordering = ['position']
        constraints = [models.UniqueConstraint(fields=['session', 'position'], name='unique_session_position'),
                       models.UniqueConstraint(fields=['session', 'question'], name='unique_session_question')]


class QuestionProgress(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    seen = models.PositiveIntegerField(default=0)
    right = models.PositiveIntegerField(default=0)
    streak = models.PositiveIntegerField(default=0)
    last_correct = models.BooleanField(null=True, blank=True)
    last_confidence = models.CharField(max_length=12, blank=True)
    last_answered = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(default=timezone.now)
    flagged = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'question'], name='unique_user_question_progress')]
        indexes = [models.Index(fields=['user', 'due_at'])]


class GenerationJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    chapter = models.ForeignKey(Chapter, on_delete=models.PROTECT)
    notes_source = models.ForeignKey(Source,on_delete=models.PROTECT,null=True,blank=True,related_name='note_generation_jobs')
    count = models.PositiveIntegerField(default=5)
    kind = models.CharField(max_length=16, default='questions', choices=[('questions', 'Questions'), ('map', 'Map learning objectives'), ('reconcile','Match learning objectives'), ('link_questions','Link existing questions')])
    retry_blocked = models.BooleanField(default=False)
    notes_first_location = models.PositiveIntegerField(null=True,blank=True)
    notes_last_location = models.PositiveIntegerField(null=True,blank=True)
    spend_limit_nok = models.DecimalField(max_digits=8,decimal_places=2,null=True,blank=True)
    audit = models.JSONField(default=dict,blank=True)
    strategy = models.CharField(max_length=16, default='coverage', choices=[('coverage', 'Increase coverage'), ('variants', 'Add useful variants')])
    generator_model = models.CharField(max_length=80, blank=True)
    reviewer_model = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=20, default='queued', choices=[('queued', 'Queued'), ('running', 'Running'), ('complete', 'Complete'), ('failed', 'Failed')])
    published = models.PositiveIntegerField(default=0)
    quarantined = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class ApiBudget(models.Model):
    # Single application-wide allowance, explicitly granted; never resets itself.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    allowance_nok = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    accounted_nok = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    nok_per_usd = models.DecimalField(max_digits=7, decimal_places=3, default=Decimal('12'))
    tax_reserve = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal('1.25'))
    approval_note = models.TextField(blank=True)
    price_checked_on = models.DateField(default='2026-09-06')
    price_valid_until = models.DateField(default='2026-11-21')

    @property
    def remaining(self):
        return max(Decimal('0'), self.allowance_nok - self.accounted_nok)


class ApiCall(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(GenerationJob, on_delete=models.PROTECT, related_name='calls')
    model = models.CharField(max_length=80)
    purpose = models.CharField(max_length=30)
    state = models.CharField(max_length=16, default='reserved')
    reserved_nok = models.DecimalField(max_digits=10, decimal_places=4)
    actual_nok = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cached_tokens = models.PositiveIntegerField(null=True,blank=True)
    cache_write_tokens = models.PositiveIntegerField(null=True,blank=True)
    reasoning_tokens = models.PositiveIntegerField(null=True,blank=True)
    service_tier = models.CharField(max_length=16,blank=True)
    provider_response_id = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class LoginThrottle(models.Model):
    key = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    window_started = models.DateTimeField(default=timezone.now)
