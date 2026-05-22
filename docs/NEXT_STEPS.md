# 下一步计划

更新时间：2026-05-22

## 当前顺序

1. 新会话优先读取 GitHub / `kumaxs/local-ai-lab` canonical docs。
2. 再读取 Google Drive / `Local-Ai-Lab` recovery mirror。
3. 再参考 VS Code 当前共享文件。
4. 最后由 Codex / 用户补充本地运行状态、未提交变更、服务状态和 ignored runtime outputs。
5. 本地文档或工程变更完成后，先提交本地 commit。
6. commit 后必须进行 GitHub remote readiness 只读检查。
7. 如安全条件满足且用户授权，执行 `git push origin main`，让 GitHub-first 恢复入口读到最新 canonical state。
8. 再审阅 `docs/DOCLING_SERVICE_DESIGN.md`。
9. 再补 `docs/DOCLING_SERVICE_CONTRACT.md`。
10. 再补 `docs/DOCLING_SERVICE_TEST_PLAN.md`。
11. 不部署 Docling。
12. 不改变 `n8n-paper-pipeline` 主路径。

## 当前边界

- 不修改运行代码。
- 不修改 `local-ai-python-worker`。
- 不修改 n8n workflow。
- 不部署 Docling。
- 不运行 `docker compose`。
- 不重启任何服务。
- 不提交 inputs、outputs、PDF、env、token、数据库、日志或缓存。

## 当前事实源

- 对账后的 canonical engineering repo：`/Users/zeyuan/Projects/local-ai-lab`
- GitHub canonical remote / 新会话首要读取入口：`kumaxs/local-ai-lab`
- ChatGPT-facing recovery mirror：Google Drive `Local-Ai-Lab`
- 本地笔记 / 恢复提示词仓库：`/Users/zeyuan/Local-AI-Lab`

## GitHub push 条件

commit 后，如满足以下条件，应建议并在用户授权后执行 `git push origin main`：

- working tree clean。
- current branch = `main`。
- remote origin = `git@github.com:kumaxs/local-ai-lab.git`。
- `main` tracks `origin/main`。
- local branch is ahead of `origin/main`。
- local branch is not behind `origin/main`。
- no tracked sensitive-risk filenames。
- no untracked non-ignored files。

push 失败时，不得自行 `pull` / `merge` / `rebase` / `reset` / `clean` / force push；只能输出完整错误和最小修复建议。
