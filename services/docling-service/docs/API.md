# HTTP API

The public contract is versioned under `/v1`. Interactive OpenAPI documentation
is available at `/docs`; the machine schema is `/openapi.json`.

If `DOCLING_SERVICE_API_TOKEN` is configured, all `/v1` endpoints require:

```http
Authorization: Bearer <token>
```

`/healthz` deliberately remains unauthenticated for local supervisors and
container health checks. It does not expose document data.

## `GET /healthz`

Returns HTTP `200` only when the API and Docling backend are reachable. Returns
`503` while models are starting or the backend is unavailable.

## `GET /v1/capabilities`

Returns the active platform profile, formula and OCR engines, quality features,
upload limit, image policy, and job concurrency.

## `POST /v1/jobs`

Submit a multipart field named `file`. Only a local PDF upload is accepted; URL
inputs are intentionally unsupported. The server checks the extension, PDF
signature, non-empty content, and configured byte limit.

Example response (`202 Accepted`):

```json
{
  "job_id": "5e2db755-b801-42fc-bbae-eb00685917d3",
  "state": "queued",
  "status_url": "/v1/jobs/5e2db755-b801-42fc-bbae-eb00685917d3",
  "outputs_url": "/v1/jobs/5e2db755-b801-42fc-bbae-eb00685917d3/outputs"
}
```

The server generates the UUID. Client-supplied paths and job IDs are not
accepted.

## `GET /v1/jobs/{job_id}`

States are `queued`, `running`, `succeeded`, `failed`, or `interrupted`.
`interrupted` means the API restarted while a job was nonterminal; resubmit the
PDF as a new job. `succeeded` means the adapter's quality gate passed, not merely
that the model process exited. Detailed signals remain in output `status.json`.

## `GET /v1/jobs/{job_id}/outputs`

Returns every regular output file with relative path, byte size, SHA-256,
media type, and a download URL. Symlinks are excluded.

## `GET /v1/jobs/{job_id}/files/{relative_path}`

Downloads one output. The resolved path is confined to that job directory;
absolute paths and traversal outside it are rejected.

## Error codes

| HTTP | Meaning |
| --- | --- |
| `400` | Invalid output path or request shape |
| `401` | Missing or incorrect bearer token |
| `404` | Unknown job or output file |
| `413` | Upload exceeds `DOCLING_MAX_UPLOAD_BYTES` |
| `415` | Missing, empty, non-PDF, or falsely named input |
| `503` | Docling backend not ready |

## Runtime variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOCLING_RELEASE_PROFILE` | `macos` | `macos` or `docker` runtime policy |
| `DOCLING_SERVE_URL` | `http://127.0.0.1:5001` | private model backend URL |
| `DOCLING_API_HOST` | `127.0.0.1` | API bind address |
| `DOCLING_API_PORT` | `8000` | API port |
| `DOCLING_INPUT_ROOT` | profile-specific | retained source PDFs |
| `DOCLING_OUTPUT_ROOT` | profile-specific | per-job outputs |
| `DOCLING_STATE_ROOT` | profile-specific | durable queue state |
| `DOCLING_MAX_UPLOAD_BYTES` | `268435456` | upload limit |
| `DOCLING_MAX_CONCURRENT_JOBS` | `1` | conversion workers |
| `DOCLING_CONVERSION_TIMEOUT_SECONDS` | `3600` macOS / `7200` Docker | model request timeout |
| `DOCLING_IMAGE_EXPORT_MODE` | `referenced` | `referenced`, `embedded`, or `placeholder` |
| `DOCLING_SERVICE_API_TOKEN` | unset | optional bearer token |
