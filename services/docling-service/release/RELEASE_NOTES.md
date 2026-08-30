# Unreleased

## Region-level quality evidence

- the quality adapter now writes bounded, deterministic `regions.json` and
  `quality_signals.json` sidecars covering picture OCR, header/footer,
  pictures, tables, algorithms, code, display formulas, and inline math;
- critical regions use explicit `verified_semantic` / `unresolved` outcomes;
  bare pictures may be `visual_only` only when their regular crop exists.
  Truncation, unsafe/missing evidence, or a sidecar write failure is fail-closed;
- structural evidence is rebound to the final document node/body, source-PDF
  digest, page/bbox, and a kind-specific source asset. Multi-page algorithms
  remain unresolved unless every covered page and both rendered surfaces are
  bound; no incomplete first-page crop is promoted. Manifest and semantic
  sidecar contributor sets, node kinds, hashes, and union geometry must agree;
- table validation now checks final cell bboxes independently of producer
  coverage flags. It rejects one-row cells that cross another row center and
  repeated tall numeric cells that collapse several visible rows into one,
  incomplete declared-grid occupancy, and empty grids without an explicit
  fallback;
- inline-math binding preserves operators, relations, punctuation, and Unicode
  math symbols while normalizing presentation-only subscript separators, and
  rejects truncated candidate expressions;
- picture-contained OCR can no longer re-enter the main body through the
  algorithm-grouping path, and the PDF inventory recognizes strict
  definition/procedure-style algorithms whose numbered steps continue on the
  next page;
- the Docker API image and release bundle include the new stdlib-only region
  validator. A locked calibration/sealed corpus and old-baseline replay report
  are retained as source documentation; PDF binaries remain outside Git.

## Operations Web UI and packaging

- the API now serves a same-origin `/ui/` page for PDF upload, queue and
  trusted phase-level (not page-level) progress, TTL/storage visibility,
  artifact download, and terminal-job deletion;
- `ui/index.html`, `ui/main.js`, and `ui/styles.css` are declared as Python
  package data and retained by the recursive source bundle; no Node.js runtime
  or additional Compose service is introduced;
- lifecycle controls are persisted in SQLite and updated with a compare-and-
  swap revision. The UI can edit input, successful/failed output, job,
  staging/temp, cleanup, idempotency, and download-lease TTLs. SQLite
  overrides take precedence over environment/default values, `null` clears an
  override, and deadlines already assigned to existing jobs are not
  recalculated;
- paths, bearer token, model/engine settings, and concurrency/capacity limits
  remain read-only in the UI. A token entered in the page is kept in memory
  only;
- the queue uses cursor-based previous/next pages with current-page counts;
  refresh/navigation generations reject stale or cyclic cursor results, and a
  bounded automatic-refresh takeover prevents a hung navigation from leaving
  the page indefinitely stale;
- token replacement/clear and `401` invalidate delayed job/config/output/
  upload responses and clear protected page data;
- unauthenticated downloads use the browser's native streaming path. Bearer-
  protected browser downloads are capped at 256 MiB; use a streaming API
  client for larger protected artifacts.

This section describes the current source tree and the next release. The
published `v1.1.1` archives and container images do not contain this Web UI.

# Docling Service 1.1.1

## V1 quality and source-evidence hardening

This maintenance release is the final reproducible checkpoint of the existing
v1 conversion architecture before work begins on a region-oriented v2. The
public `/v1/jobs` API, bounded storage lifecycle, immutable input contract, and
release packaging remain compatible with 1.1.0.

Quality changes include:

- source crops for tables, formulas, inline math, algorithms, and code are
  presented in closed, accessible disclosure panels so review evidence does
  not interrupt the main reading flow;
- table and formula crops are clamped to page, column, and neighboring-region
  geometry, with the clipping decision included in provenance verification;
- empty image-table recovery is local-first and fail-closed, with strict
  dimension, cell-offset, semantic-hint, overlap, and ruled-grid checks;
- mixed prose and inline-math evidence is grouped at paragraph scope while
  retaining transformed span text, source coordinates, and column bounds;
- CJK fallback preserves the existing semantic surface when machine formula
  normalization cannot be proven safe;
- CJK equation-number binding now requires unique HTML/Markdown occurrences
  and exact formula-body identity before source evidence or final polish is
  accepted;
- source-evidence cleanup is idempotent, Markdown code fences are protected,
  and malformed paths, URLs, bboxes, crop metadata, and table offsets fail
  closed;
- regression coverage was expanded for crop geometry, image-table recovery,
  disclosure rendering, CJK formula identity, paragraph-level inline math,
  and provenance tampering.

## Readiness boundary

Publishing this checkpoint does **not** approve the v1 parser for unattended
high-fidelity production use. A fresh three-paper blind review on 2026-08-12
found cross-document generalization failures in picture/OCR classification,
algorithm binding and coverage, table row reconstruction, and figure coverage.
Those findings motivate the planned v2 architecture: typed region IR,
independent validators, multiple parser candidates, and explicit
`verified_semantic` / `visual_only` / `unresolved` outcomes. They are not hidden
or relabeled as v1 successes.

Docker model downloads still default to `https://hf-mirror.com`. Set
`HF_ENDPOINT` before startup to select the official Hugging Face endpoint or
another compatible mirror.

Docker images are published for `linux/amd64` and `linux/arm64` at:

- `ghcr.io/kumaxs/local-ai-lab-docling-api:1.1.1`
- `ghcr.io/kumaxs/local-ai-lab-docling-backend:1.1.1`
- `ghcr.io/kumaxs/local-ai-lab-docling-formula:1.1.1`

The release bundle contains a Compose file that references only those prebuilt
images. A target machine needs Docker Engine with Compose v2; it does not need
Git, Python, or a shell script when the Compose YAML is supplied directly.
