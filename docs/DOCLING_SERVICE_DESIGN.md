# Docling Service 设计文档

更新时间：2026-05-21

## 1. 设计背景

当前 Local AI Lab 的论文摄取链路已经跑通：

```text
n8n 容器
  -> HTTP 调用 local-ai-python-worker:8765
  -> worker 调用 /pipelines/n8n-paper-pipeline
  -> 宿主机路径 /Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline
```

当前 `paper-intake` 已验证通过，结果为 `HTTP 200`、`ok=true`、`processed=0 skipped=2 total=2`。

现有主入口是：

```text
services/n8n-paper-pipeline/scripts/process_inbox.py
```

该入口负责扫描 inbox、按 SHA-256 去重、识别 PDF/HTML/unsupported、调用当前 legacy 抽取脚本，并写入 `n8n_outputs` 与 `n8n_state`。现阶段不能替换这条已经跑通的主路径。

## 2. 核心原则

- 第一阶段只允许把 `docling-service` 作为旁路能力验证。
- 不能替换 `n8n-paper-pipeline` 当前已跑通的 `paper-intake` 主路径。
- 不能让 `local-ai-python-worker` 直接变成 PDF 处理主服务。
- 任何部署前必须先做最小样本测试和回滚方案。
- 任何接入 n8n 前，必须先用命令行或脚本验证 `docling-service` 的输入输出稳定。
- 任何输出都不得污染 Git：PDF 原文、运行输出、状态文件、日志、缓存、token、env 和数据库继续由 `.gitignore` 排除。

## 3. docling-service 的职责边界

`docling-service` 应该是一个独立文档解析服务，职责集中在“把输入文档解析成结构化中间结果”。

建议职责：

- 接收单个文档解析请求。
- 判断输入是否可解析。
- 对 PDF、HTML、浏览器打印 PDF 等文档执行结构化解析。
- 输出 Markdown、JSON metadata、解析质量标记、错误类型和可选资产索引。
- 对解析失败、需 OCR、权限不足、HTML 伪装 PDF 等情况返回明确状态。
- 保持服务级健康检查，便于后续由 pipeline 或测试脚本判断服务是否可用。

`docling-service` 的输出应服务于 `n8n-paper-pipeline`，而不是直接替代 pipeline 的业务流程。

## 4. docling-service 不应承担的职责

`docling-service` 不应该：

- 不应该扫描 n8n inbox。
- 不应该维护 `processed_index.json` 或全局去重状态。
- 不应该决定论文是否进入 Obsidian、OpenClaw 或 Zotero。
- 不应该直接调用 n8n workflow。
- 不应该直接读写 n8n 数据库或 n8n credentials。
- 不应该管理 `local-ai-python-worker` 的 job registry。
- 不应该成为 PDF/论文处理的总负责人。
- 不应该清理 legacy 代码。
- 不应该替换 `scripts/process_inbox.py` 的入口职责。
- 不应该在第一阶段承担 OCR 大规模处理、复杂队列调度或长期任务状态管理。

## 5. 与 n8n-paper-pipeline 的关系

`n8n-paper-pipeline` 是 intake / metadata / status / rough triage pipeline。它不是论文处理总负责人，也不是精读引擎。原 PDF 始终是证据源，pipeline 输出的 Markdown、metadata、JSON 或质量标记都只是派生工作材料。

它负责输入分类、去重、输出目录、状态文件、质量路由和后续材料准备。

建议关系：

```text
n8n-paper-pipeline
  -> 可选调用 docling-service
  -> 根据 docling-service 返回结果决定是否采用新解析产物
  -> 失败时回退当前 legacy extraction
```

第一阶段建议只增加概念设计和测试计划，不修改运行代码。未来接入时，`n8n-paper-pipeline` 应保持一个显式开关，例如“使用 Docling 旁路解析”或“仅测试 Docling，不影响主输出”。

主路径切换前，`n8n-paper-pipeline` 必须仍能用当前 `process_inbox.py` 跑通 `paper-intake`。

## 6. 与 local-ai-python-worker 的关系

`local-ai-python-worker` 的定位是 slim Python 执行层 / 能力补丁层，不是论文处理负责人。

当前 worker 职责：

- 暴露 `POST /jobs/paper-intake/run`。
- 校验 `LOCAL_AI_WORKER_TOKEN`。
- 在容器内调用 `/pipelines/n8n-paper-pipeline/scripts/process_inbox.py`。
- 提供 n8n 与本地 Python 脚本之间的执行桥。

未来即使存在 `docling-service`，也不应让 worker 直接变成 PDF 处理主服务。worker 可以继续作为“执行入口”，由它调用 `n8n-paper-pipeline`，再由 pipeline 选择性调用 `docling-service`。

不建议链路：

```text
n8n -> local-ai-python-worker -> docling-service -> 直接产出论文业务结果
```

建议链路：

```text
n8n -> local-ai-python-worker -> n8n-paper-pipeline -> 可选调用 docling-service
```

## 7. 与 n8n 的关系

n8n 当前只应继续调用 `local-ai-python-worker:8765` 的既有 endpoint。

