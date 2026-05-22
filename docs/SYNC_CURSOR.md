# 同步游标

更新时间：2026-05-22

- Last local commit synced to Google Drive: `7996846de537c380144f4ddd06baa7f7666dc57b`
- Last Drive sync time: 2026-05-22 11:03:05 HKT +0800
- Drive folder: `Local-Ai-Lab`
- Source packet: `docs/DRIVE_SYNC_PACKET.md`
- Drive documents updated:
  - `Local AI Lab - 99 Latest Handoff`
  - `Local AI Lab - 00 Project Index`
  - `Local AI Lab - P0 Architecture Notes`
  - `Local AI Lab - P3 Paper PDF Intake Pipeline Notes`
  - `Local AI Lab - P4 Notes`
  - `Local AI Lab - P7 Notes`
- Drive documents skipped:
  - `Local AI Lab - P2 Notes`
- Drive documents failed:
  - none
- Sync result: success
- Notes: Google Drive final sync was performed append-only from `docs/DRIVE_SYNC_PACKET.md`. Older Drive content was not deleted or overwritten.

## GitHub remote sync

- GitHub repo: `kumaxs/local-ai-lab`
- GitHub visibility: public
- Remote origin: `git@github.com:kumaxs/local-ai-lab.git`
- Branch: `main`
- Latest confirmed remote HEAD: `d109f7b43efc129d8575c9478a1a4a365cfce520`
- Latest confirmed remote commit message: `document github first recovery workflow`
- Recovery role: GitHub is the first-read canonical docs and commit-state source for new ChatGPT sessions.
- Recovery order:
  1. GitHub / `kumaxs/local-ai-lab` canonical docs first
  2. Google Drive / `Local-Ai-Lab` recovery mirror second
  3. VS Code current shared files third
  4. Codex / user local runtime confirmation last
- Post-commit rule: after Codex creates a local commit, run GitHub remote readiness read-only checks. If safe and user authorizes it, run `git push origin main`.
- Closure rule: a local commit is not a synchronization closure. In the GitHub-first recovery model, an unpushed local commit is not reliable recovery state for new ChatGPT sessions.
- Auto-push scope after approval: documentation, state, sync-record, recovery-prompt, collaboration-rule, inventory, repo-structure, and service-boundary commits.
- Report-before-push scope: runtime code, Docker / compose / service configuration, n8n workflow, `local-ai-python-worker` runtime logic, `services/n8n-paper-pipeline` runtime logic, or any change that may affect live service behavior.
- Push failure rule: do not self-repair with `pull`, `merge`, `rebase`, `reset`, `clean`, or force push. Report the complete error and the smallest safe next step.
- Forbidden: do not push when readiness fails, when local branch is behind `origin/main`, when tracked sensitive-risk filenames or untracked non-ignored files are present, when GitHub push protection blocks the push, or when ignored runtime outputs would be added to Git.
