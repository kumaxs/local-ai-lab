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

The full-directory Docling Serve parity review produced inspectable files, but
manual review found four issues that are real n8n integration blockers. The
main pattern is that Docling Serve can execute extraction, but the returned
document does not yet preserve enough quality semantics for production intake.

| Issue | Evidence | Classification | Blocks n8n? | Minimal next move |
| --- | --- | --- | --- | --- |
| Chinese support poor | `CN/metadata.json`: `/Gxx=42333`, `ocr_fallback_used=false`; `CN/status.json` warns fallback-off diagnostic only | Serve pressure/fallback-policy gap plus bad text-layer quality failure | Yes | Do not accept CN fallback-off output; implement bounded page/chunk OCR fallback and fail closed when fallback fails. |
| Footnotes broken | `two-col-arxiv-ai-lora/document.json` text nodes 14-17 already split/misordered on page 1 | Docling extraction/reading-order limitation, not HTML-only | Yes | Add diagnostics/warnings and source page evidence links; treat structural footnotes as unreliable until Docling config/upstream fix is found. |
| PDF links missing | Source PDF contains `/Subtype /Link` annotations; Docling JSON has `hyperlink: None` for all 777 text nodes; HTML has zero `href=` | Docling extraction limitation plus adapter omission for plain URL autolinking | Yes | Add safe URL autolinking for plain URLs; separately evaluate PDF annotation sidecar extraction for internal article links. |
| Math symbols/equation numbers | LoRA page 6 has inline math in text nodes, no page-6 formula nodes; no literal tofu chars in HTML/JSON; equation numbers `(15)`/`(16)` absent | Mixed HTML font/rendering issue and Docling inline-formula extraction limitation | Yes | Add HTML MathJax/font/CSS and source page links; do not claim equation-number recovery unless JSON/source evidence exists. |

## Issue 1: Chinese OCR Fallback

Evidence paths:

```text
.runtime/review/docling-serve-full-dir-review-2026-06-01/CN/metadata.json
.runtime/review/docling-serve-full-dir-review-2026-06-01/CN/status.json
.runtime/review/docling-serve-full-dir-review-2026-06-01/CN.adapter_stderr.txt
.runtime/review/docling-serve-full-dir-review-2026-06-01/CN.retry2_stderr.txt
.runtime/review/docling-serve-full-dir-review-2026-06-01/CN.fallback_off_stderr.txt
```

Observed facts:

- Final inspectable `CN/` output was generated with `ocr_fallback_policy=off`.
- `metadata.json` records `text_quality_gxx_count=42333` and
  `text_quality_gxx_density=0.0038690473742381383`.
- `status.json` explicitly warns: full-document OCR fallback failed with
  Docling Serve HTTP 503 after retries, so the current CN output is diagnostic
  only and not Chinese quality success.
- The default full-document path failed in the second request, after the first
  non-forced conversion detected the bad text layer and attempted
  `force_ocr=true`.
- Later adapter code now retries transient 503/504, but the captured CN retry
  still ended with HTTP 503 in `CN.retry2_stderr.txt`.

Classification:

```text
Serve pressure/fallback-policy gap plus bad text-layer quality failure.
```

This is not evidence that OCR quality is inherently bad. Earlier bounded page
validation showed the OCR direction can improve `/Gxx`; the blocker here is
full-document fallback reliability through Serve. The safe next experiment is
page-range or chunked OCR fallback, not another unbounded full-document retry.

Minimal fix options:

1. Fail closed for CN: if `/Gxx` fails and OCR fallback fails, write failure or
   `degraded_failure`, not `degraded_success`.
2. Add chunked OCR fallback: after a full-document bad text-layer result,
   retry pages in bounded ranges with `force_ocr=true`, then aggregate outputs.
3. Add Serve readiness/backpressure checks between first pass and fallback.
4. Keep a fallback-off diagnostic output only as a review artifact, never as a
   successful intake result.

## Issue 2: Footnotes

Evidence paths:

```text
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.html
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.md
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.json
```

Observed JSON evidence on page 1:

```text
texts[14] label=footnote text="∗ Equal contribution."
texts[15] label=footnote text="0"
texts[16] label=footnote text="1 mance significantly as shown in Appendix A."
texts[17] label=footnote text="Compared to V1, this draft includes better baselines, experiments on GLUE, and more on adapter latency. While GPT-3 175B achieves non-trivial performance with few-shot learning, fine-tuning boosts its perfor-"
```

Observed HTML evidence:

```html
<p>∗ Equal contribution.</p>
<p>0</p>
<p>1 mance significantly as shown in Appendix A.</p>
<p>Compared to V1, this draft includes better baselines, experiments on GLUE, and more on adapter latency. While GPT-3 175B achieves non-trivial performance with few-shot learning, fine-tuning boosts its perfor-</p>
```

The mismatch already exists in `document.json`; it is not introduced solely by
Markdown or HTML serialization. The in-body footnote marker is also plain text
inside the preceding paragraph, not represented as superscript/subscript
metadata in the returned JSON.

Classification:

```text
Docling extraction/reading-order limitation.
```

Minimal fix options:

1. Add adapter diagnostics for suspicious footnotes: isolated numeric footnote
   nodes, hyphenated split continuations, and footnote text near page bottom.
