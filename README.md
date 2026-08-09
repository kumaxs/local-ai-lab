# Local AI Lab

本地优先的文档理解与自动化工程。当前主要交付是 **Docling Service
1.1.0**：接收 PDF，异步生成 HTML、Markdown、结构化 JSON、图片、公式与
可审计元数据，并通过标准 HTTP API、Webhook 和一次性 ZIP 下载对接 n8n
或其他业务系统。

Canonical path: `/Users/zeyuan/Projects/local-ai-lab`
Canonical GitHub: `https://github.com/kumaxs/local-ai-lab`

`/Users/zeyuan/Local-AI-Lab` 已退役（墓碑目录），当前会话 handoff 快照为唯一
可信传递文件 `HANDOFF.md`。

[最新 Release](https://github.com/kumaxs/local-ai-lab/releases/latest) ·
[API 文档](services/docling-service/docs/API.md) ·
[Docker 部署](services/docling-service/docs/DOCKER.md) ·
[macOS 部署](services/docling-service/docs/MACOS.md)

同一版本提供两种运行方式：

- Docker Compose：Linux `amd64` / `arm64`，适合服务器、NAS 和跨机器部署；
- macOS 安装包：Apple Silicon 为验收基线，保留本机 MLX/OCRMac 能力。

## 项目内容

| 路径 | 用途 |
| --- | --- |
| [`services/docling-service/`](services/docling-service/) | Docling API、任务队列、转换与发布逻辑 |
| [`services/docling-service/docs/API.md`](services/docling-service/docs/API.md) | API、Webhook、配额和环境变量 |
| [`services/docling-service/docs/DOCKER.md`](services/docling-service/docs/DOCKER.md) | Docker 部署与镜像说明 |
| [`services/docling-service/docs/MACOS.md`](services/docling-service/docs/MACOS.md) | macOS 安装与运行说明 |
| [`services/docling-service/docs/OUTPUTS.md`](services/docling-service/docs/OUTPUTS.md) | 输出文件合同 |
| [`services/docling-service/docs/DISTRIBUTION.md`](services/docling-service/docs/DISTRIBUTION.md) | 发布包、校验和与平台矩阵 |
| [`services/n8n-paper-pipeline/`](services/n8n-paper-pipeline/) | n8n 摄取与调用示例 |

仓库和发布包不内置模型权重、用户文档、生产数据库或密钥。

## 获取与部署

正式版本由一个 Git 标签 `v1.1.0` 统一锚定。标签触发测试、多架构镜像、
macOS/通用分发包和 GitHub Release；Docker 与 macOS 不再使用两个会漂移的
平台标签。

### Docker：直接用 Compose 部署

从 [GitHub Release v1.1.0](https://github.com/kumaxs/local-ai-lab/releases/tag/v1.1.0)
下载并校验 `docling-service-1.1.0.zip` 或 `.tar.gz`。目标机器只需 Docker
Engine 和 Compose v2；不需要 Git、Python，也不必执行 `.sh` 文件：

```bash
docker compose \
  -f services/docling-service/deploy/docker/compose.release.yaml \
  pull
docker compose \
  -f services/docling-service/deploy/docker/compose.release.yaml \
  up -d
```

如果设备没有 Shell，可在 Portainer、NAS 容器管理器或其他 Compose UI 中
导入同一份 `compose.release.yaml`，填写环境变量后点击部署。

Release Compose 拉取以下 GHCR 镜像：

- `ghcr.io/kumaxs/local-ai-lab-docling-api:1.1.0`
- `ghcr.io/kumaxs/local-ai-lab-docling-backend:1.1.0`
- `ghcr.io/kumaxs/local-ai-lab-docling-formula:1.1.0`

源码版 [`compose.yaml`](services/docling-service/deploy/docker/compose.yaml)
中的 `../../../..` 是合法的相对 build context：它让 Docker 构建能读取仓库
共享代码。跨机器交付使用的 `compose.release.yaml` 只有 `image:`，没有
`build:`，因此不依赖这些相对路径。

首次启动会把模型下载到 Docker named volumes。Hugging Face 默认站点为
`https://hf-mirror.com`；需要官方站点或其他兼容镜像时，在 Compose 环境中
覆盖：

```text
HF_ENDPOINT=https://huggingface.co
```

已下载模型会被复用。下载失败时容器重启会继续利用缓存重新尝试，但两个
站点之间不会在单次下载中自动切换；应通过 `HF_ENDPOINT` 明确选择可达站点。

### macOS

从同一 Release 下载、校验并解压后运行：

```bash
./install-macos.sh
```

开发环境也可从完整仓库执行：

```bash
zsh services/docling-service/deploy/macos/install.sh
```

安装器把每个版本放在
`~/Library/Application Support/Local AI Lab/docling-service/<version>`，模型缓存
位于 `~/.cache/docling/models`。详见
[`MACOS.md`](services/docling-service/docs/MACOS.md)。

## HTTP API

服务运行后可通过网络直接取得完整合同：

- OpenAPI 3.1：`GET /openapi.json`
- Swagger UI：`GET /docs`
- ReDoc：`GET /redoc`
- 健康检查：`GET /healthz`

提交 PDF：

```bash
curl -sS -X POST http://127.0.0.1:8766/v1/jobs \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Idempotency-Key: n8n-run-123' \
  -F 'client_reference=paper-flow' \
  -F 'file=@/absolute/path/paper.pdf;type=application/pdf'
```

响应为 `202 Accepted`，包含 `job_id`、状态、文件清单和 ZIP 链接。主要接口：

| 接口 | 作用 |
| --- | --- |
| `POST /v1/jobs` | multipart 上传 PDF、创建或幂等复用任务 |
| `GET /v1/jobs` | 按状态/业务引用分页查询任务列表 |
| `GET /v1/jobs/{job_id}` | 状态、错误、时间和生命周期截止点 |
| `DELETE /v1/jobs/{job_id}` | 主动删除终态任务的输入和输出 |
| `GET /v1/jobs/{job_id}/outputs` | 输出路径、大小、类型、SHA-256 和下载地址 |
| `GET /v1/jobs/{job_id}/manifest` | 不可变 manifest 及其摘要 |
| `GET /v1/jobs/{job_id}/files/{relative_path}` | 下载单个已发布文件 |
| `GET /v1/jobs/{job_id}/archive` | 流式生成一个 ZIP，不包含源 PDF |
| `GET /v1/system/storage` | 任务管理范围内的用量、预留量和磁盘余量 |
| `/v1/webhooks/subscriptions` | 创建、查看、修改和删除 Webhook 订阅 |
| `/v1/webhooks/deliveries` | 查看投递记录和手动重试 |

错误统一使用 `application/problem+json`。配置
`DOCLING_SERVICE_API_TOKEN` 后，所有 `/v1` 请求必须使用 Bearer Token。

## 一次任务的完整工作流程

```text
客户端 / n8n
  │ multipart PDF
  ▼
上传准入 ── 大小、并发槽、临时盘余量、PDF 头校验
  │
  ▼
state/temp ── 校验副本 ── 同卷临时副本/原子发布 ── inputs/<job_id>/source.pdf
  │                                      │ SQLite 登记 + 入队
  │                                      ▼
  │                            outputs/.staging/<job_id>
  │                                      │ 转换中持续监测大小/余量
  │                                      ▼
  │                            必需文件、status、路径、哈希校验
  │                                      │
  │                                      ▼ 原子发布
  │                              outputs/<job_id>
  │                                      │
  └──────── 即时清理失败路径              ├─ manifest / 单文件 / ZIP
                                         ├─ CloudEvents Webhook
                                         └─ Janitor 按 TTL 回收
```

任务状态固定为 `queued`、`running`、`succeeded`、`failed`、`interrupted`。
只有完整输出通过校验并原子发布后才会成为 `succeeded`。

## 文件与临时文件生命周期

默认目录（Docker）：

| 数据 | 位置 | 默认生命周期 | 自动治理 |
| --- | --- | --- | --- |
| multipart spool、API 校验副本 | `/data/state/temp` | 请求结束立即删；异常残留 1 小时 | 是 |
| 已登记输入 PDF | `/data/inputs/<job_id>` | 24 小时 | 是 |
| 崩溃窗口产生的孤儿输入 | `/data/inputs/<uuid>` | 未登记且超过 1 小时 | 是 |
| 转换中间产物 | `/data/outputs/.staging/<job_id>` | 成功时原子移动；异常残留 1 小时 | 是 |
| 成功输出 | `/data/outputs/<job_id>` | 7 天 | 是 |
| 失败/中断输出 | `/data/outputs/<job_id>` | 2 天 | 是 |
| 任务 tombstone/元数据 | `/data/state` SQLite + JSON mirror | 30 天 | 是 |
| Webhook 投递历史 | SQLite | 7 天 | 是 |
| Webhook 订阅 | SQLite | 直到显式 DELETE；默认最多 100 条 | 数量受限 |
| 单文件/ZIP 流 | 不生成持久 ZIP | 请求生命周期 | 关闭时释放租约 |
| Docker 服务日志 | Docker `json-file` | 默认 10 MB × 3/容器 | 是 |
| macOS 服务日志 | `.runtime/.../logs` | 默认 10 MiB × 当前文件和 3 个备份 | 是 |
| 模型缓存 | Docker model volumes / `~/.cache/docling/models` | 持久复用 | 否 |
| Docker 镜像缓存 | Docker Engine | 由 Docker 主机管理 | 否 |
| 旧 macOS 版本目录 | `Application Support/.../<version>` | 持久 | 否 |

Janitor 默认每 5 分钟运行。清理采用持久化 claim，失败会保留并重试；下载
期间的短租约会阻止输出被删除。路径必须位于配置根目录内，符号链接不会被
跟随，staging/output 的任务根本身也不得是符号链接。正在复制的上传会登记为
受保护临时文件，不会被短 TTL 的清理轮次误删。服务停止时 Janitor 也停止，
重启后会恢复清理；正常关机必须等清理线程退出后才关闭 SQLite。

### 容量边界

默认值：单 PDF 256 MiB、并发上传 2、排队/运行任务 20、单任务输出 5 GiB、
任务管理数据 50 GiB、磁盘最低保留 2 GiB。上传准入按“multipart spool +
校验副本”预留两倍请求空间；转换器运行时持续检查 staging 大小和磁盘余量，
越界即终止并清除不完整输出。转换日志只保留 stdout/stderr 各 4 MiB 尾部。

`DOCLING_MAX_DATA_BYTES` 的 50 GiB 统计任务输入、已发布输出和输出预留，
**不统计**模型、Docker 镜像、日志、SQLite/WAL 本身以及开发目录 `.runtime`
中的质量回归材料。`GET /v1/system/storage` 也只报告这一任务管理口径。

SQLite 删除记录后会复用空闲页，但数据库物理文件可能保持历史高水位；这不
等于数据继续泄漏，如需缩小文件应在停机维护窗口执行 checkpoint/VACUUM。

结论：正式 API 路径中的任务文件、临时文件、staging、下载和投递历史均有
边界，不存在已知的无限任务存储泄漏。仍需运维主动治理的是模型缓存、Docker
镜像缓存、旧 macOS 安装和 SQLite 物理高水位。开发/历史 CLI 不承诺这套 API
生命周期，生产自动化应只调用 `/v1`。

> `docker compose down` 保留 named volumes；`docker compose down -v` 会
> 删除模型、任务、输入和输出卷，是不可恢复的破坏性操作。

## Webhook / n8n

Webhook 默认关闭。设置 `DOCLING_WEBHOOK_ALLOWED_HOSTS` 后可登记回调地址；
内网/loopback 还需要显式设置 `DOCLING_WEBHOOK_ALLOW_PRIVATE_HOSTS=true`。
事件采用 CloudEvents 1.0 structured JSON，支持成功、失败和中断事件，使用
HMAC-SHA256 签名、稳定事件 ID、至少一次投递和默认最多 6 次重试。n8n 应在
持久接收后返回 2xx，并按事件 ID 去重。

## 安全与隐私

Docker 默认只把 API 绑定到 `127.0.0.1:8766`，macOS 默认绑定到
`127.0.0.1:8000`。向局域网或公网开放前，应同时启用 Bearer Token、TLS/可信
反向代理、防火墙和访问控制。不要提交 `.env`、token、Webhook secret、SQLite
数据库、用户 PDF、运行时输出、模型缓存、n8n 数据目录或私人知识库；提交前仍
应检查 `git status` 和 staged diff。

## 开发验证

```bash
cd services/docling-service
PYTHONPATH=. python3 -m unittest discover -s tests -v
docker compose -f deploy/docker/compose.yaml config --quiet
docker compose -f deploy/docker/compose.release.yaml config --quiet
```

更多质量合同、真实论文验收与发布架构见
[`services/docling-service/README.md`](services/docling-service/README.md)。
