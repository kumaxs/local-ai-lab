# HTTP API 1.1

Docling Service exposes an OpenAPI 3.1 contract and keeps every supported
operation under `/v1`.

- Swagger UI: `/docs`
- ReDoc: `/redoc`
- machine-readable schema: `/openapi.json`

The schema contains typed request and response bodies, bearer authentication,
RFC 9457 errors, and the outgoing `docling-job-event` webhook definition.

## Authentication and errors

When `DOCLING_SERVICE_API_TOKEN` is configured, every `/v1` request must send:

```http
Authorization: Bearer <token>
```

`/healthz`, `/docs`, `/redoc`, and `/openapi.json` remain public. API errors use
`application/problem+json` and include `title`, `status`, `detail`, `instance`,
and a stable `code` where available.

## Submit a PDF

`POST /v1/jobs` accepts `multipart/form-data`:

- `file` (required): a non-empty PDF whose first bytes are `%PDF-`;
- `client_reference` (optional): caller correlation text, at most 200 chars;
- `Idempotency-Key` header (optional): at most 255 chars, retained for 24 hours
  by default.

```bash
curl -sS -X POST http://127.0.0.1:8766/v1/jobs \
  -H 'Authorization: Bearer TOKEN' \
  -H 'Idempotency-Key: n8n-run-123' \
  -F 'client_reference=invoice-flow' \
  -F 'file=@/absolute/path/paper.pdf;type=application/pdf'
```

The response is `202 Accepted`:

```json
{
  "job_id": "5e2db755-b801-42fc-bbae-eb00685917d3",
  "state": "queued",
  "status_url": "/v1/jobs/5e2db755-b801-42fc-bbae-eb00685917d3",
  "outputs_url": "/v1/jobs/5e2db755-b801-42fc-bbae-eb00685917d3/outputs",
  "manifest_url": "/v1/jobs/5e2db755-b801-42fc-bbae-eb00685917d3/manifest",
  "archive_url": "/v1/jobs/5e2db755-b801-42fc-bbae-eb00685917d3/archive",
  "idempotent_replay": false
}
```

Reusing a key with the same PDF and request metadata returns the original job;
reusing it for a different request returns `409`.

## Jobs and lifecycle

States are exactly `queued`, `running`, `succeeded`, `failed`, and
`interrupted`. A job becomes `succeeded` only after the required output set and
`status.json.ok` pass verification and the staged output directory is published
atomically.

| Method and path | Purpose |
| --- | --- |
| `GET /v1/jobs?state=&client_reference=&cursor=&limit=` | Cursor-paginated task list; limit 1–100 |
| `GET /v1/jobs/{job_id}` | State, timestamps, retention deadlines, byte counts, error and links |
| `DELETE /v1/jobs/{job_id}` | Delete a terminal job and its retained artifacts; active jobs return `409` |
| `GET /v1/capabilities` | Runtime profile and request limits |
| `GET /v1/system/storage` | Managed usage, reservations, free space and configured limits |

SQLite in WAL mode is the authoritative task store. The service maintains a
strict ten-field JSON mirror under `state/jobs/` for rollback/import
compatibility. Run one API process per data directory.

## Retrieve results

| Method and path | Result |
| --- | --- |
| `GET /v1/jobs/{job_id}/outputs` | Typed list of output paths, media types, sizes, SHA-256 values and download URLs |
| `GET /v1/jobs/{job_id}/manifest` | Immutable output manifest and manifest digest |
| `GET /v1/jobs/{job_id}/files/{relative_path}` | Streams one output file |
| `GET /v1/jobs/{job_id}/archive` | Streams one ZIP containing all published outputs plus `manifest.json` |

The archive endpoint is a single request and can be called again until output
expiry. It does not include the uploaded source PDF. ZIP entry paths are
confined, symlinks and traversal are rejected, and bytes are checked against the
stored manifest while streaming. Active downloads hold a short renewable lease
so cleanup cannot remove their files mid-transfer.

Example:

```bash
curl -fSL http://127.0.0.1:8766/v1/jobs/JOB_ID/archive \
  -H 'Authorization: Bearer TOKEN' \
  -o result.zip
```

## Retention and quotas

Cleanup runs every five minutes by default. The defaults are:

| Data | Retention / limit |
| --- | --- |
| Uploaded input | 24 hours |
| Successful output | 7 days |
| Failed or interrupted output | 2 days |
| Job metadata/tombstone | 30 days |
| Webhook delivery history | 7 days |
| Staging and temporary data | 1 hour |
| Pending tasks | 20 |
| Output per task | 5 GiB |
| Total managed data | 50 GiB |
| Minimum filesystem free space | 2 GiB |

