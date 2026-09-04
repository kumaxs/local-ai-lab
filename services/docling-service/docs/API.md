# HTTP API 1.1

Docling Service exposes an OpenAPI 3.1 contract and keeps every supported
operation under `/v1`.

- Swagger UI: `/docs`
- ReDoc: `/redoc`
- machine-readable schema: `/openapi.json`

The schema contains typed request and response bodies, bearer authentication,
RFC 9457 errors, and the outgoing `docling-job-event` webhook definition.

## Web UI

The API serves a small, same-origin operations UI at `/ui/`; it is part of the
Python package and does not require Node.js, a frontend build, or another
Compose service. Open the address that matches the deployment:

This section documents the current post-1.1.1 source tree and the next tagged
release. The published `v1.1.1` archives and images do not contain the UI.

| Deployment | URL |
| --- | --- |
| Docker | `http://127.0.0.1:8766/ui/` |
| macOS | `http://127.0.0.1:8000/ui/` |

The page accepts a PDF by drag-and-drop or file picker, shows upload transport
progress, and polls the queue and job status. Queue rows include the server's
`state`, `queue_position`, `progress_stage`, `progress_message`, and optional
`progress_percent`. These are trusted **phase-level** signals (for example,
queued, running, validating, and publishing); they are not page-level
progress, and a missing percentage must not be interpreted as a page count or
as evidence that a particular page has completed. The UI can expand a terminal
job to list output files, download an individual file or ZIP archive, and
delete a terminal job.

The task list follows the API cursor contract in pages of at most 100 records.
Previous/next controls re-fetch one page at a time; the status counters describe
the **current page**, not a fabricated global total. Automatic refresh re-reads
the visible page. If a page transition is already running, one timer tick is
queued and refreshes the page reached by that transition; a second tick takes
over a hung transition and reloads the page that is still visible. Invalid,
non-advancing, or cyclic cursors are quarantined instead of accumulating stale
rows or looping between pages.

Each row shows at most 280 Unicode characters of its stage/error message and
the client scans no more than the first 65,536 UTF-16 code units. This is an
operations preview, not a log contract. For a terminal job with longer
diagnostics, expand its outputs and read the published `status.json`.

Unauthenticated downloads use the browser's native streaming path. When bearer
authentication is configured, the page must use an in-memory `Blob` and caps
downloads at 256 MiB; use a streaming API client for larger protected files or
archives.

Expiry and storage are visible in the same view. Each row shows the applicable
input/output/tombstone deadline and `artifact_state`; the storage card reads
`/v1/system/storage` and shows pending jobs, input/output/reserved bytes,
filesystem free space, limits, and the Janitor interval. Expired output is
reported by the API (normally `410`) and cannot be downloaded; an active
download lease keeps a file from being removed during the transfer. Deleting
an active job is rejected (`409`), while deleting a terminal job removes its
registered input and output and leaves the normal tombstone retention record.

The **System configuration** panel uses the authenticated endpoints
`GET /v1/system/config` and `PATCH /v1/system/config`. A patch includes the
last `revision` and a `changes` object, for example:

```json
{
  "revision": 12,
  "changes": {
    "success_output_ttl_seconds": 259200,
    "download_lease_seconds": 600
  }
}
```

The editable lifecycle controls are `input_ttl_seconds`,
`success_output_ttl_seconds`, `failed_output_ttl_seconds`,
`job_ttl_seconds`, `staging_ttl_seconds`, `temp_ttl_seconds`,
`cleanup_interval_seconds`, `idempotency_ttl_seconds`, and
`download_lease_seconds`. Configuration is persisted in SQLite and guarded by
compare-and-swap: a stale revision returns `409`, so refresh before retrying.
The effective-value precedence is **SQLite override, then environment value,
then the built-in default**. Sending `null` for an editable field removes its
SQLite override and immediately falls back to the environment/default value.
Deadlines already written to existing jobs are historical and are not
recomputed when a TTL changes; new jobs and future cleanup decisions use the
new effective policy. The UI labels this non-retroactive behavior explicitly.
Webhook delivery-history retention remains an environment/read-only runtime
setting; it is deliberately not a tenth live-editable field.

Paths (input, output, state, staging, and temporary roots), the bearer token,
model/engine and backend settings, and concurrency/capacity limits are
read-only in the UI. Change those through the deployment environment and
restart when required. The token entered on the page is kept only in page
memory, is sent as a bearer header for API calls, and is not written to
localStorage, cookies, SQLite, or server configuration; reload or clear the
page to discard it. Saving, replacing, or clearing a token invalidates all
in-flight task/configuration/output responses and immediately clears protected
task, storage, and configuration data from the page. A `401` response applies
the same clearing rule, so a delayed response authorized under an older token
cannot repopulate the UI.

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

