# AI 工作日志

## 2026-05-21：同步协议初始化

- Confirmed engineering repository path: `/Users/zeyuan/Projects/local-ai-lab`
- Confirmed note/recovery repository path: `/Users/zeyuan/Local-AI-Lab`
- Confirmed `/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline` exists and is not an independent Git repository
- GitHub connector connected as kumaxs but cannot read kumaxs/local-ai-lab because repository access is unavailable
- Current strategy changed to Google Drive as ChatGPT-facing mirror
- Need local fixed state files to prevent Codex/ChatGPT drift
- VS Code can expose currently opened files but not guaranteed full folder traversal
- Google Drive Local-Ai-Lab folder is accessible by ChatGPT

## 2026-05-21：多源对账与 canonical state 固化

- 已生成多源对账报告。
- 报告位于 `/Users/zeyuan/Local-AI-Lab/docs/RECONCILIATION_REPORT.md`。
- 已将报告复制到工程仓库 `docs/RECONCILIATION_REPORT.md`。
- 三源状态：engineering repo 为 `/Users/zeyuan/Projects/local-ai-lab`，Drive 为 Google Drive / `Local-Ai-Lab`，local notes repo 为 `/Users/zeyuan/Local-AI-Lab`。
- 当前进入 canonical state 固化阶段。
- 已新增 `docs/CANONICAL_STATE.md`，并更新本地固定状态文件。
- Google Drive 尚未 final sync。

## 2026-05-22：Google Drive final sync

- 已根据 `docs/DRIVE_SYNC_PACKET.md` 执行 Google Drive / `Local-Ai-Lab` append-only final sync。
- Source commit: `7996846de537c380144f4ddd06baa7f7666dc57b`。
- 已更新 Drive 文档：
  - `Local AI Lab - 99 Latest Handoff`
  - `Local AI Lab - 00 Project Index`
  - `Local AI Lab - P0 Architecture Notes`
  - `Local AI Lab - P3 Paper PDF Intake Pipeline Notes`
  - `Local AI Lab - P4 Notes`
  - `Local AI Lab - P7 Notes`
- 已跳过 Drive 文档：
  - `Local AI Lab - P2 Notes`
- 失败项：none。
- 写入方式：append-only，仅在目标 Google Docs 顶部插入 packet 第 4 节对应正文；未删除旧内容，未覆盖整篇文档。
- 本地回写文件：
  - `docs/SYNC_CURSOR.md`
  - `docs/AI_WORKLOG.md`
  - `codex-reports/2026-05-21-drive-final-sync.md`

## 2026-05-22：GitHub public remote and recovery order change

- GitHub repo `kumaxs/local-ai-lab` 已公开。
- GitHub remote `origin` 为 `git@github.com:kumaxs/local-ai-lab.git`。
- 已确认 remote `main` 最新 HEAD：`2df582057132d94f9fc42de1492d9f327aa58e30`，commit message: `record drive final sync`。
- GitHub 已能被 ChatGPT 读取，后续新会话恢复顺序改为：
  1. GitHub / `kumaxs/local-ai-lab` canonical docs first。
  2. Google Drive / `Local-Ai-Lab` recovery mirror second。
  3. VS Code 当前共享文件 third。
  4. Codex / 用户补充本地运行状态 last。
- Google Drive 仍是 recovery mirror，但不再是首要读取入口。
- 本地实际运行状态、未提交变更、服务状态和 ignored runtime outputs 仍必须由 Codex 或用户在本机确认。
- Codex 完成本地 commit 后，必须进行 GitHub remote readiness 只读检查。
- 如 working tree clean、branch 为 `main`、origin 正确、`main` 跟踪 `origin/main`、本地 ahead 且不 behind、没有 tracked sensitive-risk filenames、没有 untracked non-ignored files，应建议并在用户授权后执行 `git push origin main`。
- push 失败时，不得自行 `pull` / `merge` / `rebase` / `reset` / `clean` / force push，只能输出完整错误和最小修复建议。
