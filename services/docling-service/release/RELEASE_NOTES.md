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
