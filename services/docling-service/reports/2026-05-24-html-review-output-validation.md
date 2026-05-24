# HTML Review Output Validation

## Summary

Implemented a human-reviewable `document.html` path for `services/docling-service` while keeping the user-facing command unchanged: users still run `--converter docling`.

Real validation outputs:

- `services/docling-service/reports/samples/html-review/CN/`
- `services/docling-service/reports/samples/html-review/two-col-arxiv-ai-bert/`

## Root Cause Of HTML Placeholders

The primary root cause was our HTML export path. The adapter called `DoclingDocument.export_to_html()` without an image mode. In Docling 2.95.0, the installed API signature defaults `image_mode` to `ImageRefMode.PLACEHOLDER`, so the exported HTML did not reference usable image files even when Docling had generated page/picture image refs and the service later wrote `assets/`.

Investigation also showed:

- `DoclingDocument.save_as_html(..., image_mode=ImageRefMode.REFERENCED)` writes better HTML with `<img>` tags and external image files.
- Relative paths are preserved when `save_as_html` is called from the job output directory with a relative `artifacts_dir`, such as `assets/docling-html`.
- `TableItem.export_to_html()` and `export_to_markdown()` returned empty output when called without `doc`; calling them with `doc=document` produced usable table HTML/Markdown.

So the placeholders were not caused only by Docling limitations. The service was not using Docling's referenced HTML/image mode, and it was calling table exporters without the document context they need.

## What Changed

The writer now:

- writes `document.html` via `document.save_as_html("document.html", artifacts_dir="assets/docling-html", image_mode=ImageRefMode.REFERENCED)` when Docling provides that API;
- preserves relative HTML image paths so `document.html` can be opened directly from the output directory;
- falls back to the previous HTML string with a warning if referenced HTML export is unavailable;
- calls table exporters as `table.export_to_html(doc=document)` and `table.export_to_markdown(doc=document)`;
- keeps `tables/table_N.json` and now also writes `tables/table_N.html` and `tables/table_N.md` when those exports are available;
- appends a `Review Artifacts` section to `document.html` with relative image links for page/picture assets and table links/embeds for generated table artifacts;
- records warnings if HTML image/table integration is partial.

## Validation Results

`CN.pdf`:

- `ocr_fallback_used`: `true`
- `/Gxx` count: `0`
- HTML image tags: `23`
- HTML table tags: `12`
- HTML table links: `18`
- missing local HTML refs: `0`
- `table_count`: `6`
- `asset_count`: `23`

`two-col-arxiv-ai-bert.pdf`:

- `ocr_fallback_used`: `false`
- `/Gxx` count: `0`
- HTML image tags: `26`
- HTML table tags: `16`
- HTML table links: `24`
- missing local HTML refs: `0`
- `table_count`: `8`
- `asset_count`: `26`

## Table Result

Table output improved operationally: each detected/exportable table now has JSON, HTML, and Markdown artifacts, and `document.html` embeds or links those artifacts in a review section.

Remaining limitation: table fidelity is still bounded by Docling's extracted table structure. The service makes the available table outputs visible, but it does not prove row/column correspondence is semantically correct.

## Picture And Page Asset Result

`document.html` now has valid relative image references. It includes Docling's referenced image artifacts under `assets/docling-html/` and a review appendix that displays the service-generated page and picture assets under `assets/`.

## Remaining Limitations

- The accurate Docling table-structure profile still warns when the local TableFormer accurate artifact is unavailable.
- Formula parsing warnings still occur on `CN.pdf`.
- Page/picture review images improve manual inspection but are not a substitute for structural formula understanding.
- The review appendix can make `document.html` large for longer PDFs because it intentionally surfaces page and picture images.

## Recommendation

Keep the new HTML writer path. It makes `document.html` directly useful for manual review and preserves the existing contract outputs.

Next, provision the missing Docling table-structure model artifacts and compare table fidelity against Marker or MinerU on table-heavy Chinese/English papers.
