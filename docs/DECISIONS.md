# 决策记录

## 2026-05-21：将 n8n-paper-pipeline 收敛进 local-ai-lab

决策：创建 `/Users/zeyuan/Projects/local-ai-lab` 作为新的总控项目，并将 `n8n-paper-pipeline` 复制到：

```text
/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline
```

原因：让 Local AI Lab 的文档、服务目录和运行路径逐步收敛到一个总控项目下，降低后续维护成本。

## 2026-05-21：保持 worker 容器内路径不变

决策：只修改 local-ai-python-worker 的 Docker Compose 宿主机挂载源路径，容器内路径继续保持：

```text
/pipelines/n8n-paper-pipeline
```

原因：n8n workflow 和 worker 内部调用依赖容器内路径。保持容器内路径不变，可以避免破坏现有运行链路。

## 2026-05-21：保留旧路径作为回滚来源

决策：不删除 `/Users/zeyuan/Projects/n8n-paper-pipeline`。

原因：如果 worker 挂载切换或接口验证失败，可以恢复 docker-compose.yml 备份并重新挂载旧路径。

## 2026-05-21：不部署 Docling

决策：本次迁移不部署 Docling。

原因：本次任务只做路径收敛、挂载切换、链路验证和文档记录，不扩大技术变更范围。

## 2026-05-21：确认工程事实源

决策：`/Users/zeyuan/Projects/local-ai-lab` 为工程事实源。

原因：该仓库保存当前代码、服务结构、运行状态文档、设计文档和后续迁移计划。

## 2026-05-21：确认本地笔记 / 恢复提示词仓库

决策：`/Users/zeyuan/Local-AI-Lab` 为本地笔记 / 恢复提示词仓库。

原因：该路径用于保存面向会话恢复、笔记和提示词的本地材料，不作为工程代码事实源。

## 2026-05-21：确认 Google Drive 的定位

决策：Google Drive 文件夹 `Local-Ai-Lab` 为 ChatGPT 恢复入口与状态镜像。

原因：ChatGPT 可稳定读取 Drive 资料，但工程变更必须先落入本地工程仓库，再同步摘要到 Drive。

## 2026-05-21：重新定义 worker 边界

决策：`local-ai-python-worker` 不再被定义为 PDF 处理负责人。

原因：worker 是 n8n 外部 Python 执行者 / slim capability layer，只负责暴露受控执行入口和调用 pipeline。

## 2026-05-21：收缩 paper pipeline 范围

决策：`n8n-paper-pipeline` 的范围收缩为 intake / metadata / status pipeline。

原因：pipeline 负责入库前处理、类型判断、状态、去重、metadata 和质量标记，不承担论文精读职责。

## 2026-05-21：Docling 只作为旁路验证

决策：Docling 只作为 sidecar structured parsing candidate，先做旁路能力验证。

原因：当前主路径已经跑通，Docling 不能直接替换主路径；接入前必须有 contract、test plan、样本验证和回滚方案。

## 2026-05-21：n8n 方向是自动化入库

决策：n8n 负责自动化入库，不负责论文精读。

原因：n8n 更适合触发、编排、状态流转和自动化入库；论文理解与深读应交给后续 AI reading workflow。

## 2026-05-21：精读交给后续 AI reading workflow

决策：论文精读交给后续 AI reading workflow，并且必须能回查原 PDF。

原因：结构化解析、摘要和预读不能替代原文证据；精读流程必须保留原 PDF 可追溯性。

## 2026-05-21：重大变更必须维护项目文件

决策：重大变更必须维护项目固定文件，并报告成功或失败。

原因：固定状态文件和同步游标可以降低 Codex、ChatGPT、Google Drive、VS Code、本地 Git 之间的状态漂移。

## 2026-05-21：多源对账后再同步

决策：当前存在工程仓库、Google Drive、本地笔记仓库三源混杂，不能直接把任一单源当作完整事实源。

原因：Google Drive 今天包含多次人工追加内容，本地工程仓库包含 Codex 新增的同步协议和 Docling 设计文件，本地笔记仓库包含恢复提示词和部分最新上下文。直接同步会放大冲突。

## 2026-05-21：接受 canonical state 草案

决策：接受 `docs/RECONCILIATION_REPORT.md` 第 6 节 Proposed Canonical State 作为 canonical 草案，并固化到 `docs/CANONICAL_STATE.md`。

原因：该草案明确了 Local AI Lab 的项目身份、工程仓库、Drive mirror、本地笔记仓库、主路径和各服务边界。

## 2026-05-21：确认 canonical engineering repo

决策：`/Users/zeyuan/Projects/local-ai-lab` 是对账后确认的 canonical engineering repo。

原因：工程代码、服务结构、固定状态文件、同步协议、Docling 设计和后续工程变更应在该仓库中维护。

## 2026-05-21：Drive 不再单边手写成为事实源

决策：Google Drive / `Local-Ai-Lab` 保留为 ChatGPT-facing mirror / recovery entry，但后续不得单边手写扩展为新的事实源。

原因：Drive 今天曾包含最新人工内容，但也保留了旧 handoff 和旧 P4 方案。后续 Drive 更新必须来自 canonical state 或明确的 sync packet。

## 2026-05-21：本地笔记仓库保留恢复材料

决策：`/Users/zeyuan/Local-AI-Lab` 保留为本地笔记、恢复提示词和人工桥接材料仓库。

原因：该仓库对新会话恢复有价值，但不替代 canonical engineering repo，也不直接覆盖工程固定文件。

## 2026-05-21：旧 handoff 和旧 P4 方案标记 superseded

决策：旧 handoff 中的旧 P4 自定义 n8n Python image 方案应标记为 superseded 或历史 fallback，不直接删除。

原因：保留历史上下文有助于回溯，但当前主线已经改为 `n8n -> local-ai-python-worker -> services/n8n-paper-pipeline`，不能让旧方案继续作为下一步指令。
