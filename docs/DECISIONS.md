# Agreed scope

Code: /Users/tomas/Documents/Codex/EAPC/Code. Guidelines and notes: /Users/tomas/Documents/EAPC Certification.
Deployment: user's existing Dokku VPS; dedicated app hjerte and hostname hjerte.facab.se.
User tomas is learner and administrator. English interface, questions, options, explanations.
Study target: May 2027 (month only, not a confirmed examination date).

Practice by source/chapter/topic, adaptive daily sessions, configurable practice length, instant feedback after submission, explanations for every option and precise source references. Confidence: sure/unsure/guessed. Manual flags and automatic revisit scheduling.
Exam simulation: 2 x 70 unique questions, 90 minutes each, 10-minute intermission, no negative marks. Results only after the whole exam ends. Keep balanced domain sampling separate from adaptive practice. Clearly state incomplete curriculum coverage; never invent an official domain weighting or pass mark.
Progress: recent history, per-topic weaknesses, first encounters separated from repeat attempts. Persist answers and attempts so mobile/desktop can resume. PWA installation and responsive layout. Online study initially; cache only public application assets and an offline explanation page.

Admin can import PDF/DOCX/PPTX/TXT/MD sources, add/rename chapters, generate questions, edit/retire questions, and inspect costs. Guidelines are authoritative; notes support teaching but cannot override them. AI generation automatically publishes only after structural and source-reference validation and a separate blind answer check. Failed items remain quarantined for optional correction.

# API approval

2026-09-06: User approved ONLY 200 NOK for the pilot, then review measured yield before any further spend. The proposed 1,000 NOK initial allowance is NOT approved. No automatic allowance reset or expansion.

Question types must include direct knowledge and interpretation as well as clinical cases, to cover the entire curriculum. Coverage is measured by learning objectives/chapters, not just total question count. Imported sources are only a subset of the EAPC curriculum; show unrepresented domains explicitly and do not claim exam readiness.

Recommended: GPT-6 Astra high reasoning for generation, GPT-5.6 Sol high reasoning for independent verification, using Flex processing. No automatic paid retries or fallback to a more expensive tier. Both model availability and pricing must be checked for the configured account before use.

Official list prices checked 2026-09-06, USD per million tokens, contexts below 272K:

| Model | Standard input/output | Flex input/output |
| --- | --- | --- |
| GPT-6 Astra | 10 / 50 | 5 / 25 |
| GPT-5.6 Sol | 4 / 20 | 2 / 10 |

Source: https://developers.openai.com/api/docs/pricing
Sol promotional pricing is documented at least through 2026-11-21. Recheck after this date. Cache writes may cost 1.25x input price. Reasoning tokens are billable output tokens. No cache savings are assumed in the estimate.

Proposed one-time initial allowance: 1,000 NOK, with a first pilot capped at 200 NOK for around 50 questions. Pilot measurement determines the number of accepted questions achievable; do not promise an exact count. Initial planning estimate: roughly 250-600 accepted questions for 1,000 NOK, depending on source length, reasoning, rejected items, and exchange/tax conversion. This is not a recurring monthly authorization.

Illustrative five-question batch: 20K input + 12K output Astra Flex = $0.40; 25K input + 6K output Sol Flex = $0.11; total $0.51, or $0.102/question before rejected questions and cache-write uplift. At an intentionally conservative planning conversion of 12 NOK/USD and 25% tax reserve, this is about 1.53 NOK/question before rejected questions. The conversion and tax reserve are planning assumptions, not current exchange-rate/tax claims. Wider token use can materially raise this.

The application reserves a conservative maximum cost before each request and tracks actual token usage. Uncertain request outcomes retain their reservation. The allowance starts at zero and generation remains disabled until a budget and API credential are configured. Existing quizzes incur no API cost.

# Sources

Exam format checked 2026-09-06: https://www.escardio.org/education/career-development/certification-programmes/preventive-cardiology/
140 questions applies to physicians completing both parts. Five options, single best answer, 90 minutes per part, ten-minute break. No fixed official passing percentage is stated.
