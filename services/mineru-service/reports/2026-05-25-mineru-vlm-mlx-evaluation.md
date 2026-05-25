# MinerU VLM+MLX Evaluation

Date: 2026-05-25

## Summary

MinerU is now represented as a same-level local parser candidate under `services/mineru-service/`. This evaluation did not modify Docling, n8n, `local-ai-python-worker`, `services/n8n-paper-pipeline`, Docker deployment, or EXO configuration.

Result: local MinerU VLM+MLX is runnable on the Mac mini M4 16GB with the bf16 model after a small local compatibility shim. The candidate is promising enough for deeper service integration, especially for CN formula-region detection, but it is not ready to replace Docling until a real multi-page artifact writer is built and evaluated.

## Installation Method

The live probe used a disposable Python 3.13 environment because current PyPI metadata for `mineru`, `mineru-vl-utils`, and `magic-pdf` declares `>=3.10,<3.14`; the repo default Python is 3.14.

```bash
/opt/homebrew/bin/python3.13 -m venv /tmp/mineru-service-venv
/tmp/mineru-service-venv/bin/python -m pip install -U pip
/tmp/mineru-service-venv/bin/python -m pip install -r services/mineru-service/requirements.txt
```

Installed runtime packages:

- `mineru-vl-utils==1.0.0`
- `mlx-vlm==0.3.12`
- `mlx==0.31.1`
- `pymupdf==1.27.2.3`
- `torch==2.12.0`
- `torchvision==0.27.0`

`torch` and `torchvision` were required by the Qwen processor path even though inference itself runs through MLX.

## Model Download Method

The bf16 model was downloaded with the local `hfd.sh` helper:

```bash
/Users/zeyuan/Local-AI-Lab/hfd.sh carlesonielfa/MinerU2.5-Pro-2604-1.2B-mlx-bf16 \
  --local-dir /Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16 \
  --tool aria2c -x 4 -j 4
```

Local model path:

```text
/Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16
```

Model cache size after download: approximately `2.2G`.

The 8-bit model was not downloaded because bf16 loaded and completed real probes within acceptable memory pressure for this bounded evaluation. It remains the fallback if full-document runs prove too slow or memory-heavy.

## Local VLM+MLX Support

Supported path confirmed:

- `mineru-vl-utils` exposes `MinerUClient(backend="mlx-engine")`.
- The client internally resizes layout images to `(1036, 1036)` by default before layout detection.
- Content extraction uses crops from the natural page image after layout detection.
- No EXO, pipeline backend, hybrid backend, Docker, or service restart was used.

The tested bf16 model did not load directly through the stock helper without adjustment:

1. Original MLX load failed because `language_model.lm_head.weight` was missing.
2. `mineru-vl-utils` has a compatibility path for tied-embedding Qwen2-VL models, but the model's `processor_config.json` advertises Qwen3 image/video processor keys while the model config is Qwen2-VL.
3. A small temporary compatibility view fixes this without mutating the model cache:
   - flatten nested `text_config` into `config.json`;
   - map `Qwen3VLImageProcessor` to `Qwen2VLImageProcessor`;
   - map `Qwen3VLVideoProcessor` to `Qwen2VLVideoProcessor`;
   - symlink all model weights/tokenizers into a temporary directory.

This is small and reversible, but it should be treated as an integration concern until upstream `mineru-vl-utils` or the MLX model export handles it directly.

## Sample Evaluation

Probe command shape:

```bash
PYTHONPATH=services/mineru-service /tmp/mineru-service-venv/bin/python -m mineru_service.cli page-probe \
  --pdf <sample.pdf> \
  --page <page> \
  --model-path /Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16 \
  --output-json /tmp/mineru-service-eval/<sample-page>.json
```

