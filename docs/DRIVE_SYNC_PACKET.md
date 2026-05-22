# Drive Sync Packet

## 1. Source Commit

- source commit: `7996846de537c380144f4ddd06baa7f7666dc57b`
- repository path: `/Users/zeyuan/Projects/local-ai-lab`
- branch: `main`
- current HEAD: `7996846de537c380144f4ddd06baa7f7666dc57b`
- git status --short: clean before this packet was generated
- git status --short --ignored summary:
  - ignored macOS metadata: `.DS_Store`, `services/n8n-paper-pipeline/.DS_Store`
  - ignored local/runtime directories under `services/n8n-paper-pipeline/`: `.venv/`, `batch_outputs/`, `batch_outputs_ai/`, `future/`, `n8n_inbox/`, `n8n_outputs/`, `n8n_state/`, `test_pdfs/`
- clean working tree basis: yes. The canonical reconciliation commit was the current HEAD and the working tree was clean before `docs/DRIVE_SYNC_PACKET.md` was created.

## 2. Canonical State Summary

- Local AI Lab is the overall long-term project, not a single directory.
- Engineering canonical repo: `/Users/zeyuan/Projects/local-ai-lab`.
- Local notes/recovery repo: `/Users/zeyuan/Local-AI-Lab`.
- Drive mirror/recovery folder: Google Drive / `Local-Ai-Lab`.
- Current main path: `n8n -> local-ai-python-worker -> services/n8n-paper-pipeline`.
- worker role: `local-ai-python-worker` is an external Python executor / slim capability layer for n8n. It should provide bounded job execution, token authentication, and whitelisted jobs. It is not the paper processing owner.
- paper pipeline role: `services/n8n-paper-pipeline` handles intake, detection, deduplication, routing, metadata, status, and rough triage. Original PDFs remain the evidence source. It is not the close-reading engine.
- n8n role: n8n is the orchestration and automated ingestion layer. It triggers worker jobs, records status, and prepares downstream knowledge workflow inputs. It is not responsible for close reading.
- Docling role: Docling is a future sidecar structured parsing candidate. It must not be deployed or used to replace the main path until its contract, test plan, sample validation, failure and timeout policy, stop conditions, and rollback path are reviewed.
- AI reading workflow role: the future close-reading workflow may consult original PDFs and structured artifacts. It may produce preread outputs or draft notes, but human research notes remain the final knowledge asset.
- Current Docling status: do not deploy Docling.
- Current paper pipeline status: do not change the `n8n-paper-pipeline` main path.

## 3. Drive Current State Assessment

### Local AI Lab - 99 Latest Handoff

- needs update: yes.
- contains outdated content: yes. It contains older handoff sections, older commit references, pre-reconciliation sync state, and earlier direction notes that should now be treated as historical unless explicitly referenced.
- should mark superseded: yes, for older handoff sections and old local/Drive state assumptions.
- retain history only: yes, older handoff material should be preserved below the new top note.
- recommended action: append supersession note and current canonical handoff at the top.

### Local AI Lab - 00 Project Index

- needs update: yes.
- contains outdated content: yes. It contains useful 2026-05-21 direction-change material, but also older standalone path references and pre-reconciliation project layout assumptions.
- should mark superseded: yes, for old standalone path and old two-source assumptions.
- retain history only: partially. Historical sections should remain below the new canonical index update.
- recommended action: update by appending a canonical project index section at the top.

### Local AI Lab - P0 Architecture Notes

- needs update: yes.
- contains outdated content: partially. It has a mostly current architecture baseline, but still includes older next-work language around the custom n8n Python image path and a broad description of the paper pipeline as PDF processing owner.
- should mark superseded: yes, for any implication that the custom n8n Python image remains the active P4 path.
- retain history only: yes, older architecture notes should remain as history.
- recommended action: append a small canonical architecture baseline note.

### Local AI Lab - P3 Paper PDF Intake Pipeline Notes

- needs update: yes.
- contains outdated content: yes. It has a useful 2026-05-21 scope reduction note, but older standalone path references and older broader pipeline ownership language remain below.
- should mark superseded: yes, for old standalone path references and any implication that P3 owns close reading.
- retain history only: yes, older notes can remain as project history.
- recommended action: update by appending a canonical P3 role note at the top.

### Local AI Lab - P4 Notes

- needs update: yes.
- contains outdated content: yes. The top already reframes P4 as an external Python executor, but older custom n8n Python image plans remain lower in the document.
- should mark superseded: yes, for the old custom n8n Python image path except as historical fallback context.
- retain history only: yes.
- recommended action: update by appending a canonical P4 role and supersession note at the top.

### Local AI Lab - P7 Notes

