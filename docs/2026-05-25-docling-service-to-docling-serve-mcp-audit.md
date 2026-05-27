# Docling Service To Docling Serve/MCP Audit

Date: 2026-05-26

## Summary

`services/docling-service` remains a valuable validation baseline, but the long-term local backend should move to official Docling Serve. Docling Serve centralizes model loading, cache use, queue behavior, timeouts, scratch results, and HTTP access. `docling-mcp` should remain unchanged and run in remote mode against local Docling Serve.

## Inherit From `services/docling-service`

Keep these lessons when integrating n8n and agents with Docling Serve:

- Local model cache: `/Users/zeyuan/.cache/docling/models`
- Granite-Docling-CodeFormula MLX validation:
  `/Users/zeyuan/.cache/docling/models/ibm-granite--granite-docling-258M-mlx`
- CodeFormulaV2 fallback knowledge:
  `/Users/zeyuan/.cache/docling/models/docling-project--CodeFormulaV2`
- TableFormer cache paths:
  `/Users/zeyuan/.cache/docling/models/docling-project--TableFormerV2`
  and `/Users/zeyuan/.cache/docling/models/docling-project--docling-models`
- `hfd.sh` / HF mirror lesson: use explicit, documented local model provisioning instead of implicit surprise downloads when possible.
- `/Gxx` text-layer quality detection remains useful for downstream quality status.
- Chinese PDF finding: after OCR support was installed and fallback was enabled, `CN.pdf` dropped from `/Gxx=30828` to `/Gxx=0`.
- Referenced image export is required for reviewable HTML; placeholder-only HTML is not enough.
- Table export lesson: Docling table export works best when table serializers receive `doc=document`.
- Formula/table/image review strategy: structured extraction should be paired with source/context evidence links so manual review can catch misses.
- Metadata/status quality contract is still useful for n8n:
  `conversion_policy`, `ocr_fallback_used`, `/Gxx` counts/density, table/image/formula counts, generated outputs, and warnings.
- Regression samples to keep using:
  `CN.pdf`, `table-heavy-ai-table-transformer.pdf`, `two-col-arxiv-ai-gat.pdf`, and `two-col-arxiv-ai-bert.pdf`.

## Do Not Carry Forward

- MinerU custom service, wrappers, crop code, model registry experiments, or reports as active implementation.
- Direct modification of `docling-mcp` internals, tool schemas, return schemas, or transport.
- EXO for functional document parsing models.
- Treating the current custom `services/docling-service` CLI as the primary long-term server if official Docling Serve covers the backend requirement.
- Running `docling-mcp` local mode side by side with n8n direct Docling execution, because that can load separate model copies in separate Python runtimes.

## Serve/MCP Architecture Notes

Recommended baseline:

- One Docling Serve process.
- `DOCLING_SERVE_ENG_KIND=local`
- `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1`
- `DOCLING_SERVE_ENG_LOC_SHARE_MODELS=true`
- `UVICORN_WORKERS=1`
- `DOCLING_SERVE_ARTIFACTS_PATH=/Users/zeyuan/.cache/docling/models`
- `DOCLING_SERVE_LOAD_MODELS_AT_BOOT=true`
- `DOCLING_SERVE_OPTIONS_CACHE_SIZE=2`
- `docling-mcp` remote mode with `DOCLING_SERVICE_URL=http://127.0.0.1:5001`

For the current smoke on Apple Silicon, `DOCLING_DEVICE=cpu` was needed to avoid a standard-pipeline MPS float64 conversion error. MLX packages are installed in the Docling Serve venv, and the Granite MLX model cache exists, but the basic HTTP conversion smoke was validated on CPU device for reliability.

## Next Integration Step

Use n8n to call Docling Serve HTTP upload endpoints directly for local PDFs, while external agents use unchanged `docling-mcp` remote mode. Before productionizing, add an explicit decision for how MCP clients should submit local PDFs, because the current official remote converter accepts HTTP/HTTPS sources and Docling Core rejects localhost/private URLs for safety.
