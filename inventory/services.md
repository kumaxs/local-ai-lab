# 服务清单

更新时间：2026-05-21

## n8n

- 类型：Docker 容器
- 端口：`5678`
- 当前状态：已运行
- 本次审计：未修改
- 当前角色：自动化入库 / orchestration。
- 说明：n8n workflow 当前未改动，仍通过 HTTP 调用 `local-ai-python-worker:8765`。
- 边界：n8n 负责触发、编排、状态记录和准备下游知识工作流输入；不负责论文精读、复杂 PDF 理解或最终研究判断。

## local-ai-python-worker

- 类型：Docker 容器
- 端口：`8765`
- 当前状态：已运行
- 本次审计：未修改
- 当前角色：n8n 外部 Python 执行者 / capability layer。
- 边界：使用 bounded job execution、token auth 和 whitelisted jobs 调用受控 Python 任务；不是 PDF 处理负责人，不承载论文理解或研究判断逻辑。
- 宿主机挂载源：

```text
/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline
```

- 容器内挂载路径：

```text
/pipelines/n8n-paper-pipeline
```

- 当前验证记录：`POST http://localhost:8765/jobs/paper-intake/run` 已通过，返回 `HTTP 200`、`ok=true`、`processed=0 skipped=2 total=2`。

## n8n-paper-pipeline

- 类型：业务代码 / Python pipeline
- 当前角色：intake / metadata / status / rough triage pipeline。
- 边界：负责 intake、file detection、deduplication、routing、metadata、status 和 rough triage；不是精读引擎，不是高保真论文正文还原系统。
- 证据规则：原 PDF 始终是证据源，抽取文本、metadata、JSON 和预读材料都是派生工作材料。
- 当前路径：

```text
/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline
```

- 当前调用方：`local-ai-python-worker`
- 当前 worker 调用入口：

```text
/pipelines/n8n-paper-pipeline/scripts/process_inbox.py
```

- 旧路径：

```text
/Users/zeyuan/Projects/n8n-paper-pipeline
```

- 旧路径状态：暂时保留，作为回滚来源。

## docling-service

- 类型：计划中的独立解析服务
- 当前状态：未部署
- 本次审计：未部署、未启动、未接入
- 当前角色：sidecar structured parsing candidate。
- 边界：不得替换当前 `n8n -> local-ai-python-worker -> n8n-paper-pipeline` 主路径。
- 下一阶段目标：先完成 contract、test plan、sample validation、failure/timeout policy、stop conditions 和 rollback plan，再决定是否做最小可运行验证。

## AI reading workflow

- 类型：计划中的后续精读层
- 当前状态：未实现
- 当前角色：论文 close-reading / preread / draft notes workflow。
- 边界：必须能回查原 PDF，可使用 pipeline 或 Docling 产生的结构化工件；不能替代用户正式研究笔记。

## EXO

- 类型：本地 OpenAI-compatible LLM 推理底座
- 当前角色：为本地 LLM 调用、自动化和后续 Agent workflow 提供推理能力。
- 本仓库定位：只记录架构与运行状态，不保存模型权重或运行数据。

## OpenClaw

- 类型：本地 Agent / 深读与展示工作台
- 当前角色：论文深读、任务执行和 Canvas 展示方向。
- 本仓库定位：只记录与 Local AI Lab 的关系，不提交 OpenClaw 私有记忆、运行状态或本地配置。

## Obsidian

- 类型：长期知识沉淀工具
- 当前角色：保存用户正式阅读笔记、项目知识和长期研究资产。
- 本仓库定位：不提交私人 Obsidian Vault，只提交脱敏后的项目文档和结构说明。

## Zotero

- 类型：文献管理工具
- 当前角色：管理论文条目、PDF 原文和引用信息。
- 本仓库定位：不提交 Zotero 数据库、PDF 原文或私人文献库，只记录系统关系。
