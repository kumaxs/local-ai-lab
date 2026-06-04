# English HTML Review Findings - 2026-06-04

Fresh output root:

```text
.runtime/review/docling-adapter-english-review-fixes-2026-06-04
```

Documents:

```text
.runtime/review/docling-adapter-english-review-fixes-2026-06-04/two-col-arxiv-ai-lora/document.html
.runtime/review/docling-adapter-english-review-fixes-2026-06-04/two-col-arxiv-ai-transformers-gnn/document.html
```

## Fixed In Adapter

- Plain visible URLs are now autolinked in `document.html`.
  - LoRA: `html_plain_url_autolink_count=24`; the visible
    `https://github.com/microsoft/LoRA` text is now an `<a href=...>`.
  - Transformers-GNN: `html_plain_url_autolink_count=2`.
- PDF annotation link diagnostics are written to `links.json`.
  - LoRA: `pdf_annotation_link_count=404`, `pdf_uri_link_count=36`,
    `pdf_goto_link_count=405`, while `json_hyperlink_count=0`.
  - Transformers-GNN: `pdf_annotation_link_count=136`, `pdf_uri_link_count=9`,
    `pdf_goto_link_count=131`, while `json_hyperlink_count=1`.
- English HTML now gets a math-aware CSS font stack for MathML and
  math-heavy paragraphs to reduce boxed-symbol rendering risk.
  - Fresh outputs contain `boxed_math_symbol_count=0`.
- Safe footnote marker polish is applied where the marker is explicit.
  - LoRA: `footnote_superscript_polish_count=3`.
- Suspicious footnotes are now diagnosed with page evidence paths.
  - LoRA page 1 still includes the split nodes `0`, `1 mance...`, and the
    hyphenated continuation, now reported as `suspicious_footnote:*`.

## Still Present, Not Adapter-Safe To Repair

- LoRA page-1 footnote reading order mismatch remains in `document.json`.
  The split/misordered footnote content is already present in Docling Server
  JSON before HTML serialization, so a general adapter rewrite would be a
  broad parser replacement.
- LoRA page-6 formula numbers `(15)` and `(16)` remain absent from
  `document.html`, `document.md`, and `document.json`.
  Fresh diagnostics record:

```text
missing_lora_page6_formula_numbers=[15, 16]
```

  The returned document has no page-6 formula nodes for those inline/display
  equations, so restoring them requires Docling/Serve extraction changes or a
  targeted formula recovery pass from source-page evidence.

## CN Check

The accepted CN review output path remains unchanged:

```text
.runtime/review/docling-adapter-html-polish-live-fullfallback-2026-06-04/CN/document.html
```
