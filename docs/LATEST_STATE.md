# 当前状态

更新时间：2026-05-21

## 1. 仓库与镜像

- 工程事实源：`/Users/zeyuan/Projects/local-ai-lab`
- 本地笔记 / 恢复提示词仓库：`/Users/zeyuan/Local-AI-Lab`
- Google Drive 文件夹：`Local-Ai-Lab`

## 2. 当前主路径

当前主路径：

```text
n8n -> local-ai-python-worker -> services/n8n-paper-pipeline
```

说明：

- `local-ai-python-worker` 是 n8n 外部 Python 执行者 / slim capability layer。
- `n8n-paper-pipeline` 是 intake / metadata / status pipeline，不是精读引擎。
- Docling 是 sidecar structured parsing candidate，不能替换当前主路径。
- n8n 负责自动化入库，不负责论文精读。
- AI reading workflow 后续负责精读，并必须能回查原 PDF。

## 3. 当前迁移状态

- `services/n8n-paper-pipeline` 已位于工程事实源下。
- `/Users/zeyuan/Projects/n8n-paper-pipeline` 仍存在，但不是当前工程事实源。
- worker 容器内路径仍保持为 `/pipelines/n8n-paper-pipeline`。
- n8n 容器和 n8n workflow 当前不在本次同步协议中修改。

## 4. 当前下一步

1. 审阅 `docs/DOCLING_SERVICE_DESIGN.md`。
2. 补 `docs/DOCLING_SERVICE_CONTRACT.md`。
3. 补 `docs/DOCLING_SERVICE_TEST_PLAN.md`。
4. 再决定是否实现 `docling-service`。
5. 将 `docs/LATEST_STATE.md` 摘要同步到 Google Drive handoff。