- needs update: yes.
- contains outdated content: yes. It still describes the earlier Drive + Obsidian style documentation model and does not reflect the confirmed three-source structure.
- should mark superseded: yes, for the old two-layer documentation model.
- retain history only: yes.
- recommended action: update by appending the current documentation model and maintenance rule at the top.

### Local AI Lab - P2 Notes

- needs update: no immediate update required.
- contains outdated content: no direct canonical conflict was identified for the current sync objective. P2 remains paused / historical relative to the current paper pipeline and documentation reconciliation work.
- should mark superseded: no.
- retain history only: yes, keep as historical P2 context unless future P2 work resumes.
- recommended action: skip.

## 4. Exact Drive Update Plan

All Drive updates should be append-only and placed at the top of each target document. Existing content should remain below for history.

### Local AI Lab - 99 Latest Handoff

Recommended action: append this section at the top.

```markdown
# 2026-05-21 Canonical Reconciliation Completed

Canonical reconciliation has been completed for Local AI Lab.

Source commit: `7996846de537c380144f4ddd06baa7f7666dc57b`
Canonical engineering repo: `/Users/zeyuan/Projects/local-ai-lab`
Local notes/recovery repo: `/Users/zeyuan/Local-AI-Lab`
Drive mirror/recovery folder: Google Drive / `Local-Ai-Lab`

Current main path:

`n8n -> local-ai-python-worker -> services/n8n-paper-pipeline`

Current canonical state:

- Local AI Lab is the overall long-term project, not a single directory.
- `local-ai-python-worker` is the external Python executor / capability layer for n8n. It provides bounded job execution, token authentication, and whitelisted jobs. It is not the PDF processing owner.
- `services/n8n-paper-pipeline` owns intake, detection, deduplication, routing, metadata, status, and rough triage. Original PDFs remain the evidence source. It is not the close-reading engine.
- n8n owns orchestration and automated ingestion. It triggers worker jobs, records status, and prepares downstream workflow inputs. It is not responsible for close reading.
- Docling is only a future sidecar structured parsing candidate. Do not deploy it or replace the main path until contract, test plan, sample validation, failure/timeout policy, stop conditions, and rollback are reviewed.
- The future AI reading workflow may consult original PDFs and structured artifacts, and may produce preread outputs or draft notes. Human research notes remain the final knowledge asset.

Next steps:

1. Apply this Drive final sync from the canonical engineering repo.
2. Record the sync result back to `docs/SYNC_CURSOR.md` and `docs/AI_WORKLOG.md`.
3. Create `codex-reports/2026-05-21-drive-final-sync.md`.
4. Resume Docling design review only after the Drive sync is recorded.

Older handoff sections below are historical unless explicitly referenced by a newer canonical note.
```

### Local AI Lab - 00 Project Index

Recommended action: append this section at the top.

```markdown
# 2026-05-21 Canonical Project Index Update

Local AI Lab is the overall long-term project, not a single directory.

Current project structure:

1. Engineering canonical repo: `/Users/zeyuan/Projects/local-ai-lab`
2. Local notes/recovery repo: `/Users/zeyuan/Local-AI-Lab`
3. Drive mirror/recovery folder: Google Drive / `Local-Ai-Lab`

Current main path:

`n8n -> local-ai-python-worker -> services/n8n-paper-pipeline`

Service boundary:

- n8n: orchestration and automated ingestion.
- `local-ai-python-worker`: external Python executor / capability layer for n8n, with bounded jobs, token auth, and whitelisted jobs.
- `services/n8n-paper-pipeline`: intake, detection, deduplication, routing, metadata, status, and rough triage.
- Docling: future sidecar structured parsing candidate only; not deployed and not a main-path replacement.
- AI reading workflow: future close-reading layer that can consult original PDFs and structured artifacts.

Old standalone path references and older two-source documentation assumptions are historical unless explicitly renewed.
```

### Local AI Lab - P3 Paper PDF Intake Pipeline Notes

Recommended action: append this section at the top.

```markdown
# 2026-05-21 Canonical P3 Role

P3 current canonical role:

`services/n8n-paper-pipeline` is the paper PDF intake pipeline. It handles intake, detection, deduplication, routing, metadata, status, and rough triage.

Boundaries:

- Original PDFs remain the evidence source.
- P3 is not the close-reading engine.
- P3 should prepare reliable downstream inputs for orchestration and future reading workflows.
- Old standalone path references are historical.
- The current main path remains `n8n -> local-ai-python-worker -> services/n8n-paper-pipeline`.
```

### Local AI Lab - P4 Notes

Recommended action: append this section at the top.

