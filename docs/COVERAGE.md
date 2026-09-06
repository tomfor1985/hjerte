# Coverage and finite question generation

Studio → Coverage inventories the current source editions before requesting additional questions. This is an AI-assisted inventory of the imported documents, not a guarantee of complete EAPC curriculum coverage or learner mastery.

Each extracted page is split into bounded 12,000-character segments. Long pages are never discarded. Missing pages, chapter-range gaps, unreadable text, unreviewed segments, unsupported note claims and unclassified historical questions remain visible. Reading the report is free. Mapping explicitly queues at most 1, 3 or 5 compact batches (short consecutive text segments share API requests); it does not automatically queue questions or continue through the whole library.

The mapper proposes assessable objectives with primary-source quotations, meaningful testing angles, justified exclusions and links to existing questions. A separate reviewer checks completeness, semantic overlap, canonical IDs and existing-question matches. Notes require support from the selected guideline chapter; an unsupported claim blocks that segment instead of disappearing from the inventory. A failed mapping is not automatically retried. Review its audit and API call state before resetting it to pending in administration, with a recorded reason.

Objectives are shared across documents. Evidence retains chapter, source edition and page references; mapping audits retain source hashes and exact proposals/reviews. New editions are separate sources. Retiring an evidence source removes its questions from future sessions and invalidates dependent note inventories. Existing attempt snapshots are never rewritten by classification.

## Generation rules

- The selected chapter must be mapped and its existing published questions classified before generating more.
- `Increase coverage` selects uncovered, active, supported objectives first, one candidate per objective per batch. Re-plan after each batch and again check the ceiling before publication.
- `Add useful variants` is blocked while any mapped, actionable objective remains uncovered. Unmapped material is still visibly excluded from the denominator; mapping the rest of the library should take priority over adding depth.
- The default ceiling is one. Two or three require a recorded useful testing angle. Three is a database-enforced maximum. Reusing an objective from another document does not automatically raise its ceiling.
- Published questions count across all active source documents. Selected notes restrict targets to the objectives verified from those notes.
- Both generator and rationale reviewer compare the entire existing bank, including quarantined drafts, plus the current batch. Structural near-duplicates are screened locally across documents and within the batch. Semantic novelty and exact objective alignment are required in addition to blind-answer and rationale agreement.
- Two unsuccessful attempts pause an objective for review. This is an unresolved objective, not completed coverage. Empty generation results consume requested slots; there is no endless attempt to reach a requested count.
- Incorrect learner answers affect practice scheduling, not generation quotas.

Source/context bounds and the existing API allowance fail closed. Very large catalogues can exceed the conservative request bound; no last-N truncation silently weakens duplicate checks. An index/retrieval design will be needed before that limit becomes a bottleneck.

## Cost interpretation and model change

Question-production estimates use settled token costs divided by published questions, including failed candidates. Uncertain reservations stay separate and are never automatically refunded. Jobs are counted once rather than once per joined API call. Mapping cost has its own measured rate; it is unavailable until actual mapping calls have been settled. While material remains unmapped, the missing-objective count and question-production estimate are explicitly partial. Basic coverage and useful depth are alternative totals, not additive budgets. The optional upper figure is a 50% planning reserve, not a confidence interval or guaranteed ceiling.

After the initial pilot, Terra is the default question generator, with Sol performing separate blind and rationale checks with high reasoning. Studio provides independent writer (Terra, Sol, Astra) and reviewer (Sol, Astra) choices, saved with each queued job. Coverage offers a free price comparison without queuing work or changing defaults. Astra remains the objective mapper, with Sol reviewing the inventory. Same-model checks can share systematic errors; the source and quarantine gates remain necessary. No new paid calls are made merely by deploying this feature or opening Studio.

The pilot's 50 published questions cost an estimated 78.8398 NOK: 65.7561 for Astra generation, 6.4074 for Sol blind checks and 6.6763 for Sol rationale checks. Repricing exactly the same token use and yield with Sol as generator gives approximately 39.3861 NOK (0.7877 per published question). That is a scenario, not a measured Sol-generation result. With Terra generating and Sol verifying, the same-token scenario is approximately 28.40 NOK (0.57 per published question). Terra generation has not yet been evaluated on a paid pilot. The forecast distinguishes historical costs from this current-model scenario and recalculates it from the call ledger. New prompts, token usage, overlap checks and rejection rates can change the actual result.

The 200 NOK authorization is unchanged. No automatic top-up, no paid retry after an uncertain API result, and no spending from the local ledger. Production remains the sole authoritative allowance ledger.

Answer choices already shuffle independently for every new session. Stored choice order remains stable on reload, resume and feedback; grading maps the displayed position to the original answer. A test now explicitly checks different session orders and stable resumed feedback.
