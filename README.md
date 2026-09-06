# Hjerte

Private English EAPC study app at **hjerte.facab.se**, for Tomas's May 2027 study target.

Practice with immediate explanations, a timed physician exam simulation (2 × 70 questions), confidence ratings, adaptive revision, source references, progress, question editing and a mobile PWA. All progress is stored on the server. PWA study requires internet; only public assets and an offline help page are cached.

## Local development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Fill DJANGO_SECRET_KEY and HJERTE_BOOTSTRAP_PASSWORD; optionally OPENAI_API_KEY.
.venv/bin/python manage.py migrate
.venv/bin/python manage.py bootstrap_hjerte
.venv/bin/python manage.py import_library
.venv/bin/python manage.py runserver 127.0.0.1:8765
```

PDF imports require Poppler (`brew install poppler` on macOS; bundled in the container). Original guidelines and notes stay in `/Users/tomas/Documents/EAPC Certification`. Import copies documents to private storage with SHA-256 deduplication. PDFs use physical page numbers, presentations use slides, and DOCX files use logical sections. Notes in the same folder are linked to their primary guideline automatically.

## Questions and AI budget

The Studio imports documents and queues batches. Add page ranges for a new guideline in the chapter editor before generating. AI uses notes for learning ideas, but all published answers must cite an exact passage in an active guideline. Each candidate receives a blind answer check and a separate explanation check. Cases, direct questions and interpretation questions are included. A model agreement is a quality check, not a guarantee; the editor supports corrections and retirement.

```sh
# Only after explicit user authorization; this sets TOTAL allowance, not a top-up.
.venv/bin/python manage.py set_api_allowance 200 --approval-note 'User-approved pilot'
.venv/bin/python manage.py generation_worker
```

Default allowance is zero. The current authorized pilot is **200 NOK only**. Configure the key and `AI_GENERATION_ENABLED=1` in the active environment. Before every paid request, the app reserves a conservative maximum. Reported token use settles the reservation at configured conversion/tax assumptions. Uncertain calls keep their reservation. No automatic paid retries or expensive tier fallback. Background responses are polled without creating new paid requests. Interrupted jobs remain stopped for inspection; never reset them to queued without checking `ApiCall` and saved question drafts first.

Runtime pricing expires on 2026-11-21 and must be checked again before further generation. Existing practice/exams make no API calls. [Decisions and estimate](docs/DECISIONS.md).

## Verification

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test study.tests
```

Tests cover disclosure of answers, grading after shuffled options, timers, ownership, repeated submissions, immutable snapshots, source validation, notes, PWA cache boundaries and allowance enforcement. No paid API calls are made by tests.

## Deployment and recovery

Dedicated Dokku app `hjerte`, separate persistent data and backup mounts. [VPS runbook](docs/DEPLOY.md). Secrets, original documents, generated questions, personal progress and backups are excluded from Git and image layers.

Source-code repository: https://github.com/tomfor1985/hjerte
