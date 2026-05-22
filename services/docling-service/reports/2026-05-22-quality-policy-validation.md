# Quality-First Docling Policy Validation

## Summary

Implemented and validated a user-transparent `quality_first` policy for `--converter docling`. The public CLI remains unchanged: users still select only `--converter docling`.

Validation used:

- `/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/CN.pdf`
- `/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/two-col-arxiv-ai-bert.pdf`

Curated derived review outputs are under:

- `services/docling-service/reports/samples/quality-policy/CN/`
- `services/docling-service/reports/samples/quality-policy/two-col-arxiv-ai-bert/`

## Why Previous Execution Success Was Insufficient

The previous service path proved that Docling could complete and write the required contract files, but command success did not prove reading-quality success. `CN.pdf` still produced many `/Gxx` text-layer artifacts, tables were not diagnosed or exported as separate review artifacts, and visual regions were not written for manual paper-intake review.

## Internal Quality-First Policy

The new `--converter docling` behavior internally:

- records `conversion_policy: quality_first`;
- attempts table structure and page/picture/table image generation first;
- falls back to a compatible image-generating profile if optional table models are unavailable;
- measures `/G[0-9A-Fa-f]{2}` tokens across generated Markdown, HTML, and text;
- attempts OCR fallback when `/Gxx` density fails the quality threshold;
- records OCR fallback decisions and failures in `status.warnings`;
- counts tables from the Docling document structure;
- writes real table JSON artifacts under `tables/` when table data exists;
- writes real PNG review artifacts under `assets/` when Docling provides image refs;
- records `generated_outputs`, `table_count`, and `asset_count` from files/structure actually produced.

## Chinese Text Quality Result

Chinese text quality did not improve for `CN.pdf`.

The policy correctly detected the bad text layer and attempted OCR fallback. Both local OCR fallback paths failed because optional local OCR support is not ready:

- macOS OCR path failed because `ocrmac` is not installed.
- Auto/RapidOCR path failed because required RapidOCR model files are missing under `/Users/zeyuan/.cache/docling/models/RapidOcr/...`.

## /Gxx Counts And Density

`CN.pdf`:

- `/Gxx` count: `30828`
- `/Gxx` density: `0.15455420527012392`
- quality result: failed
- baseline count from the previous sample: `30828`

`two-col-arxiv-ai-bert.pdf`:

- `/Gxx` count: `0`
- `/Gxx` density: `0.0`
- quality result: passed

## OCR Fallback Result

`CN.pdf`:

- `ocr_fallback_used`: `false`
- OCR fallback was attempted but could not be used because local OCR dependencies/models were unavailable.

`two-col-arxiv-ai-bert.pdf`:

- `ocr_fallback_used`: `false`
- OCR fallback was not needed because `/Gxx` quality passed.

## Table Extraction Result

`CN.pdf`:

- `table_count`: `6`
- wrote `tables/table_1.json` through `tables/table_6.json`

`two-col-arxiv-ai-bert.pdf`:

- `table_count`: `8`
- wrote `tables/table_1.json` through `tables/table_8.json`

The first table-structure profile failed because the local TableFormer accurate model artifact is missing. The service then used the compatible Docling structure and exported available table data as JSON. This is better for review, but table row/column fidelity still needs manual validation.

## Picture/Formula/Asset Result

`CN.pdf`:

- `asset_count`: `15`
- wrote 7 page images and 8 picture images under `assets/`

`two-col-arxiv-ai-bert.pdf`:

- `asset_count`: `21`
- wrote 16 page images and 5 picture images under `assets/`

Formula parsing warnings still appeared during CN conversion. The policy does not claim structural formula parsing success; it preserves page/picture review images so humans can inspect important visual regions.

## Runtime Impact

`CN.pdf` quality-first smoke:

- status duration: `6.349202` seconds
- curated output size: about `6.7M`

`two-col-arxiv-ai-bert.pdf` quality-first smoke:

- status duration: `7.548803` seconds
- curated output size: about `12M`

The policy adds retry work when table models or OCR support are missing, and it increases output size by writing page/picture PNG artifacts. The current sample sizes remained controlled for these papers.

## Remaining Limitations

- Chinese text remains unreadable for `CN.pdf`; OCR fallback did not complete locally.
- Accurate Docling table-structure extraction is blocked by missing local TableFormer artifacts.
- RapidOCR fallback is blocked by missing model files.
- macOS OCR fallback is blocked by missing `ocrmac`.
- Formula parsing is not structurally solved; visual assets are only review aids.
- Table JSON artifacts are useful diagnostics, but not yet proof of reliable row/column correspondence.

## Recommendation

Keep this quality-first policy because it improves diagnostics, exports review artifacts, and prevents command success from being mistaken for intake-quality success.

Next options:

- provision local OCR support deliberately, either `ocrmac` or complete RapidOCR model files;
- provision Docling table-structure model artifacts for accurate table extraction;
- add PyMuPDF page/region extraction if Docling image refs are insufficient for some PDFs;
- evaluate OCR-only fallback for bad Chinese text layers;
- compare Marker or MinerU for Chinese paper intake if Docling OCR/table quality remains weak after local model provisioning.
