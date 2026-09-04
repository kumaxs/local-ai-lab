# Output contract

Each accepted request owns one directory named by the server-generated UUID.
The primary reading surfaces are semantic HTML and Markdown. Source visuals
for formula/table/algorithm/code are the authoritative visual review layer.

The operations Web UI (`/ui/`) presents this same contract without treating
page images as reading progress. It reports queue and conversion phases at job
scope, lists only files present in the verified published manifest, and uses the API's
download lease while streaming a file or archive. The UI shows the input,
output, and tombstone deadlines plus `artifact_state`; a `410`/expired result
is a lifecycle outcome, not a missing file. A terminal job can be explicitly
deleted from the UI, subject to the same active-download and retention checks
as `DELETE /v1/jobs/{job_id}`.

## Required files

| File | Meaning |
| --- | --- |
| `document.html` | primary reading surface with MathML, linked citations/footnotes, semantic tables, algorithms, code, figures, and emphasis; figure files may be job-local relative assets |
| `document.md` | portable text surface with TeX formulas, tables, code fences, algorithms, citations, and footnotes |
| `document.json` | Docling structural document plus provenance and quality annotations |
| `metadata.json` | input, engine, conversion-policy, count, provenance, and output inventory metadata |
| `status.json` | final quality decision, warnings, errors, and diagnostic signals |

The release adapter may also retain a job-local `source.pdf`. It is the
read-only copy of the immutable submitted snapshot used for final visual
evidence and PDF inventory; it is not one of the five required reading files
and is intentionally omitted from the service ZIP archive.

## Conditional files

Depending on paper content and available provenance:

```text
review_index.html
pages/page_N.png
tables/table_N.json
tables/table_N.html
tables/table_N.csv
tables/table_N.png
formulas/formula_N.png
formulas/formula_N_context.png
algorithms/algorithm_N.png
code_blocks/code_block_N.png
pictures/picture_N.png
formula_second_pass/*
```

These artifacts support traceability and manual review. They may be absent when
the source does not contain that structure or a reliable bounding box is not
available.

## Input identity and provenance

The quality-parity path snapshots the submitted PDF before conversion, records
its expected SHA-256 when supplied, and rejects a changed, replaced, symlinked,
or non-regular input. Each job uses a fresh output directory and a persistent
job lock; an old sibling output or sibling PDF is never used for recovery. The
published `source.pdf` is checked against the snapshot before inventory, before
the final visual gates, and before metadata/status publication.

The PDF inventory gate reads that same `source.pdf` and verifies its name,
location, digest, page continuity, text health, and independent high-confidence
formula/table/algorithm/code counts. `metadata.json` and
`status.json.quality_signals` may expose `pdf_structure_inventory`,
`final_pdf_inventory`, `final_source_visuals`, `final_formula_surface`, and
`final_structural_surface`. Current source builds also publish `regions.json`
and `quality_signals.json`: the first is a deterministic, request-bounded
inventory of region evidence with a 10,000-record hard cap and the second is its
compact summary. Both are listed in `metadata.json.generated_outputs` only after
they are written successfully; a failed replacement removes any older regular
sidecar generation and its output-list entries. Producer-owned diagnostic lists
are still traversed with a 1,001-item bound, and `regions.json` has a 128 MiB
serialized-size limit. The high-confidence/ambiguous counters drive gate
comparisons; persisted `records` arrays are bounded diagnostic samples and are
not a second exact-count contract. Stand-alone validator callers must serialize
sidecar-writing evaluations per output directory; the production path enforces
this with its persistent job lock and fresh-output guard.

Formula, table, algorithm, and code source visuals are occurrence-bound
evidence, not generic decorations. Their manifests bind the submitted-PDF SHA,
page/bbox and page/pixel geometry, asset digest, stable source reference, and
normalized body identity. A blank, label-only, context-only, appendix-only, or
unbound crop cannot satisfy exact coverage. Machine HTML/TeX remains searchable
but cannot override a conflicting source visual.

