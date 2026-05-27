# Docling Serve And MCP Local Baseline

Date: 2026-05-26

## Architecture

Docling Serve is the single local model execution backend. `docling-mcp` remains unchanged and uses remote mode pointing at local Docling Serve. n8n integration is intentionally left for the next phase.

This avoids running multiple Python runtimes that each load their own Docling model copies.

## Local Runtime

Project-local venv:

```bash
/Users/zeyuan/Projects/local-ai-lab/.runtime/docling-serve/.venv
```

Install command used:

```bash
/opt/homebrew/bin/python3.13 -m venv .runtime/docling-serve/.venv
.runtime/docling-serve/.venv/bin/python -m pip install -U pip
.runtime/docling-serve/.venv/bin/python -m pip install docling-serve docling-mcp
```

The first attempt with Python 3.14 and `docling-serve[ui]` failed because optional UI/KFP dependencies attempted a `grpcio` source build. The working baseline uses Python 3.13 and the official core packages.

## Model Cache

Configured artifacts path:

```bash
/Users/zeyuan/.cache/docling/models
```

Existing relevant models:

```text
docling-project--CodeFormulaV2
docling-project--TableFormerV2
docling-project--docling-layout-heron
docling-project--docling-models
ibm-granite--granite-docling-258M-mlx
```

Docling Serve initially failed at boot because RapidOCR artifacts were missing from the configured artifacts path. They were provisioned with the official Docling tool:

```bash
.runtime/docling-serve/.venv/bin/docling-tools models download rapidocr \
  --output-dir /Users/zeyuan/.cache/docling/models
```

No model files are committed to git.

## Docling Serve Startup

Conservative startup command validated:

```bash
UVICORN_WORKERS=1 \
DOCLING_DEVICE=cpu \
DOCLING_SERVE_ENG_KIND=local \
DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1 \
DOCLING_SERVE_ENG_LOC_SHARE_MODELS=true \
DOCLING_SERVE_ARTIFACTS_PATH=/Users/zeyuan/.cache/docling/models \
DOCLING_SERVE_LOAD_MODELS_AT_BOOT=true \
DOCLING_SERVE_OPTIONS_CACHE_SIZE=2 \
.runtime/docling-serve/.venv/bin/docling-serve run \
  --host 127.0.0.1 \
  --port 5001 \
  --artifacts-path /Users/zeyuan/.cache/docling/models
```

Endpoints validated:

```text
http://127.0.0.1:5001/version
http://127.0.0.1:5001/docs
http://127.0.0.1:5001/openapi.json
http://127.0.0.1:5001/v1/convert/source
```

`/version` returned:

```text
docling-serve 1.20.0
docling-jobkit 1.20.0
docling 2.95.0
docling-core 2.77.0
docling-ibm-models 3.13.2
docling-parse 5.11.0
python cpython-313 (3.13.13)
macOS arm64
```

## Serve Conversion Smoke

Input:

```text
/Users/zeyuan/Projects/n8n-paper-pipeline/test_pdfs/two-col-arxiv-ai-transformers-gnn.pdf
```

Request path:

```text
POST http://127.0.0.1:5001/v1/convert/source
```

Request used an in-body base64 file source, `page_range=[1,1]`, `to_formats=["md","json"]`, `do_ocr=true`, `do_table_structure=true`, and `image_export_mode="referenced"`.

Result:

```text
status: success
processing_time: about 2.1s
client wall-clock: about 4.7s
md_len: 3482
json_content: present
errors: []
```

Without `DOCLING_DEVICE=cpu`, the same standard-pipeline smoke failed with:

```text
Cannot convert a MPS Tensor to float64 dtype as the MPS framework doesn't support float64.
```

That is recorded as an Apple Silicon caveat, not a reason to patch official packages.

## docling-mcp Remote Baseline

Startup command validated:

```bash
DOCLING_SERVICE_URL=http://127.0.0.1:5001 \
DOCLING_CONVERSION_MODE=remote \
DOCLING_MCP_KEEP_IMAGES=false \
.runtime/docling-serve/.venv/bin/docling-mcp-server \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8000 \
  conversion
```

MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```

Tools exposed in conversion-only startup:

```text
is_document_in_local_cache
convert_document_into_docling_document
convert_directory_files_into_docling_document
```

The MCP server started unchanged and a streamable HTTP MCP client could list tools.

## MCP Conversion Caveat

The official `docling-mcp` remote converter rejects local filesystem paths because `DoclingServiceClient` remote mode expects HTTP or HTTPS sources:

```text
String sources must be HTTP or HTTPS URLs.
```

Testing with a local `http://127.0.0.1:8765/...pdf` URL also failed because Docling Core rejects private/loopback URLs:

```text
URL is not allowed: http://127.0.0.1:8765/two-col-arxiv-ai-transformers-gnn.pdf
```

This is expected safety behavior in the official stack. Do not patch `docling-mcp` in this phase. For local PDFs, n8n should use Docling Serve's file/base64 upload endpoint directly, while MCP clients should use a safe reachable HTTP/HTTPS source or a future officially supported upload-capable MCP path.

## Caveats

- Core `docling-serve` install pulls Ray/KFP-related dependencies via official package dependencies even when the local engine is used.
- `DOCLING_SERVE_LOAD_MODELS_AT_BOOT=true` surfaces missing model artifacts early; it also makes startup slower.
- RapidOCR artifacts are required when OCR auto/default settings are warmed at boot.
- Apple MPS should be retested later; the conservative baseline uses `DOCLING_DEVICE=cpu`.
- Concurrency and queue pressure were not tuned in this phase.

## Next Step

Run an n8n HTTP smoke against Docling Serve using the upload-capable `/v1/convert/file` or base64 `/v1/convert/source` path, then run an MCP client smoke with a non-localhost safe HTTP/HTTPS source or an officially supported upload strategy.
