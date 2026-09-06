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

The Studio imports documents, suggests chapters from recognised sections or PDF bookmarks, and queues batches. When bookmarks are unavailable, editable page groups cover the full document. Choose a topic on import and adjust the suggestions if needed. For notes, link supporting guidelines and optionally focus generation on that specific file. Mapped notes contribute source-checked learning objectives; question generation reuses that inventory instead of resending raw AI notes. All published answers must cite an exact passage in an active guideline. Each candidate receives one combined independent answer, explanation, source and novelty check. The author’s answer index is omitted, but explanations are visible; this is not described as a blind test. A bounded additional check uses full source pages only when omitted context might resolve uncertainty. Generation provenance retains guideline identity, version, source hash, job and supplied notes. Retiring a source removes related questions from new sessions while preserving historical attempts. Cases, direct questions and interpretation questions are included. A model agreement is a quality check, not a guarantee; the editor supports corrections and retirement.

```sh
# Only after explicit user authorization; this sets TOTAL allowance, not a top-up.
.venv/bin/python manage.py set_api_allowance 200 --approval-note 'User-approved pilot'
.venv/bin/python manage.py generation_worker
```

Default allowance is zero. The current authorized pilot is **200 NOK only**. Configure the key and `AI_GENERATION_ENABLED=1` in the active environment. Before every paid request, the app reserves a conservative maximum. Reported token use settles the reservation at configured conversion/tax assumptions. Uncertain calls keep their reservation. Source inventory uses Sol and up to two automatic repair rounds, escalating difficult points to Astra within the same job cap. No uncertain API call is automatically retried and the service tier never silently upgrades. Background responses are polled without creating new paid requests. Interrupted jobs remain stopped for inspection; never reset them to queued without checking `ApiCall` and saved question drafts first.

Retrying a partial inventory can reuse its completed source check and start directly with the recorded gaps. The check, source text, original image versions and saved objectives must still match; edited or unmatched checkpoints get a fresh check. Figure gaps go directly to Astra. Additions are independently checked before use, and a budget stop preserves already verified points. Older single-batch audits require an explicit `reuse_review_from` job reference and the same provenance checks before adoption.

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

Question writing and its normal check are reserved together before either generation call starts. If the complete pair cannot fit, the group is reduced before purchase. An additional full-context check has its own reservation; a lack of funds cannot turn a negative verdict positive. Only unsent planned reservations can be released. Paid results are saved immediately with an exact request fingerprint, separate from publication. A stopped job’s **Finish saved questions** action checks its existing unfinished drafts within its original cap; it does not regenerate them or retry uncertain API calls. Old review records remain unchanged.

Question evidence uses complete cited PDF blocks, neighbouring passages and headings, retaining exact passage IDs and original image versions. Image-dependent pages keep their full text. Any changed source context, objective evidence or question content prevents stale results from publishing. Packet sizes are recorded for measurement; lower request volume alone is not evidence that model accuracy is unchanged.
