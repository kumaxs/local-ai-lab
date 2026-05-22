# docling-service

Minimal local CLI skeleton for the Local AI Lab `docling-service`.

Current state:

- Skeleton only.
- No Docling dependency is installed.
- No real Docling conversion is performed.
- No HTTP server is provided.
- No n8n integration is provided.
- No `local-ai-python-worker` integration is provided.
- No Docker setup is provided.

The current CLI validates contract plumbing and writes placeholder-derived outputs so the request, metadata, status, and artifact layout can be tested before adding Docling.

## Example

```bash
PYTHONPATH=services/docling-service python -m docling_service.cli \
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

## Tests

```bash
python -m unittest discover services/docling-service/tests
```

No external dependencies are required for the initial skeleton.
