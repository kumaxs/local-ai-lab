# macOS release

## Requirements

- Apple Silicon with macOS 26.4 or newer is the release-tested target for the
  pinned MLX stack. Intel uses the portable Transformers engine but is not the
  human-acceptance baseline.
- Python 3.11, 3.12, or 3.13.
- Apple Silicon is recommended for MLX formula enrichment. Intel Macs use the
  portable Transformers formula engine.
- At least 12 GB free disk space for environments and model caches.

All packages are installed into a repository-local release environment. Nothing
is installed into the system Python.

The installer uses the dependency versions from the accepted runtime and only
installs the local Docling engine. Remote Ray/KFP orchestration packages are not
part of this single-Mac service.

## Install

For another machine, download and verify the `docling-service-1.1.1` bundle from
the `v1.1.1` GitHub Release, extract it, and run from the extracted directory:

```bash
./install-macos.sh
```

This copies the release into a stable versioned directory under
`~/Library/Application Support/Local AI Lab/` before installing dependencies.
It is then safe to remove the downloaded archive and extraction directory.

For development from a complete repository checkout:

```bash
zsh services/docling-service/deploy/macos/install.sh
```

If `python3` is not a supported version:

```bash
PYTHON_BIN=/opt/homebrew/bin/python3.13 \
  zsh services/docling-service/deploy/macos/install.sh
```

The installer initializes the layout, table, CodeFormulaV2, and selected Granite
formula model in `~/.cache/docling/models` (MLX on Apple Silicon, Transformers on
Intel). Existing compatible cached files are reused.

## Start, inspect, and stop

```bash
zsh services/docling-service/deploy/macos/start.sh
zsh services/docling-service/deploy/macos/status.sh
zsh services/docling-service/deploy/macos/stop.sh
```

The API defaults to `http://127.0.0.1:8000`. Runtime data and logs are under the
installed release's `.runtime/docling-release/macos/` directory. For a release
bundle installation this is below the stable versioned installation path; for a
repository checkout it is below the repository root.

`start.sh`, `status.sh`, and `stop.sh` call `deploy/macos/lifecycle.py`. Each
service instance has a supervisor, an independent guard, and one child process.
Lifecycle metadata records an instance nonce plus PID, session ID, and precise
Darwin process-birth identity, so a reused PID or unrelated listener is never
adopted or signalled. The small `pids/*.pid` files are compatibility records for
the supervisor PID; `logging_wrapper.py` remains only as a pre-1.2 CLI shim.

The supervisor owns merged stdout/stderr logging and bounded, symlink-safe
size-based rotation. Defaults are `10 MiB` per file and `3` backups. Both can be
overridden before start:

- `DOCLING_MACOS_LOG_MAX_BYTES` (default `10485760`)
- `DOCLING_MACOS_LOG_BACKUP_COUNT` (default `3`)

Example:

```bash
export DOCLING_MACOS_LOG_MAX_BYTES=5242880
export DOCLING_MACOS_LOG_BACKUP_COUNT=5
zsh services/docling-service/deploy/macos/start.sh
```

The guard recovers a killed supervisor or child and reconciles dual death. A
bounded `stop.sh` verifies the exact instance, requests orderly shutdown, then
escalates only that validated process session if needed. Atomic metadata and a
per-service lock make concurrent starts/stops fail safely. Legacy PID-only
records are inspected conservatively and migrated only when their script,
listener, and process identity agree.

