# Docling Serve Review Quality Regressions

Date: 2026-06-01

Review output root:

```text
/Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-serve-full-dir-review-2026-06-01
```

Source PDF directory:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs
```

## Summary

The full-directory Docling Serve parity review produced 10 inspectable HTML
outputs, 69 formula nodes, 0 formula placeholders, 62 tables, and 119 embedded
image refs. Manual review found four remaining quality blockers. The most
important blocker is Chinese parity: the current Serve adapter did not
reproduce the old known-good local `services/docling-service` OCR fallback path.

Critical correction: `CN.pdf` must be treated as a whole-document bad text
layer / font mapping / parser problem. The `/Gxx` symbols are distributed across
every page, not isolated to a bad page. The current Serve 503/504 result proves
only that the current Serve request/configuration/execution shape failed to
reproduce the old known-good path. It does not prove that Docling Serve cannot
produce high-quality Chinese output.

| Issue | Evidence | Classification | Blocks n8n? | Minimal next move |
| --- | --- | --- | --- | --- |
| Chinese support poor | `CN/metadata.json`: `/Gxx=42333`, `ocr_fallback_used=false`; page text-node `/Gxx` counts are nonzero on pages 1-7 | Adapter/Serve OCR configuration parity gap plus full-document execution-shape failure | Yes | Reproduce old `ocrmac` full-page Chinese OCR config through Serve, then fall back to all-page chunks if full-document forced OCR still 503/504s. |
| Footnotes broken | `two-col-arxiv-ai-lora/document.json` text nodes 14-17 already split/misordered on page 1 | Docling extraction/reading-order limitation, not HTML-only | Yes | Add diagnostics and page/source evidence links; avoid broad string repair unless provenance makes it reliable. |
| PDF links missing | Source PDF has `/Subtype /Link`, `/URI`, and `/GoTo`; Serve JSON has `hyperlink: None` for all 777 text nodes; HTML has zero `href=` | Docling extraction/export propagation limitation plus adapter omission for safe plain URL autolinking | Yes | Add URL/autolink diagnostics and side-channel PDF annotation extraction; only insert links when matching is reliable. |
| Math symbols/equation numbers | LoRA page 6 math is inline text, no page-6 formula nodes; no literal replacement/tofu chars; `(15)`/`(16)` absent | HTML font/rendering risk plus Docling inline-formula/equation-number extraction loss | Yes | Add math font/CSS/MathJax where useful and source page/context evidence for math-heavy pages. |

## External Fact-Check Notes

Official Docling documentation and local installed package inspection both
confirm that full-page OCR is supported. The official full-page OCR example
shows `OcrMacOptions(force_full_page_ocr=True)` as one supported backend option,
alongside EasyOCR, Tesseract, and RapidOCR, and notes that full-page OCR
processes each page purely via OCR and is often slower than hybrid detection.

Official RapidOCR documentation also shows explicit local model path control via
`RapidOcrOptions(det_model_path=..., rec_model_path=..., cls_model_path=...)`,
which matters if we later need an offline/local Chinese OCR alternative to
macOS Vision OCR.

The installed Docling Serve 1.20.0 / Docling 2.95.0 stack exposes runtime knobs
for local serving, OCR presets, custom OCR configs, page ranges, document
timeouts, queue/batch sizes, and local model artifacts. Therefore, one
full-document `503` cannot be treated as a capability conclusion.

Relevant upstream signals:

- Docling issue `#748` reports Chinese PDF garbling and discusses the class of
  problems around Chinese OCR language settings, OCR forcing, parser/backend
  behavior, and scanned-vs-programmatic PDF differences.
- Docling issue `#828` reports hyperlinks not being identified from PDFs. This
  aligns with our finding that link data may exist in PDF annotations while
  DoclingDocument/export layers do not propagate usable hyperlinks.

## Chinese Parity Investigation

### Old Known-Good Local Path

Evidence paths:

```text
services/docling-service/docling_service/docling_adapter.py
services/docling-service/reports/2026-05-24-formula-quality-validation.md
services/docling-service/reports/2026-05-25-docling-fallback-closeout.md
services/docling-service/reports/samples/formula-quality/CN/metadata.json
services/docling-service/reports/samples/formula-quality/CN/status.json
```

The previous local `services/docling-service` implementation used Docling's
Python API directly. For the Chinese fallback profile, the code built
`PdfPipelineOptions` with:

