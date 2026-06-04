# Unified Review QC Findings - 2026-06-04

Fresh output root:

```text
.runtime/review/docling-adapter-unified-qc-english-2026-06-04
```

Documents:

```text
.runtime/review/docling-adapter-unified-qc-english-2026-06-04/two-col-arxiv-ai-lora/document.html
.runtime/review/docling-adapter-unified-qc-english-2026-06-04/two-col-arxiv-ai-transformers-gnn/document.html
```

## Unified QC Added

- Formula numbering QC:
  - detects display formulas with no equation number;
  - detects formulas whose equation number is present only as spaced or
    TeX-wrapped text, such as `( 1 0 )` or `\text {($6$)}`;
  - safely wraps only recoverable formulas in traceable MathJax/raw-TeX HTML.
- Header/footer QC:
  - records Docling-labeled `page_header` and `page_footer` nodes;
  - flags page numbers, arXiv/conference/template text, repeated edge text, and
    rotated margin headers;
  - does not delete or reorder content.
- Footnote QC:
  - flags isolated numeric fragments;
  - flags numeric markers attached to garbled text fragments;
  - flags overlapping footnote boxes and anchor/content marker mismatch;
  - does not guess-reorder footnotes.

## Fresh English Validation

LoRA:

```text
formula_number_recovered_html_indexes=[]
formula_number_qc_count=6
header_footer_qc_count=27
footnote_qc_count=11
```

The known page-1 footnote mismatch is now recorded as structural evidence:
`isolated_numeric_footnote_fragment`, `anchor_content_marker_mismatch`,
`numeric_marker_attached_to_text_fragment`, `hyphenated_split_footnote_continuation`,
and `overlapping_footnote_bbox`.

Transformers-GNN:

```text
formula_number_recovered_html_indexes=[6, 10, 11, 12, 13, 14, 17, 18, 20]
formula_number_qc_count=20
header_footer_qc_count=10
footnote_qc_count=1
```

Recovered formula numbers came from explicit trailing equation tags already in
the formula text, not from content-specific rules.

## Remaining Non-Repaired Cases

- LoRA formulas 4-6 and several Transformers-GNN formulas are flagged as
  `display_formula_missing_equation_number` because no safe structural equation
  number exists in returned JSON/HTML.
- LoRA page-1 footnote mismatch remains diagnostic-only because repair would
  require reordering or reconstructing Docling JSON content.

## CN Check

The accepted CN review output remains unchanged:

```text
.runtime/review/docling-adapter-html-polish-live-fullfallback-2026-06-04/CN/document.html
sha256=6911693bd781c628da70ae2494471f2f4cfd28448000aa599290353cd6af97db
```