```markdown
# 2026-05-21 Canonical P4 Role

P4 current canonical role:

`local-ai-python-worker` is the external Python executor / capability layer for n8n.

Boundaries:

- It should expose bounded, whitelisted jobs.
- It should use token authentication.
- It should execute selected Python capabilities on behalf of n8n.
- It is not the PDF processing owner.
- It does not replace `services/n8n-paper-pipeline`.

The old custom n8n Python image path is historical fallback context only. It should not be treated as the active implementation path unless a future decision explicitly revives it.
```

### Local AI Lab - P7 Notes

Recommended action: append this section at the top.

```markdown
# 2026-05-21 Canonical Documentation Model

The Local AI Lab documentation model now has three distinct sources:

1. Engineering canonical repo: `/Users/zeyuan/Projects/local-ai-lab`
2. Local notes/recovery repo: `/Users/zeyuan/Local-AI-Lab`
3. Drive mirror/recovery folder: Google Drive / `Local-Ai-Lab`

Drive is a recovery mirror and ChatGPT-facing entry point. It is not the engineering fact source after reconciliation.

The project file maintenance rule remains active: project state should be maintained through canonical files or explicit sync packets, not by ad hoc one-sided edits in Drive.

Older documentation model notes below are historical unless explicitly referenced by a newer canonical note.
```

### Local AI Lab - P0 Architecture Notes

Recommended action: append this section at the top.

```markdown
# 2026-05-21 Canonical Architecture Baseline

The current canonical architecture path is:

`n8n -> local-ai-python-worker -> services/n8n-paper-pipeline`

Current role boundaries:

- n8n: orchestration and automated ingestion.
- `local-ai-python-worker`: external Python executor / capability layer, not the paper processing owner.
- `services/n8n-paper-pipeline`: intake, detection, deduplication, routing, metadata, status, and rough triage.
- Docling: future sidecar structured parsing candidate only; not deployed and not a main-path replacement.
- AI reading workflow: future close-reading layer that can consult original PDFs and structured artifacts.

Any previous next-step language about switching to a custom n8n Python image is superseded by the worker-based architecture unless a future decision explicitly revives it.
```

### Local AI Lab - P2 Notes

Recommended action: skip.

Reason: P2 is not part of the current canonical reconciliation update path. No direct conflict was identified between P2 notes and the confirmed canonical state. Keep P2 as paused / historical context until P2 work resumes.

## 5. Local Cursor Update Plan

After Drive final sync is completed, update the local engineering repo in a separate approved step.

Files to update after Drive sync:

- `docs/SYNC_CURSOR.md`
- `docs/AI_WORKLOG.md`
- `codex-reports/2026-05-21-drive-final-sync.md`

Required cursor/worklog fields:

- Last local commit synced to Google Drive: `7996846de537c380144f4ddd06baa7f7666dc57b`
- Drive documents updated:
  - `Local AI Lab - 99 Latest Handoff`
  - `Local AI Lab - 00 Project Index`
  - `Local AI Lab - P0 Architecture Notes`
  - `Local AI Lab - P3 Paper PDF Intake Pipeline Notes`
  - `Local AI Lab - P4 Notes`
  - `Local AI Lab - P7 Notes`
- Drive documents skipped:
  - `Local AI Lab - P2 Notes`
- Drive documents failed: record any failed document names, or `none` if all writes succeed.
- sync result: `success`, `partial`, or `failed`.
- timestamp: record the actual completion timestamp when Drive writes finish.

The post-sync report `codex-reports/2026-05-21-drive-final-sync.md` should include:

- source commit synced
- Drive documents updated
- Drive documents skipped
- Drive documents failed
- whether Drive content was append-only
- whether local running code was modified
- whether services were started or restarted
- final `git status --short`
- final `git status --short --ignored` summary

## 6. Risks and Stop Conditions

- Drive documents should be append-only. Do not delete old Drive content.
- Old content should be marked `historical` or `superseded` to preserve project history while preventing stale instructions from being treated as current.
- If Drive reading fails, do not execute Drive sync.
- If Drive writing fails, retry at most two times per failed document, then stop and report the failure.
- If source commit is not equal to current HEAD, stop and report.
- If the working tree is not clean before Drive sync, stop and report.
- Do not sync runtime outputs, PDFs, env files, tokens, databases, logs, caches, or ignored files.
- Do not modify n8n workflow, `local-ai-python-worker` runtime logic, or `services/n8n-paper-pipeline` runtime logic as part of Drive sync.

## 7. Verification

- Running code modified: no.
- Dependencies installed: no.
- Services started or restarted: no.
- Google Drive modified: no.
- Git commit created: no.
- This file is only a Drive synchronization packet for later user-approved Drive updates.
