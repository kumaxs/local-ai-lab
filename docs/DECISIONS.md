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
