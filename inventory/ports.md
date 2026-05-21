# 端口清单

## Docker 服务端口

| 服务 | 端口 | 说明 |
|---|---:|---|
| n8n | 5678 | n8n 当前运行在 Docker，不在本次任务中修改 |
| local-ai-python-worker | 8765 | worker HTTP API，供 n8n 调用 |

## 当前链路

```text
n8n -> http://local-ai-python-worker:8765 -> /pipelines/n8n-paper-pipeline
```

本次迁移只切换 worker 的宿主机 volume 源路径，不修改 n8n 容器和 n8n workflow。
