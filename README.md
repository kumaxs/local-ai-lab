# Local AI Lab

Local AI Lab 是一个面向本地部署的文档 AI 与自动化实验仓库。当前正式交付组件是
**Docling Service 1.1.0**：它把 PDF 转换成结构化 HTML、Markdown、Docling JSON、
图片、表格、质量报告与可复核证据，并通过任务 API、Webhook 和 ZIP 下载接口供
n8n 或其他网络客户端调用。

[最新 Release](https://github.com/kumaxs/local-ai-lab/releases/latest) ·
[Docling Service 文档](services/docling-service/README.md) ·
[HTTP API](services/docling-service/docs/API.md) ·
[Docker 部署](services/docling-service/docs/DOCKER.md) ·
[macOS 部署](services/docling-service/docs/MACOS.md)

## 项目组成

| 目录 | 定位 | 发布状态 |
| --- | --- | --- |
| `services/docling-service/` | 高质量 PDF 转换服务、任务 API、Docker/macOS 发行配置 | 正式发布 |
| `services/n8n-paper-pipeline/` | 文档摄取、去重、路由及 n8n/worker 集成代码 | 集成与演进中 |
| `docs/integrations/` | Docling 质量对齐、公式识别和回归验证材料 | 工程证据 |
| `docs/`、`inventory/` | 架构决策、运行状态和历史审计记录 | 文档；部分记录具有时间点属性 |

本仓库不包含模型权重、用户 PDF、n8n 数据库、Obsidian/Zotero 私人数据、访问
令牌或生产运行结果。

## Docling Service 架构

```text
客户端 / n8n
    |
    |  HTTP multipart、任务查询、文件或 ZIP 下载
    v
Docling API :8766
    |-- SQLite WAL：任务、清单、幂等键、下载租约、Webhook outbox
    |-- /data/inputs：上传的源 PDF
    |-- /data/outputs/.staging：尚未发布的转换结果
    |-- /data/outputs/<job_id>：验证后发布的结果
    |
    +--> Docling backend :5001（Compose 私有网络）
    +--> Formula sidecar :8001（Compose 私有网络）
    +--> Webhook / CloudEvents --> n8n 或其他允许的主机
```

Docker 发行版由三个相互隔离的镜像组成：

- `ghcr.io/kumaxs/local-ai-lab-docling-api:1.1.0`
- `ghcr.io/kumaxs/local-ai-lab-docling-backend:1.1.0`
- `ghcr.io/kumaxs/local-ai-lab-docling-formula:1.1.0`

Release 工作流发布 `linux/amd64` 与 `linux/arm64` 镜像，以及可校验的 `.zip`、
`.tar.gz`、`SHA256SUMS` 和逐文件完整性清单。macOS 发行路径使用 Apple Silicon
原生组件；Docker/Linux 路径不依赖 OCRMac、MLX、Metal 或 Apple Vision。

## 在另一台机器上部署

### Docker：不执行 shell 脚本

从 [v1.1.0 Release](https://github.com/kumaxs/local-ai-lab/releases/tag/v1.1.0)
下载并解压 `docling-service-1.1.0.zip`，进入解压目录后直接运行 Compose：

```bash
docker compose \
  -f services/docling-service/deploy/docker/compose.release.yaml \
  pull

docker compose \
  -f services/docling-service/deploy/docker/compose.release.yaml \
  up -d
```

`compose.release.yaml` 只引用预构建 GHCR 镜像，没有 `build:`，也没有 `../../`
构建上下文，因此适合不能执行项目 shell 脚本的设备。源码开发用的
`compose.yaml` 才会使用仓库相对构建上下文。

默认只在 `127.0.0.1:8766` 暴露 API。若要向局域网或公网开放，必须同时配置
Bearer token、TLS/可信反向代理、防火墙和访问控制，不能只把绑定地址改成
`0.0.0.0`。

### macOS

从同一 Release 下载并验证归档，在 Apple Silicon Mac 上运行：

```bash
zsh install-macos.sh
```

安装、启动、停止、日志路径和回滚方式见
[macOS 部署说明](services/docling-service/docs/MACOS.md)。

## 模型从哪里下载

Docker 首次启动时，backend 和 formula 容器会把模型下载到独立 Docker named
volumes。Hugging Face 端点默认是：

```text
https://hf-mirror.com
```

需要切换到官方站点或其他兼容镜像时，在启动前覆盖：

```bash
export HF_ENDPOINT=https://huggingface.co
```

Docling/RapidOCR 模型保存在 `docling-models`，UniMERNet/PP-FormulaNet 模型保存在
`docling-formula-models`。模型卷不会随普通 `docker compose down` 删除，避免每次
启动重复下载。

## API 快速开始

服务启动后可直接通过网络读取完整 OpenAPI 3.1 文档：

- Swagger UI：`http://127.0.0.1:8766/docs`
- ReDoc：`http://127.0.0.1:8766/redoc`
- OpenAPI JSON：`http://127.0.0.1:8766/openapi.json`

提交 PDF：

```bash
curl -sS -X POST http://127.0.0.1:8766/v1/jobs \
  -H 'Authorization: Bearer TOKEN' \
  -H 'Idempotency-Key: n8n-run-123' \
  -F 'client_reference=paper-intake' \
  -F 'file=@/absolute/path/paper.pdf;type=application/pdf'
```

任务创建后可查询列表和状态，也可分别下载文件或一次请求下载完整 ZIP：

```text
GET    /v1/jobs
GET    /v1/jobs/{job_id}
DELETE /v1/jobs/{job_id}
GET    /v1/jobs/{job_id}/outputs
GET    /v1/jobs/{job_id}/manifest
GET    /v1/jobs/{job_id}/files/{relative_path}
GET    /v1/jobs/{job_id}/archive
GET    /v1/system/storage
```

`archive` 中包含所有已发布结果及 `manifest.json`，不包含用户上传的源 PDF。每个
文件在下载前或流式传输期间都会按照清单检查路径、大小和 SHA-256；ZIP 可在输出
过期前重复请求。

## 一次任务的完整工作流程

1. API 在接收 multipart 请求时限制请求体和 PDF 大小，并先写入受管临时目录。
2. 文件头、大小和参数通过校验后，服务计算 SHA-256 与幂等指纹。
3. 队列容量、总数据预算和磁盘剩余空间通过检查后，PDF 原子移动到任务输入目录，
   SQLite 同一事务登记任务和幂等键。
4. Worker 把转换结果写到 `.staging/<job_id>`；这时结果对下载接口不可见。
5. 只有必需文件、`status.json.ok=true`、路径、单任务总大小和清单校验全部通过，
   staging 目录才会原子发布为 `/outputs/<job_id>`，任务才进入 `succeeded`。
6. 失败或超时任务进入 `failed`，服务异常重启时未结束任务会恢复为
   `interrupted`；可验证的部分输出仍按较短保留期管理。
7. 终态、不可变 manifest 和 Webhook outbox 在数据库中一起落盘。Webhook 使用
   CloudEvents 1.0、稳定事件 ID、HMAC-SHA256 和最多六次投递。
8. 调用方查询任务、下载单文件或 ZIP；下载期间的可续租 lease 阻止清理线程移除
   正在传输的结果。
9. 后台 janitor 周期性删除过期输入、输出、孤立临时数据和历史记录。终态任务也可
   通过 `DELETE /v1/jobs/{job_id}` 提前清理。

任务状态只有 `queued`、`running`、`succeeded`、`failed` 和 `interrupted`。
SQLite WAL 是权威状态源；`state/jobs/` 下同时保存严格的兼容 JSON 镜像。一个数据
目录只能运行一个 API 实例。

## 文件和临时文件生命周期

默认清理周期为 5 分钟。所有时间和配额都可通过 `DOCLING_*` 环境变量调整。

| 数据 | 位置（Docker） | 默认生命周期 | 删除方式 |
| --- | --- | ---: | --- |
| multipart 临时上传 | `/data/state/temp` | 正常请求结束立即删除；崩溃遗留 1 小时 | API `finally` + janitor |
| 源 PDF | `/data/inputs/<job_id>/source.pdf` | 24 小时 | janitor 或终态任务 DELETE |
| 转换 staging | `/data/outputs/.staging/<job_id>` | 活跃任务保留；孤立数据 1 小时 | janitor；活跃任务不会被误删 |
| 成功输出 | `/data/outputs/<job_id>` | 7 天 | janitor 或终态任务 DELETE |
| 失败/中断输出 | `/data/outputs/<job_id>` | 2 天 | janitor 或终态任务 DELETE |
| 任务元数据/兼容 JSON | SQLite、`/data/state/jobs` | 30 天 | tombstone 清理 |
| 幂等键 | SQLite | 24 小时 | janitor maintenance |
| 下载 lease | SQLite | 5 分钟并在传输时续租 | 正常释放或过期清理 |
| Webhook 投递历史 | SQLite | 7 天 | janitor maintenance |
| 模型缓存 | Docker model volumes | 无自动 TTL | 运维人员显式清理 |

清理失败不会被当作成功：失败原因写回清理记录，并在租约过期后的后续扫描中重试。
清理路径必须位于配置的输入、输出或状态根目录内；符号链接和目录穿越不会被跟随。

## 存储膨胀与泄漏边界

任务受管数据不是无限增长的。默认保护线如下：

| 控制项 | 默认值 |
| --- | ---: |
| 单次上传上限 | 256 MiB |
| queued + running 任务数 | 20 |
| 单任务发布输出上限 | 5 GiB |
| 输入、输出和预留输出总预算 | 50 GiB |
| 文件系统最小剩余空间 | 2 GiB |

新任务会在突破任一保护线前被拒绝，`GET /v1/system/storage` 可查看当前受管字节、
预留空间、空闲空间和配置上限。任务完成时，最坏 5 GiB 的输出预留会释放并换成
实际 manifest 大小，避免配额长期虚占。

需要明确区分以下边界：

- **服务停止时不会清理。** Docker volumes 在停机期间仍然保留；服务重启后
  janitor 才会继续处理已过期数据。
- **模型卷不计入 50 GiB 任务预算。** 它们是有意持久化的下载缓存，升级或更换
  模型后可能增长，需要使用 `docker system df -v` 定期观察。
- **Docker 容器日志不属于任务生命周期。** 生产主机应配置 Docker 日志轮转；
  否则长时间运行的 stdout/stderr 可以独立占用磁盘。
- **SQLite 文件会复用已释放页，但通常不会自动缩小到历史最小尺寸。** 这不是活动
  数据泄漏；经历异常大流量后，数据库文件可能保持高水位，维护窗口内可备份后执行
  SQLite `VACUUM`。
- **手工写入 named volume 的文件不受数据库配额追踪。** 不要绕过 API 向
  `/data/inputs`、`/data/outputs` 或 `/data/state` 放置文件。
- `docker compose down` 保留数据；`docker compose down -v` 会不可恢复地删除模型、
  任务、输入和输出卷，只应在确认不再需要任何数据时使用。

因此，在正常 API 路径、服务持续或定期启动、没有人为绕过受管目录的前提下，输入、
临时文件、任务输出和 Webhook 历史都有明确上限与清理路径，不会无限增长。需要单独
运维的是模型缓存、Docker 日志和 SQLite 高水位。

## Webhook 与 n8n

Webhook 默认关闭。只有在 `DOCLING_WEBHOOK_ALLOWED_HOSTS` 显式列出回调主机后才能
创建订阅。事件包括：

- `docling.job.succeeded`
- `docling.job.failed`
- `docling.job.interrupted`

本机 n8n 使用私网地址时，还必须显式设置
`DOCLING_WEBHOOK_ALLOW_PRIVATE_HOSTS=true`。n8n 应在原始请求体上验证 HMAC，仅在
事件已经可靠入库后返回 2xx，并按 `X-Docling-Event-Id` 去重。完整订阅、投递查询和
手工重试接口见 [API 文档](services/docling-service/docs/API.md#webhooks)。

## 质量和安全原则

- 成功状态必须经过必需输出集、质量状态和不可变 manifest 校验，不能“有文件就算
  成功”。
- backend 和 formula sidecar 只在 Compose 私有网络中通信，不对宿主机发布端口。
- 输出路径被限制在任务目录内，拒绝符号链接、绝对路径和 `..` 穿越。
- Webhook 使用主机白名单、DNS 复查、私网阻断和禁止重定向来降低 SSRF 风险。
- API 支持 Bearer token；错误使用 RFC 9457 `application/problem+json`。
- 原 PDF 是证据源，所有 HTML、Markdown、JSON 和图片都是可重新生成的派生工件。

## 开发与验证

```bash
PYTHONPATH=services/docling-service \
  python3 -m unittest discover services/docling-service/tests

docker compose \
  -f services/docling-service/deploy/docker/compose.release.yaml \
  config --quiet
```

发行标签必须与代码版本完全一致。推送 `vMAJOR.MINOR.PATCH` 标签后，GitHub Actions
会执行测试、Python 编译、Compose/脚本验证、macOS 包验证、多架构镜像构建、SBOM/
provenance 生成和 GitHub Release 发布。

## 更多文档

- [跨机器分发与完整性验证](services/docling-service/docs/DISTRIBUTION.md)
- [Docker 安装和运维](services/docling-service/docs/DOCKER.md)
- [macOS 安装和运维](services/docling-service/docs/MACOS.md)
- [HTTP API 1.1](services/docling-service/docs/API.md)
- [输出契约](services/docling-service/docs/OUTPUTS.md)
- [发行架构与平台边界](services/docling-service/docs/RELEASES.md)
- [v1.1.0 Release Notes](services/docling-service/release/RELEASE_NOTES.md)

## 隐私与仓库边界

不要提交 `.env`、token、secret、credential、SQLite/数据库文件、用户 PDF、运行时
输入输出、模型缓存、n8n 数据目录、Obsidian Vault 或 Zotero 私人资料。仓库的
`.gitignore` 已覆盖常见路径，但提交前仍应检查 `git status` 和 staged diff。
