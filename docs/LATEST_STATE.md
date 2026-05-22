# 当前状态

更新时间：2026-05-21

## 1. Reconciliation 后状态

本状态是在 2026-05-21 多源对账后写入。对账来源包括：

- 工程仓库：`/Users/zeyuan/Projects/local-ai-lab`
- Google Drive：`Local-Ai-Lab`
- 本地笔记 / 恢复文档仓库：`/Users/zeyuan/Local-AI-Lab`

对账前，Drive 曾包含今天最新人工追加内容，本地工程仓库也包含 Codex 新增的同步协议和 Docling 设计文件。因此当前不是简单镜像同步，而是先确认 canonical state，再执行 Drive final sync。

对账后，将 `/Users/zeyuan/Projects/local-ai-lab` 作为 canonical engineering repo。Google Drive / `Local-Ai-Lab` 保留为 recovery mirror。本地笔记仓库 `/Users/zeyuan/Local-AI-Lab` 保留为 recovery docs 和人工桥接材料仓库。

当前 git HEAD 以执行时 `git rev-parse HEAD` 为准：

```text
341f3d086eb4653305ef1248e4d968577cadc0f4
```

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
- Google Drive final sync 尚未完成，下一步需要用户先审查本地 canonical files。
- `docs/SYNC_CURSOR.md` 只有在 Drive final sync 实际完成后再回写。

## 4. 当前下一步

1. 用户审查本地 canonical files。
2. 如通过，提交 canonical reconciliation commit。
3. 生成 Drive sync packet。
4. 更新 Google Drive / `Local-Ai-Lab`。
5. 回写 `docs/SYNC_CURSOR.md` 和 `docs/AI_WORKLOG.md`。
6. 再审阅 `docs/DOCLING_SERVICE_DESIGN.md`。
