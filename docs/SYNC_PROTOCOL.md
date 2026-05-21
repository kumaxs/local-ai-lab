# 同步协议

更新时间：2026-05-21

## 1. 事实源分工

Local AI Lab 采用单主事实源加镜像摘要的同步模型。

- 本地 Git 仓库 `/Users/zeyuan/Projects/local-ai-lab` 是工程事实源。
- Google Drive 文件夹 `Local-Ai-Lab` 是 ChatGPT 恢复入口与状态镜像。
- 本地仓库 `/Users/zeyuan/Local-AI-Lab` 是本地笔记 / 恢复提示词仓库。
- Codex 是本地仓库写入者，负责把工程事实写入固定文件。
- ChatGPT 负责读取 Google Drive、审查、总结、维护 handoff。
- VS Code 是临时人工桥接，用于人工打开、查看、复制或核对文件。
- n8n 后续可作为自动同步器，但不是当前同步事实源。

## 2. 禁止双主自由写入

不允许 Google Drive 和本地工程仓库双主自由写入。

原因：

- Drive 更适合 ChatGPT 恢复上下文和维护摘要。
- 本地 Git 仓库更适合保存工程状态、结构、代码和可审计历史。
- 双主写入会导致 Codex、ChatGPT、VS Code 和 Drive 之间出现状态漂移。

## 3. 重大变更流程

重大变更必须先落入本地工程仓库固定文件，再同步 Google Drive 摘要。

建议顺序：

1. Codex 在 `/Users/zeyuan/Projects/local-ai-lab` 中更新工程文件或状态文档。
2. Codex 输出本地变更摘要和 Git 状态。
3. 用户确认后再提交 Git。
4. 确认需要同步时，将摘要写入 Google Drive `Local-Ai-Lab` 对应 handoff 或状态文档。
5. 更新 `docs/SYNC_CURSOR.md`，记录同步结果。

## 4. 固定状态文件

以下文件用于降低 Codex / ChatGPT / Drive 漂移：

- `docs/LATEST_STATE.md`：当前工程状态摘要。
- `docs/SYNC_PROTOCOL.md`：同步规则。
- `docs/SYNC_CURSOR.md`：本地到 Drive 的同步游标。
- `docs/AI_WORKLOG.md`：AI 协作工作日志。
- `docs/NEXT_STEPS.md`：下一步计划。
- `docs/DECISIONS.md`：已确认决策。

## 5. 同步记录要求

每次同步必须记录成功或失败。

记录内容至少包括：

- 本地提交 hash 或本地文件状态。
- Drive 文件夹或目标文档。
- 同步时间。
- 同步结果：成功、失败、部分成功或待确认。
- 失败原因和下一步补救动作。

## 6. 角色边界

Codex：

- 写入本地工程仓库。
- 维护 Markdown 状态文件。
- 生成同步摘要。
- 不把 Drive 当作工程主仓库。

ChatGPT：

- 读取 Drive。
- 审查和总结工程状态。
- 维护 handoff。
- 不绕过本地仓库直接重写工程事实。

Google Drive：

- 保存 ChatGPT 可读的恢复入口、handoff 和状态镜像。
- 不作为代码、运行逻辑或服务配置主事实源。

VS Code：

- 作为临时人工桥接。
- 可以暴露当前打开文件。
- 不保证完整文件夹遍历能力。

n8n：

- 后续可自动同步本地状态摘要到 Drive。
- 在同步自动化稳定前，不参与事实源裁决。
