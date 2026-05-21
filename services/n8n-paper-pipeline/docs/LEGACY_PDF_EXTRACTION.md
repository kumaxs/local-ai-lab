# Legacy PDF Extraction

## Status

The direct PDF extraction path is legacy.

It remains in place because the current inbox processor can still call it, and existing n8n/local worker flows may rely on its output shape.

## Files

- `scripts/pdf_extract.py`: extracts raw text and basic PDF metadata.
- `scripts/intake_detect.py`: detects source type and routes PDFs to `pdf_extract.py`.
- `scripts/batch_test_pdf_extract.py`: runs batch smoke tests over `test_pdfs/*.pdf`.

## Why Legacy

The old path is intentionally simple:

- `pdfplumber` extracts page text.
- `pypdf` reads page count, encryption state, dimensions, and metadata.
- Heuristics flag OCR need, rough two-column layouts, and possible garbled text.

This is not enough for the future main path because papers often need:

- layout-aware reading order;
- tables and figures;
- captions and references;
- formulas and footnotes;
- page images and derived assets;
- structured JSON suitable for downstream agents.

## Compatibility Promise

The legacy scripts should remain callable while the current worker and n8n flows are active.

Safe changes:

- documentation;
- clearer warnings;
- bug fixes that preserve CLI arguments and output files;
- tests that verify current behavior.

Avoid without a migration plan:

- renaming these scripts;
- changing CLI arguments;
- changing output file naming;
- changing `process_inbox.py` routing;
- replacing outputs in `n8n_outputs` with a different schema.
