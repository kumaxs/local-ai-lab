# Docling Formula Quality Validation

Date: 2026-05-24

## Summary

Formula handling was the remaining blocker after the article-extraction work at commit `ded32444bb80130692ae5ec52cabc59562653070`.

This change improves formula review quality without changing the public interface. Users still run:

```bash
--converter docling
```

Fixed:

- formula crops are now generated from high-resolution page images when formula coordinates are available;
- each formula also gets a wider `formula_N_context.png` crop with surrounding text and equation number when possible;
- `document.html` turns `Formula not decoded` into review links to the corresponding context crop;
- metadata/status now record `formula_context_asset_count` and `formula_placeholder_link_count`;
- page/image scale is raised for quality-first output so formula and table/page crops are more legible.

Improved but still limited:

- CodeFormulaV2 works well on normal text-layer English samples in the test set;
- CN.pdf still does not get reliable structured formula decoding through the OCR fallback path;
- inline formulas mixed inside paragraphs remain a Docling/CodeFormula limitation in these samples;
- formula review is now human-readable through crops/context, even when structured decoding is missing.

## Investigation

### CN OCR Fallback With Formula Enrichment

A bounded real validation was run with:

- CN.pdf
- full-page `ocrmac`
- accurate TableFormer with cell matching
- CodeFormulaV2 enabled
- page images enabled
- `images_scale=2.0`

The run loaded CodeFormulaV2 successfully and produced no immediate model error, but it did not complete within about 11 minutes on the local CPU path. It was manually stopped. This was not Codex quota exhaustion and not a program crash; it was a manual stop after exceeding the bounded validation window.

The implementation therefore keeps CN OCR fallback quality-first for text/tables/images, but defers whole-document formula VLM on OCR fallback and writes high-resolution formula review fallbacks. This is recorded in status warnings:

```text
formula_enrichment_deferred_on_ocr_after_bounded_validation; high_res_review_fallback_enabled
```

### Formula Item Geometry

Docling formula items expose page provenance and bounding boxes. A CN probe with `images_scale=3.0` showed formula items with page numbers and `BOTTOMLEFT` bounding boxes, while page images were available at high resolution. The writer now crops formulas from those page images rather than relying only on the low-resolution item image.

## Implementation

Changed files:

- `docling_service/docling_adapter.py`
- `docling_service/writer.py`
- `tests/test_writer.py`
- `README.md`

Policy changes:

- quality-first image scale increased to `3.0`;
- normal text-layer PDFs still use CodeFormulaV2 when local model artifacts exist;
- OCR fallback still prioritizes readable text and table/image assets, while formula VLM is deferred after bounded validation;
- warnings distinguish deferred formula VLM from model failure.

Writer changes:

- writes `assets/formula_N.png` from high-resolution page crops where possible;
- writes `assets/formula_N_context.png` with larger padding for manual review;
- falls back to Docling item image only if high-resolution page crop is unavailable;
- links `Formula not decoded` placeholders in `document.html` to formula context crops;
- appends formula review figures separately from generic visual assets;
- records formula crop/link counts in metadata/status.

## Final CN.pdf Validation

Input:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/CN.pdf
```

Final-code output:

```text
/tmp/docling-formula-final-check/11111111-1111-4111-8111-111111111111
```

Curated review sample:

```text
services/docling-service/reports/samples/formula-quality/CN/
```

Result:

- OCR fallback: true
- `/Gxx` count: 0
- `/Gxx` density: 0.0
- formula enrichment enabled: false on final OCR fallback path
- formula placeholders: 24
- formula tight crops: 24
- formula context crops: 24
- formula placeholder links in HTML: 24
- local broken HTML refs: 0
- table_count: 6
- table image crops: 6
- total assets: 77

Manual visual check:

- `assets/formula_1.png` is readable as a tight formula crop;
- `assets/formula_1_context.png` is readable and includes surrounding Chinese text and equation number.

Conclusion: CN formulas are not structurally decoded, but they are now human-reviewable from the final HTML and sample artifacts.

## English Formula Sample Validation

Input:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/table-heavy-ai-table-transformer.pdf
```

Final-code output:

```text
/tmp/docling-formula-final-check/22222222-2222-4222-8222-222222222222
```

