# Docling Service 1.0.2

Patch release of the cross-machine Local AI Lab Docling Service distribution.
It keeps the accepted API and output contract from 1.0.1 and changes Docker
Hugging Face model downloads to use `https://hf-mirror.com` by default.
`HF_ENDPOINT` remains configurable for users who need the official service or
another compatible endpoint.

The two deployment profiles remain:

- macOS Apple Silicon with Granite Docling MLX and OCRMac;
- Docker/Linux with portable OCR and an isolated, guarded
  UniMERNet-Small/PP-FormulaNet-L formula service.

Release assets contain deterministic `.tar.gz` and `.zip` installation bundles,
matching `.sha256` sidecars, combined `SHA256SUMS`, a per-file release manifest,
and one-command installation helpers.
Docker images are published for `linux/amd64` and `linux/arm64` at:

- `ghcr.io/kumaxs/local-ai-lab-docling-api:1.0.2`
- `ghcr.io/kumaxs/local-ai-lab-docling-backend:1.0.2`
- `ghcr.io/kumaxs/local-ai-lab-docling-formula:1.0.2`

Docker requires at least 8 GB of assigned memory; 12 GB is recommended. The
macOS acceptance target is Apple Silicon with macOS 26.4 or newer. Both profiles
download model weights during first startup and therefore require network access
for initial installation.
