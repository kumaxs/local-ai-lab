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
