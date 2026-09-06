"""Versioned local page renders for source checks; never edit the original PDF."""
import base64
import hashlib
import io
import subprocess
import tempfile
from pathlib import Path
from PIL import Image
from django.conf import settings
from django.core.exceptions import ValidationError

RENDERER = 'poppler-png-2000-v1'
MAX_IMAGES = 8


def page_image(page):
    source = page.source
    if not source.active or source.kind != 'guideline' or not source.original_name.lower().endswith('.pdf') or not source.file:
        raise ValidationError('Visual evidence requires an active original guideline PDF.')
    path = Path(source.file.path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != source.sha256:
        raise ValidationError('The original PDF no longer matches its source hash.')
    destination = Path(settings.MEDIA_ROOT) / 'page-images' / source.sha256 / f'{page.number}-{RENDERER}.png'
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as directory:
            stem = Path(directory) / 'page'
            subprocess.run(['pdftoppm', '-f', str(page.number), '-l', str(page.number), '-singlefile',
                            '-scale-to', '2000', '-png', str(path), str(stem)],
                           check=True, capture_output=True, timeout=60)
            data = stem.with_suffix('.png').read_bytes()
            temporary = destination.with_suffix('.tmp')
            temporary.write_bytes(data)
            temporary.replace(destination)
    data = destination.read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if image.format != 'PNG' or min(width, height) < 1 or max(width, height) > 2000:
            raise ValidationError('Unexpected source page image dimensions.')
        image.verify()
    return {'page_id': page.pk, 'pdf_page': page.number, 'source_sha256': source.sha256,
            'image_sha256': hashlib.sha256(data).hexdigest(), 'renderer': RENDERER,
            'width': width, 'height': height, 'data_url': 'data:image/png;base64,' + base64.b64encode(data).decode()}


def metadata(images):
    return [{k: v for k, v in image.items() if k != 'data_url'} for image in images]


def validate_images(images):
    from .models import SourcePage
    for image in images:
        page = SourcePage.objects.select_related('source').get(pk=image['page_id'])
        current = page_image(page)
        if metadata([current])[0] != {k: v for k, v in image.items() if k != 'data_url'} or ('data_url' in image and image['data_url'] != current['data_url']):
            raise ValidationError('Visual source evidence changed during processing.')


def reference_images(references):
    from .models import SourcePage
    requested = {}
    for ref in references:
        for item in ref.get('visual_evidence', []):
            previous = requested.get(item['page_id'])
            if previous is not None and previous != item:
                raise ValidationError('Conflicting visual evidence versions.')
            requested[item['page_id']] = item
    if len(requested) > MAX_IMAGES:
        raise ValidationError('Too many figure pages for one bounded request.')
    images = [page_image(SourcePage.objects.select_related('source').get(pk=pk)) for pk in sorted(requested)]
    if {i['page_id']: i for i in metadata(images)} != requested:
        raise ValidationError('Saved visual evidence no longer matches the original PDF.')
    return images
