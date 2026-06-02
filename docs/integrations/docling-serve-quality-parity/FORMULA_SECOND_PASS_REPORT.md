# Formula Second-Pass Prototype: Final Report

Date: 2026-06-02
Scope: Minimal formula-only second-pass using Route B (VlmPipeline) as formula-candidate source.

---

## DONE

- `docs/integrations/docling-serve-quality-parity/formula_only_second_pass.py` - prototype script, all py_compile checks pass.

---

## commit

Tracked report/helper are intended for commit. Ignored `.runtime` output files, caches, and generated documents are not intended for commit.

---

## pushed

Pending final git step.

---

## changed

- **New file**: `docs/integrations/docling-serve-quality-parity/formula_only_second_pass.py`
- All other files: unchanged (no modifications to existing adapter, n8n, worker, or pipeline code).

---

## tests

Validation runs completed on CN.pdf + 4 English formula PDFs. All Python files in `docs/integrations/docling-serve-quality-parity/` pass `python3 -m py_compile`. `git diff --check` is clean.

| PDF | Route A formulas | Route B formulas | Suspicious | Replaced | No match | OK |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CN.pdf | 24 | 16 | 7 | 5 | 2 | OK |
| two-col-arxiv-ai-transformers-gnn | 20 | 20 | 0 | 0 | 0 | OK |
| two-col-arxiv-ai-gat | 6 | 6 | 0 | 0 | 0 | OK |
| two-col-arxiv-ai-lora | 6 | 6 | 0 | 0 | 0 | OK |
| two-col-arxiv-ai-rag | 3 | 3 | 0 | 0 | 0 | OK |

CN.pdf suspicious/replaced details:

| Formula | Route A problem | Detection reason | Route B candidate | Status |
| --- | --- | --- | --- | --- |
| (3) | CJK `\text{...}` tail | `contains_cjk` | `w_t = softmax([...])` | **Replaced** |
| (4) | Only `( 4 )`, no body | `number_only_missing_body` | `l_i = O(l_i) * W_l` | **Replaced** |
| (5) | repeated `\frac{\sqrt{d}}{\sqrt{d}}` hallucination | `repeated_frac_hallucination` | `r_{h->p} = sum(...)` | **Replaced** |
| (7) | Number-only `( 7 )` | `number_only_missing_body` | none | No match |
| (8) | Number-only `( 8 )` | `number_only_missing_body` | none | No match |
| (13) | CJK contamination in formula text | `contains_cjk` | `qr_i = ReLU([...])` | **Replaced** |
| (16) | `\and\and\and...` hallucination (many repeats) | `repeated_and_hallucination` | `w_i = h_i / sum_k` | **Replaced** |

---

## output_roots

All under `.runtime/` (gitignored):

- CN: `.runtime/review/docling-formula-second-pass-cn-2026-06-02/CN/`
  - `document.json` (patched)
  - `document.md` (patched)
  - `second_pass_summary.json`
- transformers-gnn: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-transformers-gnn/`
- gat: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-gat/`
- lora: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-lora/`
- rag: `.runtime/review/docling-formula-second-pass-2026-06-02/two-col-arxiv-ai-rag/`

---

## best_result

CN.pdf: 5 of 7 suspicious formulas replaced when using the quality Route A output as the document backbone and Route B only as the formula-candidate source. The 2 unresolved cases (formulas 7 and 8) are right-column number-only placeholders that Route B also fails to detect. All 4 key target formulas from the feasibility report - (3), (4), (5), (16) - are resolved.

Key detection patterns that work:

- `contains_cjk`: detects CJK characters in formula text (Granite contamination).
- `number_only_missing_body`: detects formula nodes with only equation number, no body (`\([0-9]+\)`).
- `repeated_frac_hallucination`: count-based detection of 3+ repetitions of `\frac {\sqrt{d}} {\sqrt{d}}`.
- `repeated_and_hallucination`: regex detection of 3+ repetitions of `\quad \ \ a n d`.

Key matching strategies:

1. **Equation number match** (primary): `(page, eq_number)` match from Route A formula text to Route B formula text. Works for formulas (3), (4), (5).
2. **Vertical-center proximity** (fallback): Convert Route A BOTTOMLEFT PDF coords to Route B TOPLEFT pixel space, match by vertical center within 100px threshold. Critical for formulas (13) and (16) where Route B omits equation numbers.
3. **Content-prefix fallback** (markdown only): for formulas without eq_numbers in Route A text, match the markdown `$$...$$` block by the formula's first 30 characters.

English PDFs show **zero regressions**: no suspicious formulas detected, no false replacements, document structure preserved.

---

## english_regression

**None.** All 4 English PDFs (transformers-gnn, gat, lora, rag) show 0 suspicious formulas, 0 replacements. Route A formulas on English documents are clean and are kept intact.

---

## conclusion

The formula-only second-pass prototype is **viable** for the CN.pdf use case. Route B (VlmPipeline) provides materially better formula candidates than Route A for all 4 target formulas, and the matching pipeline correctly pairs them using equation numbers and vertical-center proximity. English PDFs are unaffected (no regressions).

The approach has three practical constraints to document:

1. **Right-column formulas not recovered**: formulas (7) and (8) are right-column equation numbers where Route B's VLM pipeline also failed to detect a body. No replacement is possible without a separate right-column-specific VLM pass or improving the CN.pdf VLM conversion.
2. **Route B as formula-only source**: Route B (VlmPipeline) is not safe as full-document output; it drops right-column text on CN.pdf pages 3-4 and renders pictures unreliably. The second-pass uses Route B **only** for formula text replacement, never for layout or text.
3. **Markdown patching limitations**: formulas without embedded equation numbers (like formula 16) are patched via content-prefix matching. The patched markdown loses the equation number tag when `eq_num is None`, which is acceptable for the prototype but should be addressed in a production version.

---

## next

1. **Test on 2-3 more English formula-heavy PDFs** to confirm zero-regression holds across diverse paper styles (e.g., IEEE, NeurIPS, ACL layouts).
2. **Add equation-number restoration** to the markdown replacer for formulas without `main_eq`; currently formula (16) markdown loses `(16)`.
3. **Investigate right-column formula candidates**: Route B fails on CN.pdf right-column (formulas 7, 8). Consider a targeted right-column crop pass or improved CN VLM conversion.
4. **Production integration decision**: if the approach is approved, wire `formula_only_second_pass.py` as a post-processing step after `quality_parity_adapter.py` in the n8n job chain, calling Route B VLM only for formula nodes flagged as suspicious by the adapter.
5. **Do not commit** `.runtime/` outputs, model caches, or batch logs.
