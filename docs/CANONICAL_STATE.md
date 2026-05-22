# Canonical State

更新时间：2026-05-21

## 1. 来源

本文件是 2026-05-21 多源对账后确认的 canonical state 草案。

对账输入包括：

- 工程仓库：`/Users/zeyuan/Projects/local-ai-lab`
- Google Drive：`Local-Ai-Lab`
- 本地笔记 / 恢复文档仓库：`/Users/zeyuan/Local-AI-Lab`

本文件用于在更新 Google Drive 前，先让用户审查本地 canonical files。

## 2. 项目身份

Local AI Lab 是整个长期项目，不是单一目录。

Local AI Lab 覆盖本地 AI、n8n 自动化、local-ai-python-worker、论文 intake pipeline、OpenClaw、EXO、Obsidian/Zotero、Google Drive 恢复材料和后续 AI reading workflow。

## 3. 仓库与镜像分工

- Engineering canonical repo: `/Users/zeyuan/Projects/local-ai-lab`
- Local notes/recovery repo: `/Users/zeyuan/Local-AI-Lab`
- Drive mirror/recovery folder: Google Drive / `Local-Ai-Lab`

对账后，`/Users/zeyuan/Projects/local-ai-lab` 是确认后的工程 canonical repo。

Google Drive / `Local-Ai-Lab` 是 ChatGPT-facing mirror / recovery entry。Drive 曾包含今天最新人工追加内容，因此后续不能再单边手写扩展为新的事实源；应从本地 canonical files 或明确 sync packet 同步。

`/Users/zeyuan/Local-AI-Lab` 保留为本地笔记、恢复提示词和人工桥接材料仓库，不替代工程 canonical repo。

## 4. 当前主路径

当前主路径：

```text
n8n -> local-ai-python-worker -> services/n8n-paper-pipeline
```

当前不改变 `n8n-paper-pipeline` 主路径。

## 5. 角色边界

### n8n

n8n 是 orchestration 和自动化入库层。

n8n 负责：

- 触发 worker jobs。
- 记录执行状态。
- 编排自动化入库。
- 准备后续知识工作流输入。

n8n 不负责论文精读，不承担复杂 PDF 理解或最终研究判断。

### local-ai-python-worker

`local-ai-python-worker` 是 n8n 的 external Python executor / slim capability layer。

worker 负责：

- 暴露受控 HTTP 执行入口。
- 使用 token auth。
- 执行 whitelisted jobs。
- 调用挂载的 pipeline 代码。
- 返回结构化状态。

worker 不是 PDF 处理负责人，也不应承载论文理解或研究判断逻辑。

### n8n-paper-pipeline

`n8n-paper-pipeline` 是 intake / detection / deduplication / routing / metadata / status / rough triage pipeline。

它负责自动化入库前处理和可解释状态输出，不是精读引擎，也不是高保真论文正文还原系统。

原 PDF 始终是证据源。`extract.md`、metadata、JSON、摘要或预读材料都只是派生工作材料。

### Docling

Docling 当前是 future sidecar structured parsing candidate。

当前不部署 Docling。

Docling 不得替换当前主路径。任何接入前必须先完成：

- API contract。
- Test plan。
- Sample validation。
- Failure / timeout policy。
- Stop conditions。
- Rollback plan。

### AI reading workflow

AI reading workflow 是后续精读层。

它应能回查原 PDF，也可以使用 pipeline 或 Docling 产生的结构化工件。它可以生成 preread outputs 或 draft notes，但不能替代用户正式研究笔记。

用户的人类研究笔记仍是最终知识资产。

## 6. 同步关系

同步顺序应为：

1. 本地工程仓库确认 canonical files。
2. 用户审查并确认。
3. 生成 Drive sync packet。
4. 更新 Google Drive / `Local-Ai-Lab`。
5. 回写 `docs/SYNC_CURSOR.md` 和 `docs/AI_WORKLOG.md`。

Google Drive 不能绕过 canonical state 单边成为新事实源。本地笔记仓库可以保存恢复提示词和人工桥接内容，但不覆盖工程 canonical files。

## 7. 当前下一步

1. 用户审查本地 canonical files。
2. 如通过，提交 canonical reconciliation commit。
3. 生成 Drive sync packet。
4. 更新 Google Drive / `Local-Ai-Lab`。
5. 回写 `docs/SYNC_CURSOR.md` 和 `docs/AI_WORKLOG.md`。
6. 再审阅 `docs/DOCLING_SERVICE_DESIGN.md`。
7. 再补 `docs/DOCLING_SERVICE_CONTRACT.md`。
8. 再补 `docs/DOCLING_SERVICE_TEST_PLAN.md`。
9. 不部署 Docling。
10. 不改变 `n8n-paper-pipeline` 主路径。
