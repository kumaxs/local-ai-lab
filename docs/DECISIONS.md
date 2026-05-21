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
