# 运行状态

更新时间：2026-05-21

## 当前容器

- n8n：Docker 容器运行，端口 `5678`。
- local-ai-python-worker：Docker 容器运行，端口 `8765`。

## 当前调用链

```text
n8n 容器
  -> HTTP 调用 local-ai-python-worker:8765
  -> local-ai-python-worker 调用 /pipelines/n8n-paper-pipeline
  -> 宿主机路径 /Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline
```

## 当前迁移状态

- `n8n-paper-pipeline` 已从旧路径完整复制到新路径。
- 新路径：`/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline`
- 旧路径：`/Users/zeyuan/Projects/n8n-paper-pipeline`
- 旧路径暂时保留，作为回滚来源。
- `local-ai-python-worker` 的 Docker Compose 挂载源已切换为新路径。
- worker 容器内路径仍保持为 `/pipelines/n8n-paper-pipeline`。

## 未变更内容

- 没有修改 n8n 容器。
- 没有修改 n8n workflow。
- 没有移动 `/Users/zeyuan/AI/n8n/local-ai-python-worker`。
- 没有改变容器内路径 `/pipelines/n8n-paper-pipeline`。
- 没有部署 Docling。
- 没有清理 legacy 代码。
- 没有重构 paper pipeline 业务逻辑。

## 安全边界

本项目只记录文档、路径、运行状态和模板。不要提交 `.env`、token、密钥、`n8n_data`、数据库、PDF 原文、私人笔记或运行时输出。

## 本次验证结果

- `local-ai-python-worker` 已重建并重启成功。
- Docker 挂载源已切换为 `/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline`。
- worker 容器内路径保持为 `/pipelines/n8n-paper-pipeline`。
- worker 容器内可以访问 `/pipelines/n8n-paper-pipeline`。
- `POST http://localhost:8765/jobs/paper-intake/run` 使用现有 `LOCAL_AI_WORKER_TOKEN` 测试成功。
- 接口返回 `HTTP 200`，job 返回 `ok=true`，本次结果为 `processed=0 skipped=2 total=2`。

结论：n8n 到 worker，再到 pipeline 的既有运行链路未被本次路径迁移破坏。
