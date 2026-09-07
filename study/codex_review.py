"""Staff-only, resumable import of an independently recorded Codex review.

No model call is made here. The trusted operator supplies the actual reviewer
session identifier and its saved outputs; this is not model attestation.
"""
import hashlib
import json
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import GenerationJob, Question
from .question_import import run_imported, AuthoredReview
from .question_checks import dump, checked_indices, fingerprint
from .pdf_images import metadata


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


class Captured(Exception):
    pass


def guard(job, user):
    if not user.is_staff or job.requested_by_id != user.pk:
        raise ValidationError('Only the importing administrator can manage this review.')
    if job.audit.get('question_pipeline') != 'authored-1' or not job.audit.get('authored_outside_api'):
        raise ValidationError('Choose an independently authored draft batch.')
    if job.status != 'failed' or job.calls.exists():
        raise ValidationError('Choose stopped drafts with no API calls. Existing paid checks are preserved.')
    if GenerationJob.objects.filter(status__in=['running', 'queued']).exists():
        raise ValidationError('Wait for active work before preparing or importing an external review.')


def captured_payload(job, body, images):
    questions = Question.objects.filter(verification__provenance__job_id=str(job.pk), status='quarantined',
        verification__state__in=['awaiting_independent_review', 'checked_pending_publication']).order_by('created_at', 'pk')
    return {'body': json.loads(body), 'images': metadata(images),
            'fingerprints': [{'id': str(q.pk), 'hash': fingerprint(q),
                'objective': q.verification.get('proposed_objective')} for q in questions]}


@transaction.atomic
def export_review(job_id, user, author_session):
    if not author_session.strip():
        raise ValidationError('Record the actual author session identifier.')
    job = GenerationJob.objects.select_for_update().get(pk=job_id)
    guard(job, user)
    output = {}
    def capture(_job, _model, _purpose, _instructions, body, _schema, _limit, **kwargs):
        payload = captured_payload(job, body, kwargs.get('images', []))
        key = digest(payload)
        existing = job.audit.get('codex_review', {})
        if existing.get('snapshot_sha256') == key and existing.get('author_session') != author_session:
            raise ValidationError('The recorded author session cannot be relabelled.')
        job.audit = {**job.audit, 'codex_review': {'snapshot_sha256': key,
            'author_session': author_session, 'prepared_at': timezone.now().isoformat()}}
        job.message = 'Saved for a separate Codex source check; no API request queued.'
        job.save(update_fields=['audit', 'message'])
        b = payload['body']
        blind = {'source': b['source'], 'questions': [
            {'index': q['index'], 'stem': q['stem'], 'choices': [c['text'] for c in q['choices']],
             'references': q['references']} for q in b['questions']]}
        output.update(version=1, job_id=str(job.pk), snapshot_sha256=key, blind=blind,
            rationale=b, images=kwargs.get('images', []))
        raise Captured()
    try:
        run_imported(job, capture)
    except Captured:
        pass
    return output


@transaction.atomic
def apply_review(job_id, user, document):
    job = GenerationJob.objects.select_for_update().get(pk=job_id)
    if not user.is_staff or job.requested_by_id != user.pk:
        raise ValidationError('Only the importing administrator can apply the recorded review.')
    result_hash = digest(document)
    saved = job.audit.get('codex_review', {})
    if job.status == 'complete' and saved.get('result_sha256') == result_hash:
        return job, False
    guard(job, user)
    if (document.get('version') != 1 or document.get('job_id') != str(job.pk) or
            not saved.get('snapshot_sha256') or document.get('snapshot_sha256') != saved['snapshot_sha256']):
        raise ValidationError('The review does not identify this exported draft snapshot.')
    reviewer = document.get('reviewer_session', '').strip()
    if not reviewer or reviewer == saved['author_session']:
        raise ValidationError('Record a separate reviewer session, not the author session.')
    review = AuthoredReview.model_validate({'verdicts': document.get('verdicts')})
    count = len(review.verdicts)
    verdicts = checked_indices(review, count)
    blind = document.get('blind_answers', [])
    if (len(blind) != count or {r.get('index') for r in blind} != set(range(count)) or
            any(type(r.get('best_answer')) is not int or r['best_answer'] not in range(-1, 5)
                or not r.get('reason', '').strip() for r in blind)):
        raise ValidationError('Supply one saved independent answer and reason per question.')
    for row in blind:
        if verdicts[row['index']].best_answer != row['best_answer']:
            raise ValidationError('Blind answer and final verdict disagree; resolve with a fresh review.')
    def recorded(_job, _model, _purpose, _instructions, body, _schema, _limit, **kwargs):
        if digest(captured_payload(job, body, kwargs.get('images', []))) != saved['snapshot_sha256']:
            raise ValidationError('Drafts, sources, question bank or objectives changed; export and review again.')
        return review
    job.reviewer_model = 'Codex independent review'
    run_imported(job, recorded)
    provenance = {**saved, 'reviewer_session': reviewer, 'result_sha256': result_hash,
                  'completed_at': timezone.now().isoformat(), 'recorded_result': document}
    job.audit = {**job.audit, 'codex_review': provenance}
    job.status = 'complete'; job.finished_at = timezone.now()
    job.message = f'{job.published} published; {job.quarantined} held. Separate Codex check; no API use.'
    job.save(update_fields=['audit', 'status', 'finished_at', 'message', 'reviewer_model'])
    for q in Question.objects.filter(verification__provenance__job_id=str(job.pk)):
        q.verification = {**q.verification, 'codex_review': {'snapshot_sha256': saved['snapshot_sha256'],
            'result_sha256': result_hash, 'author_session': saved['author_session'], 'reviewer_session': reviewer}}
        q.save(update_fields=['verification'])
    return job, True