The multipart request is a **single submission**, not a polling operation.
Persist the returned `job_id`; call `GET /v1/jobs/{job_id}` until `state` is
terminal, then call the returned outputs, manifest, or archive URL. Do not
repeat `POST /v1/jobs` to ask for progress. If transport uncertainty requires a
submission retry, send the same `Idempotency-Key` so the server can return the
original job rather than enqueueing a second conversion.

## Jobs and lifecycle

States are exactly `queued`, `running`, `succeeded`, `failed`, and
`interrupted`. A job becomes `succeeded` only after the required output set and
`status.json.ok` pass verification and the staged output directory is published
atomically.

After an API process restart, a job that was still `queued` is dispatched again
only after the conversion dependencies are healthy and only when its exact
managed `source.pdf` still matches the persisted digest and both per-job output
paths are absent. A previously `running` job, an invalid input, or any partial or
ambiguous output state becomes `interrupted`; the service does not blindly
replay a conversion that may already have reached the backend.

| Method and path | Purpose |
| --- | --- |
| `GET /v1/jobs?state=&client_reference=&cursor=&limit=` | Cursor-paginated task list; limit 1–100 |
| `GET /v1/jobs/{job_id}` | State, timestamps, retention deadlines, byte counts, error and links |
| `DELETE /v1/jobs/{job_id}` | Delete a terminal job and its retained artifacts; active jobs return `409` |
| `GET /v1/capabilities` | Runtime profile, request limits, and current effective lifecycle policy |
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

For jobs produced by the current quality adapter, the output list and archive
also include `regions.json` and `quality_signals.json` after successful sidecar
publication. `regions.json` contains bounded per-region evidence and the
`verified_semantic` / `visual_only` / `unresolved` outcome; the compact companion
contains counts and failure reasons. Its default and hard inventory limit is
10,000 records, producer-owned diagnostic lists are traversed with a 1,001-item
bound, and the serialized sidecar is limited to 128 MiB. A failed replacement
removes any older regular sidecar generation and its output-list entries.
Consumers must still read `status.json.ok` first: any unresolved critical region
makes the job a `degraded_failure`.
Structural success also requires final-node/body, source-PDF, page/bbox, and
kind-specific visual identity. A detected multi-page algorithm remains
unresolved unless every covered page has real evidence and both HTML and
Markdown are bound. Machine-binding-expected pictures likewise require their
exact source crop and one real image reference on each surface; diagnostic
tiny/decorative or quarantined pictures remain advisory. Algorithm contributor lists must agree across the manifest
and semantic sidecar; tables require complete declared-grid occupancy unless
the explicit empty-table fallback applies, and inline-math bindings preserve
operators and Unicode math symbols.

Formula authority is narrower than the whole-document route. Route A remains
the non-formula structural/reading-contract baseline. In Docker
`formula_policy=formula_service`, an accepted private formula-service sidecar is
the final formula surface and the generic second pass is skipped. With an
explicit `apply-all` second pass, Route-B/guarded formulas become final only
after source identity, complete JSON/Markdown coverage, occurrence binding,
and every later rollback-protected final-surface gate pass. If those conditions
do not hold, the candidate cannot replace the prior formula surface.

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
| Concurrent uploads | 2 per API process |
| Pending tasks | 20 |
| Output per task | 5 GiB |
| Total managed data | 50 GiB |
| Minimum filesystem free space | 2 GiB |

Expired input and output bytes are removed independently. Metadata remains long
enough for callers to distinguish an expired artifact (`410` for the archive)
from an unknown job (`404`). Cleanup failures are retried.

Multipart request spooling and the API's validated copy can coexist briefly.
Both live under `state/temp`; admission reserves twice the maximum request size,
preserves the free-space floor, and returns `429` when both upload slots are in
use or `507` when temporary storage is too full. The production adapter is also
monitored while it runs and is terminated if staging crosses the per-job output
limit or the output filesystem crosses the free-space floor.
When state and input roots are separate Docker volumes, the validated upload is
copied to a hidden partial file on the input volume, flushed, and atomically
published there; the input volume's free-space floor is checked before and
after the copy.

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
Subscriptions persist until explicitly deleted and are limited to 100 by
default. Header and filter maps are bounded by entry count and serialized size;
filter nesting depth is also limited.

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
| `DOCLING_MAX_CONCURRENT_UPLOADS` | `2` |
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
| `DOCLING_MAX_WEBHOOK_SUBSCRIPTIONS` | `100` |
| `DOCLING_WEBHOOK_ALLOWED_HOSTS` | empty (webhooks disabled) |
| `DOCLING_WEBHOOK_ALLOW_PRIVATE_HOSTS` | `false` |
| `DOCLING_SERVICE_API_TOKEN` | unset |

Docker exposes the API on `127.0.0.1:8766` by default. It also defaults both
Hugging Face-backed model containers to `HF_ENDPOINT=https://hf-mirror.com`;
set `HF_ENDPOINT` before startup to override it.
