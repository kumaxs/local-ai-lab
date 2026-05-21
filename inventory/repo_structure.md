# 仓库结构索引

更新时间：2026-05-21

## 1. 顶层结构

```text
/Users/zeyuan/Projects/local-ai-lab
├── README.md
├── docs/
│   ├── DECISIONS.md
│   ├── NEXT_STEPS.md
│   └── RUNTIME_STATE.md
├── infra/
├── inventory/
│   ├── paths.md
│   ├── ports.md
│   ├── repo_structure.md
│   └── services.md
├── services/
│   └── n8n-paper-pipeline/
└── templates/
```

## 2. n8n-paper-pipeline 顶层结构

```text
services/n8n-paper-pipeline
├── README.md
├── requirements.txt
├── docs/
├── runtime/
├── scripts/
├── n8n_inbox/        # 运行时输入，Git 忽略
├── n8n_outputs/      # 运行时输出，Git 忽略
├── n8n_state/        # 运行时状态，Git 忽略
├── n8n_logs/         # 运行时日志，Git 忽略
├── batch_outputs/    # 历史测试/批处理输出，Git 忽略
├── batch_outputs_ai/ # 历史测试/批处理输出，Git 忽略
├── test_pdfs/        # PDF 样本，Git 忽略
├── .venv/            # 本地虚拟环境，Git 忽略
└── future/           # Docling 未来占位，当前 Git 忽略
```

## 3. 代码目录

当前允许提交的业务代码主要在：

- `services/n8n-paper-pipeline/scripts/`

当前主要入口文件：

- `services/n8n-paper-pipeline/scripts/process_inbox.py`：worker 当前调用的 paper-intake 入口。
- `services/n8n-paper-pipeline/scripts/pdf_extract.py`：PDF 文本抽取与质量标记逻辑。
- `services/n8n-paper-pipeline/scripts/intake_detect.py`：输入类型检测逻辑。
- `services/n8n-paper-pipeline/scripts/batch_test_pdf_extract.py`：批量测试辅助脚本。

当前依赖文件：

- `services/n8n-paper-pipeline/requirements.txt`

## 4. 文档目录

仓库级文档：

- `README.md`
- `docs/RUNTIME_STATE.md`
- `docs/DECISIONS.md`
- `docs/NEXT_STEPS.md`
- `inventory/paths.md`
- `inventory/ports.md`
- `inventory/repo_structure.md`
- `inventory/services.md`

pipeline 内部文档：

- `services/n8n-paper-pipeline/README.md`
- `services/n8n-paper-pipeline/docs/ARCHITECTURE.md`
- `services/n8n-paper-pipeline/docs/CURRENT_RUNTIME.md`
- `services/n8n-paper-pipeline/docs/LEGACY_PDF_EXTRACTION.md`
- `services/n8n-paper-pipeline/runtime/README.md`

## 5. 运行时数据目录

以下目录是运行时数据或历史输出，不应提交：

- `services/n8n-paper-pipeline/n8n_inbox/`
- `services/n8n-paper-pipeline/n8n_outputs/`
- `services/n8n-paper-pipeline/n8n_state/`
- `services/n8n-paper-pipeline/n8n_logs/`
- `services/n8n-paper-pipeline/batch_outputs/`
- `services/n8n-paper-pipeline/batch_outputs_ai/`
- `services/n8n-paper-pipeline/test_pdfs/`
- `services/n8n-paper-pipeline/.venv/`
- `services/n8n-paper-pipeline/future/`

## 6. .gitignore 忽略范围

当前 `.gitignore` 已覆盖：

- `.env`、`*.env`、token、secret、credential、`secrets/`、`credentials/`
- `n8n_data/`、`n8n_inbox/`、`n8n_outputs/`、`n8n_state/`、`n8n_logs/`
- `inputs/`、`outputs/`、`data/`、`tmp/`
- `batch_outputs/`、`batch_outputs_ai/`、`test_pdfs/`
- `*.sqlite`、`*.sqlite3`、`*.db`、`*.log`
- `*.pdf`、`*.epub`、`*.docx`、`*.pptx`、`*.xlsx`
- `__pycache__/`、`.venv/`、`venv/`、`node_modules/`
- `.DS_Store`、IDE 本地配置
- `future/docling-ready/`

## 7. 当前 Git 状态

截至本次审计前：

- 最新提交：`c7ebebe init local-ai-lab after paper pipeline migration`
- Git 工作区干净。
- 只有 ignored 文件未跟踪，主要是运行时目录、PDF 样本、虚拟环境、输出和状态文件。
