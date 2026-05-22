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