```text
accelerator_options: AcceleratorOptions(device="cpu")
do_ocr: True
ocr_options: OcrMacOptions(
  lang=["zh-Hans", "zh-Hant", "en-US"],
  force_full_page_ocr=True
)
do_table_structure: True
table_structure_options: TableStructureOptions(
  do_cell_matching=True,
  mode=TableFormerMode.ACCURATE
)
do_formula_enrichment: formula_model is not None
generate_page_images: True
generate_picture_images: True
generate_table_images: True
images_scale: 3.0
artifacts_path: /Users/zeyuan/.cache/docling/models
```

The adapter tried `ocr_fallback_mac` first, then `ocr_fallback_auto` if needed.
The known-good CN sample recorded:

```text
ocr_fallback_used: true
text_quality_gxx_count: 0
text_quality_gxx_density: 0.0
table_count: 6
asset_count: 77
warnings include: ocr_fallback_mac_used_after_gxx_quality_failure
```

The 2026-05-25 closeout reported `CN.pdf` runtime `74.894s`,
`ocr_fallback_used=true`, `/Gxx=0 / 0.0`, `formula_model=granite_docling_mlx`,
0 placeholders, 24 formula crops/context crops, 6 table crops, 77 assets, and
0 broken local refs.

### Current Serve Adapter Path

Evidence paths:

```text
docs/integrations/docling-serve-quality-parity/quality_parity_adapter.py
docs/2026-06-01-docling-serve-quality-parity-adapter.md
.runtime/review/docling-serve-full-dir-review-2026-06-01/CN/metadata.json
.runtime/review/docling-serve-full-dir-review-2026-06-01/CN/status.json
.runtime/review/docling-serve-full-dir-review-2026-06-01/CN.adapter_stderr.txt
.runtime/review/docling-serve-full-dir-review-2026-06-01/CN.retry2_stderr.txt
.runtime/review/docling-serve-full-dir-review-2026-06-01/run_summary.json
```

The current Serve adapter starts from:

```text
UVICORN_WORKERS=1
DOCLING_DEVICE=cpu
DOCLING_SERVE_ALLOW_CUSTOM_CODE_FORMULA_CONFIG=true
DOCLING_SERVE_ENG_KIND=local
DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1
DOCLING_SERVE_ENG_LOC_SHARE_MODELS=true
DOCLING_SERVE_ARTIFACTS_PATH=/Users/zeyuan/.cache/docling/models
DOCLING_SERVE_LOAD_MODELS_AT_BOOT=true
DOCLING_SERVE_OPTIONS_CACHE_SIZE=2
```

The request options are:

```text
from_formats: ["pdf"]
to_formats: ["md", "json", "html"]
image_export_mode: embedded
do_ocr: True
force_ocr: False on first pass, True on fallback
ocr_preset: "auto"
ocr_custom_config: not set
ocr_lang: not set
do_table_structure: True
table_mode: accurate
table_cell_matching: True
include_images: True
images_scale: 2.0
do_formula_enrichment: True
code_formula_custom_config: Granite MLX custom config
page_range: only when explicitly requested
```

The final retained CN output was created with `ocr_fallback_policy=off` after
the full-document forced OCR retry failed. It is diagnostic only:

```text
ok: true
success_class: degraded_success
ocr_fallback_used: false
text_quality_gxx_count: 42333
text_quality_gxx_density: 0.0038690473742381383
failure_reason: full-document OCR fallback path failed with Docling Serve HTTP 503 after retries
```

`CN.adapter_stderr.txt` and `CN.retry2_stderr.txt` show `HTTP Error 503:
Service Unavailable` on the fallback `force_ocr=true` request.

The bad text layer is whole-document. Counting `/Gxx` tokens in text nodes by
page gives:

| Page | `/Gxx` count in text nodes | Density in text nodes |
| ---: | ---: | ---: |
| 1 | 2377 | 0.188996 |
| 2 | 479 | 0.106777 |
| 3 | 573 | 0.091417 |
| 4 | 574 | 0.038013 |
| 5 | 778 | 0.140917 |
| 6 | 658 | 0.127767 |
| 7 | 4479 | 0.200888 |

The top-level `/Gxx=42333` metric is higher because the adapter measures the
combined Markdown, HTML, text, and serialized JSON payload; both metrics confirm
the same whole-document bad text-layer failure.

### Old vs Current Comparison

