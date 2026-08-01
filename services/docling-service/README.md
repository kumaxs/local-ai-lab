# docling-service 1.0.0

Formal quality-first PDF conversion service for Local AI Lab. It produces
semantic HTML, Markdown, Docling JSON, quality metadata, review evidence, and
machine-readable job status. The release path includes the semantic reconstruction
that passed the combined old/new and blind random-paper human acceptance runs.

Two deployments implement one API and one output contract:

- macOS: OCRMac for required OCR fallback and Granite Docling through MLX on
  Apple Silicon.
- Docker/Linux: portable automatic OCR (RapidOCR in the supplied image) and
  Granite Docling through Transformers. It has no imports or runtime dependency
  on OCRMac, MLX, Metal, Vision, or other macOS frameworks.

Start here:

- [macOS installation and operation](docs/MACOS.md)
- [Docker installation and operation](docs/DOCKER.md)
- [HTTP API](docs/API.md)
- [output contract](docs/OUTPUTS.md)
- [release architecture and platform parity](docs/RELEASES.md)

The HTTP service listens on loopback by default. Swagger UI is available at
`/docs` after startup.

## Development validation

```bash
PYTHONPATH=services/docling-service python3 -m unittest discover services/docling-service/tests
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m unittest discover services/docling-service/tests
```

The earlier placeholder CLI remains available for contract-level tests. It is
not the formal release conversion path; production requests go through the
versioned HTTP API and the accepted quality-parity adapter.
