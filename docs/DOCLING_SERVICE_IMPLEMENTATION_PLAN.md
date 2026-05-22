# Docling Service Implementation Plan

## Purpose

This document defines the minimal implementation plan for `docling-service` v0.

It follows the accepted contract in `docs/DOCLING_SERVICE_CONTRACT.md`, the passed test plan in `docs/DOCLING_SERVICE_TEST_PLAN.md`, and the design boundary in `docs/DOCLING_SERVICE_DESIGN.md`.

## Current Phase

Current phase: implementation plan only, no code yet.

This document does not implement `docling-service`, does not deploy Docling, does not create `services/docling-service/`, does not create `artifacts/`, does not install dependencies, and does not run any service.

## Minimal Implementation Goal

The first implementation should prove the local request, validation, output, metadata, status, and failure contract with the smallest useful code path.

The next coding step should:

- Create a minimal `services/docling-service/` skeleton.
- Implement a local CLI that accepts `job_uuid` and `input_file_path`.
- Validate request fields before writing derived outputs.
- Write `status.json` and `metadata.json`.
- Initially allow a placeholder conversion path if the Docling dependency is not yet installed.
- Only after skeleton validation decide whether to add the actual Docling dependency.

The first implementation should be a local executable module or CLI, not an HTTP service.

## Non-goals

The minimal implementation does not:

- Start an HTTP server.
- Add n8n integration.
- Add `local-ai-python-worker` integration.
- Modify `n8n-paper-pipeline`.
- Replace the current paper intake main path.
- Add Docker or docker compose.
- Install dependencies globally.
- Create sample files.
- Fetch external URLs.
- Write Google Drive.
- Perform batch orchestration.
- Implement OCR at scale.

## Proposed Directory Layout for the Next Coding Step

The next coding step may create:

```text
services/docling-service/
  README.md
  pyproject.toml
  requirements.txt
  docling_service/
    __init__.py
    cli.py
    contract.py
    validate.py
    writer.py
    converter.py
  tests/
    test_validate.py
    test_writer.py
```

This plan does not create that directory.

`artifacts/docling-service/<job_uuid>/` remains runtime output and must not be committed.

## Proposed CLI / Module Entrypoint

Prefer a simple local CLI first:

```bash
python -m docling_service.cli \
  --job-uuid 550e8400-e29b-41d4-a716-446655440000 \
  --input-file-path /absolute/local/path/to/file.pdf \
  --display-name file.pdf \
  --image-export-mode referenced \
  --timeout-seconds 300
```

The CLI should process one local file per invocation and return a small JSON response on stdout with:

- `ok`
- `status`
- `job_uuid`
- `output_dir`
- `metadata_path`
- `status_path`
- `error`

No HTTP server should be added in the first implementation unless later explicitly approved.

## Proposed Request Contract Mapping

Map CLI arguments to the contract fields:

| Contract field | CLI argument | Required | Notes |
| --- | --- | --- | --- |
| `job_uuid` | `--job-uuid` | Yes | Must be UUIDv4 and is the only authoritative identity. |
| `input_file_path` | `--input-file-path` | Yes | Must be a local readable file path. |
| `display_name` | `--display-name` | No | Human-readable only, never identity. |
| `original_name` | `--original-name` | No | Human-readable only. |
| `source_name` | `--source-name` | No | Human-readable only. |
| `requested_outputs` | `--requested-output` | No | Repeatable or comma-separated if needed. |
| `image_export_mode` | `--image-export-mode` | No | `referenced`, `embedded`, or `placeholder`; default `referenced`. |
| `timeout_seconds` | `--timeout-seconds` | No | Default and v0 maximum: `300`. |

The implementation should reject missing required fields, invalid UUIDs, unreadable local files, remote URLs, unsupported image modes, and unsupported formats with explicit failure statuses.

## Proposed Output Writer

The writer should create a job-scoped output directory:

```text
artifacts/docling-service/<job_uuid>/
```

On successful placeholder conversion, it may write minimal derived outputs:

- `document.html`
- `document.md`
- `document.json`
- `metadata.json`
- `status.json`

The placeholder conversion path may mark content as placeholder-derived while still proving the file layout and contract fields. It must not pretend that Docling conversion has happened.

Conditional outputs should be added only when supported:

- `assets/`
- `links.json`
- `tables/`
- `text.txt`
- `doctags.txt`

The writer must never copy the original file into the artifact directory.

## Proposed Metadata and Status Writer

`metadata.json` should be written for both successful and failed attempts when an output directory can be safely created.

Minimum metadata fields:

- `job_uuid`
- `display_name`
- `original_name`
- `source_name`
- `input_file_path`
- `input_sha256`
- `file_size_bytes`
- `input_mtime`
- `detected_format`
- `page_count`
- `docling_version`
- `image_export_mode`
- `requested_outputs`
- `generated_outputs`
- `link_count`
- `table_count`
- `asset_count`

`status.json` should include:

- `job_uuid`
- `status`
- `started_at`
- `finished_at`
- `duration_seconds`
- `input_file_path`
- `input_sha256`
- `output_dir`
- `outputs_written`
- `warnings`
- `error_code`
- `error_message`

The status writer should use ISO-8601 timestamps and numeric `duration_seconds`.

## Proposed Failure Handling

Use the contract status values exactly:

- `failed_timeout`
- `failed_invalid_input`
- `failed_unsupported_format`
- `failed_conversion`
- `failed_internal`

Validation failures should happen before conversion.

Safe examples:

- Missing `job_uuid`: `failed_invalid_input`.
- Non-UUIDv4 `job_uuid`: `failed_invalid_input`.
- Remote URL as input: `failed_invalid_input`.
- Missing or unreadable local file: `failed_invalid_input`.
- Unsupported file extension or detected type: `failed_unsupported_format`.
- Converter cannot produce required outputs: `failed_conversion`.
- Unexpected exception: `failed_internal`.