| Dimension | Old local `services/docling-service` | Current Serve adapter |
| --- | --- | --- |
| Execution boundary | Direct Python `DocumentConverter` | HTTP `/v1/convert/source` via Docling Serve |
| Device | CPU standard pipeline | CPU standard pipeline |
| Bad text detection | `/Gxx` count/density | `/Gxx` count/density |
| OCR fallback trigger | `/Gxx` failure | `/Gxx` failure |
| OCR engine | `ocrmac` first, then `auto` | `ocr_preset="auto"` |
| OCR language | `["zh-Hans", "zh-Hant", "en-US"]` | Not set |
| Full-page OCR | `OcrMacOptions(force_full_page_ocr=True)` | Only `force_ocr=true`; no explicit engine config |
| OCR custom config | Python `OcrMacOptions` object | Not used |
| Table mode | Accurate, cell matching true | Accurate, cell matching true |
| Formula model | Granite MLX when available | Granite MLX custom config |
| Images/crops | Page, picture, table, formula/context crops | Embedded images from Serve; no source crop layer |
| CN result | `/Gxx=0`, readable | fallback-off diagnostic, `/Gxx=42333` |

### Serve Expressibility

The installed Docling Serve request model exposes the needed OCR fields:

```text
ocr_preset
ocr_custom_config
ocr_lang
force_ocr
page_range
document_timeout
```

The installed Docling jobkit parser supports `ocr_custom_config` with a required
`kind` field and passes `force_full_page_ocr=request.force_ocr` into the selected
OCR engine. The installed OCR factory registers `ocrmac`, `rapidocr`, `auto`,
`easyocr`, `tesserocr`, and `tesseract`.

Therefore, a Serve request can likely express the old macOS OCR path in one of
these shapes:

```json
{
  "force_ocr": true,
  "ocr_preset": "ocrmac",
  "ocr_lang": ["zh-Hans", "zh-Hant", "en-US"]
}
```

or, if custom config is enabled for OCR:

```json
{
  "force_ocr": true,
  "ocr_custom_config": {
    "kind": "ocrmac",
    "lang": ["zh-Hans", "zh-Hant", "en-US"],
    "recognition": "accurate",
    "framework": "vision"
  }
}
```

The second shape requires Serve startup with:

```text
DOCLING_SERVE_ALLOW_CUSTOM_OCR_CONFIG=true
```

The first shape may not require custom OCR config because `ocrmac` is a
registered OCR preset/kind. Both shapes should be validated before changing n8n.

### Future CN Parity Success Criterion

Do not accept fallback-off diagnostic output as success. A future CN parity run
passes only if:

- all seven CN pages are covered;
- OCR fallback is recorded as whole-document or per-page/per-chunk equivalent;
- final merged `/Gxx` is approximately zero and no page remains with material
  `/Gxx` density;
- no 503/504 remains unresolved;
- outputs remain contract-compatible: `document.md`, `document.html`,
  `document.json`, `metadata.json`, `status.json`, and table assets where
  available.

## Footnotes

Evidence paths:

```text
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.html
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.md
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.json
```

Observed page-1 JSON evidence:

```text
texts[14] label=footnote text="∗ Equal contribution."
texts[15] label=footnote text="0"
texts[16] label=footnote text="1 mance significantly as shown in Appendix A."
texts[17] label=footnote text="Compared to V1, this draft includes better baselines, experiments on GLUE, and more on adapter latency. While GPT-3 175B achieves non-trivial performance with few-shot learning, fine-tuning boosts its perfor-"
```

The matching HTML is a direct paragraph rendering of the same split/misordered
nodes. The error is already present in Docling JSON, not introduced only by
Markdown or HTML serialization. In-body footnote markers are plain text inside
paragraph content; the returned JSON does not preserve reliable
superscript/subscript marker semantics for this case.

Classification:

```text
Docling extraction/reading-order limitation.
```

Minimal workaround:

1. Add diagnostics for isolated numeric footnote nodes, hyphenated split
   continuations, and bottom-of-page footnote candidates.
2. Link suspicious footnote warnings to page/source evidence once page images
   or source crops are restored.
3. Restore `<sup>` only where a marker can be matched to a body with high
   confidence; otherwise warn instead of guessing.

## Links

