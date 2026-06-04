# Formula Second-Pass Prototype: Final Report

Date: 2026-06-03
Scope: Minimal formula-only second-pass using Route B (VlmPipeline) as formula-candidate source, with human-reviewable evidence output.

---

## DONE

- `docs/integrations/docling-serve-quality-parity/formula_only_second_pass.py` - prototype script, review HTML writer, and all py_compile checks pass.

---

## commit

Tracked report/helper are intended for commit. Ignored `.runtime` output files, caches, and generated documents are not intended for commit.

---

## pushed

Pending final git step.

---

## changed

- **Updated file**: `docs/integrations/docling-serve-quality-parity/formula_only_second_pass.py`
- Adds `review_index.html` generation beside each second-pass output.
- Adds review evidence fields to `second_pass_summary.json`: Route A/Route B evidence links plus before/after markdown snippets.
- Adds guarded fallback sources via `--guarded-fallback-dir LABEL=DIR` plus reviewed `--guarded-fallback-eq` allowlist.
- Adds review-only candidate sources via `--review-candidate-dir LABEL=DIR`; these candidates are shown in review output but are never patched into `document.json` or `document.md`.
- Adds compact formula diagnostics and review notes for complex replacements and right-column no-match formulas.
- Adds MathJax rendering blocks for Route A text, replacement candidates, fallback candidates, and before/after markdown snippets while preserving raw LaTeX text.
- No modifications to existing adapter, n8n, worker, or pipeline code.

---

## tests

Validation runs completed on CN.pdf + 4 English formula PDFs. CN used `route-a-full=.runtime/review/docling-serve-full-dir-review-2026-06-01/CN` as a guarded fallback source for equations 5, 7, and 8. All Python files in `docs/integrations/docling-serve-quality-parity/` pass `python3 -m py_compile`. `git diff --check` is clean. Generated review HTML links were parsed and all referenced local CN evidence assets resolve.

| PDF | Route A formulas | Route B formulas | Suspicious | Replaced | No match | OK |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CN.pdf | 24 | 16 | 7 | 7 | 0 | OK |
| two-col-arxiv-ai-transformers-gnn | 20 | 20 | 0 | 0 | 0 | OK |
| two-col-arxiv-ai-gat | 6 | 6 | 0 | 0 | 0 | OK |
| two-col-arxiv-ai-lora | 6 | 6 | 0 | 0 | 0 | OK |
| two-col-arxiv-ai-rag | 3 | 3 | 0 | 0 | 0 | OK |

CN.pdf suspicious/replaced details:

| Formula | Route A problem | Detection reason | Route B candidate | Status |
| --- | --- | --- | --- | --- |
| (3) | CJK `\text{...}` tail | `contains_cjk` | `w_t = softmax([...])` | **Replaced** |
| (4) | Only `( 4 )`, no body | `number_only_missing_body` | `l_i = O(l_i) * W_l` | **Replaced** |
| (5) | repeated `\frac{\sqrt{d}}{\sqrt{d}}` hallucination | `repeated_frac_hallucination` | route-a-full guarded fallback | **Replaced** |
| (7) | Number-only `( 7 )` | `number_only_missing_body` | route-a-full guarded fallback | **Replaced** |
| (8) | Number-only `( 8 )` | `number_only_missing_body` | route-a-full guarded fallback | **Replaced** |
| (13) | CJK contamination in formula text | `contains_cjk` | `qr_i = ReLU([...])` | **Replaced** |
| (16) | `\and\and\and...` hallucination (many repeats) | `repeated_and_hallucination` | `w_i = h_i / sum_k` | **Replaced** |

---

## output_roots

All under `.runtime/` (gitignored):

- CN: `.runtime/review/docling-formula-second-pass-cn-2026-06-02/CN/`
  - `document.json` (patched)
  - `document.md` (patched)
  - `second_pass_summary.json`
  - `review_index.html`
- transformers-gnn: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-transformers-gnn/`
- gat: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-gat/`
- lora: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-lora/`
- rag: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-rag/`

---

## review_html_paths

- CN: `.runtime/review/docling-formula-second-pass-cn-2026-06-02/CN/review_index.html`
- transformers-gnn: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-transformers-gnn/review_index.html`
- gat: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-gat/review_index.html`
- lora: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-lora/review_index.html`
- rag: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-rag/review_index.html`