Error messages must be short, safe, and must not include secrets, environment values, tokens, cookies, or long stack traces.

## Proposed Timeout Handling

Default timeout: `300` seconds.

For the skeleton, timeout enforcement can wrap the conversion function, even if the placeholder converter is fast. The behavior should be in place before adding Docling.

Rules:

- Missing `timeout_seconds` uses `300`.
- Values less than or equal to zero return `failed_invalid_input`.
- Values greater than `300` return `failed_invalid_input` for v0.
- Work exceeding the configured timeout returns `failed_timeout`.

Retry policy remains outside `docling-service` v0.

## Proposed Image Mode Handling

Support these values:

- `referenced`
- `embedded`
- `placeholder`

Default: `referenced`.

The skeleton may record the requested image mode in `metadata.json` before actual image extraction exists.

Behavior:

- `referenced`: future Docling outputs may reference files under `assets/`.
- `embedded`: future outputs may embed images only when explicitly requested.
- `placeholder`: future outputs may preserve image positions without asset extraction.

Invalid values return `failed_invalid_input`.

## Proposed Link Handling

The first skeleton should not fetch links and should not require network access.

When conversion later detects links:

- Preserve internal links when available.
- Preserve external URLs as metadata.
- Write `links.json` when links are detected or preserved.
- Never fetch external URLs.
- Never trigger downloads from link metadata.

For placeholder conversion, link counts may be `0` and `links.json` may be omitted.

## Original File Policy

The original file remains the evidence source.

The implementation must:

- Never copy the original file into `artifacts/docling-service/<job_uuid>/`.
- Never delete the original file.
- Never modify the original file.
- Record `input_file_path`.
- Record `input_sha256`.
- Record file size and mtime when available.

Artifacts are derived runtime outputs and can be deleted safely.

## Security Boundary

The minimal implementation boundary is:

- Local files only.
- No remote URL input.
- No external URL fetching.
- No arbitrary shell execution.
- No public service exposure.
- No secrets in artifacts.
- No original file copying.
- No n8n credential access.
- No `local-ai-python-worker` token access.
- No Google Drive writes.

The CLI should use Python libraries directly and must not shell out to user-provided strings.

## Dependency Strategy

Do not install dependencies in this task.

For the next coding step, prefer the dependency path with the smallest blast radius:

| Option | Use | Tradeoff |
| --- | --- | --- |
| Isolated virtual environment inside `services/docling-service/` | Preferred for local experimentation after skeleton validation | Keeps dependencies scoped to the service directory. |
| Optional `requirements.txt` | Useful once actual dependencies are selected | Documents dependencies without installing them globally. |
| Later Dockerization | Only if local dependency isolation becomes painful or deployment is approved | Adds operational weight and should not be first. |

The first skeleton can use only the Python standard library. Actual Docling should be added only after request validation, writer behavior, failure statuses, and local tests pass.

## Local Test Strategy

Initial tests should use temporary files created by test code, not committed sample PDFs.

Test the skeleton before installing Docling:

- UUIDv4 validation.
- Missing UUID failure.
- Missing input path failure.
- Remote URL rejection.
- Unsupported file rejection.
- Timeout value validation.
- Image mode validation.
- Output directory naming by `job_uuid`.
- `metadata.json` required fields.
- `status.json` required fields.
- Original file is not copied.
- Same input file with different UUIDs creates different output directories.
- Duplicate display names do not collide.

After skeleton validation, run the sample matrix from `docs/DOCLING_SERVICE_TEST_PLAN.md` only when real samples and dependencies are explicitly prepared.

## Stop Conditions

Stop implementation work if:

- Any change touches n8n workflows.
- Any change touches `local-ai-python-worker` runtime logic.
- Any change touches `services/n8n-paper-pipeline` runtime logic.
- The implementation requires Docker before CLI validation.
- The implementation requires global dependency installation.
- The implementation needs sample files committed to Git.
- The implementation copies or mutates original files.
- External URLs are fetched.
- Artifact paths are derived from `display_name` instead of `job_uuid`.
- Secrets or environment values appear in artifacts.
- Runtime artifacts become staged for Git.

## Rollback Plan

If the next implementation fails before integration:

- Remove the new `services/docling-service/` skeleton directory.
- Do not touch n8n.
- Do not touch `local-ai-python-worker`.
- Do not touch the `n8n-paper-pipeline` main path.
- Delete derived runtime artifacts if they were created during local testing.
- Never delete or modify original input files.

Because there is no n8n, worker, pipeline, Docker, or service integration in the first implementation, rollback should be limited to the new skeleton directory and ignored runtime outputs.

## Acceptance Criteria

The minimal implementation plan is accepted when it:

- Keeps the first implementation local and CLI-based.
- Keeps one local file per invocation.
- Avoids HTTP server work until separately approved.
- Avoids n8n integration.
- Avoids `local-ai-python-worker` integration.
- Avoids Docker.
- Avoids Google Drive writes.
- Avoids sample file creation.
- Avoids external URL fetching.
- Preserves UUIDv4 `job_uuid` as identity.
- Preserves the original file policy.
- Defines status and metadata writer behavior.
- Allows placeholder conversion before adding Docling.
- Defers actual Docling dependency installation until after skeleton validation.

## Next Step After This Plan

After this plan is reviewed, the next step is to create the minimal `services/docling-service/` skeleton.

That coding step should implement the local CLI, request validation, `status.json`, `metadata.json`, safe failure handling, and placeholder conversion path first. Only after that skeleton passes local tests should the project decide whether to add the actual Docling dependency.
