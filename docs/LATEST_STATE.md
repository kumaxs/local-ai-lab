# 当前状态

更新时间：2026-05-22

## 1. Reconciliation 后状态

本状态是在 2026-05-21 多源对账后写入。对账来源包括：

- 工程仓库：`/Users/zeyuan/Projects/local-ai-lab`
- Google Drive：`Local-Ai-Lab`
- 本地笔记 / 恢复文档仓库：`/Users/zeyuan/Local-AI-Lab`

对账前，Drive 曾包含今天最新人工追加内容，本地工程仓库也包含 Codex 新增的同步协议和 Docling 设计文件。因此当前不是简单镜像同步，而是先确认 canonical state，再执行 Drive final sync。

对账后，将 `/Users/zeyuan/Projects/local-ai-lab` 作为 canonical engineering repo。Google Drive / `Local-Ai-Lab` 保留为 recovery mirror。本地笔记仓库 `/Users/zeyuan/Local-AI-Lab` 保留为 recovery docs 和人工桥接材料仓库。

当前 git HEAD 以执行时 `git rev-parse HEAD` 为准。当前已确认本地与 GitHub remote `main` 的 HEAD 为：

```text
2df582057132d94f9fc42de1492d9f327aa58e30
```

GitHub repo `kumaxs/local-ai-lab` 已公开，并且 ChatGPT 已能读取。2026-05-22 之后，新会话恢复入口改为 GitHub-first：

1. GitHub / `kumaxs/local-ai-lab` canonical docs first。
2. Google Drive / `Local-Ai-Lab` recovery mirror second。
3. VS Code 当前共享文件 third。
4. Codex / 用户补充本地运行状态 last。

Google Drive 仍是 recovery mirror，但不再是首要读取入口。本地运行状态、未提交变更、服务状态和 ignored runtime outputs 仍必须由 Codex 或用户在本机确认。

## 2. 当前主路径

当前主路径：

```text
n8n -> local-ai-python-worker -> services/n8n-paper-pipeline
```

角色边界：

- `local-ai-python-worker` 是 n8n external Python executor / slim capability layer，使用 bounded job execution、token auth 和 whitelisted jobs；它不是 PDF 处理负责人。
- `n8n-paper-pipeline` 是 intake / detection / deduplication / routing / metadata / status / rough triage pipeline，不是精读引擎。
- n8n 负责 orchestration 和自动化入库，不负责论文精读。
- Docling 是 future sidecar structured parsing candidate；完成 contract、test plan、sample validation、failure/timeout policy、stop conditions 和 rollback 前，不部署、不替换主路径。
- AI reading workflow 是后续精读层，必须能回查原 PDF；人类研究笔记仍是最终知识资产。

原 PDF 始终是证据源。

## 3. 同步状态

- Reconciliation report 已生成于 `/Users/zeyuan/Local-AI-Lab/docs/RECONCILIATION_REPORT.md`，并复制到本仓库 `docs/RECONCILIATION_REPORT.md`。
- Canonical state 已写入 `docs/CANONICAL_STATE.md`。
- Google Drive final sync 已完成，结果 success，写入方式为 append-only。
- GitHub remote `origin` 指向 `git@github.com:kumaxs/local-ai-lab.git`，`main` 已 push 到 `2df582057132d94f9fc42de1492d9f327aa58e30`。
- 本地 commit 不等于同步闭环完成。Codex 完成本地 commit 后，必须进行 GitHub remote readiness 只读检查；若安全条件满足，应建议并在用户授权后执行 `git push origin main`。
- push 失败时，不得自行 `pull` / `merge` / `rebase` / `reset` / `clean` / force push，只能输出完整错误和最小修复建议。

## 4. 当前下一步

1. 保持 GitHub `kumaxs/local-ai-lab` 作为新会话首要 canonical docs 读取入口。
2. 本地文档变更完成后，先 commit，再做 GitHub remote readiness 只读检查。
3. 满足安全条件且用户授权后，及时 `git push origin main`。
4. 再审阅 `docs/DOCLING_SERVICE_DESIGN.md`。
