# CN Formula Quality Diagnostics

Date: 2026-06-02

Scope: Route A Docling Server adapter only. Route B remains evaluation-only and
is not used as full-document text/layout input.

## Targeted Findings

The current `CN.pdf` failures are concentrated on Docling formula nodes on page
3:

- Formula node 3, rendered formula `(3)`, has Docling label `formula`, page 3,
  bbox `l=114.91, t=542.08, r=288.50, b=530.92` with bottom-left origin. The
  formula text includes `\\text {所}`, which indicates formula enrichment/text
  contamination from neighboring prose. The bbox itself remains inside the left
  column and does not cross the expected column boundary, so the primary issue
  is formula text enrichment/region interpretation rather than adapter crop
  coordinate conversion.
- Formula node 5, rendered formula `(5)`, has Docling label `formula`, page 3,
  bbox `l=83.13, t=65.11, r=288.50, b=55.11` with bottom-left origin. The bbox
  is only about 10 PDF points tall while the enriched formula text is very long
  and repeatedly emits fraction fragments. This means the original Docling
  formula bbox appears to cover only a thin line/separator, so the previous
  source/context crop was faithful but not useful.

## Adapter Improvement

The adapter now keeps a tight source crop (`formulas/formula_N.png`) but writes
a much larger context crop (`formulas/formula_N_context.png`) for formulas. It
also records geometry diagnostics in `metadata.json` and `status.json`,
including:

- formula text containing CJK/prose-like fragments;
- number-only formulas;
- too-thin bboxes for complex formulas;
- source crops likely too thin or useless;
- bboxes near or crossing expected two-column boundaries;
- nearby same-page nodes that help explain contamination.

The review index now links suspicious formulas to source crop, context crop, and
full page evidence. If a source crop is likely useless, the adapter warns that
manual review should use the context crop or full page instead.

## Non-Fixes

This change does not claim to semantically fix formulas `(3)` or `(5)`. Formula
`(3)` still requires parser/enrichment-level correction, and formula `(5)` still
appears to originate from a bad Docling formula bbox. The adapter now makes
those failures explicit and easier to inspect without replacing Route A text or
layout with Route B.
