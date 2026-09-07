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

Studio shows one recommended next step from saved drafts, active runs and checked learning objectives. Source import and advanced model/notes settings are collapsed until needed. Setup links only prefill forms; API work still requires an explicit POST within a recorded cap.

The Studio cost view separates settled usage-based spend, planned/in-progress reservations and unconfirmed costs, and reconciles their sum against the authoritative allowance ledger without changing it. Categories are mutually exclusive; source preparation is excluded from question-production unit costs. New question runs are tagged separately from earlier/mixed runs so the next pilot does not inherit historical prices. Each recent run shows its spend, cap, unfinished drafts and cost breakdown. Empty or unproductive cohorts show no unit price. Retiring a source or exhausting a run cap blocks saved-draft continuation.

The former full-library density extrapolation is no longer displayed as the cost of adequate coverage. A tiny sample of fine-grained objectives cannot establish how many questions are educationally necessary. Studio separately measures authored-draft review, direct source-to-question API runs, and older workflows. No question count is presented as syllabus completion.

## Lower-cost authoring workflow

Prepare original MCQs in Codex from the original primary sources, retaining the exact edition and passage IDs. Export context with `python manage.py export_question_context --chapter ID --output PATH`. This is local preprocessing, with no API calls. Each JSON bundle follows `AuthoredBundle` in `study/question_import.py` and contains 1–5 questions for one chapter. Codex authoring uses the user's Codex allowance and is excluded from the app's API ledger.

Import with `python manage.py import_questions PATH --cap 4`, or upload the JSON in Studio. Import is atomic, idempotent and free; questions remain quarantined. Only `--queue-review` or **Finish saved questions** starts independent API verification within the original cap and remaining approved allowance. The check receives full cited pages and original PDF images, verifies the answer and every explanation, and matches shared objectives against the saved catalogue. Passing questions acquire source-backed objectives; rejected drafts do not inflate coverage. Existing objective variant ceilings remain enforced. No routine manual clinical approval is required, and uncertainty is not retried automatically.

For later documents, Studio also offers **Source → questions → independent check**. It writes questions and proposed objectives together and funds the normal review before generation. Source pages with fewer existing questions are preferred; the selected page window is partial, not a full inventory. Optional notes suggest ideas only; this bounded flow currently reads up to the first 16,000 characters of the selected note. Notes beyond that window can be prepared in Codex or handled through the advanced coverage planner. Every published answer still requires primary evidence. The old mapped-objective workflow remains available as an explicit advanced choice.

### Resumable Codex-authored and separately reviewed batches

For subscription-based work, keep at most five questions per chapter in each saved bundle. `import_questions` saves drafts without a model call. Export with `codex_review export JOB_ID DIRECTORY --author-session ACTUAL_SESSION_ID`; the export binds the exact answers, wording, sources, PDF image hashes, question bank and objective inventory. It writes a blinded packet, a separate rationale packet and original page images. A separate Codex reviewer first saves answers without seeing the intended answers/explanations, then checks the rationale and novelty. Keep both outputs and the actual reviewer session ID. This operator-recorded provenance is not cryptographic model attestation.

Apply a saved JSON result with `codex_review apply JOB_ID RESULT.json`. The result must contain version=1, job_id, snapshot_sha256, reviewer_session, blind_answers (index, best_answer, reason) and the normal authored-review verdicts. The importer requires a different author/reviewer session, complete consistent blind answers, unchanged evidence and drafts, passing source checks and the existing objective variant ceilings. Identical completed results are idempotent. Source or bank changes require a fresh export and review. A negative result stays unpublished. No API call or allowance change occurs, and these publications are separated from API unit-cost statistics.

A batch assigned to a Codex review cannot be resumed through the API button. Continue through its saved files. The dated run folder under ignored `data/` stores draft files, manifests, reviewer outputs, import receipts and a progress record. Never overwrite completed review evidence or silently retry rejected questions. Changes require a new draft/version and fresh review. This workflow runs through Codex; the website's direct source-generation workflow remains API-backed.