Curated review sample:

```text
services/docling-service/reports/samples/formula-quality/table-heavy-ai-table-transformer/
```

Result:

- OCR fallback: false
- `/Gxx` count: 0
- formula enrichment enabled: true
- formula placeholders: 0
- formula tight crops: 1
- formula context crops: 1
- local broken HTML refs: 0
- table_count: 5
- table image crops: 5
- total assets: 25

Conclusion: CodeFormulaV2 can eliminate formula placeholders on normal text-layer English papers when Docling detects formula regions.

## Full Directory Regression

Directory:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs
```

Temporary outputs:

```text
/tmp/docling-formula-quality-full/
```

All 10 PDFs completed. No timeout, program error, model error, memory/resource failure, quota exhaustion, or user interruption occurred during the full-directory smoke.

| File | Runtime sec | OCR | /Gxx | Formula VLM | Placeholders | Formula crops | Context crops | Tables | Assets |
| --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| CN.pdf | 22.030 | true | 0 | false | 24 | 24 | 24 | 6 | 77 |
| layout-doc-ai-donut.pdf | 27.774 | false | 0 | true | 0 | 0 | 0 | 3 | 60 |
| layout-doc-ai-layoutlm.pdf | 16.694 | false | 0 | true | 0 | 0 | 0 | 5 | 18 |
| table-heavy-ai-complex-tables-gtr.pdf | 356.630 | false | 0 | true | 0 | 9 | 9 | 7 | 50 |
| table-heavy-ai-table-transformer.pdf | 35.323 | false | 0 | true | 0 | 1 | 1 | 5 | 25 |
| two-col-arxiv-ai-bert.pdf | 21.956 | false | 0 | true | 0 | 0 | 0 | 8 | 34 |
| two-col-arxiv-ai-gat.pdf | 154.022 | false | 0 | true | 0 | 6 | 6 | 3 | 31 |
| two-col-arxiv-ai-lora.pdf | 175.209 | false | 0 | true | 0 | 6 | 6 | 18 | 72 |
| two-col-arxiv-ai-rag.pdf | 87.489 | false | 0 | true | 0 | 3 | 3 | 7 | 40 |
| two-col-arxiv-ai-transformers-gnn.pdf | 499.980 | false | 0 | true | 0 | 20 | 20 | 0 | 57 |

Local relative HTML references were checked separately with external URLs ignored. All samples had 0 broken local refs.

## Inline and Block Formula Behavior

Block/display formulas:

- English samples with detected formula regions generally work with CodeFormulaV2: placeholders were 0 across the normal text-layer full-directory run.
- High-resolution crops/context are still written for detected formulas, so a human can verify the decoded result.

Inline formulas:

- Inline formulas embedded in paragraph text are not reliably separated into formula items by Docling in these samples.
- The service cannot crop a formula that Docling does not expose as a region.
- This remains a model/capability limitation rather than a writer bug.

CN formulas:

- Docling emits MathML parse failures for CN formula regions.
- CN OCR fallback preserves readable text and generates formula review crops/context.
- Structured decoding remains blocked by model/runtime behavior on the OCR fallback path.

## Table and Image Regression

No table/image regression was found in the full-directory smoke:

- existing table JSON/Markdown/HTML artifacts are still written where Docling exports them;
- table image crops are still written;
- image references in HTML remain local-resolvable;
- CN OCR text quality remains `/Gxx=0`.

## Remaining Limitations

- CN formulas remain review fallback only, not structured formula output.
- Inline formula detection remains weak when Docling does not create formula regions.
- Raising image scale improves formula readability but increases output size. The curated samples intentionally omit full `document.json` and page images to avoid committing large temporary outputs.
- Some formula-heavy English samples take several minutes on CPU with CodeFormulaV2 enabled.

## Recommendation

Keep this quality-first Docling behavior as the default. It now makes formula failures visible and reviewable instead of leaving bare placeholders.

For true structured formula extraction, evaluate a second-stage specialist path:

- region-level CodeFormulaV2 on selected pages/formula candidates;
- a formula-specific VLM/OCR pipeline for CN OCR fallback outputs;
- Marker or MinerU comparison on CN and formula-heavy samples;
- inline formula detection using paragraph-level OCR/VLM post-processing.
