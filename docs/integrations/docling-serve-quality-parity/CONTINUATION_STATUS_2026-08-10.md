# Quality-delivery continuation status — 2026-08-10 (updated 2026-08-11)

> Historical checkpoint. For current implementation, deployment, validation,
> and quality status, use the repository `HANDOFF.md` and
> `evaluation/results-2026-08-30.md`. Counts below are intentionally preserved
> as 2026-08-10 evidence and must not be read as current results.

This is the durable continuation record for the quality-parity delivery work.
It records the current implementation and verification state, including the
completed nine-PDF offline delivery replay.

## Acceptance standard

The delivered semantic HTML/Markdown must preserve the understandable meaning
of the submitted PDF. Formula, table, algorithm, and code source visuals are
authoritative only when their submitted-PDF SHA, page/bbox, asset identity,
stable source reference, and occurrence body identity all verify. Missing,
ambiguous, stale, label-only, blank, context-only, or appendix-only evidence
fails closed. Machine HTML/Markdown/MathML remains searchable and useful, but
does not override conflicting source evidence.

Canonical repository:

```text
/Users/zeyuan/Projects/local-ai-lab
```

Formal dependency-backed interpreter:

```text
/Users/zeyuan/Projects/local-ai-lab/.runtime/docling-release/macos/venv/bin/python
```

`HANDOFF.md` is user-owned and is outside this continuation record.

## Current implementation status

### Immutable input and fresh delivery

- The direct quality-parity adapter creates an `_ImmutableInputSnapshot` before
  network conversion, verifies an optional expected SHA-256, and keeps the
  snapshot descriptor verifiable for the complete job.
- A persistent per-job lock spans the run. A pre-existing output directory is
  rejected; a fresh output guard cleans only output owned by the current run.
- The submitted snapshot is authoritative. Automatic sibling text-layer
  recovery is disabled, so a nearby or older PDF cannot silently become the
  semantic or visual source.
- The adapter publishes a regular read-only `source.pdf` in the job directory,
  rechecks its identity and SHA before and after PDF inventory and before final
  metadata/status publication.

### Inventory and provenance gates

- The PDF inventory gate reads that exact `source.pdf` and checks the expected
  filename/path, SHA-256, page sequence, text health, and independent
  high-confidence formula/table/algorithm/code counts.
- Final formula and structural gates reconcile inventory counts with the
  semantic document and occurrence-bound source visuals.
- Formula, table, algorithm, and code manifests carry source-PDF SHA,
  page/bbox and page/pixel geometry, asset digest, stable source ref, and
  normalized body identity. Unbound or appendix-only crops remain diagnostic
  evidence and cannot satisfy exact coverage.

### Route B VLM publication

- VLM evaluation attempts snapshot `source.pdf` into a fresh sibling staging
  directory, use a per-job `.<job-id>.vlm_publish.lock`, and publish with one
  atomic rename. Active output never contains a mixture of old and new assets.
- Existing output is quarantined as a complete directory. The quarantine note is
  created with `lstat`/exclusive no-follow semantics, so a symlink cannot redirect
  the write. Retention keeps only the newest two quarantine siblings (`keep2`),
  and pruning removes symlinks as leaves.
- VLM output is evaluation-only and is not enrolled in the service Janitor.

### Route-B metadata/job binding

When the service resolves a direct or shared Route-B directory, contained
regular non-symlink `document.json`, `status.json`, and `metadata.json` files are
required. For a job-aware lookup, `metadata.json.job_id` must exactly match the
requested job. Missing, malformed, stale, one-sided, or mismatched artifacts
disable `auto` and fail explicit `apply-all` before the adapter is launched.

## Formal verification

The latest formal discovery results are:

- integration suite: **584 OK, 5 skipped**;
- service suite: **165 OK**;
- unified real-PDF offline delivery replay: **9/9 passed**;
- `git diff --check`: clean.

Use the formal interpreter and avoid system/root Python for the integration
suite:

```bash
cd /Users/zeyuan/Projects/local-ai-lab/docs/integrations/docling-serve-quality-parity
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  /Users/zeyuan/Projects/local-ai-lab/.runtime/docling-release/macos/venv/bin/python \
  -m unittest discover -s . -p 'test_*.py' -q

cd /Users/zeyuan/Projects/local-ai-lab
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=services/docling-service \
  /Users/zeyuan/Projects/local-ai-lab/.runtime/docling-release/macos/venv/bin/python \
  -m unittest discover -s services/docling-service/tests -p 'test_*.py' -q
```

A historical pre-fix reviewer snapshot is superseded and must not be used as the
current acceptance result.

## Nine-PDF replay status

The required cases are:

1. `new/2607.24235`
2. `new/2607.25988`
3. `old/CN`
4. `old/table-heavy-ai-complex-tables-gtr`
5. `old/table-heavy-ai-table-transformer`
6. `old/two-col-arxiv-ai-bert`
7. `new/2607.25463`
8. `new/2607.25802`
9. `new/2607.25967`

The final replay used one fresh temporary root for all nine cases and completed
with exit code zero. Every result reported `status.ok=true`, formula gate true,
structural gate true, PDF inventory gate true, and zero broken local refs. The
replay exercised contract writes, `restore_review_artifact_layer`, portable and
optional formula phases (under the configured off policies), source-backed
semantic reflow, independent inventory, final visual restoration, and the final
formula/structural/inventory gates against each retained real source PDF.

The replay intentionally remains an offline regression: it starts with the
retained converter document instead of calling Docling Serve over the network,
and it does not claim or test the production job lock/snapshot transaction.
Those lifecycle paths are covered by the formal unit/integration suites. The
temporary replay helper and output root are deleted after recording this
result; they are not durable runtime artifacts.

Original-resolution visual checks covered the CN formula-5 context, CN table 1,
the `2607.25988` algorithm block, and BERT table 1. The crops were readable,
page-correct, and contained the expected body rather than a blank/label-only
substitute. CN delivered 20 semantic formulas and six tables with four
standalone equation-number artifacts explicitly dropped; all 20 surviving
formula occurrences and all six table occurrences had exact HTML/Markdown
source coverage.

## Lifecycle and archive boundaries

The service API owns registered inputs, `.staging`, published outputs, temp
files, tombstones, SQLite cleanup claims, and the background Janitor. It applies
the configured TTLs and protects active downloads and pending uploads.

The direct quality-parity CLI, `batch_full_dir_review.py`, and
`vlm_full_dir_review.py` use independent output roots and are not registered
with that Janitor. Operators must remove their temporary replay, staging, and
review roots after evidence is recorded.

The service archive intentionally excludes `source.pdf`; its ZIP contains the
published deliverables and `manifest.json`, while source bytes remain governed
by input retention and access policy. This exclusion does not weaken source
SHA/size verification inside the job lifecycle.

## Final handoff checklist

1. Inspect and stage the intended diff while excluding user-owned `HANDOFF.md`.
2. Remove temporary replay/helper artifacts and verify no orphan snapshots,
   locks, scripts, or bytecode were created by this work.
3. Commit and push the verified change set.
