# Docs index

This directory keeps **active integration sources** only.

- `docs/integrations/docling-serve-quality-parity/`
  - parity adapter runtime scripts
  - parity quality tests

All historical notes, legacy reviews, and closed investigations are retained in git
history. Query them with `git log` / `git log --follow` rather than duplicating in
this file.

## Local-only acceptance evidence

The following ignored paths may exist in this checkout. They are private,
pruned local evidence—not source-of-truth documentation or portable release
artifacts—and are expected to be absent from a clean clone:

- `.runtime/review/docling-adapter-html-polish-live-fullfallback-2026-06-04/`
- `.runtime/review/docling-vlm-full-dir-review-2026-06-01/CN/`
- `.runtime/review/combined-new-old-acceptance-2026-07-29/`
- `.runtime/review/random-three-paper-acceptance-2026-07-28/`
- `.runtime/review/random-generalization-blind-2026-07-29/`

The first two paths support local adapter fallback defaults and intentionally
retain only the `document.json` consumed by the adapter. The last three retain
textual manifests and selected outputs behind historical acceptance claims.
All five are deliberately pruned and are not self-contained browsing or
reproduction bundles; regenerate fresh review output for new validation.
