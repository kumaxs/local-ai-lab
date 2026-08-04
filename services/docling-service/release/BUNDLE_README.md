# Docling Service 1.0.2 distribution bundle

This archive contains the exact source, deployment definitions, dependency
locks, and quality adapter required by the Local AI Lab Docling Service 1.0.2
release. The bundle preserves repository-relative paths so both deployment
profiles use the same accepted semantic output implementation.

Before installation, download the matching `.sha256` sidecar from the same
GitHub Release and verify the archive, for example:

```bash
shasum -a 256 -c docling-service-1.0.2.tar.gz.sha256
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

The script pulls the immutable `1.0.2` image tags from GitHub Container
Registry and starts the API, parser, and private formula service. If the GitHub
package is private, first authenticate with a token that has `read:packages`:

```bash
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
```

Stop while preserving models and job data with `./docker-down.sh`.

The two Hugging Face-backed model containers use `https://hf-mirror.com` by
default. Set `HF_ENDPOINT` before startup to select another compatible endpoint.

Detailed documentation is under `services/docling-service/docs/`.
