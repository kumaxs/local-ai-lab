# Docling V1 Parity Checklist for Docling Server Adapter

Date: 2026-06-01

## Naming

- Docling V1: the earlier `services/docling-service` quality-first route.
- Docling Server: the official `docling-serve` HTTP API backend.
- Route A: the current Docling Server quality-parity adapter.
- Route B: the `VlmPipeline` evaluation route only.

Docling Server is a backend/service replacement candidate for Docling V1, not a
new user-facing contract. A naked Docling Server API response is not acceptable
as the contract. Route A is acceptable only after it reproduces the quality
policy, review artifacts, metadata/status diagnostics, and unresolved-gap
warnings that Docling V1 already established.

Route B is not a default-route candidate at this stage. Manual review found that
Route B can produce cleaner formulas on `CN.pdf` section 2.3, but it fatally
drops right-column text on pages 3 and 4, wrongly concatenates left-column text,
does not meaningfully improve footnotes, emits inline formula text such as
`x$_{i}$` without proper HTML rendering, and renders most pictures as black
blocks. Route B remains evaluation-only evidence, not full-document text/layout
source material.

## Checklist

| # | Parity item | Docling V1 baseline | Current Route A status | Action |
| ---: | --- | --- | --- | --- |
| 1 | Chinese OCR fallback trigger and `/Gxx` detection | Count `/Gxx` tokens with regex `/G[0-9A-Fa-f]{2}`; fail at count >= 10 and density >= 0.002; then try OCR fallback. | Aligned in adapter defaults. Route A keeps `/Gxx` metrics in metadata/status and can request OCRMac parity fallback through Docling Server. | Ported V1 threshold constants into Route A. |
| 2 | `CN.pdf` text completeness and no dropped columns | OCRMac fallback preserved the Chinese text layer much better than the bad embedded text layer. | Route A is currently preferred over Route B for full-document CN text because Route B drops right-column text on pages 3 and 4. | Keep Route A as the only Server route. Do not replace text/layout with Route B. |
| 3 | `CN.pdf` section 2.3 formula contamination | V1 still depended on Docling's formula region detection, with visual artifacts for review. | Route A still shows suspicious section 2.3 formula contamination in formula 3. Route B avoids this formula issue but breaks full-document text/layout. | Parity blocker. Do not implement broad Route A/Route B hybrid merging without design approval. |
| 4 | Formula missing/incomplete detection, including number-only formulas | V1 counted placeholders and surfaced decode-limited formulas through warnings and crops. | Route A detects `Formula not decoded`, number-only formulas such as `(4)`, repeated-pattern formulas, and CJK contamination inside formulas. | Aligned and retained. |
| 5 | Formula/source/context crop traceability | V1 wrote `assets/formula_N.png` plus `assets/formula_N_context.png` when coordinates existed, and exposed links in HTML. | Route A now writes `formulas/formula_N.png` and `formulas/formula_N_context.png`, records source/context counts, and injects best-effort source/context links into `document.html`. | Ported low-risk V1 behavior. |
| 6 | Page images and `review_index.html` | V1 wrote page images under `assets/`; later review flows used visible HTML appendices. | Route A writes `pages/page_N.png` and `review_index.html`, and adds a visible review banner to `document.html`. | Aligned in adapter-owned review layer. |
| 7 | Picture crops and image rendering reliability | V1 exported picture assets when Docling had picture regions or images. | Route A uses PDF-rendered provenance crops for pictures, which avoids Route B's black-block rendering failure mode. | Keep Route A picture crops as evidence. Route B picture rendering failure is documented as an evaluation finding. |
| 8 | Table JSON/HTML/CSV/image artifacts | V1 wrote table JSON and HTML/Markdown when exportable, plus table crops when possible. | Route A writes `tables/table_N.json`, `tables/table_N.html`, `tables/table_N.csv`, and `tables/table_N.png` from Docling Server JSON and PDF provenance. | Aligned except Markdown table sidecars; HTML/CSV/image are sufficient current review parity. |
| 9 | Contract outputs | V1 contract included `document.html`, `document.md`, `document.json`, `metadata.json`, and `status.json`. | Route A preserves those files and adds review sidecars without changing the contract shape. | Aligned. |
| 10 | Explicit warnings for unresolved gaps | V1 made command success distinct from reading-quality success and surfaced review caveats. | Route A now emits explicit unresolved warnings for footnotes, PDF links, inline formula HTML rendering, and math symbol rendering. | Ported as metadata/status warnings. |

## Low-Risk Fixes Implemented

- Aligned the adapter's default `/Gxx` fallback thresholds with Docling V1:
  count >= 10 and density >= 0.002.
- Added formula source crops (`formulas/formula_N.png`) in addition to formula
  context crops (`formulas/formula_N_context.png`).
- Added formula asset/context counters and best-effort formula source links in
  `document.html`.
- Added explicit unresolved V1 parity warnings for footnotes, PDF links, inline
  formula HTML rendering, and math symbol rendering.
- Kept Docling Server output behind the adapter contract and review layer.

## Parity Blockers Not Solved Here

- `CN.pdf` section 2.3 formula contamination remains a Route A parser/region
  issue. Evidence and warnings are present, but semantic repair is intentionally
  not attempted.
- Footnote handling, PDF link extraction, inline formula HTML rendering, and
  math symbol rendering remain unresolved quality gaps.
- Route B cannot be used to patch full-document text/layout because it drops
  right-column CN text and has picture rendering failures.

## Required Posture

Before further parser improvement, compare new Docling Server changes against
this checklist. Implement only direct Docling V1 parity regressions in this
adapter. Document larger parser redesigns as blockers until they have an
approved design.
