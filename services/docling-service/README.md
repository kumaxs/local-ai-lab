# docling-service

Minimal local CLI for the Local AI Lab `docling-service`.

Current state:

- Default path remains a placeholder converter.
- `--converter docling` uses Docling's Python API when the dependency is installed.
- No HTTP server is provided.
- No n8n integration is provided.
- No `local-ai-python-worker` integration is provided.
- No Docker setup is provided.

The CLI validates contract plumbing and writes local conversion outputs. The Docling path uses `docling.document_converter.DocumentConverter` and does not shell out to the Docling CLI or fetch remote URLs.

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

## Tests

```bash
python3 -m unittest discover services/docling-service/tests
```

No external dependencies are required for the placeholder path. `requirements.txt` includes `docling` for the real local conversion path, but the default CLI path remains `--converter placeholder`.