Expired input and output bytes are removed independently. Metadata remains long
enough for callers to distinguish an expired artifact (`410` for the archive)
from an unknown job (`404`). Cleanup failures are retried.

## Webhooks

Webhook configuration is disabled until `DOCLING_WEBHOOK_ALLOWED_HOSTS` lists
explicit callback hostnames. Private/loopback addresses remain blocked unless
`DOCLING_WEBHOOK_ALLOW_PRIVATE_HOSTS=true` is deliberately set for a trusted
local n8n deployment.

| Method and path | Purpose |
| --- | --- |
| `POST /v1/webhooks/subscriptions` | Register callback URL, event types, filters, secret and optional headers |
| `GET /v1/webhooks/subscriptions` | List subscriptions |
| `GET /v1/webhooks/subscriptions/{id}` | Read one subscription; secrets are never returned |
| `PATCH /v1/webhooks/subscriptions/{id}` | Update or enable/disable a subscription |
| `DELETE /v1/webhooks/subscriptions/{id}` | Delete a subscription |
| `GET /v1/webhooks/deliveries?subscription_id=&status=&job_id=&cursor=&limit=` | Inspect delivery attempts |
| `POST /v1/webhooks/deliveries/{id}/retry` | Manually retry a failed delivery |

Supported event types are `docling.job.succeeded`, `docling.job.failed`, and
`docling.job.interrupted`. Delivery uses CloudEvents 1.0 structured JSON with
`Content-Type: application/cloudevents+json`. Event IDs remain stable across
retries. Delivery is at least once, redirects are rejected, DNS is revalidated,
and retryable failures are attempted at most six times by default.

Every signed request contains:

```text
X-Docling-Signature-Timestamp: <unix-seconds>
X-Docling-Signature: <hex HMAC-SHA256(secret, timestamp + raw-body)>
X-Docling-Event-Id: <stable-event-id>
X-Docling-Event-Type: <event-type>
```

n8n should verify the HMAC against the unmodified raw request body, return a 2xx
only after it durably accepts the event, and deduplicate on the event ID.

## Runtime variables

| Variable | Default |
| --- | --- |
| `DOCLING_RELEASE_PROFILE` | `macos` |
| `DOCLING_SERVE_URL` | `http://127.0.0.1:5001` |
| `DOCLING_API_HOST` / `DOCLING_API_PORT` | `127.0.0.1` / `8000` |
| `DOCLING_MAX_UPLOAD_BYTES` | `268435456` |
| `DOCLING_MAX_CONCURRENT_JOBS` | `1` |
| `DOCLING_INPUT_TTL_SECONDS` | `86400` |
| `DOCLING_SUCCESS_OUTPUT_TTL_SECONDS` | `604800` |
| `DOCLING_FAILED_OUTPUT_TTL_SECONDS` | `172800` |
| `DOCLING_JOB_TTL_SECONDS` | `2592000` |
| `DOCLING_WEBHOOK_DELIVERY_TTL_SECONDS` | `604800` |
| `DOCLING_STAGING_TTL_SECONDS` / `DOCLING_TEMP_TTL_SECONDS` | `3600` / `3600` |
| `DOCLING_CLEANUP_INTERVAL_SECONDS` | `300` |
| `DOCLING_MAX_PENDING_JOBS` | `20` |
| `DOCLING_MAX_OUTPUT_BYTES` | `5368709120` |
| `DOCLING_MAX_DATA_BYTES` | `53687091200` |
| `DOCLING_MIN_FREE_BYTES` | `2147483648` |
| `DOCLING_IDEMPOTENCY_TTL_SECONDS` | `86400` |
| `DOCLING_DOWNLOAD_LEASE_SECONDS` | `300` |
| `DOCLING_WEBHOOK_MAX_ATTEMPTS` | `6` |
| `DOCLING_WEBHOOK_ALLOWED_HOSTS` | empty (webhooks disabled) |
| `DOCLING_WEBHOOK_ALLOW_PRIVATE_HOSTS` | `false` |
| `DOCLING_SERVICE_API_TOKEN` | unset |

Docker exposes the API on `127.0.0.1:8766` by default. It also defaults both
Hugging Face-backed model containers to `HF_ENDPOINT=https://hf-mirror.com`;
set `HF_ENDPOINT` before startup to override it.
