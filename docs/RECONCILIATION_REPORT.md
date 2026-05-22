# Local AI Lab Reconciliation Report

Generated: 2026-05-21

## 1. Purpose

This is not a simple sync task. The current Local AI Lab state is mixed across the engineering repository, Google Drive, and the local notes/recovery repository. The goal of this report is to identify differences, conflicts, gaps, duplicates, and merge recommendations before any new canonical state is written.

This report is read-only with respect to all existing project state files. It does not update Google Drive and does not overwrite `docs/LATEST_STATE.md`, `docs/DECISIONS.md`, `docs/NEXT_STEPS.md`, `docs/SYNC_CURSOR.md`, or `docs/AI_WORKLOG.md`.

## 2. Sources Read

### Engineering repository

- path: `/Users/zeyuan/Projects/local-ai-lab`
- pwd: `/Users/zeyuan/Projects/local-ai-lab`
- branch: `main`
- HEAD: `341f3d086eb4653305ef1248e4d968577cadc0f4`
- git status: clean from `git status --short`
- git status with ignored files:

```text
!! services/n8n-paper-pipeline/.DS_Store
!! services/n8n-paper-pipeline/.venv/
!! services/n8n-paper-pipeline/batch_outputs/
!! services/n8n-paper-pipeline/batch_outputs_ai/
!! services/n8n-paper-pipeline/future/
!! services/n8n-paper-pipeline/n8n_inbox/
!! services/n8n-paper-pipeline/n8n_outputs/
!! services/n8n-paper-pipeline/n8n_state/
!! services/n8n-paper-pipeline/test_pdfs/
```

Files read:

| File | Status | Modified time |
|---|---|---|
| `README.md` | read | May 21 14:19:48 2026 |
| `docs/NEXT_STEPS.md` | read | May 21 21:35:16 2026 |
| `docs/DECISIONS.md` | read | May 21 21:35:16 2026 |
| `docs/LATEST_STATE.md` | read | May 21 21:35:16 2026 |
| `docs/SYNC_PROTOCOL.md` | read | May 21 21:35:16 2026 |
| `docs/SYNC_CURSOR.md` | read | May 21 21:35:16 2026 |
| `docs/AI_WORKLOG.md` | read | May 21 21:35:16 2026 |
| `docs/DOCLING_SERVICE_DESIGN.md` | read | May 21 17:37:52 2026 |
| `inventory/services.md` | read | May 21 16:59:13 2026 |
| `inventory/repo_structure.md` | read | May 21 16:59:13 2026 |
| `codex-reports/2026-05-21-sync-protocol-init.md` | read | May 21 21:35:16 2026 |

### Google Drive

- folder: Google Drive / `Local-Ai-Lab`
- folder URL: `https://drive.google.com/drive/folders/12LYj3f36ZwZ3RqWUu4B7M-mVRkqizMPu`
- all requested documents were readable.

Documents read:

| Document | Modified time from Drive | Today appended topics |
|---|---:|---|
| `Local AI Lab - 99 Latest Handoff` | 2026-05-21T14:21:32.584Z | naming correction, sync protocol initialized, local path verification, project file maintenance rule, direction change, Drive folder structure, current handoff |
| `Local AI Lab - 00 Project Index` | 2026-05-21T11:02:25.294Z | direction change, Drive folder structure, repository/service boundary correction |
| `Local AI Lab - P0 Architecture Notes` | 2026-05-21T09:51:10.983Z | current architecture baseline, formal repo path, current execution path, service roles |
| `Local AI Lab - P3 Paper PDF Intake Pipeline Notes` | 2026-05-21T11:01:25.753Z | P3 scope reduced to automated intake, repository move, worker integration |
| `Local AI Lab - P4 Notes` | 2026-05-21T11:01:55.008Z | P4 reframed as external Python executor, worker validation, old custom image plan retained below |
| `Local AI Lab - P2 Notes` | 2026-05-20T13:07:37.144Z | no 2026-05-21 append observed |
| `Local AI Lab - P7 Notes` | 2026-05-20T13:08:35.849Z | no 2026-05-21 append observed |

Drive contains local-repo-missing content: yes. It contains the naming correction, path verification narrative, project file maintenance rule, detailed direction-change prose, and some old/new layering not fully represented in engineering files.

Drive contains obviously outdated content: yes. Old 2026-05-20 handoff and old P4 custom n8n Python image instructions remain appended below newer updates.

