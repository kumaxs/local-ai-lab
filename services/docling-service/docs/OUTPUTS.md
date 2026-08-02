# Output contract

Each accepted request owns one directory named by the server-generated UUID.
The primary reading surfaces are semantic HTML and Markdown; source images are
supporting evidence and never a substitute for recognized content.

## Required files

| File | Meaning |
| --- | --- |
| `document.html` | primary reading surface with MathML, linked citations/footnotes, semantic tables, algorithms, code, figures, and emphasis; figure files may be job-local relative assets |
| `document.md` | portable text surface with TeX formulas, tables, code fences, algorithms, citations, and footnotes |
| `document.json` | Docling structural document plus provenance and quality annotations |
| `metadata.json` | input, engine, conversion-policy, count, provenance, and output inventory metadata |
| `status.json` | final quality decision, warnings, errors, and diagnostic signals |

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
pictures/picture_N.png
formula_second_pass/*
```

These artifacts support traceability and manual review. They may be absent when
the source does not contain that structure or a reliable bounding box is not
available.

## HTML semantics

- Display formulas include MathML for browser rendering and retain source TeX.
- Bibliography entries have stable anchors. Numeric and author-year citations
  link to the corresponding entries when mapping is unambiguous.
- Footnote callouts and notes have forward/back links.
- Algorithm line indentation is encoded in preformatted semantic blocks; line
  numbers are separate from content indentation.
- Algorithms and code preserve reliable bold/italic spans and syntax roles.
- Tables are real HTML tables; intentional cell line breaks remain visible.
- Page, formula, and table crops may be linked from `review_index.html` as QA
  evidence, but are never embedded in or linked from the primary surfaces as a
  replacement for recognized content. Paper figures remain ordinary semantic
  figure assets referenced by `document.html`.

## Markdown semantics

- Display formulas use TeX math blocks and retain equation numbers.
- Multiline or structurally rich tables may use embedded HTML where pipe-table
  syntax would lose cell line breaks or spans.
- Code and algorithms use fenced/preformatted blocks so whitespace is material.
- Citation and footnote relationships use Markdown links and anchors.

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
coverage, `portable_formula_ocr` recognition/patch results (selected model
variant, source-semantic coverage, missing symbols, repairs, and guarded primary
and fallback evidence), plus `source_semantic_gate` explaining whether a reliable
PDF text layer was available,
table counts, `/Gxx` bad-text-layer density, broken local references, OCR
fallback use, semantic reflow application, citation links, footnotes, algorithm
blocks, and actual formula/OCR engines.

`portable_formula_ocr.surface_sync_ok` describes the provisional writer output
before semantic reflow and may be false when a late formula anchor is rebuilt in
the authoritative surfaces. Acceptance is determined by
`final_formula_surface`: every formula must appear as MathML in HTML and a TeX
block in Markdown, with no fallback or undecoded placeholder.

The API output manifest adds a SHA-256 and byte size for every downloadable
regular file. Consumers should not infer completeness merely from the presence
of `document.html`; use `status.json` and the manifest together.

For Docker, `portable_formula_ocr.crop_tightening` records the visible-ink crop,
edge-clipping decision, and the primary/fallback image selected per formula.
`high_resolution_crop_indexes` records formula bboxes for which a column-bounded
six-times PDF render was available; that image is selected only when the preview
crop is edge-clipped. These transient images are sent only to the private
formula container and are not substituted into `document.html` or `document.md`.
