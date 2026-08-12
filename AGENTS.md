# AGENTS.md

## Project identity

This repository is the canonical engineering repository for Local AI Lab.

Canonical local path:

```text
/Users/zeyuan/Projects/local-ai-lab
```

GitHub:

```text
https://github.com/kumaxs/local-ai-lab
```

Legacy path `/Users/zeyuan/Local-AI-Lab` is retired as a tombstone; avoid
using it for active source-of-truth work.

Local AI Lab covers local AI automation, n8n orchestration, local Python worker utilities, paper intake pipeline work, Docling validation, OpenClaw, EXO, Obsidian/Zotero workflow support, and recovery/sync documentation.

Session handoff snapshot policy:

`HANDOFF.md` is the single current handoff snapshot for this repository.
Do not create timestamped handoff copies or duplicate snapshots.

## Roles

- User: product owner and final decision maker.
- ChatGPT: project manager, architecture reviewer, task designer, and GitHub-side reviewer.
- Codex: local engineering executor.
- GitHub: canonical synchronization surface between Codex and ChatGPT.
- Google Drive: recovery mirror, not the primary engineering source.

Codex should read code, implement, test, diagnose, commit, and push when the task succeeds.

## Operating mode

Prefer autonomous execution within the task boundary.

Do not stop after ordinary implementation errors, import errors, dependency issues, or failing tests if they can be safely diagnosed and fixed inside the allowed scope.

Stop only when a stop condition is reached.

Do not ask the user to approve each internal step. Ask for user input only when a decision, credential, missing local file, forbidden-scope change, or destructive action is required.

## Session handoff protocol

The root-level handoff file is:

```text
HANDOFF.md
```

It lives beside `AGENTS.md` and is the single current handoff snapshot. Do not
create timestamped handoff copies elsewhere in the repository.

When the user says `准备交接`:

1. Continue only long enough to leave the current operation in a safe,
   internally consistent state. Do not start a new workstream.
2. Inspect the actual repository and runtime state instead of relying only on
   conversation memory. At minimum check the current branch and HEAD, Git
   status, relevant diffs, tests already run, active processes or external
   jobs when applicable, and any untracked task artifacts.
3. Replace `HANDOFF.md` with a self-contained snapshot using its documented
   structure. Record exact paths, commands, commit/tag/run identifiers, test
   results, incomplete work, blockers, risks, and the next recommended action.
4. Clearly distinguish completed, verified, in-progress, unverified, and
   blocked work. Preserve important failed attempts when they affect the next
   decision.
5. Never put secrets, tokens, credentials, private document contents, user
   PDFs, model/cache data, or large generated output in `HANDOFF.md`.
6. Verify that every referenced local path still exists where relevant, then
   report that the handoff snapshot is ready. Do not claim the underlying task
   is complete merely because the handoff document was written.

When the user says `交接继续`:

1. Before changing files or resuming commands, read `AGENTS.md` and
   `HANDOFF.md` completely.
2. Validate the snapshot against current reality: check Git branch/HEAD/status,
   referenced files, and external job or service state when applicable.
3. Treat `HANDOFF.md` as context, not unquestionable truth. Resolve stale or
   conflicting information from the repository and tell the user about any
   material discrepancy.
4. Resume from the recorded next action without repeating work already marked
   completed and verified.
5. Keep `HANDOFF.md` until a later `准备交接` replaces it. Do not delete it as
   routine cleanup.

The two trigger phrases are project-level commands and apply in every future
session for this repository.

## Repository map

Important paths:

```text
docs/
docs/integrations/docling-serve-quality-parity/
services/n8n-paper-pipeline/
services/docling-service/
```

Current main paper intake path:

```text
n8n -> local-ai-python-worker -> services/n8n-paper-pipeline
```

`local-ai-python-worker` is a slim Python capability layer for n8n. It is not the PDF processing owner.

`n8n-paper-pipeline` is intake / detection / deduplication / routing / metadata / status infrastructure. It is not a close-reading engine.

`docling-service` is a local foundational document parsing service. It is not a submodule of `n8n-paper-pipeline` and is not paper-only.

## Current Docling state

`services/docling-service` is the canonical v1.1.1 product line and is now shipped
as an official asynchronous HTTP API (`POST /v1/jobs`) with release workflow and
official package distribution.

Production user path is now API-first (`/v1` plus webhooks and download APIs); the
legacy local CLI is retained only as a contract-test surface and is not the user-facing
entrypoint.