Region outcomes are `verified_semantic`, `visual_only`, and `unresolved`.
Unresolved picture-OCR, header/footer, table, algorithm, code, formula, or
inline-math evidence is critical and forces `degraded_failure`. Every
machine-binding-expected source picture must preserve its source ref,
page/bbox, source-PDF digest, crop digest, and exactly one real image reference
on each HTML and Markdown surface; missing or tampered bindings are critical.
Tiny/decorative, furniture, quarantined, and formula-child pictures remain
noncritical advisory evidence. Structural records are rebound to a final node/body identity, source
PDF digest, page/bbox, and kind-specific source asset. Cross-page algorithms
also require a real source asset for every covered page; until the producer can
publish that set, they intentionally remain unresolved. Table regions
independently detect cells that cross semantic row boundaries or collapse
repeated visual rows, require every declared grid slot to be covered, and allow
an empty grid only through the explicit empty-table fallback. Algorithm
contributors must agree between the provenance manifest and
`algorithm_blocks.json`. Inline-math identity keeps operators, relations,
punctuation, and Unicode math symbols, so a correctly linked crop cannot
certify a truncated expression or malformed semantic grid.

Formula authority is occurrence-bound and independent of the whole-document
route. Route A is the non-formula structural/reading baseline. If the guarded
private `formula_service` result passes its source-identity, coverage, and final
surface gates, it owns the final formula surface and the generic second pass is
skipped. Otherwise an explicit Route-B/guarded `apply-all` result becomes
authoritative only after every coverage and rollback-protected final-surface
gate passes; a rejected candidate cannot partially replace the prior surface.

The second-pass formula repair policy is configured by
`DOCLING_FORMULA_SECOND_PASS_POLICY` (formal release:
`off`, `auto`, `apply-all`).
In release mode, `DOCLING_FORMULA_SECOND_PASS_ROUTE_B_DIR` can be either a
direct route-B document directory (`.../<job-id>`) or a shared route-B root that
contains per-job directories; the service resolves `<route-b-root>/<job-id>` first
when present.
For any job-aware lookup, the resolved directory must contain regular,
non-symlink `document.json`, `status.json`, and `metadata.json` files;
`metadata.json.job_id` must exactly match the requested job. A missing or stale
job binding disables `auto` and causes explicit `apply-all` to fail before the
adapter is launched.

- `off`: no second-pass refinement.
- `auto`: run second pass only when a route-B reference directory exists and is
  valid.
- `apply-all`: always run second pass when `formula_second_pass_route_b_dir` is
  configured and valid.

- `apply-all` requires both route status files to report `ok=true`, distinct
  input/output directories, successful formula coverage for JSON and Markdown,
  readable source PDF bytes (`source.pdf` / `input.pdf` / declared source path),
  symmetric matching route job identities, and matching persisted SHA-256
  provenance for both routes.
- Legacy route-B VLM artifacts that do not provide verifiable input hash + source
  PDF evidence are rejected and must be regenerated before they can be consumed.

Legacy aliases are still accepted for compatibility:
`review` maps to `auto`, and `apply` maps to `apply-all`.
`formula_second_pass_route_b_dir` must point to an existing directory for
`apply-all`.

Route-B VLM review output uses the same source identity discipline. Each attempt
is built in a sibling staging directory under a per-job publish lock and is
published with one atomic rename. An existing job directory is quarantined as a
whole; quarantine markers are created exclusively without following symlinks,
and retention keeps only the newest two quarantine siblings. Quarantined VLM
directories are evaluation artifacts, not service output, and are outside the
service Janitor.

## HTML semantics

- Display formulas include MathML for browser rendering and retain source TeX.
- Bibliography entries have stable anchors. Numeric and author-year citations
  link to the corresponding entries when mapping is unambiguous.
- Footnote callouts and notes have forward/back links.
- Algorithm line indentation is encoded in preformatted semantic blocks; line
  numbers are separate from content indentation.
