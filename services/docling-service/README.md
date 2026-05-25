# docling-service

Minimal local CLI for the Local AI Lab `docling-service`.

Current state:

- Default path remains a placeholder converter.
- `--converter docling` uses Docling's Python API when the dependency is installed.
- The real Docling path applies an internal `quality_first` policy; users do not choose fast, structure, or OCR profiles.
- No HTTP server is provided.
- No n8n integration is provided.
- No `local-ai-python-worker` integration is provided.
- No Docker setup is provided.

The CLI validates contract plumbing and writes local conversion outputs. The Docling path uses `docling.document_converter.DocumentConverter` and does not shell out to the Docling CLI or fetch remote URLs.

For `--converter docling`, the service now prioritizes reading-quality diagnostics over smoke-test speed. Internally it first probes text-layer quality, then applies the best local article-extraction policy it can use without exposing profiles to callers. Normal text-layer PDFs use accurate TableFormer settings, referenced page/picture/table images, and formula enrichment when a local supported model is present. On Apple Silicon, Granite-Docling-CodeFormula through MLX is preferred when both the `ibm-granite/granite-docling-258M-mlx` model and `mlx-vlm` runtime are available; CodeFormulaV2 remains the fallback. PDFs with dense `/Gxx` fallback tokens, including bad Chinese text layers, automatically use full-page OCR fallback while preserving table/page/picture assets. Formulas that cannot be decoded are surfaced through high-resolution visual review artifacts and warnings instead of being hidden.

## Example

```bash
PYTHONPATH=services/docling-service python3 -m docling_service.cli \
  --job-uuid 550e8400-e29b-41d4-a716-446655440000 \
  --input-file-path /absolute/local/path/to/file.pdf \
  --display-name file.pdf \
  --image-export-mode referenced \
  --timeout-seconds 300
```

By default, outputs are written under:

```text
artifacts/docling-service/<job_uuid>/
```

Use `--output-root` for local tests or explicit isolated runs.

If using an isolated virtual environment, use:

```bash
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m docling_service.cli \
  --job-uuid 550e8400-e29b-41d4-a716-446655440000 \
  --input-file-path /absolute/local/path/to/file.pdf \
  --output-root /tmp/docling-service-outputs
```

To run the real Docling converter:

```bash
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m docling_service.cli \
  --converter docling \
  --job-uuid 550e8400-e29b-41d4-a716-446655440000 \
  --input-file-path /absolute/local/path/to/file.pdf \
  --output-root /tmp/docling-service-outputs
```

Users still only select `--converter docling`; there is no public fast/structure/OCR profile to understand.

Required outputs remain:

```text
document.html
document.md
document.json
metadata.json
status.json
```

Quality-first runs may also write:

```text
tables/table_N.json
tables/table_N.md
tables/table_N.html
assets/*.png
text.txt
doctags.txt
```

`tables/table_N.html` and `tables/table_N.md` are written only when Docling can export a real table representation. `assets/table_N.png`, `assets/formula_N.png`, and `assets/formula_N_context.png` are written when Docling can provide table or formula region coordinates, so a table/formula remains human-reviewable even when structural extraction is weak. `document.html` turns `Formula not decoded` occurrences into links to the corresponding formula context crops when those crops exist. Converted formulas also get compact nearby source/context links when Docling exposes matching formula coordinates, so manual review can compare the decoded formula with the original page region.

`metadata.json` and `status.json` include the internal policy and quality diagnostics, including `conversion_policy`, `ocr_fallback_used`, `text_quality_gxx_count`, `text_quality_gxx_density`, `table_count`, `asset_count`, `table_artifact_count`, `table_image_count`, `formula_count`, `formula_placeholder_count`, `formula_asset_count`, `formula_context_asset_count`, `formula_placeholder_link_count`, `formula_source_link_count`, `formula_enrichment_enabled`, `formula_model`, and `generated_outputs`. `asset_count` is the count of real files written under `assets/`; `table_count` is counted from Docling document structure when available.

Command success is not the same as reading-quality success. In particular, Chinese PDFs with bad embedded text layers may still produce poor text if the local OCR engine or model files are unavailable, and tables/formulas can still require visual review even when contract files exist. Check `status.warnings`, `/Gxx` metrics, table/formula counts, and the linked artifacts before treating an output as intake-quality.

## Tests

```bash
PYTHONPATH=services/docling-service python3 -m unittest discover services/docling-service/tests
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m unittest discover services/docling-service/tests
```

No external dependencies are required for the placeholder path. `requirements.txt` includes `docling` for the real local conversion path, but the default CLI path remains `--converter placeholder`.