Drive contains conflicts with engineering repo: yes. Drive references older HEAD values and untracked file states in some appended sections, while the engineering repo now has HEAD `341f3d086eb4653305ef1248e4d968577cadc0f4` and a clean short status.

### Local notes repository

- path: `/Users/zeyuan/Local-AI-Lab`
- branch: `main`
- HEAD: `ccd45509ec7fa14374a41d0446641c83f8ac0848`
- git status:

```text
?? local_ai_lab_new_session_recovery_prompt.md
```

Files read:

| File | Status | Modified time |
|---|---|---|
| `README.md` | read | May 21 13:05:32 2026 |
| `local_ai_lab_new_session_recovery_prompt.md` | read | May 21 17:52:35 2026 |
| `00-Project-Index.md` | read | May 20 21:40:25 2026 |
| `99-Session-Handoffs/Latest-Handoff.md` | read | May 20 21:38:58 2026 |
| `01-Architecture/P0-Architecture-Notes.md` | read | May 20 21:38:58 2026 |
| `04-Paper-Pipeline/P3-Paper-PDF-Intake-Notes.md` | read | May 20 21:38:58 2026 |
| `02-n8n/P4-n8n-Python-Notes.md` | read | May 20 21:38:58 2026 |
| `03-OpenClaw/P2-OpenClaw-Memory-Notes.md` | read | May 20 21:38:58 2026 |
| `06-Agent-Workflow/README.md` | read | May 21 13:05:26 2026 |
| `docs/RUNTIME_STATE.md` | read | May 21 13:02:03 2026 |
| `docs/DECISIONS.md` | read | May 21 13:02:03 2026 |
| `docs/ARCHITECTURE.md` | read | May 21 13:02:03 2026 |
| `docs/REPOSITORY_STRUCTURE.md` | read | May 21 13:05:26 2026 |

Files containing today latest context:

- `local_ai_lab_new_session_recovery_prompt.md`: contains the clearest local summary of today's path correction, Drive access rule, worker boundary, Docling boundary, and next-session recovery guidance.
- `README.md`, `docs/RUNTIME_STATE.md`, `docs/DECISIONS.md`, `docs/ARCHITECTURE.md`, `docs/REPOSITORY_STRUCTURE.md`: updated today but less current than Drive and engineering repo.

## 3. Timeline Reconstruction

1. 2026-05-20: Google Drive and local notes created an initial two-layer documentation model: Drive for ChatGPT-facing recovery and local notes/Obsidian-style files for durable project notes.
2. 2026-05-20: P3 paper pipeline existed as `/Users/zeyuan/Projects/n8n-paper-pipeline`, with rough PDF extraction, file detection, metadata, OCR flags, quality flags, and deduplication.
3. 2026-05-20: P4 originally pointed toward a custom n8n Python image switch.
4. 2026-05-20 to 2026-05-21: `local-ai-python-worker` was created and validated as an external Python executor for n8n, with token auth and job whitelist.
5. 2026-05-21: `n8n-paper-pipeline` was moved into `/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline`.
6. 2026-05-21: `local-ai-python-worker` was redefined as an n8n external Python executor / slim capability layer, not as the paper-processing owner.
7. 2026-05-21: the worker mount was changed so the container path remains `/pipelines/n8n-paper-pipeline` while the host source points into the engineering repo.
8. 2026-05-21: `paper-intake` was validated through the worker with `HTTP 200`, `ok=true`, `processed=0`, `skipped=2`, `total=2`.
9. 2026-05-21: paper-pipeline scope was reduced to intake / metadata / status / rough triage, not high-fidelity extraction or close reading.
10. 2026-05-21: n8n direction changed to automated ingestion and orchestration, not paper close reading.
11. 2026-05-21: AI reading workflow was assigned future close reading responsibility and must be able to consult original PDFs.
12. 2026-05-21: Docling was positioned as a sidecar structured parsing candidate requiring contract, test plan, sample validation, stop conditions, and rollback before implementation.
13. 2026-05-21: Google Drive folder `Local-Ai-Lab` was established and populated with project documents.
14. 2026-05-21: ChatGPT appended multiple Drive updates covering path confirmation, sync protocol initialization, maintenance rules, naming correction, and direction changes.
15. 2026-05-21: GitHub connector was connected as `kumaxs` but could not read `kumaxs/local-ai-lab`; Drive stayed the ChatGPT-facing mirror.
16. 2026-05-21: local path roles were confirmed: `/Users/zeyuan/Projects/local-ai-lab` as engineering repository and `/Users/zeyuan/Local-AI-Lab` as local notes/recovery repository.
17. 2026-05-21: Codex added synchronization protocol files in the engineering repository: `docs/SYNC_PROTOCOL.md`, `docs/LATEST_STATE.md`, `docs/SYNC_CURSOR.md`, `docs/AI_WORKLOG.md`, and `codex-reports/2026-05-21-sync-protocol-init.md`.
18. 2026-05-21 later state: engineering repo now reports HEAD `341f3d086eb4653305ef1248e4d968577cadc0f4` and clean short status, which is newer than several Drive references.

