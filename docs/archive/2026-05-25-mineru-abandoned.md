# MinerU Abandoned Archive

Date: 2026-05-26

## Summary

MinerU was evaluated as a same-level local parser candidate after Docling became the fallback/review parser baseline. The experiment is now closed. MinerU-specific service code, reports, tests, and ignored sample outputs were removed from the repository, and clearly MinerU-specific local runtime artifacts were deleted.

This archive exists only to preserve the decision trail. Do not continue MinerU wrapper work, crop fixes, model-management work, or parser research from these removed files.

## What Was Tried

- Created `services/mineru-service/` as a same-level candidate component.
- Installed MinerU in a temporary local Python environment, not global Python.
- Downloaded and tested the community MLX bf16 model:
  `/Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16`
- Probed local VLM + MLX only. EXO, pipeline backend, and hybrid backend were intentionally not used.
- Ran sample evaluation on known PDFs including `CN.pdf`, `table-heavy-ai-table-transformer.pdf`, and `two-col-arxiv-ai-gat.pdf`.
- Confirmed official MinerU output could run in the local investigation, but custom wrapper/crop expansion produced blank formula/image crops during manual review.

## Why MinerU Is Abandoned

Project direction changed to centralize document model execution through official Docling Serve. MinerU is no longer part of the current parser architecture.

The immediate reasons:

- The custom MinerU wrapper was drifting away from official MinerU output.
- Blank crop artifacts showed that custom coordinate/crop handling was not trustworthy enough to build on.
- The user wants a production path around official Docling Serve plus unchanged `docling-mcp`, not another custom parser runtime.
- Continuing MinerU would split focus and model/runtime ownership.

## Removed

Repository path removed:

- `services/mineru-service/`

Local MinerU runtime artifacts listed and removed:

- `/tmp/mineru-service-venv`
- `/Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16`
- Ignored MinerU sample outputs that lived under `services/mineru-service/reports/samples/`

No source PDFs, Docling models, Docling caches, `.venv` required by Docling, n8n workflows, Docker deployment, or Google Drive files were removed.

## Lessons To Keep

- Functional document VLMs should run through runtimes they officially support, not through EXO unless compatibility is proven for that model class.
- Official parser output should be reproduced first before building custom review artifacts.
- Custom crop extraction is easy to get wrong and should be avoided unless official artifacts are missing and the coordinate model is fully verified.
- Local model caches must stay outside git and be documented by path and provisioning method.

## Decision

MinerU is rejected for the current project phase. The active path is official Docling Serve as the central backend, with `docling-mcp` unchanged in remote mode for external agents.