When the installed lifecycle helper is available, `status.sh` prints a JSON
array with one object for `backend` and one for `api`. `running` means the
supervisor and child identities match and the
recorded loopback health endpoint responds. `stale` means metadata remains but
all recorded roles are gone. `unknown` means identity, listener, session, child,
or health evidence conflicts and requires inspection. An old PID-only record is
reported as `legacy-running` or `legacy-stale`. For a normal status report, exit
status is `0` only when both services are healthy `running` and `1` for stopped,
unknown, legacy, or otherwise nonhealthy state. Installation/invocation failures
outside that report can exit `2` (or the shell's own command error).

Custom loopback ports can be selected before start and are persisted in instance
metadata, so a later fresh shell can still inspect and stop the same deployment:

```bash
export DOCLING_BACKEND_PORT=55001
export DOCLING_API_PORT=58001
zsh services/docling-service/deploy/macos/start.sh
```

### Web UI

This section applies to installs built from the current post-1.1.1 source tree
and the next tagged release. The published `v1.1.1` bundle does not contain
this UI.

With the API running, open `http://127.0.0.1:8000/ui/`. The static page is
served by the API process and is included in the release package, so no Node.js
installation or second service is needed. It supports PDF upload, upload
progress, queue inspection, trusted phase-level (not page-level) progress,
output listing/download, terminal-job deletion, TTL countdowns, and storage
usage.

The task list uses cursor-based previous/next pages of at most 100 jobs and
refreshes the visible page. It rejects non-advancing/cyclic cursors; a queued
timer refresh or bounded takeover prevents navigation from leaving the list
indefinitely stale. Token replacement/clear and `401` clear protected page
state and invalidate delayed responses from the prior token.
Stage/error messages have a 280-Unicode-character preview bound and scan at
most the first 65,536 UTF-16 code units. For a terminal job with longer
diagnostics, open its published `status.json` from the output list.

Unauthenticated downloads use the browser's native streaming path. When bearer
authentication is enabled, page downloads are capped at 256 MiB; use a
streaming API client for larger protected artifacts.

The page's System configuration panel uses the authenticated
`GET/PATCH /v1/system/config` endpoints and a SQLite CAS revision. It edits
only input, successful/failed output, job, staging, temporary, cleanup,
idempotency, and download-lease TTLs. SQLite overrides take precedence over
environment values, which take precedence over built-in defaults; `null`
clears an override. Existing job deadlines are not rewritten, so a change is
not retroactive. Input/output/state paths, token, model/engine settings, and
concurrency/capacity limits are read-only deployment settings.

To require a bearer token, export it before starting both processes:

```bash
export DOCLING_SERVICE_API_TOKEN='replace-with-a-long-random-value'
zsh services/docling-service/deploy/macos/start.sh
```

Keep the default loopback bind unless a trusted reverse proxy supplies TLS and
access control. PDF uploads and converted papers can contain sensitive data.

## Convert a paper

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Authorization: Bearer ${DOCLING_SERVICE_API_TOKEN}" \
  -F 'file=@/absolute/path/paper.pdf;type=application/pdf'
```

Submit once, save the returned `job_id`, and poll its `status_url` with
`GET /v1/jobs/{job_id}` until a terminal state; then read `outputs_url` or the
archive. Do not poll by repeating the multipart `POST`. If submission itself
must be retried, reuse one `Idempotency-Key`. Omit the `Authorization` header
when no service token is configured.

## Configuration

Common environment variables are documented in [API.md](API.md). macOS-specific
defaults are:

- `DOCLING_FORMULA_POLICY=granite_mlx` on Apple Silicon.
- `DOCLING_CN_OCR_PARITY=true` to use OCRMac after bad text-layer detection.
- `DOCLING_DEVICE=cpu` for the standard PDF pipeline; this avoids known MPS
  numeric incompatibilities while the formula submodel still uses MLX.
- `DOCLING_MAX_CONCURRENT_JOBS=1` to avoid competing large model loads.
- `DOCLING_MAX_CONCURRENT_UPLOADS=2`; multipart spooling and the validated copy
  use the installed release's managed `data/state/temp` directory.
- `DOCLING_IMAGE_EXPORT_MODE=embedded` so figures survive the backend/API
  process boundary and can be written into each job output directory.

Set `DOCLING_FORMULA_POLICY=granite_transformers` to diagnose an MLX-specific
problem without changing the output contract. This is a diagnostic mode, not
the accepted Apple Silicon release default.

Task input, output, staging, and upload temporary data use the same TTL and
quota rules described in [API.md](API.md). Model files in
`~/.cache/docling/models` and older versioned installations under
`~/Library/Application Support/Local AI Lab/` are persistent and must be
removed deliberately when no longer needed.