| Sample page | Runtime | Peak footprint | Blocks | Equations | Tables | Images | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `CN.pdf` page 1 | 58.791s | ~4.7GB | 26 | 0 | 0 | 0 | Chinese text readable; inline math/citations emitted in LaTeX-ish `\\(...\\)` form |
| `CN.pdf` page 2 | 62.892s | ~4.7GB | 30 | 0 | 1 | 1 | Table emitted as HTML; image region preserved |
| `CN.pdf` page 3 | 63.701s | ~4.8GB | 48 | 10 | 0 | 2 | Strongest result: display formulas detected as equation blocks with LaTeX-style output |
| `table-heavy-ai-table-transformer.pdf` page 1 | 28.096s | ~4.8GB | 19 | 0 | 1 | 0 | Table structure emitted as HTML with rowspan/colspan |
| `two-col-arxiv-ai-gat.pdf` page 1 | 48.806s | ~4.7GB | 23 | 0 | 0 | 0 | Reading order and two-column text were usable |
| `two-col-arxiv-ai-gat.pdf` page 2 | 47.492s | ~4.8GB | 9 | 0 | 0 | 0 | Text flow readable; no formula-heavy content on tested page |

The page-level output is JSON blocks with normalized bboxes, block types, and recognized content. It is not yet a full service contract with `document.html`, assets, table crops, formula crops, and metadata/status.

## Comparison Against Docling Baseline

Docling remains the fallback/review parser baseline:

- full PDF service contract already exists;
- CN OCR fallback produces `/Gxx=0`;
- images and tables are linked in HTML;
- formula source/context crops are linked in final HTML;
- known issue: missed CN formula 7 and inline/text-interleaved formula limitations.

MinerU page probes compare as follows:

- CN formula detection: promising improvement. On CN page 3, MinerU detected 10 display equations as `equation` blocks with LaTeX-style output. This suggests it may address at least part of Docling's missed-formula problem.
- Inline formulas: mixed but promising. Inline math and citations are emitted inline as `\\(...\\)`, but page-level probes do not yet prove robust inline/text-interleaved formula handling across the full paper.
- Formula correctness: mostly readable on the tested CN page, but the output needs manual review; some formatting includes nested display delimiters such as `$$ \\[ ... \\] $$` in the simple Markdown preview.
- Tables: promising. The table-heavy page produced structured HTML with row/column spans; CN page 2 also produced a small symbol table as HTML.
- Images: regions are detected, but the candidate service does not yet crop/write image artifacts.
- HTML/Markdown usability: not production-ready yet. The current service has a probe/registry only, not a complete article writer.
- Runtime: page-level bf16 probes took roughly 28-64 seconds per page. A full-document run would likely be several minutes unless optimized with model reuse and batching.
- Memory: peak process footprint was about 4.7-4.8GB in `/usr/bin/time -l`, acceptable for bounded runs on 16GB but not yet validated for long concurrent jobs.

## Model Management Recommendation

Prefer a small internal model registry first. External managers do not cleanly fit these functional document model needs today:

- EXO: rejected for functional document models based on user tests; suitable mainly for chat-model serving right now.
- Ollama / LM Studio: not suitable unless they explicitly support this exact MinerU functional VLM class, two-step layout/content flow, and image crop protocols.
- `mlx-vlm` direct runtime: best current fit for Apple Silicon functional VLMs.
- Project-local registry: recommended first step.

Proposed registry fields:

- model id;
- local path;
- source repo;
- quantization;
- expected runtime;
- disk size;
- required Python version;
- required packages/runtime;
- health check files;
- compatibility shim needed yes/no;
- cleanup policy;
- sample health check command.

The initial implementation in `mineru_service.model_registry` records these fields for bf16 and 8-bit candidates and marks in-progress `.aria2` downloads as not present.

## Go / No-Go Decision

Go for deeper MinerU integration as a candidate parser, not as a Docling replacement yet.

Recommended next phase:

1. Build a real multi-page writer for MinerU outputs under `services/mineru-service/`.
2. Export `document.md`, `document.html`, `document.json`, `metadata.json`, and `status.json`.
3. Crop and link image/table/formula regions.
4. Compare full-paper outputs against Docling on CN formula 7, inline formulas, tables, images, and HTML usability.
5. Only then decide whether MinerU becomes the preferred paper parser.

Do not connect MinerU to n8n yet.