```text
Canonical Docling acceptance points:
- services/docling-service/
- services/docling-service/docs/
- docs/integrations/docling-serve-quality-parity/
```

## Safety boundaries

Unless a task explicitly authorizes it, do not modify:

```text
docs/
docs/integrations/docling-serve-quality-parity/
services/n8n-paper-pipeline/
local-ai-python-worker runtime logic
n8n workflows
.gitignore
```

Never commit:

```text
.venv/
artifacts/
Hugging Face cache
Docling model cache
temporary outputs
original PDFs
runtime logs
secrets or tokens
```

Generated review artifacts (regression reports, parity outputs, review captures) must
only be written under `.runtime/review/` and should not be committed unless
explicitly authorized.

Do not globally install dependencies.

Do not start, stop, or restart services unless explicitly authorized.

Do not run Docker or docker compose unless explicitly authorized.

Do not write Google Drive unless explicitly authorized.

Do not change the current `n8n -> local-ai-python-worker -> n8n-paper-pipeline` main path unless explicitly authorized.

## Git rules

Work on `main` unless the task explicitly says otherwise.

Allowed after successful validation:

```bash
git add <allowed paths>
git commit -m "<task-specific message>"
git push origin main
```

Before committing, ensure staged files are only within the task's allowed paths.

Never run without explicit authorization:

```text
git pull
git merge
git rebase
git reset
git clean
git push --force
```

If push fails because the remote changed, stop and report. Do not pull, merge, rebase, reset, clean, or force push.

## Testing rules

For `services/docling-service`, use an isolated Python 3.11, 3.12, or 3.13
environment with the project API dependencies installed. Set `TEST_PYTHON` to
that environment's interpreter, then run both suites:

```bash
TEST_PYTHON=/absolute/path/to/supported-venv/bin/python
PYTHONPATH=services/docling-service "$TEST_PYTHON" -m unittest discover services/docling-service/tests
PYTHONPATH=docs/integrations/docling-serve-quality-parity "$TEST_PYTHON" -m unittest discover -s docs/integrations/docling-serve-quality-parity -p 'test_*.py'
```

The GitHub CI/release workflows provision Python 3.12 and run the same suites.

When changing real Docling conversion behavior, also run smoke tests on:

```text
/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline/test_pdfs/CN.pdf
```

and at least one English or two-column paper from:

```text
/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline/test_pdfs
```

Use temporary output directories for smoke tests.

Do not commit temporary or review outputs. Write review runs only under:

```text
.runtime/review/
```

If a task needs durable acceptance evidence, commit a small textual manifest or
summary instead of generated papers, page renders, model outputs, or source PDFs.

## Quality-first Docling requirements

For the internal Docling conversion contract (`--converter docling`), prefer quality
over speed.

The service may internally:

```text
- enable table structure extraction
- generate picture/table/page assets when useful
- detect bad text quality
- count /Gxx tokens
- trigger OCR fallback for bad text layers
- write tables/ and assets/ outputs
- record quality metrics in metadata/status
```

API users should not be required to choose an internal profile.

Metadata/status should record internal decisions, including where feasible:

```text
conversion_policy
ocr_fallback_used
text_quality_gxx_count
text_quality_gxx_density
table_count
asset_count
generated_outputs
warnings
```

If quality does not improve, report that honestly. Do not present command success as reading-quality success.

## Stop conditions

Stop and report if any of these occur:

```text
- The task requires changing a forbidden path.
- The task requires global dependency installation.
- The task requires Docker or service restarts without authorization.
- The task requires committing PDFs, cache, .venv, artifacts, or temporary outputs.
- Tests fail and cannot be fixed inside the allowed scope.
- The required local PDF/sample path is missing.
- A model/dependency cannot be obtained using the allowed local/mirror mechanisms.
- The implementation needs a product decision from the user.
- Git remote has changed and push would require pull/merge/rebase.
```

Do not stop for ordinary code errors, import errors, missing helper functions, or failing tests if they can be fixed inside the allowed scope.

## Final report format

Use a short final report:

```text
DONE
commit: <hash or none>
pushed: yes/no
remote: origin/main at <hash or known remote hash>
status: <clean / not clean / clean except ignored .venv>
tests: <brief test summary>
changed: <brief path summary>
blocked: <none or exact blocker>
next: <recommended next step>
```

For quality-validation tasks, also include:

```text
quality: <short finding>
review_outputs: <paths if any>
```
