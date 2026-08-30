# Cross-machine distribution

Docling Service 1.1.1 is distributed from the Git tag `v1.1.1`. The tag starts
the release workflow, which validates the service, builds deterministic source
bundles, checks the bundle on a clean macOS runner, publishes multi-platform
Docker images, and creates the GitHub Release.

## Release assets and integrity

Download these files from the same GitHub Release:

- `docling-service-1.1.1.tar.gz` or `docling-service-1.1.1.zip`;
- the archive's matching `.sha256` file, or the combined `SHA256SUMS` when both
  archive formats are downloaded.

Verify before extracting:

```bash
shasum -a 256 -c docling-service-1.1.1.tar.gz.sha256
```

Each archive also contains `RELEASE_MANIFEST.json`, which records the Git commit,
supported Docker platforms, image tags, and a SHA-256 digest and byte size for
every bundled file. The verification utility can check both archive formats
without extracting them:

```bash
python3 services/docling-service/release/verify_release_bundle.py \
  --checksums SHA256SUMS \
  docling-service-1.1.1.tar.gz
```

The bundle excludes PDFs, reports, runtime state, logs, model caches, virtual
environments, credentials, and Git history.

## macOS delivery

Extract the bundle and run:

```bash
./install-macos.sh
```

The wrapper first copies the complete release to the stable path
`~/Library/Application Support/Local AI Lab/docling-service/1.1.1`. The Python
environment, scripts, quality adapter, model references, and runtime data then
remain independent of the Downloads directory or extracted archive. The
installer prints persistent start, status, and stop commands.

The acceptance target is Apple Silicon with macOS 26.4 or newer. Python
3.11–3.13 and 12 GB of free disk space are required. Dependency and model
downloads require network access during installation.

## Docker delivery

The release workflow publishes these OCI images for `linux/amd64` and
`linux/arm64`:

```text
ghcr.io/kumaxs/local-ai-lab-docling-api:1.1.1
ghcr.io/kumaxs/local-ai-lab-docling-backend:1.1.1
ghcr.io/kumaxs/local-ai-lab-docling-formula:1.1.1
```

From the extracted bundle:

```bash
./docker-up.sh
```

This uses `deploy/docker/compose.release.yaml`, which contains only versioned
image references and never builds from an arbitrary local checkout. If the GHCR
package is private, authenticate with a token containing `read:packages` before
running the script. Image manifests include OCI source, version, revision, and
build-time labels plus GitHub-generated provenance and SBOM attestations.

The Docker containers default Hugging Face model downloads to
`https://hf-mirror.com`. Users can set `HF_ENDPOINT` before startup to select a
different compatible endpoint without editing the Compose file.

The Docker host needs Compose v2, 8 GB assigned memory at minimum (12 GB
recommended), 15 GB free disk space, and initial network access for model
downloads. The API binds only to `127.0.0.1` unless explicitly overridden.

## Web UI in the bundle

This section applies to bundles built from the current post-1.1.1 source tree
and to the next tagged release. The published `v1.1.1` archives and images do
not contain the Web UI.

Both deployment profiles serve the same packaged UI from the API process:

- Docker: `http://127.0.0.1:8766/ui/`
- macOS: `http://127.0.0.1:8000/ui/`

`docling_service/ui/index.html`, `main.js`, and `styles.css` are declared as
package data and are also copied by the release builder's recursive
`docling_service` source path into both archive formats. The bundle therefore
needs no Node.js toolchain and does not add a UI Compose service. The page
supports upload/queue monitoring with trusted phase-level (not page-level)
progress, artifact download/delete, TTL and storage visibility, and lifecycle
configuration.

The configuration panel sends revisioned SQLite CAS updates to
`/v1/system/config`. It can change only input/success-output/failed-output/job,
staging/temp, cleanup interval, idempotency, and download-lease TTLs. SQLite
overrides win over environment values, which win over defaults; a `null`
change clears an override. Existing job deadlines remain unchanged. Paths,
token, model/engine settings, and concurrency/capacity limits are read-only.
Unauthenticated downloads use the browser's native streaming path. Bearer-
protected page downloads are capped at 256 MiB; use a streaming API client for
larger protected artifacts.

## Source-build fallback

The bundle retains the tested Dockerfiles and source-build Compose definition.
If GHCR access is unavailable, build locally with:

```bash
docker compose -f services/docling-service/deploy/docker/compose.yaml up -d --build
```

This path must build from an intact release bundle or the exact tagged source;
do not copy only the Docker directory because the API image also needs the
shared quality adapter files under `docs/integrations/`.

## Release automation

The workflow lives at `.github/workflows/docling-service-release.yml` and runs
only for semantic-version tags matching `v*.*.*`. A release is complete only
after validation, macOS bundle inspection, all three multi-platform image
publishes, and GitHub Release asset upload succeed.