The CN review page includes all suspicious formulas, especially formula numbers 3, 4, 5, 7, 8, 14/equation-like `(1 4)`, and 16. Each card shows Route A text, replacement candidate/source, Route A crop/context/full-page evidence, Route B full-page evidence, before/after markdown snippets, formula diagnostics, review notes, rendered MathJax math, and raw LaTeX text.

Formula 5 now uses the guarded `route-a-full` fallback. Formulas 7 and 8 now use guarded right-column fallback candidates from `route-a-full`; these are written into the patched JSON/markdown only because they are explicitly allowlisted.

---

## best_result

CN.pdf: 7 of 7 suspicious formulas replaced when using the quality Route A output as the document backbone. Route B remains the formula source for formulas 3, 4, 14, and 16. Guarded `route-a-full` fallback is used only for reviewed equations 5, 7, and 8.

Key detection patterns that work:

- `contains_cjk`: detects CJK characters in formula text (Granite contamination).
- `number_only_missing_body`: detects formula nodes with only equation number, no body (`\([0-9]+\)`).
- `repeated_frac_hallucination`: count-based detection of 3+ repetitions of `\frac {\sqrt{d}} {\sqrt{d}}`.
- `repeated_and_hallucination`: regex detection of 3+ repetitions of `\quad \ \ a n d`.

Key matching strategies:

1. **Equation number match** (primary): `(page, eq_number)` match from Route A formula text to Route B formula text. Works for formulas (3), (4).
2. **Vertical-center proximity** (fallback): Convert Route A BOTTOMLEFT PDF coords to Route B TOPLEFT pixel space, match by vertical center within 100px threshold. Critical for formulas (13) and (16) where Route B omits equation numbers.
3. **Guarded route-a-full fallback**: explicit reviewed equation allowlist for CN formulas 5, 7, and 8.
4. **Content-prefix fallback** (markdown only): for formulas without eq_numbers in Route A text, match the markdown `$$...$$` block by the formula's first 30 characters.

English PDFs show **zero regressions**: no suspicious formulas detected, no false replacements, document structure preserved.

---

## result_5

Formula 5 is now patched from guarded `route-a-full`, not Route B. Route A quality output is an obvious hallucination with repeated `\frac{\sqrt{d}}{\sqrt{d}}`; Route B produced a plausible but wrong candidate that repeated the `p` subscript where manual review confirmed `h` was needed. The guarded fallback now uses the reviewed `route-a-full` candidate.

---

## result_7_8

Formulas 7 and 8 are now patched from guarded `route-a-full` because Route B has no matching candidates and manual review confirmed the right-column fallback candidates:

- Formula 7 candidate: `e_{q_i -> e_p} = sum_{h=1}^{N} e_{h -> p}, Q_{i,h}=1 (7)`.
- Formula 8 candidate: `el_{q_i -> c_p} = e_{q_i -> c_p} + l_{q_i} (8)`.

These candidates are now applied only under the explicit reviewed allowlist; this keeps the document backbone as Route A and avoids using route-a-full as a general replacement source.

---

## latex_rendering

`review_index.html` now loads MathJax v3 from CDN and renders Route A formula text, replacement candidates, review-only candidate attempts, and before/after markdown snippets in display math blocks. Raw LaTeX remains visible below every rendered block as the inspection fallback.

---

## english_regression

**None.** All 4 English PDFs (transformers-gnn, gat, lora, rag) show 0 suspicious formulas, 0 replacements. Route A formulas on English documents are clean and are kept intact.

---

## conclusion

The formula-only second-pass prototype is **viable** for the CN.pdf use case. Route B (VlmPipeline) provides materially better formula candidates for several targets, while guarded route-a-full fallback handles reviewed CN formulas 5, 7, and 8. English PDFs are unaffected (no regressions).

The approach has three practical constraints to document:

1. **Right-column formulas require guarded fallback**: formulas (7) and (8) are right-column equation numbers where Route B's VLM pipeline failed to detect a body. The current fallback is explicitly allowlisted and should not become broad replacement logic without more validation.
2. **Route B as formula-only source**: Route B (VlmPipeline) is not safe as full-document output; it drops right-column text on CN.pdf pages 3-4 and renders pictures unreliably. The second-pass uses Route B **only** for formula text replacement, never for layout or text.
3. **Markdown patching limitation fixed on 2026-06-04**: formulas without a clean `main_eq` but with spaced equation tags such as `( 1 6 )` are still patched via content-prefix matching, and the patched markdown now restores a single-number equation tag.

