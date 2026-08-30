# docling-service 1.1.1

Formal quality-first PDF conversion service for Local AI Lab. It produces
semantic HTML, Markdown, Docling JSON, quality metadata, review evidence, and
machine-readable job status. The v1.1.1 path retains auditable source evidence
and fail-closed gates, while its known blind-holdout generalization limits are
recorded explicitly in the release notes rather than being presented as a
production-readiness approval.

Two deployments implement one API and one output contract:

- macOS: OCRMac for required OCR fallback and Granite Docling through MLX on
  Apple Silicon.
- Docker/Linux: portable automatic OCR (RapidOCR in the supplied image) and a
  guarded UniMERNet-Small/PP-FormulaNet-L ensemble in an isolated private
  formula container. It has no imports or runtime dependency on OCRMac, MLX,
  Metal, Vision, or other macOS frameworks.

Start here:

- [cross-machine distribution and integrity verification](docs/DISTRIBUTION.md)
- [macOS installation and operation](docs/MACOS.md)
- [Docker installation and operation](docs/DOCKER.md)
- [HTTP API](docs/API.md)
- [Web UI and lifecycle configuration](docs/API.md#web-ui)
- [output contract](docs/OUTPUTS.md)
- [release architecture and platform parity](docs/RELEASES.md)

The HTTP service listens on loopback by default. Swagger UI is available at
`/docs` after startup.

The same API process serves the operations Web UI at `/ui/`. Docker users open
`http://127.0.0.1:8766/ui/`; macOS users open
`http://127.0.0.1:8000/ui/`. The page uploads PDFs, shows queue and
phase-level (not page-level) progress, displays input/output TTLs and managed
storage, lists downloadable artifacts, and deletes terminal jobs. It is
packaged with `ui/index.html`, `ui/main.js`, and `ui/styles.css`; no Node.js
runtime or additional Compose service is needed. This describes the current
post-1.1.1 source tree and the next tagged release; the published `v1.1.1`
archives and images do not contain the UI.

The queue uses cursor-based previous/next pages of at most 100 tasks and
refreshes the currently visible page; page counters are deliberately not shown
as global totals. Non-advancing/cyclic cursors and stale refresh responses are
rejected. Replacing or clearing the in-memory token invalidates delayed task,
configuration, output, and upload responses and clears protected page data;
`401` has the same fail-closed behavior.

The System configuration panel is backed by SQLite compare-and-swap revisions.
It edits only lifecycle controls (input, successful/failed output, job,
staging, temporary, cleanup interval, idempotency, and download-lease TTLs).
The effective value is SQLite override, then environment, then the built-in
default; `null` clears an override. Deadlines already recorded on a job are
not recalculated, so a TTL change applies to new deadlines and future cleanup
only. Paths, token, model/engine settings, and concurrency/capacity limits are
read-only. A token entered in the page remains in browser memory only and is
never persisted by the UI. Unauthenticated downloads use the browser's native
streaming path. Bearer-protected page downloads are capped at 256 MiB; use a
streaming API client for larger protected artifacts.

Tagged releases publish deterministic `.tar.gz` and `.zip` bundles, per-file
integrity manifests, SHA-256 checksums, and prebuilt `linux/amd64` and
`linux/arm64` images in GitHub Container Registry. The release archive is the
supported entry point on machines that do not have a repository checkout.

## Development validation

```bash
PYTHONPATH=services/docling-service python3 -m unittest discover services/docling-service/tests
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m unittest discover services/docling-service/tests
```

When Node.js is available, that service suite also runs the dependency-free
Web UI concurrency tests in `tests/webui_state.test.mjs`. Node.js is a test-only
tool and is not required to serve the packaged UI.

The earlier placeholder CLI remains available for contract-level tests. It is
not the formal release conversion path; production requests go through the
versioned HTTP API and the accepted quality-parity adapter.
