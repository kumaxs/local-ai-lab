# Local AI Lab

Local AI Lab 是本地 AI、n8n 自动化、Python worker、论文处理流水线和项目文档的总控项目。

## 当前范围

本次初始化只做路径收敛、worker 挂载切换、运行链路验证和中文文档记录。

## 当前运行链路

```text
n8n 容器
  -> HTTP 调用 local-ai-python-worker:8765
  -> worker 调用 /pipelines/n8n-paper-pipeline
  -> 对应宿主机 /Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline
```

## 重要边界

- n8n 当前运行在 Docker，端口 5678。
- local-ai-python-worker 当前运行在 Docker，端口 8765。
- n8n-paper-pipeline 已迁入 `/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline`。
- worker 容器内挂载路径仍为 `/pipelines/n8n-paper-pipeline`。
- 旧路径 `/Users/zeyuan/Projects/n8n-paper-pipeline` 暂时保留作为回滚来源。
- Docling 论文转换服务已有 macOS 与 Docker 两套正式发行配置，入口见
  [`services/docling-service/README.md`](services/docling-service/README.md)。
- 当前没有修改 n8n 容器或 n8n workflow。

## 不应提交的内容

不要提交 `.env`、token、密钥、n8n_data、数据库、PDF 原文、Obsidian 私人笔记、Zotero 私人数据或运行时输出。
