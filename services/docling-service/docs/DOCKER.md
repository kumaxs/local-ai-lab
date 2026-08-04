# Docker release

The Docker release is Linux-native. It does not contain or call OCRMac, MLX,
Metal, MPS, Apple Vision, or macOS frameworks.

## Requirements

- Docker Engine with Compose v2.
- At least 8 GB Docker memory (12 GB recommended) and 15 GB free disk space for
  the CPU profile and persistent model caches. Complex or long papers may
  require more memory.
- Network access during the initial image build and first model download.

## Pull and start on another machine

Download and verify the `docling-service-1.0.2` bundle from the `v1.0.2` GitHub
Release, extract it, and run:

```bash
./docker-up.sh
```

The release Compose file pulls the versioned `docling-api`, `docling-backend`,
and `docling-formula` images from the `ghcr.io/kumaxs` namespace. Image
manifests support `linux/amd64` and `linux/arm64`. If the package is private,
authenticate first with a GitHub token that has `read:packages` permission.

Follow initialization with:

```bash
docker compose \
  -f services/docling-service/deploy/docker/compose.release.yaml \
  logs -f backend formula
```

Stop without deleting persisted models, jobs, or outputs with:

```bash
./docker-down.sh
```

## Build from tagged source

The source-build fallback remains available from an intact release bundle or
the exact `v1.0.2` repository checkout:

```bash
docker compose \
  -f services/docling-service/deploy/docker/compose.yaml \
  up -d --build
```

On the first start, the parser initializes the portable layout, table, and
RapidOCR models in `docling_models`. A separate private formula container
downloads UniMERNet-Small and PP-FormulaNet-L into `docling_formula_models`.
The first start can take 10 minutes or more and requires network access;
subsequent starts reuse both named volumes. Source-build logs can be followed
with:

```bash
docker compose -f services/docling-service/deploy/docker/compose.yaml logs -f backend formula
```

Hugging Face downloads from both Docker model containers default to
`https://hf-mirror.com`. Set `HF_ENDPOINT` before `docker compose up` to use a
different compatible endpoint, for example `https://huggingface.co`. The
PP-FormulaNet-L fallback continues to use Paddle BOS independently of this
setting.

The API is bound to `127.0.0.1:8766`. The Docling backend is private to the
Compose network and is not published on the host. The API sends PDF content to
the backend over that private network; only the API can create or modify input
files, job state, and outputs.

Check health and logs:

```bash
curl -fsS http://127.0.0.1:8766/healthz
docker compose -f services/docling-service/deploy/docker/compose.yaml logs -f
```

Stop a source-build deployment without deleting persisted models, jobs, or
outputs:

```bash
docker compose -f services/docling-service/deploy/docker/compose.yaml down
```

## Convert a paper

```bash
curl -sS -X POST http://127.0.0.1:8766/v1/jobs \
  -F 'file=@/absolute/path/paper.pdf;type=application/pdf'
```

For bearer authentication, set `DOCLING_SERVICE_API_TOKEN` in the shell before
`docker compose up`, then send `Authorization: Bearer <token>`.

## Resource and portability controls

- `DOCLING_CPU_THREADS=4`: OpenMP/MKL thread count inside the backend.
- `DOCLING_MAX_CONCURRENT_JOBS=1`: API queue worker count. Increase only after
  measuring peak model memory.
- `DOCLING_DEVICE=cpu`: portable backend device. A host-specific CUDA image and
  verified PyTorch stack are required before selecting CUDA.
- `DOCLING_CONVERSION_TIMEOUT_SECONDS=7200`: per-paper backend timeout. The
  conservative CPU default covers formula-dense papers; lower it only after
  measuring the target hardware and corpus.
- `DOCLING_MAX_UPLOAD_BYTES=268435456`: maximum upload size.
- `DOCLING_API_BIND=127.0.0.1`: host bind address. Do not use `0.0.0.0` without
  TLS and authentication.

Named volumes preserve `/models`, uploaded inputs, outputs, and job state.
Inputs are retained so result provenance remains auditable. Apply an external
retention policy appropriate to the documents; do not remove a running job's
files.

The backend transfers figure bytes to the API with `image_export_mode=embedded`.
The semantic writer externalizes those bytes into per-job `pictures/` assets
where possible. A plain referenced filename would point at the backend
container's private filesystem and produce broken images in downloaded output.

The default formula policy is `formula_service`. Docling performs formula layout
detection without its unreliable Linux formula generator. The private sidecar
uses UniMERNet-Small as the primary recognizer and compares its semantic symbols
with text extracted from the same PDF bounding box. Formula crops are tightened
to visible ink before recognition so adjacent prose does not pollute TeX. The
same tightened crop is used by PP-FormulaNet-L when structure, source coverage,
or an ambiguous symbol pattern needs an independent visual cross-check. A
column-bounded crop rendered at six times the source PDF resolution is used only
when the preview crop is visibly clipped at an edge. The primary model is
released before the fallback loads, the batch size is one, and the two model
peaks therefore do not accumulate.
After every parser response the API also calls Docling Serve's converter-cache
release endpoint before formula recognition. This prevents completed layout/OCR
pipelines from retaining model memory while the isolated formula model loads.
When the document's original text layer has already failed the `/Gxx` quality
gate, that corrupt text is not treated as formula ground truth: recognition is
accepted only through the visual model plus strict TeX structure checks.

This separation also prevents the parser's Transformers dependency from
constraining the formula engines. The sidecar has no published host port, and
non-local formula service URLs are rejected. Any high-resolution crop is an
internal OCR input and review evidence only; it is never used as HTML or
Markdown paper content.

OCR fallback is automatic, and the parser image installs RapidOCR plus CJK,
monospaced, and mathematical fonts. The actual engine, formula patch count, and
all quality warnings are recorded per job. A missing, rejected, or non-renderable
formula, insufficient reliable source-semantic coverage, or incomplete MathML coverage
makes the job fail rather than silently falling back to malformed text.
