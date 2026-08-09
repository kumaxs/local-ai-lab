# Docling Service 1.1.0

## Storage lifecycle hardening

- orphan inputs created by a crash before SQLite registration are reclaimed;
- successful cleanup claims are retired instead of accumulating indefinitely;
- concurrent multipart uploads reserve bounded temporary space and share the
  managed state temporary directory;
- Docker cross-volume uploads use a flushed same-volume partial file before
  atomic input publication;
- the production converter is stopped while running if staging output exceeds
  its per-job limit or the filesystem free-space floor;
- abandoned file/ZIP downloads close producers and release download leases;
- Docker and macOS service logs use bounded rotation;
- webhook subscription count, headers, and filters have explicit bounds.

This release turns the conversion endpoint into a bounded, automatable task
service while preserving the existing `/v1/jobs` paths and output contract.

Highlights:

- OpenAPI 3.1 at `/openapi.json`, Swagger UI at `/docs`, ReDoc at `/redoc`,
  typed schemas, bearer security declarations, and RFC 9457 errors;
- SQLite WAL task authority with the v1.0.2 ten-field JSON rollback mirror;
- cursor-paginated task and webhook-delivery lists;
- configurable input, output, metadata, staging, temporary-data, and webhook
  retention with periodic cleanup and download leases;
- queue, per-job output, total-data, and free-space limits;
- CloudEvents 1.0 webhooks with stable IDs, HMAC-SHA256 signatures, retries,
  delivery history, manual retry, redirect rejection, and host allowlisting;
- one-request streaming ZIP downloads containing all published outputs and a
  manifest, but never the source PDF;
- atomic staged output publication and restart recovery for interrupted work;
- optional 24-hour `Idempotency-Key` handling for safe workflow retries.

Docker model downloads still default to `https://hf-mirror.com`. Set
`HF_ENDPOINT` before startup to select the official Hugging Face endpoint or
another compatible mirror.

Docker images are published for `linux/amd64` and `linux/arm64` at:

- `ghcr.io/kumaxs/local-ai-lab-docling-api:1.1.0`
- `ghcr.io/kumaxs/local-ai-lab-docling-backend:1.1.0`
- `ghcr.io/kumaxs/local-ai-lab-docling-formula:1.1.0`

The release bundle contains a Compose file that references only those prebuilt
images. A target machine needs Docker Engine with Compose v2; it does not need
Git, Python, or a shell script when the Compose YAML is supplied directly.
