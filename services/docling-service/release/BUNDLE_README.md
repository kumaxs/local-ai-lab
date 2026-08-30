# Docling Service 1.1.1 distribution bundle

This archive contains the exact source, deployment definitions, dependency
locks, and quality adapter required by the Local AI Lab Docling Service 1.1.1
release. The bundle preserves repository-relative paths so both deployment
profiles use the same accepted semantic output implementation.

Before installation, download the matching `.sha256` sidecar from the same
GitHub Release and verify the archive, for example:

```bash
shasum -a 256 -c docling-service-1.1.1.tar.gz.sha256
```

The combined `SHA256SUMS` file is also provided when both archive formats are
downloaded. `RELEASE_MANIFEST.json` records a SHA-256 digest for every file
inside the archive.

## macOS Apple Silicon

The accepted target is Apple Silicon with macOS 26.4 or newer, Python 3.11–3.13,
12 GB free disk space, and network access for the initial dependency and model
downloads.

```bash
./install-macos.sh
```

The wrapper copies this bundle to a stable, versioned location under
`~/Library/Application Support/Local AI Lab/` before installing it. The final
output prints the persistent start, status, and stop commands.

## Docker

The image release supports `linux/amd64` and `linux/arm64`. Docker Engine with
Compose v2, at least 8 GB of Docker memory (12 GB recommended), 15 GB free disk
space, and initial model-download network access are required.

```bash
./docker-up.sh
curl -fsS http://127.0.0.1:8766/healthz
```

If the target cannot execute shell scripts, run Compose directly:

```bash
docker compose -f services/docling-service/deploy/docker/compose.release.yaml pull
docker compose -f services/docling-service/deploy/docker/compose.release.yaml up -d
```

The script pulls the immutable `1.1.1` image tags from GitHub Container
Registry and starts the API, parser, and private formula service. If the GitHub
package is private, first authenticate with a token that has `read:packages`:

```bash
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
```

Stop while preserving models and job data with `./docker-down.sh`.

The two Hugging Face-backed model containers use `https://hf-mirror.com` by
default. Set `HF_ENDPOINT` before startup to select another compatible endpoint.

## Post-1.1.1 source builds: Web UI

The published `v1.1.1` archives and images predate this section. The following
describes a bundle built from the current post-1.1.1 source tree and will apply
to the next tagged release.

The API in this bundle serves the same operations page at
`http://127.0.0.1:8766/ui/` in Docker and `http://127.0.0.1:8000/ui/` on
macOS. It accepts uploads, shows queue and trusted phase-level (not page-level)
progress, displays TTL/storage state, lists and downloads artifacts, and
deletes terminal jobs. The page is static package data (`ui/index.html`,
`ui/main.js`, `ui/styles.css`); no Node.js runtime or extra Compose service is
needed.

Lifecycle controls are edited through the revisioned SQLite config endpoint:
input, successful/failed output, job, staging, temporary, cleanup interval,
idempotency, and download-lease TTLs. SQLite overrides take precedence over
environment/default values; `null` clears an override, and deadlines already
recorded on existing jobs are not recalculated. Paths, token, model/engine,
and concurrency/capacity values are read-only. A token typed into the page is
held in memory only.

Without a bearer token, downloads use the browser's native streaming path.
Bearer-protected downloads from the page are capped at 256 MiB; use a streaming
API client for larger protected files or archives.

Detailed documentation is under `services/docling-service/docs/`.
