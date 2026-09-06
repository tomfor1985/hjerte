"""Small, exact evidence packets from previously source-checked objectives."""
from django.core.exceptions import ValidationError
from .pdf_reading import evidence_part, page_text, prompt_part
from .pdf_images import metadata
from .sources import validate_references


def validate_packet(chapter, context):
    from .models import Source
    source=Source.objects.get(pk=chapter.source_id)
    if not source.active or source.sha256!=context['source_sha256']:
        raise ValidationError('The source version changed during question verification.')
    for saved in context['pages']:
        page=source.pages.select_related('source','reading').get(pk=saved['page_id'])
        if not chapter.first_page<=page.number<=chapter.last_page:
            raise ValidationError('The question evidence is outside this chapter.')
        current=prompt_part(evidence_part(page,0,len(page_text(page))))
        rows={p[0]:p for p in current['passages']}
        boxes={b[0]:b for b in current['blocks']}
        if (current['page']!=saved['page'] or any(rows.get(p[0])!=p for p in saved['passages']) or
                any(boxes.get(b[0])!=b for b in saved['blocks'])):
            raise ValidationError('The quoted passage or its surrounding source context changed.')


def evidence_packet(chapter, references, images, *, full=False):
    validate_references(references)
    required={r['page_id'] for r in references}|{i['page_id'] for i in images}
    pages=list(chapter.source.pages.select_related('source','reading').filter(
        pk__in=required,number__gte=chapter.first_page,number__lte=chapter.last_page).order_by('number'))
    if not pages or {p.pk for p in pages}!=required or sum(len(page_text(p)) for p in pages)>70000:
        raise ValidationError('The objective evidence is missing or exceeds the bounded source packet.')
    parts=[]
    image_ids={i['page_id'] for i in images}
    total=0
    for page in pages:
        part=evidence_part(page,0,len(page_text(page)))
        total+=len(part['text'])
        if not full and page.pk not in image_ids:
            spans=part['passages']; selected=set()
            for ref in (r for r in references if r['page_id']==page.pk):
                start=ref.get('passage_start')
                end=ref.get('passage_end')
                if start is None or end is None:
                    start=part['text'].find(ref['quote'])
                    end=start+len(ref['quote'])
                # Legacy normalized quotations may not locate exactly. Keep the
                # full page rather than guessing which contextual block supports it.
                if start<0:
                    selected.update(range(len(spans)));continue
                cited=[i for i,p in enumerate(spans) if p['start']<end and p['end']>start]
                blocks={spans[i].get('block') for i in cited if 'block' in spans[i]}
                cited += [i for i,p in enumerate(spans) if p.get('block') in blocks]
                for i in cited:
                    selected.update(range(max(0,i-2),min(len(spans),i+3)))
            # Preserve page headings as well as neighbouring qualifiers/footnotes.
            selected.update(range(min(2,len(spans))))
            part['passages']=[p for i,p in enumerate(spans) if i in selected]
            part['text']='\n\n'.join(p['text'] for p in part['passages'])
        parts.append(part)
    context={'guideline':chapter.source.title,'year':chapter.source.year,'doi':chapter.source.doi,
        'chapter':chapter.title,'source_sha256':chapter.source.sha256,'excerpted':not full,
        'pages':[prompt_part(p) for p in parts]}
    if images:context['original_page_images']=metadata(images)
    stats={'full_text_characters':total,'sent_text_characters':sum(len(p['text']) for p in parts),
           'image_pages':[i['pdf_page'] for i in images]}
    return context,parts,stats
