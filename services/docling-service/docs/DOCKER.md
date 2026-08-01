# Docker release

The Docker release is Linux-native. It does not contain or call OCRMac, MLX,
Metal, MPS, Apple Vision, or macOS frameworks.

## Requirements

- Docker Engine with Compose v2.
- At least 12 GB memory and 15 GB free disk space for the CPU profile and model
  cache. Complex or long papers may require more memory.
- Network access during the initial image build and first model download.

## Build and start

```bash
docker compose \
  -f services/docling-service/deploy/docker/compose.yaml \
  up -d --build
```

On the first start, the backend initializes the portable layout, table,
CodeFormulaV2, Granite Docling Transformers, and RapidOCR models in the named
`docling_models` volume before opening port 5001. This can take several minutes
and requires network access.
Subsequent starts reuse the volume. Follow initialization with:

```bash
docker compose -f services/docling-service/deploy/docker/compose.yaml logs -f backend
```

The API is bound to `127.0.0.1:8766`. The Docling backend is private to the
Compose network and is not published on the host. The API sends PDF content to
the backend over that private network; only the API can create or modify input
files, job state, and outputs.

Check health and logs:

```bash
curl -fsS http://127.0.0.1:8766/healthz
docker compose -f services/docling-service/deploy/docker/compose.yaml logs -f
```

Stop without deleting persisted models, jobs, or outputs:

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

The default formula engine is `granite_transformers`; OCR fallback is automatic
and the image installs RapidOCR plus CJK, monospaced, and mathematical fonts.
The actual engine and any quality warnings are recorded per job.
