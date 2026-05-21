# Legacy Script Markers

Legacy PDF extraction code currently remains in `scripts/` so existing runtime paths do not break.

Marked legacy:

- `../pdf_extract.py`
- `../intake_detect.py`
- `../batch_test_pdf_extract.py`

Do not move these scripts until n8n and `local-ai-python-worker` have been migrated to a Docling-ready entry point.
