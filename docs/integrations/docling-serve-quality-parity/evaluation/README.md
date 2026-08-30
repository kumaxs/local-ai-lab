# Document quality evaluation protocol

This directory records document-level corpus locks and reproducible quality
results. PDF binaries and conversion outputs are deliberately kept outside
Git; committed manifests identify them by canonical URL, source version, byte
size, and SHA-256.

## Split rules

- `development`: previously opened, rendered, converted, or manually reviewed.
  All repository `test_pdfs` and the 23-paper 2026-08-12 review bundle are in
  this split.
- `calibration`: selected and hash-locked before inspection, then opened only
  for the current evaluation/threshold-setting run. The moment its output or
  pages are inspected it is not a blind holdout.
- `sealed`: selected and hash-locked before any PDF/page/output inspection.
  Automated release scoring may read it once. After any human or developer
  inspection, move it to `development` and replace it with a new sealed item.

Selection metadata must be committed before downloading a new candidate.
After download, append the exact resolved URL, byte size, SHA-256, retrieval
time, and outcome in a later commit. Deduplicate by document identity and
SHA-256; different arXiv versions are different immutable inputs and must not
cross splits.

## Release comparison

Every quality change runs both groups:

1. the complete old development baseline (including all ten `test_pdfs` and
   the stored 23-paper audit when its outputs remain available); and
2. all current calibration documents plus the sealed set in a non-interactive
   scoring run.

Report per-document and per-region counts for `verified_semantic`,
`visual_only`, and `unresolved`, including critical unresolved counts for
picture OCR, header/footer, table topology, formula/inline math, algorithm,
and code. A release fails on any new critical unresolved region, any old
baseline regression, an unreadable/malformed sidecar, or a false-success gate.

Do not tune rules from filenames, paper titles, sample names, known hashes, or
literal text unique to a document. Persistent evaluation directories outside
Git must contain their own README explaining provenance, retention, and safe
cleanup.

Recorded run: [`results-2026-08-30.md`](results-2026-08-30.md), with immutable
input metadata in [`corpus-lock-2026-08-30.json`](corpus-lock-2026-08-30.json).