2. For human review, link affected footnote warnings to page images or page
   evidence once the adapter has a page-image/source-evidence layer.
3. Do not attempt broad footnote repair by string heuristics yet; it is fragile
   and can corrupt valid papers.
4. Re-evaluate if Docling Serve exposes a footnote/link/order option in a later
   targeted test.

## Issue 3: Missing Article Links

Evidence paths:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/two-col-arxiv-ai-lora.pdf
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.json
.runtime/review/docling-serve-full-dir-review-2026-06-01/two-col-arxiv-ai-lora/document.html
```

Source PDF evidence from raw inspection:

```text
/Annots [...]
<< /Type /Annot /Subtype /Link
/A << /Type /Action /S /URI /URI (https://github.com/microsoft/LoRA) >>
<< /Type /Annot /Subtype /Link /A << /D (Hfootnote.1) /S /GoTo >>
<< /Type /Annot /Subtype /Link /A << /D (appendix.A) /S /GoTo >>
```

Docling output evidence:

- `document.json` contains `hyperlink` fields, but all 777 text nodes have
  `hyperlink: None`.
- `document.html` contains zero `href=` attributes.
- `document.html` still contains plain text URLs such as
  `https://github.com/microsoft/LoRA`, so some external URL text is present but
  not clickable.

Classification:

```text
Docling extraction limitation for PDF annotations, plus adapter omission for
safe plain-URL autolinking.
```

Minimal fix options:

1. Low-risk adapter fix: auto-link plain `http://` and `https://` strings in
   generated HTML text nodes.
2. Add `link_count`, `plain_url_count`, and `pdf_annotation_link_count` quality
   signals where possible.
3. For internal citation/section/footnote links, investigate a PDF annotation
   sidecar using an existing dependency. Do not fake these links from text.
4. If Docling Serve later exposes a supported link-preservation option, prefer
   that over sidecar reconstruction.

## Issue 4: Math Symbols And Equation Numbers

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

- No literal replacement/tofu characters were found in JSON or HTML for
  `□`, `�`, `▯`, `◻`, `☐`, or `■`.
- The output contains mathematical Unicode characters such as `𝑊`, `𝑟`, `𝑑`,
  `Θ`, `∆`, `Φ`, and `ℝ`, which can render as square boxes if the browser/font
  stack lacks coverage.
- The strings `(15)` and `(16)` were not found in `document.html`,
  `document.md`, or `document.json` as equation numbers. The only relevant
  `15`/`16` occurrences in JSON are table numbers, page footers, reference
  years/pages, or table text.

Classification:

```text
HTML rendering/font/MathJax issue for square boxes, and Docling inline-formula
extraction limitation for missing equation numbers.
```

Minimal fix options:

1. Add HTML-level math rendering support: MathJax for MathML/LaTeX and a CSS
   font stack with broad math Unicode coverage.
2. Add warnings when a page has math-heavy Unicode text but no formula nodes.
3. Restore source evidence links/page images for math-heavy pages so humans can
   verify lost inline formulas and equation numbers.
4. Do not claim equation-number recovery unless the number is present in
   Docling JSON or recovered from a source image/annotation layer.

## Blocking Assessment

All four issues block n8n integration as a production-quality parser path:

1. Chinese is a hard blocker because the current CN output is explicitly not a
   quality success.
2. Footnotes are a blocker because the structured reading order is wrong in
   JSON before HTML export.
3. Links are a blocker because source PDF navigation/reference information is
   dropped.
4. Math rendering/inline formula loss is a blocker because HTML is not reliable
   for human verification or AI reading without source evidence.

## Minimal Fix Plan

Priority order:

1. **Fail closed on quality fallback failures.** Change adapter status semantics
   so a failed required OCR fallback is not reported as `ok=true`; keep
   fallback-off output only as a diagnostic artifact.
2. **Implement bounded CN OCR fallback.** Retry forced OCR by page or small page
   chunks after `/Gxx` failure. Aggregate only if all chunks succeed; otherwise
   report exact failed pages.
3. **Add low-risk HTML output polish.** Auto-link plain external URLs and add
   MathJax/CSS/font support. This improves reviewability without changing
   extraction data.
4. **Add quality diagnostics.** Record suspicious footnotes, all-null hyperlink
   fields, math-heavy pages without formula nodes, and missing source-evidence
   links in `status.json`.
5. **Restore source evidence links.** Reintroduce page image/context links for
   footnotes and math-heavy pages before n8n integration.
6. **Investigate PDF annotation sidecar.** Use an existing local PDF dependency
   only if it can extract URI/GoTo annotations reliably without global installs.

## Recommended Next Codex Task

Implement the low-risk adapter changes only:

- fail closed when OCR fallback is required but fails;
- add bounded page/chunk OCR fallback for CN;
- auto-link plain external URLs in `document.html`;
- add MathJax/CSS/font support to generated HTML;
- add diagnostic warning fields for suspicious footnotes, all-null hyperlinks,
  and math-heavy pages without formula nodes;
- regenerate a small targeted review set: `CN.pdf` and
  `two-col-arxiv-ai-lora.pdf`.

Do not proceed to n8n until this targeted set passes manual inspection.
