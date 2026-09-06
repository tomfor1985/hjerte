"""Explicit format-copy links; no clinical text is silently merged by similarity."""
from pathlib import Path
import unicodedata
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import Source, GenerationJob


def document_name(source):
    return unicodedata.normalize('NFKC',Path(source.original_name).stem).casefold().strip()


def format_pairs(source):
    """Suggest PDF/other-format pairs with matching names, never merge by title alone."""
    candidates=Source.objects.for_study().filter(kind=source.kind,year=source.year).exclude(pk=source.pk)
    source_is_pdf=source.original_name.lower().endswith('.pdf')
    pairs=[]
    if source.duplicate_of_id or not source.active:
        return pairs
    for other in candidates:
        other_is_pdf=other.original_name.lower().endswith('.pdf')
        if source_is_pdf==other_is_pdf or document_name(other)!=document_name(source):
            continue
        if source.doi and other.doi and source.doi.casefold()!=other.doi.casefold():
            continue
        pairs.append({'main':source if source_is_pdf else other,'copy':other if source_is_pdf else source})
    return pairs


@transaction.atomic
def prefer_pdf(copy, main, user):
    copy=Source.objects.get(pk=copy.pk)
    main=Source.objects.get(pk=main.pk)
    if copy.duplicate_of_id==main.pk:
        return False
    if not any(p['main'].pk==main.pk and p['copy'].pk==copy.pk for p in format_pairs(copy)):
        raise ValidationError('Choose a PDF and its matching alternate-format document. Different names or editions are kept separately.')
    supporting_ids=Source.objects.filter(study_notes__pk__in=[copy.pk,main.pk]).values('pk')
    if GenerationJob.objects.filter(status__in=['queued','running']).filter(
            Q(chapter__source_id__in=[copy.pk,main.pk])|Q(notes_source_id__in=[copy.pk,main.pk])|
            Q(kind='questions',notes_source__isnull=True,chapter__source_id__in=supporting_ids)).exists():
        raise ValidationError('Let the mapping or generation jobs for these documents finish before linking copies.')
    copy.duplicate_of=main
    entry=f'PDF {main.pk} selected as the main copy by {user.username} on {timezone.now().isoformat()}. Original file and historical provenance retained.'
    copy.duplicate_note='\n'.join(filter(None,[copy.duplicate_note,entry]))
    copy.full_clean(exclude=['file'])
    copy.save(update_fields=['duplicate_of','duplicate_note'])
    main.supporting_guidelines.add(*copy.supporting_guidelines.all())
    # Do not retire sources/questions, rewrite references, move source pages or
    # pretend that Word section numbers are PDF page numbers.
    return True


@transaction.atomic
def restore_separate_copy(source,user):
    source=Source.objects.get(pk=source.pk)
    if source.duplicate_of_id:
        source.duplicate_of=None
        source.duplicate_note+=f'\nRestored as a separate source by {user.username} on {timezone.now().isoformat()}.'
        source.save(update_fields=['duplicate_of','duplicate_note'])
