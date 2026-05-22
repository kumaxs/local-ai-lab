# docling-service

Minimal local CLI for the Local AI Lab `docling-service`.

Current state:

- Default path remains a placeholder converter.
- `--converter docling` uses Docling's Python API when the dependency is installed.
- The real Docling path applies an internal `quality_first` policy; users do not choose fast, structure, or OCR profiles.
- No HTTP server is provided.
- No n8n integration is provided.
- No `local-ai-python-worker` integration is provided.
- No Docker setup is provided.

The CLI validates contract plumbing and writes local conversion outputs. The Docling path uses `docling.document_converter.DocumentConverter` and does not shell out to the Docling CLI or fetch remote URLs.

For `--converter docling`, the service now prioritizes reading-quality diagnostics over smoke-test speed. Internally it attempts Docling table structure and page/picture/table image generation, checks generated text/Markdown/HTML for dense `/Gxx` PDF fallback tokens, and tries a full-page OCR fallback when the text layer looks bad. If optional OCR, table, or image support is unavailable, conversion should still write the required contract files and record honest warnings in `status.json`.

## Example

```bash
PYTHONPATH=services/docling-service python3 -m docling_service.cli \
  --job-uuid 550e8400-e29b-41d4-a716-446655440000 \
  --input-file-path /absolute/local/path/to/file.pdf \
  --display-name file.pdf \
  --image-export-mode referenced \
  --timeout-seconds 300
```

By default, outputs are written under:

```text
artifacts/docling-service/<job_uuid>/
```

Use `--output-root` for local tests or explicit isolated runs.

If using an isolated virtual environment, use:

```bash
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m docling_service.cli \
  --job-uuid 550e8400-e29b-41d4-a716-446655440000 \
  --input-file-path /absolute/local/path/to/file.pdf \
  --output-root /tmp/docling-service-outputs
```

To run the real Docling converter:

```bash
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m docling_service.cli \
  --converter docling \
  --job-uuid 550e8400-e29b-41d4-a716-446655440000 \
  --input-file-path /absolute/local/path/to/file.pdf \
  --output-root /tmp/docling-service-outputs
```

Users still only select `--converter docling`; there is no public fast/structure/OCR profile to understand.

Required outputs remain:

```text
document.html
document.md
document.json
metadata.json
status.json
```

Quality-first runs may also write:

```text
tables/table_N.json
tables/table_N.md
tables/table_N.html
assets/*.png
text.txt
doctags.txt
```

`metadata.json` and `status.json` include the internal policy and quality diagnostics, including `conversion_policy`, `ocr_fallback_used`, `text_quality_gxx_count`, `text_quality_gxx_density`, `table_count`, `asset_count`, and `generated_outputs`. `asset_count` is the count of real files written under `assets/`; `table_count` is counted from Docling document structure when available.

Command success is not the same as reading-quality success. In particular, Chinese PDFs with bad embedded text layers may still produce poor text if the local OCR engine or model files are unavailable; check `status.warnings` and the `/Gxx` metrics before treating an output as intake-quality.

## Tests

```bash
PYTHONPATH=services/docling-service python3 -m unittest discover services/docling-service/tests
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m unittest discover services/docling-service/tests
```

No external dependencies are required for the placeholder path. `requirements.txt` includes `docling` for the real local conversion path, but the default CLI path remains `--converter placeholder`.
