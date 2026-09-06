"""Deterministic PDF reading views with exact, positioned citation passages."""
from collections import Counter
import hashlib
import math
import re
import subprocess
from pathlib import Path
from lxml import etree
from django.core.exceptions import ValidationError
from django.db import transaction
from .models import PageReading

EXTRACTOR = 'poppler-bbox-blocks-v1'
PASSAGE_SIZE = 800
NS = {'h': 'http://www.w3.org/1999/xhtml'}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def reading_for(page):
    try:
        reading = page.reading
    except PageReading.DoesNotExist:
        return None
    if (reading.source_sha256 == page.source.sha256 and reading.layout_sha256 == digest(page.text)
            and reading.text_sha256 == digest(reading.text)):
        return reading
    return None


def page_text(page):
    reading = reading_for(page)
    return reading.text if reading else page.text


def _box(node):
    box = [float(node.get(k)) for k in ('xMin', 'yMin', 'xMax', 'yMax')]
    if not all(math.isfinite(n) for n in box) or box[2] < box[0] or box[3] < box[1]:
        raise ValidationError('Invalid PDF text coordinates.')
    return [round(n, 3) for n in box]


def _order(blocks, width):
    """Read page columns separately, split by full-width headings/figure blocks.

    Coordinates remain attached: ordering never claims that separate table cells
    form a sentence or that a figure's arrows have been interpreted.
    """
    middle = width / 2
    left = [b for b in blocks if b['bbox'][2] <= middle]
    right = [b for b in blocks if b['bbox'][0] >= middle]
    if min(sum(len(b['text']) for b in side) for side in (left, right)) < 100:
        return sorted(blocks, key=lambda b: (b['bbox'][1], b['bbox'][0]))
    spanning = sorted([b for b in blocks if b not in left and b not in right], key=lambda b: b['bbox'][1])
    result, pending = [], left + right
    for divider in spanning:
        above = [b for b in pending if b['bbox'][3] <= divider['bbox'][1]]
        result.extend(sorted(above, key=lambda b: (b['bbox'][0] >= middle, b['bbox'][1], b['bbox'][0])))
        pending = [b for b in pending if b not in above]
        result.append(divider)
    result.extend(sorted(pending, key=lambda b: (b['bbox'][0] >= middle, b['bbox'][1], b['bbox'][0])))
    return result


def parse_readings(xml):
    # Some embedded PDF fonts yield XML-forbidden control glyphs. Preserve an
    # explicit unknown-character marker rather than dropping or guessing them.
    xml=re.sub(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]', '\ufffd'.encode(), xml)
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(xml, parser)
    result = []
    for page in root.findall('.//h:page', NS):
        blocks = []
        for index, block in enumerate(page.findall('.//h:block', NS)):
            lines = [' '.join(line.xpath('./h:word/text()', namespaces=NS))
                     for line in block.findall('h:line', NS)]
            text = '\n'.join(lines).replace('\x00', '').replace('\u00ad', '')
            if text.strip():
                blocks.append({'block': index, 'bbox': _box(block), 'text': text})
        # Every word from every column/cell remains in the view, including small
        # labels and footnotes. No model, OCR guesses or semantic joins are used.
        all_words = ''.join(page.xpath('.//h:word/text()', namespaces=NS)).replace('\u00ad', '')
        characters = lambda text: Counter(c for c in text if not c.isspace())
        if characters(all_words) != characters(''.join(b['text'] for b in blocks)):
            raise ValidationError('PDF reading order lost or duplicated extracted characters.')
        text, passages = '', []
        for block in _order(blocks, float(page.get('width'))):
            remaining = block['text']
            while remaining:
                end = len(remaining)
                if end > PASSAGE_SIZE:
                    end = remaining.rfind('\n', 0, PASSAGE_SIZE + 1)
                    if end < PASSAGE_SIZE // 2:
                        end = remaining.rfind(' ', 0, PASSAGE_SIZE + 1)
                    if end <= 0:
                        end = PASSAGE_SIZE
                chunk, remaining = remaining[:end], remaining[end:]
                if text:
                    text += '\n\n'
                start = len(text)
                text += chunk
                passages.append({'start': start, 'end': len(text), 'bbox': block['bbox'], 'block': block['block'],
                                 'unreadable_glyphs':chunk.count('\ufffd')})
        result.append({'text': text, 'passages': passages})
    if not result or sum(len(p['text']) for p in result) > 3_000_000:
        raise ValidationError('PDF reading view is empty or exceeds the import limit.')
    return result


