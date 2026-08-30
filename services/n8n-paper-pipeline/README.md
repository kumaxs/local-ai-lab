# n8n-paper-pipeline

本项目是 Local AI Lab 的文档 / 论文摄取管道。

## 项目定位

本项目负责文档进入本地 AI 系统后的业务流程，包括：

- 接收或扫描新文档；
- 判断文档类型；
- 调用外部解析服务，例如 Docling；
- 整理 Markdown、JSON、图片、表格、日志等解析结果；
- 生成 Paper Intake Card；
- 为 OpenClaw 深读准备材料；
- 为 Obsidian 知识沉淀准备最终笔记素材。

本项目不是独立 PDF 阅读器，也不应该依赖脆弱的“PDF 全文抽取 + 直接总结”作为主路径。

## 当前运行关系

当前文档基于静态配置关系：

```text
n8n
↓ HTTP 调用
local-ai-python-worker
↓ 挂载项目目录
/pipelines/n8n-paper-pipeline
↓ 宿主机真实路径
/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline
```

本轮未对容器运行态进行验证，以上为本项目当前配置关系。

## 当前入口脚本

当前入口脚本是：

```bash
scripts/process_inbox.py
```

典型调用方式：

```bash
python3 scripts/process_inbox.py \
  --input-dir n8n_inbox \
  --output-dir n8n_outputs \
  --state n8n_state/processed_index.json
```

`process_inbox.py` 负责：

- 遍历 inbox；
- 按 SHA-256 去重；
- 识别输入是 PDF、HTML 还是 unsupported；
- 写入 `n8n_outputs/run_summary.json` 和 `n8n_outputs/run_summary.md`；
- 更新 `n8n_state/processed_index.json`。

## Legacy 状态

旧 PDF 提取逻辑仍然保留，以免影响当前运行链路：

- `scripts/pdf_extract.py`
- `scripts/intake_detect.py`
- `scripts/batch_test_pdf_extract.py`

这些脚本现在被标记为 legacy。它们适合做兼容和回归验证，但不应继续扩展为主解析路径。

## Future: Docling-Ready

Docling Service v1.1.1 已发布，但尚未自动接入该 pipeline。当前 pipeline 仍沿用现有入口。

项目保留未来 Docling-ready 的演进边界，预期结构包括：

```text
future-artifacts-root (planned)
├── inbox/
├── staging/
├── outputs/
│   ├── markdown/
│   ├── json/
│   ├── assets/
│   ├── tables/
│   └── logs/
```

该结构暂时为未来占位，不接管当前运行入口，也不改变 Docker Compose。

## 文档

- `docs/ARCHITECTURE.md`: 当前架构、入口、legacy 和未来 Docling-ready 方向。
- `docs/LEGACY_PDF_EXTRACTION.md`: 旧 PDF 提取链路说明。

## 安全边界

迁移前不要移动当前入口脚本、改变输出合同或假设容器运行状态；服务状态和
挂载关系必须从实际运行环境另行核验。