## 4. Content Inventory by Topic

### Project identity and paths

- Engineering repo says: `/Users/zeyuan/Projects/local-ai-lab` is the engineering fact source; `/Users/zeyuan/Local-AI-Lab` is local notes/recovery; Drive `Local-Ai-Lab` is the ChatGPT mirror.
- Google Drive says: same high-level naming correction, plus explicit warning not to describe `/Users/zeyuan/Local-AI-Lab` as the whole Local AI Lab project.
- Local notes say: older files still present `/Users/zeyuan/Local-AI-Lab` as the project notebook/Obsidian-style area; recovery prompt has the newer correction.
- Gap / conflict / duplicate: local notes project index and handoff are stale; Drive has the clearest naming correction; engineering has the cleanest fixed-file summary.

### Sync protocol and cursor

- Engineering repo says: single-main engineering state plus Drive mirror; `docs/SYNC_CURSOR.md` records last local commit synced to Drive as `dffaeab2992805a07891cbb8280d34db87ca872a` and says cursor pending first confirmed sync.
- Google Drive says: sync protocol initialized in engineering repo and major changes must maintain project files.
- Local notes say: recovery prompt instructs new sessions to check Drive tools first and read Drive handoff/index before work.
- Gap / conflict / duplicate: engineering HEAD is now `341f3d086eb4653305ef1248e4d968577cadc0f4`, so the cursor does not reflect current HEAD. Drive has updates that were not merged back into fixed engineering state files.

### Worker boundary

- Engineering repo says: `local-ai-python-worker` is a slim capability layer / n8n external Python executor, not PDF owner.
- Google Drive says: same, with more detail about job whitelist, token auth, bounded behavior, and why old custom n8n image plan was superseded.
- Local notes say: older P4 notes still say custom n8n Python image is the current decision; recovery prompt has the newer worker boundary.
- Gap / conflict / duplicate: local notes P4 is stale; Drive and engineering agree on worker boundary.

### Paper pipeline boundary

- Engineering repo says: `n8n-paper-pipeline` is intake / metadata / status pipeline and current paper-intake main path.
- Google Drive says: P3 scope reduced to automated intake, detection, dedupe, routing, metadata/status, rough triage, and preparation for later AI reading.
- Local notes say: older P3 still includes rough extraction and next step as n8n integration; it does not fully include the new reduced scope.
- Gap / conflict / duplicate: local notes need superseding text; engineering does not yet include all of Drive's explanatory P3 scope language.

### n8n direction

- Engineering repo says: n8n is Docker port `5678`, current workflow not modified, n8n handles automation/inbound orchestration.
- Google Drive says: n8n direction changed to automated literature intake, not close reading.
- Local notes say: older P4 custom-image n8n direction remains in local files; recovery prompt corrects it.
- Gap / conflict / duplicate: local notes P4 and handoff are outdated; Drive has the best narrative rationale.

### Docling direction

- Engineering repo says: `docs/DOCLING_SERVICE_DESIGN.md` defines Docling as sidecar structured parsing candidate, not main-path replacement, with health/parse API, output contract suggestions, timeout/failure strategy, OCR decision points, and reversible deployment plan.
- Google Drive says: Docling must remain a sidecar candidate until contract, test plan, sample validation, timeout/failure policy, stop conditions, and rollback are reviewed.
- Local notes say: recovery prompt reflects this; older project docs do not.
- Gap / conflict / duplicate: engineering has the most detailed design file, but Drive has the recovery-friendly summary. A future `DOCLING_SERVICE_CONTRACT.md` and `DOCLING_SERVICE_TEST_PLAN.md` are still missing.

### AI reading workflow

