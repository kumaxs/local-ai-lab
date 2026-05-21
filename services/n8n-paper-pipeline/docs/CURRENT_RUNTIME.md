# 当前运行态说明

## 1. 结论

当前 `n8n-paper-pipeline` 没有独立的 Docker 容器，也没有由 launchd 或 cron 直接管理。

它当前是作为宿主机项目目录，被 `local-ai-python-worker` Docker 容器挂载后调用。

## 2. 当前真实路径

宿主机项目路径：

```text
/Users/zeyuan/Projects/n8n-paper-pipeline
```

容器内挂载路径：

```text
/pipelines/n8n-paper-pipeline
```

## 3. 当前调用关系

```text
n8n
↓ HTTP 调用
local-ai-python-worker
↓ bind mount
/pipelines/n8n-paper-pipeline
↓ 对应宿主机路径
/Users/zeyuan/Projects/n8n-paper-pipeline
```

`n8n` 本身当前没有直接挂载本项目。项目是通过 `local-ai-python-worker` 暴露的本地 HTTP 工作器间接被调用。

## 4. 当前运行中的相关容器

审计时观察到：

- `n8n`: 运行中，监听宿主机 `5678`。
- `local-ai-python-worker`: 运行中，监听宿主机 `8765`。

`local-ai-python-worker` 的 compose 文件位于：

```text
/Users/zeyuan/AI/n8n/local-ai-python-worker/docker-compose.yml
```

该 compose 文件将本项目挂载到容器内：

```text
/Users/zeyuan/Projects/n8n-paper-pipeline:/pipelines/n8n-paper-pipeline
```

## 5. 当前入口脚本

当前项目入口脚本是：

```text
scripts/process_inbox.py
```

典型调用方式：

```bash
python3 scripts/process_inbox.py \
  --input-dir n8n_inbox \
  --output-dir n8n_outputs \
  --state n8n_state/processed_index.json
```

入口脚本会继续调用：

- `scripts/intake_detect.py`
- `scripts/pdf_extract.py`

其中旧 PDF 提取逻辑已经标记为 legacy，但仍保留在原路径，避免破坏当前运行链路。

## 6. 未发现的管理方式

本轮审计未发现：

- 相关 launchd user agent；
- 相关 cron job；
- 独立的 `n8n-paper-pipeline` Docker 容器；
- 宿主机上直接运行的 paper pipeline 进程。

## 7. 当前不要改动的边界

在正式迁移到 Docling-ready 主路径之前，不建议：

- 删除 legacy 脚本；
- 移动 `scripts/process_inbox.py`；
- 修改 Docker Compose；
- 停止 `n8n` 或 `local-ai-python-worker`；
- 假设 `n8n` 容器直接访问本项目目录。

## 8. Future 方向

未来 Docling-ready 结构位于：

```text
future/docling-ready/
```

该目录目前只是未来解析产物和流程设计的占位结构，尚未接管当前运行入口。