- Algorithms and code preserve reliable bold/italic spans and syntax roles.
- Tables are real HTML tables; intentional cell line breaks remain visible.
- Page, formula, table, algorithm, and code source crops are included for QA.
  A crop is authoritative only when it is visibly bound to one unique body
  occurrence, originates from the submitted PDF, contains the actual body rather
  than an equation label or surrounding context alone, and passes content and
  geometry identity checks. Appendix-only or unbound crops do not satisfy the
  delivery gate.
- Machine-rendered HTML/TeX remains for searchable output and traceability, but is
  not a substitute when source visuals are available.
- Inline math recovery is geometry-driven only. A remaining unresolved cluster
  may be delivered as degraded machine-surface quality only with a tight, open
  source crop at the exact body occurrence; ambiguous or appendix-only evidence
  fails. Paper-specific substitutions are never injected.

## Markdown semantics

- Display formulas use TeX math blocks and retain equation numbers.
- Multiline or structurally rich tables may use embedded HTML where pipe-table
  syntax would lose cell line breaks or spans.
- Code and algorithms use fenced/preformatted blocks so whitespace is material.
- Citation and footnote relationships use Markdown links and anchors.
- Formula links and inline formulas in Markdown remain searchable, with source visual
  checks recorded in the quality artifacts.

## Status interpretation

Read `status.json.ok` first, then `status.json.success_class`, `warnings`, and
`quality_signals`.

- `success`: conversion and quality checks passed.
- `degraded_success`: readable outputs exist but a recorded caveat requires
  review.
- `degraded_failure`: required quality evidence failed; do not ingest the main
  surfaces as authoritative.
- `failure`: conversion did not produce an acceptable output.

Useful signals include formula counts and placeholders, per-formula MathML
coverage, formula recognition/patch diagnostics (selected model variant,
source-semantic coverage, missing symbols, repairs, and guarded primary and
fallback evidence), plus whether a reliable PDF text layer was available,
table counts, `/Gxx` bad-text-layer density, broken local references, OCR
fallback use, semantic reflow application, citation links, footnotes, algorithm
blocks, and actual formula/OCR engines.

A fully machine-renderable result has `machine_surface_ok=true`: every semantic
formula appears as MathML in HTML and a TeX block in Markdown, with no TeX
fallback.

When machine coverage is incomplete but an exact, open source visual is bound to
every affected body occurrence, the output may remain `ok=true` only as
`degraded_success` and must carry explicit inline-formula/formula-surface
warnings. Appendix-only evidence, raw model tokens, undecoded placeholders, or
missing source visuals are hard failures and drive `degraded_failure`.

`final_formula_surface` separates machine and visual delivery. Implementations may
also emit `final_source_visuals` and `final_structural_surface` for traceability.

The API output manifest adds a SHA-256 and byte size for every downloadable
regular file. Consumers should not infer completeness merely from the presence
of `document.html`; use `status.json` and the manifest together.

The service Janitor owns TTL cleanup for registered input PDFs, staging,
published output, temporary files, and tombstones using persistent cleanup
claims. Direct `quality_parity_adapter.py` runs, batch review helpers, and VLM
evaluation runs are not registered with that Janitor; operators must remove
their temporary output roots after review. The archive endpoint deliberately
filters out `source.pdf` even when it exists in the internal job tree.

Lifecycle TTLs can be viewed and edited from the Web UI through the
SQLite-backed `GET/PATCH /v1/system/config` contract. Editable controls are
the input, successful-output, failed-output, job, staging, temporary,
cleanup-interval, idempotency, and download-lease TTLs. Configuration updates
use a compare-and-swap revision; `null` removes a SQLite override and falls
back to the environment/default precedence. Deadlines persisted on existing
jobs are not recomputed, so changing a TTL does not retroactively extend or
shorten an already accepted task. Paths, token, models, and concurrency or
capacity limits are deployment-level read-only values.

In some deployments, `portable_formula_ocr.crop_tightening` records the visible-ink crop,
edge-clipping decision, and the primary/fallback image selected per formula.
`high_resolution_crop_indexes` records formula bboxes for which a column-bounded
six-times PDF render was available; that image is selected only when the preview
crop is edge-clipped. These transient images are sent only to the private
formula container and are not substituted into `document.html` or `document.md`.
