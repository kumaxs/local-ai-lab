# All Test PDF QC Report - 2026-06-05

Fresh output root:

```text
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05
```

Scope:

- Fresh outputs were generated for all non-CN PDFs in
  `/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs`.
- CN was not regenerated for this report; the accepted CN output was checked by
  SHA regression.
- Every formula discovered in fresh outputs entered the adapter-owned
  second-pass review gate.

## Fresh Output Paths

```text
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05/layout-doc-ai-donut/document.html
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05/layout-doc-ai-layoutlm/document.html
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05/table-heavy-ai-complex-tables-gtr/document.html
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05/table-heavy-ai-table-transformer/document.html
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05/two-col-arxiv-ai-bert/document.html
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05/two-col-arxiv-ai-gat/document.html
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05/two-col-arxiv-ai-lora/document.html
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05/two-col-arxiv-ai-rag/document.html
.runtime/review/docling-adapter-all-testpdf-qc-2026-06-05/two-col-arxiv-ai-transformers-gnn/document.html
```

## First-Page Footnote Report

LoRA page 1 has one safe footnote recovery and one unresolved isolated numeric
marker.

Recovered footnote 1:

```text
1 Compared to V1, this draft includes better baselines, experiments on GLUE,
and more on adapter latency. While GPT-3 175B achieves non-trivial performance
with few-shot learning, fine-tuning boosts its performance significantly as
shown in Appendix A.
```

Evidence:

```text
lead_fragment_index=4
tail_fragment_index=3
lead_fragment=Compared to V1, ... fine-tuning boosts its perfor-
tail_fragment=1 mance significantly as shown in Appendix A.
reason=same_page_bottom_footnote_fragments,hyphenated_lead_fragment,numeric_tail_fragment_continues_hyphenated_word
action=html_recovery_preserve_original_fragments
evidence=pages/page_1.png
```

The final HTML contains a `docling-footnote-recovery` block with the recovered
display text and a `<details>` trace containing the original Docling fragments.

Unresolved footnote 0:

```text
reason=isolated_numeric_marker_without_recoverable_body
action=diagnostic_only_no_recovery
```

No body text was guessed for footnote 0.

## Formula Numbering Coverage

| Sample | Formulas | Number QC | Recovered Numbers | TeX QC |
| --- | ---: | ---: | --- | ---: |
| layout-doc-ai-donut | 0 | 0 | none | 0 |
| layout-doc-ai-layoutlm | 0 | 0 | none | 0 |
| table-heavy-ai-complex-tables-gtr | 9 | 9 | none | 0 |
| table-heavy-ai-table-transformer | 1 | 1 | none | 0 |
| two-col-arxiv-ai-bert | 0 | 0 | none | 0 |
| two-col-arxiv-ai-gat | 6 | 6 | none | 0 |
| two-col-arxiv-ai-lora | 6 | 6 | none | 0 |
| two-col-arxiv-ai-rag | 3 | 3 | none | 0 |
| two-col-arxiv-ai-transformers-gnn | 20 | 20 | 6, 10, 11, 12, 13, 14, 17, 18, 20 | 1 |

Transformers-GNN formula 12 remains the only fresh sample formula with the
bare-alignment-marker TeX safety diagnostic. Its MathJax display TeX is
sanitized, and its raw TeX is preserved.

## Second Pass Apply-All Report

| Sample | Reviewed | Enhanced | Evidence Only | `formulas.tex` |
| --- | ---: | ---: | ---: | --- |
| layout-doc-ai-donut | 0 | 0 | 0 | no formulas |
| layout-doc-ai-layoutlm | 0 | 0 | 0 | no formulas |
| table-heavy-ai-complex-tables-gtr | 9 | 0 | 9 | yes |
| table-heavy-ai-table-transformer | 1 | 0 | 1 | yes |
| two-col-arxiv-ai-bert | 0 | 0 | 0 | no formulas |
| two-col-arxiv-ai-gat | 6 | 0 | 6 | yes |
| two-col-arxiv-ai-lora | 6 | 0 | 6 | yes |
| two-col-arxiv-ai-rag | 3 | 0 | 3 | yes |
| two-col-arxiv-ai-transformers-gnn | 20 | 9 | 11 | yes |

Each `formulas.tex` file contains raw LaTeX-like source. When display TeX is
sanitized, the sidecar records both raw and display forms for editing.

## Header/Footer QC Report

| Sample | Header/Footer QC | Footnote QC | Pages | Two-Column Candidate Pages |
| --- | ---: | ---: | ---: | ---: |
| layout-doc-ai-donut | 55 | 18 | 29 | 1 |
| layout-doc-ai-layoutlm | 1 | 10 | 9 | 7 |
| table-heavy-ai-complex-tables-gtr | 1 | 11 | 11 | 10 |
| table-heavy-ai-table-transformer | 11 | 1 | 10 | 6 |
| two-col-arxiv-ai-bert | 2 | 17 | 16 | 16 |
| two-col-arxiv-ai-gat | 25 | 2 | 12 | 2 |
| two-col-arxiv-ai-lora | 27 | 11 | 26 | 8 |
| two-col-arxiv-ai-rag | 19 | 3 | 19 | 7 |
| two-col-arxiv-ai-transformers-gnn | 10 | 1 | 9 | 2 |

Header/footer QC is conservative. It flags Docling-labeled page-edge nodes,
page numbers, arXiv/conference/template text, repeated page-edge text, and
rotated margin headers. It does not delete or reorder body content.

## CN Regression

Accepted CN output remained unchanged:

```text
.runtime/review/docling-adapter-html-polish-live-fullfallback-2026-06-04/CN/document.html
sha256=6911693bd781c628da70ae2494471f2f4cfd28448000aa599290353cd6af97db
```

## Files Modified

```text
docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py
docs/integrations/docling-serve-quality-parity/test_quality_parity_adapter.py
docs/integrations/docling-serve-quality-parity/all_testpdf_qc_report_2026_06_05.md
```

## Blocked / Unresolved

- LoRA footnote 0 remains evidence-only because there is no recoverable body
  text in the returned page-1 footnote fragments.
- Runtime page/formula image files are still not generated when the active
  Python runtime lacks `pypdfium2`; the adapter records bbox/page metadata and
  expected evidence links.
- Most missing formula numbers in the non-GNN samples cannot be recovered
  safely from structural formula text, so they remain evidence-only.

## Next Recommendation

Use the new metadata fields as the QC contract for downstream review:

```text
first_page_footnote_recovery_diagnostics
first_page_footnote_recovery_applied
formula_second_pass_apply_all_review
formula_number_qc_diagnostics
formula_tex_qc_diagnostics
header_footer_qc_diagnostics
layout_qc_diagnostics
formula_latex_sources
```

The next useful improvement is enabling the review renderer dependency in the
local runtime so the existing evidence links resolve to actual page/formula
crops during batch review.