---

## next

1. **Generalize guarded fallback carefully** with more CN/right-column examples before production use.
2. **Keep equation-number restoration covered in regression** for formulas without `main_eq`; formula (16) now preserves `(16)` in patched markdown.
3. **Consider bundling local MathJax assets** if offline review is required; current review uses CDN MathJax with raw LaTeX always visible.
4. **Production integration decision**: if approved, keep Route A as the backbone and call candidate sources only for formula nodes flagged as suspicious.
5. **Do not commit** `.runtime/` outputs, model caches, or batch logs.

---

## 2026-06-04 broad regression addendum

### DONE

Ran the formula-only second pass across all 10 available local `test_pdfs` samples represented in the existing full-dir review outputs. This broadened validation covers:

- CN formula-quality case (`CN.pdf`)
- English two-column papers (`gat`, `rag`, `lora`, `transformers-gnn`, `bert`)
- Formula-heavy English samples (`transformers-gnn`, `complex-tables-gtr`)
- Table-heavy samples (`table-transformer`, `complex-tables-gtr`)
- Image/layout-heavy samples (`donut`, `layoutlm`)

### output_roots

All outputs are ignored `.runtime` files:

- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/CN/`
- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/table-heavy-ai-table-transformer/`
- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/table-heavy-ai-complex-tables-gtr/`
- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/layout-doc-ai-layoutlm/`
- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/layout-doc-ai-donut/`
- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/two-col-arxiv-ai-gat/`
- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/two-col-arxiv-ai-rag/`
- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/two-col-arxiv-ai-lora/`
- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/two-col-arxiv-ai-transformers-gnn/`
- `.runtime/review/docling-formula-second-pass-regression-2026-06-04/two-col-arxiv-ai-bert/`

Each output root contains `document.json`, `document.md`, `second_pass_summary.json`, and `review_index.html`.

### regression_summary

| PDF/sample | Route A formulas | Route B formulas | Suspicious | Replaced | No match | Review links missing |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CN | 24 | 16 | 7 | 7 | 0 | 0 |
| table-heavy-ai-table-transformer | 1 | 1 | 0 | 0 | 0 | 0 |
| table-heavy-ai-complex-tables-gtr | 9 | 9 | 0 | 0 | 0 | 0 |
| layout-doc-ai-layoutlm | 0 | 0 | 0 | 0 | 0 | 0 |
| layout-doc-ai-donut | 0 | 0 | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-gat | 6 | 6 | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-rag | 3 | 3 | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-lora | 6 | 6 | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-transformers-gnn | 20 | 20 | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-bert | 0 | 0 | 0 | 0 | 0 | 0 |

Totals: 10 samples, 69 Route A formulas, 61 Route B formulas, 7 suspicious formulas, 7 replacements, 0 no-match cases, 0 missing generated review links.

CN replacement sources:

- Route B: formulas 3, 4, 14, and 16.
- Guarded `route-a-full` fallback: formulas 5, 7, and 8.

### artifact_presence

The second pass itself preserves the Route A document backbone and does not generate table/image assets. Regression evidence confirms the source review roots still contain table/image/page artifacts for the representative sample classes:

- CN formula-quality Route A source: 24 table files, 8 picture files, 7 page images, 48 formula crop/context images.
- VLM Route B source across the 10 samples: tables and page images are present for every sample; `artifacts/` image regions are present for every non-empty layout/image/table/two-column sample.
- Full-dir Route A English/table/layout sources contain table files where Docling emitted tables; they generally do not include page/formula/picture crops, which is expected for those pre-existing full-dir review roots.

### false_positives

No false-positive replacements were observed in the broadened regression. Every non-CN sample had:

- 0 suspicious formulas
- 0 replacements
- 0 no-match cases

This includes English two-column, formula-heavy, table-heavy, and image/layout-heavy samples.

### remaining_blockers

- Guarded fallback remains manual/allowlisted for CN formulas 5, 7, and 8. It should not become broad fallback logic without more right-column examples.
- MathJax in `review_index.html` still loads from CDN; raw LaTeX remains visible offline.

---

## 2026-06-04 equation-number restoration addendum

### DONE

Fixed markdown equation-number restoration for replacements whose Route A formula text has no clean `main_eq` but does contain a spaced equation number, such as CN formula 16's `( 1 6 )`.

### formula_16

CN formula 16 now patches markdown from the repeated `and` hallucination to:

```text
$$w _ { i } = \frac { h _ { i } } { \sum _ { k = 1 } ^ { h _ { i } } } \quad i \in [ 1 , t ) \cap \in \mathbb { N } \quad ( 16 )$$
```

Formula 14 also benefits from the same restoration path, changing the split `( 1 4 )` tag to `( 14 )` in patched markdown. Guarded fallback behavior remains unchanged and allowlisted only for CN formulas 5, 7, and 8.

---

## 2026-06-04 adapter optional post-step addendum

### DONE

Wired `formula_only_second_pass.py` into `quality_parity_adapter.py` as an optional post-processing step. The default is `--formula-second-pass-policy off`.

### integration_shape

- `off`: no formula second pass.
- `review`: run the second pass into a sidecar evidence directory without changing adapter contract files.
- `apply`: run the same sidecar evidence step, then replace only `document.md` and `document.json` with patched formula text.

Route A remains the document backbone. Route B is accepted only through `--formula-second-pass-route-b-dir` and is used only as a formula candidate source. Guarded fallback sources are accepted only through `--formula-second-pass-guarded-fallback-dir`, and only equations explicitly passed with `--formula-second-pass-guarded-fallback-eq` may use them.

Default sidecar:

```text
<adapter-output>/<job-id>/formula_second_pass/
  document.md
  document.json
  second_pass_summary.json
  review_index.html
