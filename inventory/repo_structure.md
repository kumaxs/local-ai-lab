# 仓库结构索引

更新时间：2026-05-22

## 0. 外部仓库与恢复入口

- Local engineering canonical repo: `/Users/zeyuan/Projects/local-ai-lab`
- GitHub canonical remote: `kumaxs/local-ai-lab`
- GitHub remote URL: `git@github.com:kumaxs/local-ai-lab.git`
- Latest confirmed remote HEAD: `d109f7b43efc129d8575c9478a1a4a365cfce520`
- Google Drive recovery mirror: `Local-Ai-Lab`
- Local notes/recovery repo: `/Users/zeyuan/Local-AI-Lab`
- Push closure rule: local commit is not complete synchronization until GitHub remote readiness passes and an authorized `git push origin main` succeeds.

新会话恢复顺序：

1. GitHub / `kumaxs/local-ai-lab` canonical docs first。
2. Google Drive / `Local-Ai-Lab` recovery mirror second。
3. VS Code 当前共享文件 third。
4. Codex / 用户补充本地运行状态 last。

## 1. 顶层结构

```text
/Users/zeyuan/Projects/local-ai-lab
├── README.md
├── codex-reports/
│   ├── 2026-05-21-canonical-state-init.md
│   └── 2026-05-21-sync-protocol-init.md
├── docs/
│   ├── AI_WORKLOG.md
│   ├── CANONICAL_STATE.md
│   ├── DECISIONS.md
│   ├── DOCLING_SERVICE_DESIGN.md
│   ├── LATEST_STATE.md
│   ├── NEXT_STEPS.md
│   ├── RECONCILIATION_REPORT.md
│   ├── RUNTIME_STATE.md
│   ├── SYNC_CURSOR.md
│   └── SYNC_PROTOCOL.md
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
- `docs/AI_WORKLOG.md`
- `docs/CANONICAL_STATE.md`
- `docs/DECISIONS.md`
- `docs/DOCLING_SERVICE_DESIGN.md`
- `docs/LATEST_STATE.md`
- `docs/NEXT_STEPS.md`
- `docs/RECONCILIATION_REPORT.md`
- `docs/RUNTIME_STATE.md`
- `docs/SYNC_CURSOR.md`
- `docs/SYNC_PROTOCOL.md`
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

当前 Git 状态以执行 `git status --short` 和 `git status --short --ignored` 为准。