n8n 负责 orchestration 和自动化入库。n8n 不负责论文精读，也不负责最终学术判断。

第一阶段不建议 n8n 直接调用 `docling-service`，原因：

- 会绕过 `n8n-paper-pipeline` 的去重、状态、输出目录和质量路由。
- 会让 workflow 过早绑定未验证的新服务。
- 会增加回滚复杂度。

接入 n8n 前，必须先用命令行或脚本验证 `docling-service` 的输入输出稳定，并确认 pipeline 的可选调用路径稳定。

## 7.1 与 AI reading workflow 的关系

未来 AI reading workflow 是论文精读层。

它可以使用 `n8n-paper-pipeline` 输出和 Docling sidecar artifacts 作为工作材料，但必须能回查原 PDF。人类研究笔记仍是最终知识资产。

Docling 输出、pipeline metadata、JSON、Markdown 或预读材料都不能替代原 PDF 证据，也不能替代用户正式研究笔记。

## 8. 输入输出边界建议

### 8.1 输入

第一阶段建议只支持单文件解析。

可选输入形态：

- 容器内可访问文件路径。
- 未来可扩展为 multipart 文件上传。

建议输入字段：

```json
{
  "input_path": "/data/inbox/example.pdf",
  "source_type_hint": "pdf",
  "output_mode": "markdown_json",
  "enable_ocr": false,
  "request_id": "optional-stable-id"
}
```

输入约束：

- `input_path` 必须位于允许挂载的输入目录内。
- 不允许传入任意宿主机路径。
- 不允许直接传入 token、cookie 或浏览器 session。
- 第一阶段不处理批量目录扫描。

### 8.2 输出

建议输出包括：

```json
{
  "ok": true,
  "status": "parsed",
  "parser": "docling",
  "request_id": "optional-stable-id",
  "source_type": "pdf",
  "markdown_path": "/data/outputs/example.md",
  "metadata_path": "/data/outputs/example.meta.json",
  "assets_dir": "/data/outputs/assets/example",
  "quality": {
    "needs_ocr": false,
    "has_tables": true,
    "has_figures": true,
    "warnings": []
  },
  "error": null
}
```

失败时建议输出：

```json
{
  "ok": false,
  "status": "failed",
  "error": {
    "type": "unsupported_or_parse_failed",
    "message": "short safe error message",
    "retryable": false
  }
}
```

输出原则：

- 响应体返回状态和路径，不返回超大正文。
- Markdown、metadata、assets、tables、logs 由服务写入运行时输出目录。
- 路径必须是容器内路径或 pipeline 可理解的相对路径。
- 输出目录必须被 `.gitignore` 覆盖。

## 9. 最小 API 调用边界建议

第一阶段最小 API：

```text
GET /health
POST /parse
```

`GET /health`：

- 用于确认服务启动。
- 不检查模型下载或 OCR 大依赖。
- 返回 `ok=true` 和版本信息即可。

`POST /parse`：

- 只处理单文件。
- 同步返回解析结果状态。
- 超时必须明确。
- 不做长期任务队列。

可选未来 API：

```text
GET /version
POST /parse-async
GET /jobs/{job_id}
```

这些不应出现在第一阶段最小部署里，除非同步解析已经被证明不可行。

## 10. 文件路径和数据落盘边界

建议未来服务目录：

```text
services/docling-service/
```

建议运行时挂载目录：

```text
runtime/docling/
├── inbox/
├── outputs/
│   ├── markdown/
│   ├── json/
│   ├── assets/
│   ├── tables/
│   └── logs/
└── tmp/
```

注意：以上只是未来建议，本设计阶段不创建 Docker 配置、不部署服务、不启动容器。

落盘原则：

- PDF 原文、HTML 原文、解析输出、资产、日志和临时文件都属于运行时数据。
- 运行时数据必须被 `.gitignore` 排除。
- Git 只提交服务代码、接口文档、测试说明和脱敏模板。
- `n8n-paper-pipeline` 负责决定是否复制、引用或转换 Docling 输出。

## 11. 超时、失败、回退策略

### 11.1 超时

建议第一阶段设置短超时，避免卡死现有链路：

- 健康检查：2 秒。
- 单文件解析：30 到 120 秒，按样本大小调整。
- OCR 如果启用，应有单独更长超时，并默认关闭。

### 11.2 失败类型

建议标准化错误类型：

- `unsupported_source_type`
- `parse_failed`
- `timeout`
- `needs_ocr`
- `encrypted_pdf`
- `downloaded_html_instead_of_pdf`
- `permission_denied`
- `output_write_failed`

### 11.3 回退

Docling 失败时，`n8n-paper-pipeline` 应能回退：

```text
Docling parse failed
  -> 记录失败 metadata
  -> 回退当前 pdf_extract.py / intake_detect.py
  -> 保持 paper-intake endpoint 返回可解释结果
```

第一阶段不允许因为 Docling 失败导致当前 `paper-intake` 主路径不可用。

## 12. OCR 决策点

第一阶段建议默认不启用 OCR。

启用 OCR 前需要确认：

