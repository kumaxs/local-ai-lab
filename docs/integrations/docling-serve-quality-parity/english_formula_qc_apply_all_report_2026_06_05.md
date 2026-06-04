# English Formula QC Apply-All Report - 2026-06-05

Fresh output root:

```text
.runtime/review/docling-adapter-qc-apply-all-english-2026-06-05
```

Documents:

```text
.runtime/review/docling-adapter-qc-apply-all-english-2026-06-05/two-col-arxiv-ai-lora/document.html
.runtime/review/docling-adapter-qc-apply-all-english-2026-06-05/two-col-arxiv-ai-transformers-gnn/document.html
```

## Formula 12 Analysis

Transformers-GNN formula 12 source text in `document.json` contains raw
alignment markers:

```text
m _ { i } ^ { \ell } & = \bigoplus _ { j \in \mathcal { N } _ { i } } m _ { i j } ^ { \ell } , & ( 1 2 )
```

The extra `&` is therefore not introduced by HTML replacement. It comes from
the formula text returned by the conversion path. The issue is a systemic class:
bare alignment markers can leak from equation alignment recognition into a
formula that is not wrapped in an alignment environment. In the fresh English
samples, only Transformers-GNN formula 12 hit this class.

Safe repair applied:

```text
reason=bare_alignment_marker_without_alignment_environment
action=sanitize_display_tex_preserve_raw_tex
evidence=formulas/formula_12_context.png plus page/bbox metadata
```

Final HTML now uses sanitized display TeX for MathJax:

```text
m _ { i } ^ { \ell } = \bigoplus _ { j \in \mathcal { N } _ { i } } m _ { i j } ^ { \ell } , ( 1 2 )
```

The original raw TeX is preserved in the trace block.

## Missing Number Coverage

LoRA:

```text
formula_count=6
formula_number_recovered_html_indexes=[]
formula_number_qc_count=6
```

No LoRA formula number was safely recoverable from structural formula text, so
the adapter records evidence-only diagnostics.

Transformers-GNN:

```text
formula_count=20
formula_number_recovered_html_indexes=[6, 10, 11, 12, 13, 14, 17, 18, 20]
formula_number_qc_count=20
```

Recovered numbers came from explicit equation-number evidence already present
inside formula text, including spaced forms such as `( 1 2 )`.

## Formula Second Pass Apply-All Review

Every formula now receives an adapter-side second-pass review gate.

LoRA:

```text
reviewed_count=6
enhanced_count=0
evidence_only_count=6
preserved_count=0
elapsed_seconds=0.000509
```

Transformers-GNN:

```text
reviewed_count=20
enhanced_count=9
evidence_only_count=11
preserved_count=0
elapsed_seconds=0.00229
```

The gate enhances final HTML only when structural evidence is safe: equation
numbers recovered from formula text, and display-TeX sanitization for the formula
12 bare alignment marker. It does not reorder, guess, or replace formulas from
document-specific content rules.

## Header/Footer and Footnote QC

LoRA:

```text
header_footer_qc_count=27
footnote_qc_count=11
```

Transformers-GNN:

```text
header_footer_qc_count=10
footnote_qc_count=1
```

The QC flags Docling-labeled page-edge nodes, page numbers, arXiv/template text,
rotated margin headers, and footnote fragments. It is diagnostic-only for
header/footer and footnote content, so body text is not destructively changed.

## CN Regression

Accepted CN output remained unchanged:

```text
.runtime/review/docling-adapter-html-polish-live-fullfallback-2026-06-04/CN/document.html
sha256=6911693bd781c628da70ae2494471f2f4cfd28448000aa599290353cd6af97db
```

## Notes

The runtime did not generate page/formula images because the local review
renderer dependency `pypdfium2` is unavailable in the active Python runtime.
The QC records page, bbox, and expected evidence links, and the existing warning
`review_artifact_pdf_renderer_missing:No module named 'pypdfium2'` remains.
