# Docling Article Extraction Validation

Date: 2026-05-24

## Summary

Implemented a user-transparent article-extraction policy for `--converter docling`.
The service still exposes one public Docling converter, but internally it now:

- probes text quality before choosing the heavier extraction path;
- keeps OCR fallback working for bad Chinese text layers;
- uses accurate TableFormer settings with cell matching for table structure;
- enables CodeFormulaV2 formula enrichment for normal text-layer PDFs when the local model is present;
- skips formula VLM during OCR fallback when it is too slow for practical CN.pdf intake;
- writes table JSON/Markdown/HTML when Docling can export them;
- writes table and formula visual review crops when Docling can provide region images;
- links those derived assets from `document.html`.

## Why Previous Execution Success Was Insufficient

The prior service could complete a Docling run and write the required contract files, but that only proved execution. It did not guarantee readable article intake:

- bad Chinese text layers could produce `/Gxx` tokens;
- Docling table HTML/Markdown could lose row/column meaning;
- formulas could remain as `Formula not decoded`;
- images and visual regions were not always exposed as review artifacts.

The new policy treats command success and reading-quality success as separate things. It records text, table, formula, and asset signals in metadata/status and exposes visual fallbacks when structure remains weak.

## Internal Quality-First Article Policy

Public usage remains:

```bash
--converter docling
```

Internal behavior now uses:

- `text_quality_probe`: lightweight Docling pass to detect dense `/G[0-9A-Fa-f]{2}` tokens before invoking formula VLM.
- `article_quality_formula`: accurate TableFormer with `do_cell_matching=True`, page/picture/table images, and CodeFormulaV2 enrichment when local model artifacts exist.
- `ocr_fallback_mac` / `ocr_fallback_auto`: full-page OCR fallback with accurate table structure and visual assets. Formula VLM is skipped here because CN.pdf runtime became impractically long when formula enrichment was applied to OCR fallback.

The skipped formula VLM path is not hidden. Status warnings record `formula_enrichment_skipped_for_ocr_runtime; visual_fallback_enabled`, and writer output includes formula crops when Docling exposes formula regions.

## Model Artifacts

Local model cache used:

- `/Users/zeyuan/.cache/docling/models/docling-project--docling-models`
- `/Users/zeyuan/.cache/docling/models/docling-project--CodeFormulaV2`
- `/Users/zeyuan/.cache/docling/models/docling-project--TableFormerV2`

`/Users/zeyuan/Local-AI-Lab/hfd.sh` was used to provision missing Docling/Hugging Face model artifacts into the local cache. No model cache files are committed.

## CN.pdf Result

Input:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/CN.pdf
```

Output sample:

```text
services/docling-service/reports/samples/article-extraction/CN/
```

Observed result:

- OCR fallback: true
- `/Gxx` count: 0
- `/Gxx` density: 0.0
- HTML image references: 53 `<img>` tags, 0 broken relative image refs
- table_count: 6
- table artifacts: 18 files, including JSON/Markdown/HTML for each table
- table image crops: 6
- formula_count: 24
- formula placeholders: 24
- formula image crops: 24
- formula enrichment: skipped for OCR runtime; visual fallback written

The Chinese text layer is fixed for intake readability: `/Gxx` dropped to 0 with OCR fallback. Tables are improved operationally because every detected table now has structural artifacts and a visual crop. The structural table rendering is still not fully trustworthy for human review, especially for compact definition-style tables, so `assets/table_N.png` is the review fallback. Formula decoding is not fixed for CN.pdf; formulas remain placeholders, but the missing formula regions are visible through `assets/formula_N.png` and page images.

## English Two-Column Result

Input:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/two-col-arxiv-ai-bert.pdf
```

Output sample:

```text
services/docling-service/reports/samples/article-extraction/two-col-arxiv-ai-bert/
```

Observed result:

- OCR fallback: false
- `/Gxx` count: 0
- `/Gxx` density: 0.0
- HTML image references: 34 `<img>` tags, 0 broken relative image refs
- table_count: 8
- table artifacts: 24 files, including JSON/Markdown/HTML for each table
- table image crops: 8
- formula_count: 0
- formula placeholders: 0
- formula enrichment: enabled with CodeFormulaV2

For this sample, Docling produced usable text, visible images, table structure exports, and table visual crops. No formula placeholders were present after formula enrichment.

## Table Extraction Result

Fixed:

- accurate TableFormer mode and cell matching are now enabled in article extraction paths;
- table JSON/Markdown/HTML artifacts remain available when Docling exports them;
- `assets/table_N.png` crops are now generated and linked from `document.html`.

Improved but limited:

- structural table HTML/Markdown can still be poor for dense or compact tables;
- the service does not claim structural table success solely from placeholders;
- table crops make weak tables reviewable even when structure is not reliable.

## Picture and Formula Result

Fixed:

- referenced image HTML output is preserved;
- page, picture, table, and formula image candidates are written as real files when Docling exposes them;
- `document.html` links and displays the derived visual artifacts with valid relative paths.

Improved but limited:

- CodeFormulaV2 enrichment is usable for normal text-layer PDFs;
- CN.pdf still has formula placeholders because formula VLM on the OCR fallback path was too slow for practical intake;
- CN.pdf formula content is reviewable through formula crops and page images, not decoded into structured math.

## Runtime Impact

The two validation runs completed locally on CPU:

- CN.pdf: about 15 seconds after the policy avoided formula VLM on OCR fallback.
- `two-col-arxiv-ai-bert.pdf`: about 14 seconds with CodeFormulaV2 enabled.

An earlier CN.pdf run with formula enrichment enabled before OCR fallback was manually stopped after more than 7 minutes. That is why the final policy performs text quality probing before choosing formula enrichment.

## Remaining Limitations

- CN.pdf formulas are not structurally decoded.
- CN.pdf tables remain only partially reliable as structured HTML/Markdown.
- Docling prints MathML parse failures for CN.pdf formula regions.
- TableStructureV2 was downloaded for local evaluation, but the implemented default keeps the stable accurate TableFormer path because it completed real validations successfully.
- The CLI status duration field predates this change and should not be used as the authoritative wall-clock runtime metric.

## Recommendation

Keep this policy as the Docling default for `--converter docling`. It gives readable Chinese text, valid HTML image links, real table artifacts, and formula/table visual fallbacks without exposing profiles to users.

For better structured tables and decoded formulas, evaluate a second-stage specialist path on hard papers: PyMuPDF region cropping plus OCR, Marker, MinerU, or a table/formula-specific VLM pipeline. Docling should remain the first-pass converter, with downstream automation checking `table_image_count`, `formula_placeholder_count`, and warnings before treating outputs as final AI-reading quality.