- OCR 依赖是否会显著增加镜像体积。
- OCR 是否需要额外模型下载。
- OCR 是否影响解析速度和资源占用。
- OCR 输出是否稳定。
- OCR 失败是否能被清晰标记为 `needs_ocr` 或 `ocr_failed`。

建议策略：

- 默认：`enable_ocr=false`
- 当 Docling 或 legacy 抽取检测到 `needs_ocr=true` 时，只记录状态。
- 第二阶段后再考虑少量扫描 PDF 样本的 OCR 验证。

## 13. 同步或异步调用决策点

第一阶段建议同步调用。

同步调用适合：

- 最小可运行验证。
- 少量样本。
- 清晰地比较输入输出。
- 简单回滚。

异步调用适合未来场景：

- 大 PDF。
- OCR。
- 多文件批量。
- 需要排队和进度查询。

切换异步前必须定义：

- job id。
- job 状态机。
- job 输出路径。
- 过期清理策略。
- n8n 或 pipeline 如何轮询。

## 14. 最小可逆部署方案

未来最小部署建议：

1. 新增 `services/docling-service/`，只包含最小服务代码、README、测试脚本和脱敏配置模板。
2. 新增独立 compose 或 infra 文档，但不修改 n8n compose，不修改 worker compose。
3. 服务只暴露本地端口，例如 `8766`，并保持与 worker 的 `8765` 分离。
4. 先用命令行调用 `/health` 和 `/parse`。
5. 使用少量样本输出到 ignored 运行时目录。
6. 验证通过后，再让 `n8n-paper-pipeline` 用显式开关调用。

可逆性要求：

- 停止 `docling-service` 不影响当前 `paper-intake`。
- 删除或禁用 Docling 调用开关后，pipeline 仍走 legacy 路径。
- 不改 n8n workflow。
- 不改 worker endpoint。

## 15. 测试样本设计

第一阶段样本应少而有代表性。

建议样本组：

- 普通文本 PDF：验证基础 Markdown 和 metadata。
- 双栏论文 PDF：验证阅读顺序和章节结构。
- 扫描版 PDF：验证 `needs_ocr` 判断。
- HTML 文件：验证非 PDF 输入和错误分类。
- 浏览器打印 PDF：验证网页转 PDF 的结构稳定性。

每个样本记录：

- 输入文件类型。
- 预期状态。
- 是否需要 OCR。
- Markdown 是否有标题/章节层级。
- metadata 是否有页数、来源、质量标记和错误类型。
- 是否产生不可控大文件。
- 是否可被 `n8n-paper-pipeline` 消费。

## 16. 切换主路径前的停止条件

出现以下任一情况，停止切换主路径：

- `docling-service` 解析结果不稳定。
- 同一样本多次运行输出结构明显漂移。
- 解析失败时没有清晰错误类型。
- OCR 默认开启导致速度或资源占用不可控。
- 输出路径不受 `.gitignore` 保护。
- pipeline 无法回退当前 legacy 抽取路径。
- 需要修改 n8n workflow 才能继续。
- 需要让 `local-ai-python-worker` 直接承担 PDF 处理职责。
- 需要提交 PDF、outputs、logs、env、token 或数据库。
- 没有明确回滚方案。

## 17. 回滚方案

第一阶段回滚应非常简单：

1. 不启动或停止 `docling-service`。
2. 保持 `n8n-paper-pipeline` 当前 `process_inbox.py` 主路径不变。
3. 关闭任何未来 Docling 可选调用开关。
4. 保留 `local-ai-python-worker` 当前 endpoint 和挂载路径。
5. 不修改 n8n workflow。
6. 使用当前已验证链路继续运行：

```text
n8n -> local-ai-python-worker -> n8n-paper-pipeline -> legacy extraction
```

如果未来已经部署了 Docling，但验证失败，应只回滚 Docling 相关新增服务和配置，不回滚当前已稳定运行的 worker/pipeline 挂载关系。

## 18. 不确定项清单

当前仍需确认：

- Docling 在本机环境下的镜像体积、启动时间和依赖下载成本。
- Docling 对中文论文、双栏论文、表格密集论文和浏览器打印 PDF 的实际解析质量。
- 是否需要 OCR，以及 OCR 的依赖、速度和准确性。
- Docling 输出格式是否足够稳定，能否被 `n8n-paper-pipeline` 消费。
- 最小服务是否应该接收文件路径还是文件上传。
- `docling-service` 与 `n8n-paper-pipeline` 之间是否需要共享 volume。
- 是否需要为 Docling 输出资产建立单独 ignored runtime 目录。
- 后续是否需要异步 job 模式。
- 是否需要在 OpenClaw 或 Obsidian 侧定义新的 Markdown/Paper Card schema。

## 19. 当前阶段结论

当前阶段只应完成设计、样本计划和最小可逆部署方案。下一步不应直接修改 n8n、worker 或 pipeline 主路径。

推荐下一步是：

1. 编写 `docs/DOCLING_SERVICE_CONTRACT.md`。
2. 编写 `docs/DOCLING_SERVICE_TEST_PLAN.md`。
3. 明确 sample validation plan。

下一步不是 implementation，不是 deployment，不是 n8n workflow change，也不是 `n8n-paper-pipeline` main-path replacement。