```

### regression_summary

Validation used copied adapter-produced Route A outputs under ignored `.runtime/review/docling-adapter-second-pass-integration-2026-06-04/`, then invoked the adapter post-step in `apply` mode against all 10 broad regression samples.

| PDF/sample | Suspicious | Replaced | No match | Review links missing |
| --- | ---: | ---: | ---: | ---: |
| CN | 7 | 7 | 0 | 0 |
| table-heavy-ai-table-transformer | 0 | 0 | 0 | 0 |
| table-heavy-ai-complex-tables-gtr | 0 | 0 | 0 | 0 |
| layout-doc-ai-layoutlm | 0 | 0 | 0 | 0 |
| layout-doc-ai-donut | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-gat | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-rag | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-lora | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-transformers-gnn | 0 | 0 | 0 | 0 |
| two-col-arxiv-ai-bert | 0 | 0 | 0 | 0 |

Totals: 10 samples, 7 suspicious formulas, 7 replacements, 0 no-match cases, 0 missing sidecar review links, and 0 non-CN false-positive replacements.

Live adapter CLI probe was attempted, but Docling Server returned HTTP 503 from `/version`; per AGENTS.md, no service start/restart was performed during this task.

---

## 2026-06-04 live HTML apply regression

### diagnosis

The optional second-pass `apply` mode copied patched `document.md` and
`document.json` from the sidecar output, but left `document.html` as the original
Route A HTML. The corrected formula text was visible in
`formula_second_pass/review_index.html`, `formula_second_pass/document.md`,
`formula_second_pass/document.json`, final `document.md`, and final
`document.json`, but not in final `document.html` for CN formulas 3, 4, 5, 7,
and 16. Formulas 8 and 14 appeared partly visible only because their corrected
text overlapped enough with the original Route A HTML to make spot checks
misleading.

### fix

`quality_parity_adapter.py` now patches affected formula blocks in
`document.html` during `--formula-second-pass-policy apply`. Each patched block
contains rendered MathJax display math, traceable raw TeX, plus links to the
adapter formula evidence and the second-pass review page. The final HTML display
text is sourced from the patched markdown body so restored equation numbers are
preserved. The HTML quality gate fails the adapter result if any reported
replacement is not visible in decoded `document.html` with a traceable formula
marker and display wrapper.

### validation

Fresh live CN output:

```text
.runtime/review/docling-adapter-html-patch-live-2026-06-04/CN/
```

CN apply result after the fix:

- suspicious: 7
- replaced: 7
- no match: 0
- HTML patched indexes: 16, 14, 8, 7, 5, 4, 3
- HTML gate missing replacements: none

For CN formulas 3, 4, 5, 7, 8, 14, and 16, the corrected formula text is now
present in:

- second-pass review evidence
- `formula_second_pass/document.md`
- `formula_second_pass/document.json`
- final `document.md`
- final `document.json`
- final `document.html`