- Engineering repo says: future AI reading workflow is responsible for close reading and must consult original PDFs.
- Google Drive says: AI reading should consume metadata/status/intake artifacts and original PDFs, then produce preread outputs or draft notes without replacing formal human research notes.
- Local notes say: older P6 only says local LLM path through EXO and n8n is proven; recovery prompt includes newer AI reading responsibility.
- Gap / conflict / duplicate: engineering lacks a dedicated AI reading workflow design; Drive contains direction but not implementation plan.

### Google Drive folder structure

- Engineering repo says: Drive folder `Local-Ai-Lab` is the ChatGPT recovery mirror.
- Google Drive says: folder contains 7 project documents and new sessions should read 99 handoff first, then index and relevant P-notes.
- Local notes say: older P7 records a smaller/more initial Drive setup; recovery prompt lists the 7 documents.
- Gap / conflict / duplicate: local notes P7 is stale. Drive folder structure is confirmed by actual folder listing.

### GitHub connector status

- Engineering repo says: GitHub connector can authenticate as `kumaxs` but cannot read `kumaxs/local-ai-lab`, so Drive remains the ChatGPT-facing mirror.
- Google Drive says: this status is included in the sync protocol initialized update.
- Local notes say: recovery prompt emphasizes Drive access, not GitHub connector.
- Gap / conflict / duplicate: no source currently records a remedy for GitHub connector access.

### Project documentation maintenance rule

- Engineering repo says: major changes must update fixed project files and report success/failure.
- Google Drive says: check relevant project files/data sources, update existing notes, add stable new notes, mark outdated content, record failed documentation attempts, then report what changed or failed.
- Local notes say: recovery prompt encodes Codex task style and read-only-first discipline.
- Gap / conflict / duplicate: rule is duplicated across Drive and engineering, but the exact required file list should be canonicalized.

### Next steps

- Engineering repo says: review `docs/DOCLING_SERVICE_DESIGN.md`; update README and inventory/services for sync protocol and P3/P4 boundaries; add Docling contract and test plan; then decide whether to implement; sync latest state to Drive.
- Google Drive says: review repo files read-only, align repository docs with today's direction, review Docling design, commit separately if clean, then add contract and test plan.
- Local notes say: older files say resume P4 custom image / P3 n8n integration; recovery prompt says review Docling design and do not deploy.
- Gap / conflict / duplicate: local notes handoff and P4 are outdated; engineering next steps should incorporate this reconciliation step before Docling review continues.

## 5. Conflicts and Ambiguities

1. Whether `/Users/zeyuan/Projects/local-ai-lab` should be confirmed as future canonical engineering state after this multi-source reconciliation.
2. Whether Drive's 2026-05-21 hand-written/appended updates should be merged back into `docs/LATEST_STATE.md`, `docs/DECISIONS.md`, and `docs/NEXT_STEPS.md`.
3. Whether `local_ai_lab_new_session_recovery_prompt.md` should stay only in `/Users/zeyuan/Local-AI-Lab`, be tracked in that repo, or be represented in the engineering repo as a sanitized recovery document.
4. Whether `docs/LATEST_STATE.md` is semantically complete enough; Markdown fence check found balanced code fences.
5. Whether `docs/DECISIONS.md` is semantically complete enough; Markdown fence check found balanced code fences.
6. Whether `docs/NEXT_STEPS.md` needs a reconciliation step before Docling review; no code fence break was detected.
7. Whether `docs/SYNC_CURSOR.md` is accurate: it references `dffaeab2992805a07891cbb8280d34db87ca872a`, while current engineering HEAD is `341f3d086eb4653305ef1248e4d968577cadc0f4`.
8. Whether Drive old handoff sections should be marked superseded instead of repeatedly appended below newer updates.
9. Whether local notes files from 2026-05-20 should be updated, marked stale, or left as historical snapshots.
10. Whether the old custom n8n Python image plan should remain only as historical fallback, since Drive and engineering now prefer `local-ai-python-worker`.
11. Whether Drive should be treated as a mirror only, given it currently contains newer human-written context that engineering files do not fully capture.
12. Whether `inventory/repo_structure.md` should be updated because it lists older docs such as `docs/RUNTIME_STATE.md` and does not include the new sync files in its visible top-level tree.

## 6. Proposed Canonical State

