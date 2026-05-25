# mineru-service

Service-level candidate for evaluating MinerU as a local document parser beside `docling-service`.

This service is intentionally narrow:

- local MinerU VLM + MLX only;
- no EXO;
- no MinerU pipeline backend;
- no MinerU hybrid backend;
- no n8n integration;
- no replacement of Docling.

The current evaluation runtime uses Python 3.13 because the published MinerU packages declare `>=3.10,<3.14`.

## Candidate Runtime

Disposable validation environment used for the first probe:

```bash
/opt/homebrew/bin/python3.13 -m venv /tmp/mineru-service-venv
/tmp/mineru-service-venv/bin/python -m pip install -U pip
/tmp/mineru-service-venv/bin/python -m pip install -r services/mineru-service/requirements.txt
```

Model download is expected to use the local `hfd.sh` helper into a cache path outside the repository:

```bash
/Users/zeyuan/Local-AI-Lab/hfd.sh carlesonielfa/MinerU2.5-Pro-2604-1.2B-mlx-bf16 \
  --local-dir /Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16 \
  --tool aria2c -x 4 -j 4
```

The 8-bit model is a fallback candidate if bf16 is unstable or too memory-heavy.

## Local Tests

```bash
PYTHONPATH=services/mineru-service python3 -m unittest discover services/mineru-service/tests
```

When validating against the MinerU runtime, use the temporary Python 3.13 environment:

```bash
PYTHONPATH=services/mineru-service /tmp/mineru-service-venv/bin/python -m unittest discover services/mineru-service/tests
```

## Multi-page Review Contract

The service now includes a first multi-page wrapper that keeps MinerU on local VLM + MLX and writes Local AI Lab parser contract outputs:

```bash
PYTHONPATH=services/mineru-service /tmp/mineru-service-venv/bin/python -m mineru_service.cli convert \
  --pdf /path/to/paper.pdf \
  --output-dir services/mineru-service/reports/samples/mineru-integration-2026-05-25/paper-pages \
  --pages 1,3-4
```

The wrapper writes:

- `document.html`
- `document.md`
- `document.json`
- `metadata.json`
- `status.json`
- `assets/`
- `official/<file_stem>/vlm/`

The `official/<file_stem>/vlm/` directory mirrors the MinerU artifact shape used for review integration: Markdown, content-list JSON, middle/model JSON, and `images/`. The current wrapper builds those artifacts from `MinerUClient.two_step_extract()` blocks because this candidate intentionally avoids MinerU pipeline/hybrid backends.

Protocol constraints:

- layout pass uses `layout_image_size=(1036, 1036)`;
- content extraction receives rendered page images and uses natural rendered crop dimensions;
- original full-resolution page images are not passed to the layout pass directly;
- EXO, pipeline, and hybrid backends remain disabled.