def prepare_source_reading(source):
    """Free, idempotent preprocessing. Does not modify original text or old audits."""
    if Path(source.original_name).suffix.lower() != '.pdf' or not source.file:
        return 0
    with source.file.open('rb') as handle:
        data = handle.read(50 * 1024 * 1024 + 1)
    if len(data) > 50 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != source.sha256:
        raise ValidationError('The PDF file no longer matches its recorded source hash.')
    pages = list(source.pages.select_related('source', 'reading').order_by('number'))
    if all((r := reading_for(p)) and r.extractor == EXTRACTOR for p in pages) and pages:
        return 0
    # Never replace a persisted view which may already be referenced. New
    # extractors require a deliberate migration/version, not an implicit refresh.
    if PageReading.objects.filter(page__source=source).exists():
        raise ValidationError('The saved PDF reading view is stale. Review its source version before rebuilding.')
    extracted = subprocess.run(['pdftotext', '-bbox-layout', '-', '-'], input=data,
                               capture_output=True, timeout=90, check=True)
    views = parse_readings(extracted.stdout)
    if len(views) != source.page_count or [p.number for p in pages] != list(range(1, len(views) + 1)):
        raise ValidationError('PDF reading views do not match the stored page numbers.')
    with transaction.atomic():
        if PageReading.objects.filter(page__source=source).exists():
            raise ValidationError('The PDF reading view changed during extraction. Reload before continuing.')
        PageReading.objects.bulk_create([PageReading(page=p, source_sha256=source.sha256,
            layout_sha256=digest(p.text), text_sha256=digest(view['text']), extractor=EXTRACTOR,
            text=view['text'], passages=view['passages']) for p, view in zip(pages, views, strict=True)])
    return len(views)


def evidence_part(page, start, end, *, part_id=None):
    reading = reading_for(page)
    text = reading.text if reading else page.text
    spans = reading.passages if reading else [{'start': s, 'end': min(s + PASSAGE_SIZE, len(text))}
                                              for s in range(0, len(text), PASSAGE_SIZE)]
    passages = []
    version = digest(text)
    for span in spans:
        first, last = max(start, span['start']), min(end, span['end'])
        if first >= last or not text[first:last].strip():
            continue
        passages.append({'id': f'{page.pk}:{first}', 'start': first, 'end': last,
                         'text': text[first:last], **({'bbox': span['bbox'], 'block': span['block'],
                         'unreadable_glyphs':span.get('unreadable_glyphs',0)} if reading else {})})
    return {'page_id': page.pk, 'page': page.number, 'start': start, 'end': end,
            'reading_sha256': version if reading else None, 'text': text[start:end],
            'passages': passages, **({'part_id': part_id} if part_id is not None else {})}


def prompt_part(part):
    # One copy of the text and one coordinate row per block. Full hashes and
    # offsets stay in the saved context; the model selects short IDs only.
    blocks={p['block']:p['bbox'] for p in part['passages'] if 'block' in p}
    return {key:part[key] for key in ('part_id','page_id','page') if key in part} | {
        'passage_fields':['id','block','text'], 'block_fields':['block','left','top','right','bottom'],
        'blocks':[[key,*box] for key,box in blocks.items()],
        'passages':[[p['id'],p.get('block'),p['text']] for p in part['passages']]}