- Overall project: Local AI Lab.
- Engineering canonical repo: `/Users/zeyuan/Projects/local-ai-lab`.
- Local notes/recovery repo: `/Users/zeyuan/Local-AI-Lab`.
- Drive mirror/recovery folder: Google Drive / `Local-Ai-Lab`.
- Current main path: `n8n -> local-ai-python-worker -> services/n8n-paper-pipeline`.
- Worker role: external Python executor / slim capability layer for n8n, with bounded job execution, token auth, and whitelisted jobs; it is not the paper processing owner.
- Paper pipeline role: intake / detection / deduplication / routing / metadata / status / rough triage pipeline; original PDFs remain the evidence source.
- n8n role: orchestration and automated ingestion, including triggering worker jobs, recording status, and preparing downstream knowledge workflow inputs; not close reading.
- Docling role: future sidecar structured parsing candidate; no deployment or main-path replacement until contract, test plan, sample validation, failure/timeout policy, stop conditions, and rollback are reviewed.
- AI reading role: future close-reading workflow that can consult original PDFs and structured artifacts; it may produce preread outputs or draft notes, but human research notes remain the final knowledge asset.
- Current next steps:
  1. User reviews this reconciliation report.
  2. User confirms the canonical state.
  3. Update engineering fixed files to reflect reconciled state.
  4. Mark or summarize old Drive handoff sections as superseded after approval.
  5. Update sync cursor after confirmed Drive synchronization.
  6. Resume Docling design review, then contract and test plan.

## 7. Proposed File Updates After User Approval

### Engineering repository should update

- `docs/LATEST_STATE.md`: merge Drive's naming correction, path distinction, current HEAD/sync state, and reconciliation outcome.
- `docs/DECISIONS.md`: add decisions for multi-source reconciliation, Drive append policy, old P4 supersession, and local notes role.
- `docs/NEXT_STEPS.md`: add reconciliation approval before Docling review and include Drive/local-notes cleanup.
- `docs/SYNC_CURSOR.md`: update only after Drive sync is actually confirmed.
- `docs/AI_WORKLOG.md`: record this reconciliation task and sources read.
- `README.md`: align current scope with Drive's project identity wording and P3/P4 boundary.
- `inventory/services.md`: ensure service roles match worker-as-executor and Docling-sidecar wording.
- `inventory/repo_structure.md`: update top-level docs list to include sync files and Docling design.
- Consider adding `docs/CANONICAL_STATE.md`: recommended if the project needs one compact, user-approved canonical state separate from change logs.

### Google Drive should update

- `Local AI Lab - 99 Latest Handoff`: add a new canonical handoff after user approval; mark older 2026-05-20 and older P4 sections as superseded.
- `Local AI Lab - 00 Project Index`: update current priority and repository baseline after canonical confirmation.
- `Local AI Lab - P3 Paper PDF Intake Pipeline Notes`: keep reduced-scope P3 at top; mark old standalone path text as historical.
- `Local AI Lab - P4 Notes`: keep external worker direction at top; mark custom n8n image plan as historical fallback only.
- `Local AI Lab - P7 Notes`: update documentation model to include engineering repo plus local notes repo plus Drive mirror, not only Drive/Obsidian.

### Local notes repo should update

- `local_ai_lab_new_session_recovery_prompt.md`: decide whether to track it, then update after canonical approval.
- `README.md`: clarify that this repository is local notes/recovery, not the full project.
- `00-Project-Index.md`: mark stale or merge the 2026-05-21 direction change.
- `99-Session-Handoffs/Latest-Handoff.md`: replace or supersede the 2026-05-20 handoff.
- `02-n8n/P4-n8n-Python-Notes.md`: mark custom image plan as historical fallback.
- `04-Paper-Pipeline/P3-Paper-PDF-Intake-Notes.md`: update path to engineering repo service path and reduced P3 scope.

## 8. Recommended Reconciliation Procedure

1. User reviews this report.
2. User confirms canonical state.
3. Codex updates canonical files in the local engineering repository.
4. Codex or ChatGPT updates Drive after explicit approval.
5. Sync result is written back to `docs/SYNC_CURSOR.md`.
6. Continue Docling design review only after the canonical state is stable.

## 9. Verification

- This task did not modify running code.
- This task did not install dependencies.
- This task did not start or restart services.
- This task did not run `docker compose`.
- This task did not delete files.
- This task did not create a git commit.
- This task did not modify Google Drive.
- This task only added or overwrote `docs/RECONCILIATION_REPORT.md` in `/Users/zeyuan/Local-AI-Lab`.

Current engineering repository git status before writing this report:

```text
(clean from git status --short)
```

Current local notes repository git status before writing this report:

```text
?? local_ai_lab_new_session_recovery_prompt.md
```
