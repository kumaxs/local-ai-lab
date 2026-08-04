# Docling Service 1.0.1

First cross-machine distribution of the human-accepted Local AI Lab Docling
Service. It provides one API and output contract through two deployment
profiles:

- macOS Apple Silicon with Granite Docling MLX and OCRMac;
- Docker/Linux with portable OCR and an isolated, guarded
  UniMERNet-Small/PP-FormulaNet-L formula service.

Release assets contain deterministic `.tar.gz` and `.zip` installation bundles,
matching `.sha256` sidecars, combined `SHA256SUMS`, a per-file release manifest,
and one-command installation helpers.
Docker images are published for `linux/amd64` and `linux/arm64` at:

- `ghcr.io/kumaxs/local-ai-lab-docling-api:1.0.1`
- `ghcr.io/kumaxs/local-ai-lab-docling-backend:1.0.1`
- `ghcr.io/kumaxs/local-ai-lab-docling-formula:1.0.1`

Docker requires at least 8 GB of assigned memory; 12 GB is recommended. The
macOS acceptance target is Apple Silicon with macOS 26.4 or newer. Both profiles
download model weights during first startup and therefore require network access
for initial installation.
