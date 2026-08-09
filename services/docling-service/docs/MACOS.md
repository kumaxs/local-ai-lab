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

For another machine, download and verify the `docling-service-1.1.0` bundle from
the `v1.1.0` GitHub Release, extract it, and run from the extracted directory:

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

Log output from `run-backend.sh` and `run-api.sh` is now managed by
`deploy/macos/logging_wrapper.py`, which merges stdout and stderr and performs
size-based rotation. Defaults are `10MiB` per file and `3` backups.
Both defaults can be overridden by environment variables (read by the wrapper):

- `DOCLING_MACOS_LOG_MAX_BYTES` (default `10485760`)
- `DOCLING_MACOS_LOG_BACKUP_COUNT` (default `3`)

Example:

```bash
export DOCLING_MACOS_LOG_MAX_BYTES=5242880
export DOCLING_MACOS_LOG_BACKUP_COUNT=5
zsh services/docling-service/deploy/macos/start.sh
```

The PIDs written by `start.sh` now point to the wrapper process. `stop.sh`
retains its existing safety check and still sends termination signals to the
wrapper.

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

Poll the returned `status_url`, then read the `outputs_url`. Omit the
`Authorization` header when no service token is configured.

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
