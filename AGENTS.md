# Hjerte

Private EAPC study application for Tomas, hosted on his own VPS at hjerte.facab.se.
Source library: /Users/tomas/Documents/EAPC Certification. Never modify originals.

- Keep code here; do not register or publish this application with OpenAI Sites.
- All learner-facing content is English. Target study month: May 2027; no exact exam date is confirmed.
- Five MCQ options, one best answer. Exam: two 70-question, 90-minute parts and a 10-minute break.
- Keep answers and explanations on the server until practice submission or complete exam submission. Do not cache private pages or exam content in the service worker.
- Persist immutable question snapshots in attempts; editing a question must not rewrite historical scores.
- Source references and a separate AI verification are mandatory for automatically published questions. Failed verification goes to quarantine; no routine human approval is required.
- API spending requires the user's approved budget. Default allowance is zero. Keep secrets out of code and logs; never reuse another application's API key without authorization.
- Use Django migrations, source-file hashes, authenticated file access, and idempotent import. Keep generated content out of schema migrations.
- Required checks: Django system checks, migration drift check, and the study test suite. Test grading, ownership, answer disclosure, timers, imports, and budget enforcement.
- Deploy only to the requested VPS application. Do not alter other apps or wildcard DNS. No GitHub publishing unless requested.