Evidence paths:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/two-col-arxiv-ai-lora.pdf
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.json
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.html
```

Raw source PDF inspection found:

```text
/Subtype /Link: 404
/URI: 72
/GoTo: 406
https://github.com/microsoft/LoRA: 1
Hfootnote.1: 2
appendix.A: 3
```

Docling Serve output evidence:

- `document.json` contains `hyperlink` fields, but all 777 text nodes have
  `hyperlink: None`.
- `document.html` contains zero `href=` attributes.
- The missing links are not merely a browser rendering issue; link information
  is not present in the returned DoclingDocument text nodes.

Classification:

```text
Docling extraction/export propagation limitation plus adapter omission for safe
plain URL autolinking.
```

Minimal workaround:

1. Produce link diagnostics: `pdf_annotation_link_count`, `json_link_count`,
   `html_href_count`, and `plain_url_count`.
2. Generate a side-channel `links.json` from PDF annotations using an existing
   local dependency if available.
3. Regex-link obvious `http(s)`, DOI, arXiv, and ORCID text only when the exact
   visible string exists in HTML.
4. Do not reconstruct internal `GoTo` section/citation/footnote links unless
   text and bounding boxes can be matched reliably.

## Math Symbols And Equation Numbers

Evidence paths:

```text
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.html
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.md
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.json
```

Observed facts:

- Page 6 has no `label=formula` nodes in Docling JSON.
- Page 6 math appears as inline text, for example:

```text
| Θ | = d model × ( l p + l i )
| Θ | = L × d model × ( l p + l i )
| Θ | = 2 × ˆ L LoRA × d model × r
```

- No literal replacement/tofu characters were found in HTML, Markdown, or JSON
  for `□`, `�`, `▯`, `◻`, `☐`, or `■`.
- The output contains mathematical Unicode characters such as `𝑊`, `𝑟`, `𝑑`,
  `Θ`, `∆`, `Φ`, and `ℝ`, which can render as square boxes if the browser/font
  stack lacks coverage.
- The parenthesized equation numbers `(15)` and `(16)` are absent from
  `document.html`, `document.md`, and `document.json`.

Classification:

```text
HTML rendering/font risk for square boxes, plus Docling inline-formula and
equation-number extraction loss.
```

Minimal workaround:

1. Add a math-aware CSS font stack and MathJax only where LaTeX/MathML source is
   present.
2. Warn when a page has math-heavy Unicode text but no formula nodes.
3. Add source page/context evidence links for math-heavy pages.
4. Do not claim equation-number recovery unless the number exists in Docling
   JSON or can be recovered from a source image/annotation layer.

## Blocking Assessment

All four issues block n8n integration:

1. Chinese is the first-priority blocker because the current CN output is known
   bad and the old local path proves better output is possible.
2. Footnotes are a structural blocker because the JSON reading order is wrong
   before HTML export.
3. Links are a review/navigation blocker because PDF article links disappear.
4. Math rendering and inline formula loss are review blockers because humans and
   downstream AI cannot verify the missing symbols/numbers without source
   evidence.

## Minimal Fix Plan

Priority order:

1. **Reproduce old CN OCR config through Serve.** Add a targeted adapter option
   or request shape for `ocr_preset="ocrmac"` plus
   `ocr_lang=["zh-Hans", "zh-Hant", "en-US"]`; if needed, start Serve with
   `DOCLING_SERVE_ALLOW_CUSTOM_OCR_CONFIG=true` and use
   `ocr_custom_config.kind="ocrmac"`.
2. **Fail closed on required OCR fallback failure.** If `/Gxx` fails and the
   required fallback fails, write `failure` or `degraded_failure`, not
   `degraded_success`.
3. **Cover all CN pages with bounded fallback.** If full-document forced OCR
   still returns 503/504, retry all pages serially or in small `page_range`
   chunks with the same OCRMac full-page semantics, then aggregate outputs.
4. **Tune Serve execution shape before judging quality.** Try smaller OCR/layout
   batch sizes, longer request/document timeout, and readiness/backpressure
   waits between first pass and fallback.
5. **Add low-risk review HTML improvements.** Add safe external URL autolinking
   and math font/CSS/MathJax support where it cannot corrupt extraction data.
6. **Add diagnostics and source evidence.** Record suspicious footnotes,
   all-null hyperlinks despite source annotations, math-heavy pages without
   formula nodes, and missing page/source evidence links.
7. **Investigate PDF annotation sidecar.** Extract `links.json` from source PDF
   annotations with existing local dependencies only; defer internal link
   insertion until text/bbox matching is reliable.

## Recommended Next Codex Task

Implement only the low-risk adapter changes needed for targeted validation:

- add a CN parity OCR request path using `ocrmac` with Chinese locale and
  full-page OCR semantics;
- fail closed when required OCR fallback fails;
- add all-page page/chunk fallback for CN if full-document OCR 503/504s;
- add diagnostics for links, footnotes, and math-heavy pages;
- add safe external URL autolinking and math CSS/MathJax polish;
- regenerate a targeted review set for `CN.pdf` and
  `two-col-arxiv-ai-lora.pdf`.

Do not proceed to n8n until the targeted set passes manual inspection.
