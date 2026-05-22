# 下一步计划

更新时间：2026-05-21

## 当前顺序

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
- ChatGPT-facing recovery mirror：Google Drive `Local-Ai-Lab`
- 本地笔记 / 恢复提示词仓库：`/Users/zeyuan/Local-AI-Lab`
