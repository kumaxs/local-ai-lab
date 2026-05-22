# Canonical State

更新时间：2026-05-22

## 1. 来源

本文件是 2026-05-21 多源对账后确认的 canonical state 草案。

对账输入包括：

- 工程仓库：`/Users/zeyuan/Projects/local-ai-lab`
- Google Drive：`Local-Ai-Lab`
- 本地笔记 / 恢复文档仓库：`/Users/zeyuan/Local-AI-Lab`

本文件用于在更新 Google Drive 前，先让用户审查本地 canonical files。

## 2. 项目身份

Local AI Lab 是整个长期项目，不是单一目录。

Local AI Lab 覆盖本地 AI、n8n 自动化、local-ai-python-worker、论文 intake pipeline、OpenClaw、EXO、Obsidian/Zotero、Google Drive 恢复材料和后续 AI reading workflow。

## 3. 仓库与镜像分工

- Engineering canonical repo: `/Users/zeyuan/Projects/local-ai-lab`
- GitHub canonical remote: `kumaxs/local-ai-lab`
- Local notes/recovery repo: `/Users/zeyuan/Local-AI-Lab`
- Drive mirror/recovery folder: Google Drive / `Local-Ai-Lab`

对账后，`/Users/zeyuan/Projects/local-ai-lab` 是确认后的工程 canonical repo。

GitHub remote `kumaxs/local-ai-lab` 已公开，并可作为 ChatGPT 新会话读取 canonical docs 和 commit state 的首要入口。当前已确认 remote `main` 同步到：

```text
d109f7b43efc129d8575c9478a1a4a365cfce520
```

Google Drive / `Local-Ai-Lab` 是 ChatGPT-facing mirror / recovery entry。Drive 曾包含今天最新人工追加内容，因此后续不能再单边手写扩展为新的事实源；应从本地 canonical files 或明确 sync packet 同步。

`/Users/zeyuan/Local-AI-Lab` 保留为本地笔记、恢复提示词和人工桥接材料仓库，不替代工程 canonical repo。

## 3.1 GitHub recovery order and push rule

2026-05-22 之后，新会话恢复顺序改为：

1. GitHub / `kumaxs/local-ai-lab` canonical docs first。
2. Google Drive / `Local-Ai-Lab` recovery mirror second。
3. VS Code 当前共享文件 third。
4. Codex / 用户补充本地运行状态 last。

Google Drive 仍是 recovery mirror，但不再是首要读取入口。VS Code 当前共享文件只能作为局部上下文，不能替代 GitHub / 本地 Git 状态。

本地实际运行状态、未提交变更、服务状态和 ignored runtime outputs 仍必须由 Codex 或用户在本机确认。

本地 commit 不等于同步闭环完成。GitHub-first 恢复方式下，未 push 的本地 commit 不能作为新会话可靠恢复状态，因为新会话可能只能看到 GitHub remote 上的最新 canonical docs。

Codex 产生本地 commit 后，必须继续进行 GitHub remote readiness 只读检查。若 readiness 通过，且用户已授权，应及时执行：

```bash
git push origin main
```

安全条件：

- working tree clean。
- current branch = `main`。
- remote origin = `git@github.com:kumaxs/local-ai-lab.git`。
- `main` tracks `origin/main`。
- local branch is ahead of `origin/main`。
- local branch is not behind `origin/main`。
- no tracked sensitive-risk filenames。
- no untracked non-ignored files。

如果 push 失败，不得自行 `pull` / `merge` / `rebase` / `reset` / `clean` / force push；只能输出完整错误和最小修复建议。

自动 push 适用范围：

- 文档类 commit。
- 状态类 commit。
- 同步记录类 commit。
- 恢复提示词 / 协作规则类 commit。
- inventory / repo structure / service boundary 类 commit。

不应自动 push 的范围：

- 运行代码变更。
- Docker / compose / service 配置变更。
- n8n workflow 变更。
- `local-ai-python-worker` 运行逻辑变更。
- `services/n8n-paper-pipeline` 运行逻辑变更。
- 任何可能影响实际服务运行的变更。

这些高风险场景 commit 后应先报告，等待用户明确授权再 push。

禁止行为：

- readiness 不通过时不得 push。
- behind `origin/main` 时不得 push。
- 发现疑似 secret / token / env / key / credential 文件时不得 push。
- push 失败时不得自行 `pull` / `merge` / `rebase` / `reset` / `clean` / force push。
- 不得绕过 GitHub push protection。
- 不得把 ignored runtime outputs 加入 git。

Codex 完成每次任务后应尽量使用极简状态报告，避免用户频繁复制长报告：

```text
DONE
commit: <hash or none>
pushed: yes/no
remote: origin/main at <hash or unknown>
status: clean / not clean
blocked: none / <reason>
next: <one-line next step>
```

## 4. 当前主路径

当前主路径：

```text
n8n -> local-ai-python-worker -> services/n8n-paper-pipeline
```

当前不改变 `n8n-paper-pipeline` 主路径。

## 5. 角色边界

### n8n

n8n 是 orchestration 和自动化入库层。

n8n 负责：

- 触发 worker jobs。
- 记录执行状态。
- 编排自动化入库。
- 准备后续知识工作流输入。

n8n 不负责论文精读，不承担复杂 PDF 理解或最终研究判断。

### local-ai-python-worker

`local-ai-python-worker` 是 n8n 的 external Python executor / slim capability layer。

worker 负责：

- 暴露受控 HTTP 执行入口。
- 使用 token auth。
- 执行 whitelisted jobs。
- 调用挂载的 pipeline 代码。
- 返回结构化状态。

worker 不是 PDF 处理负责人，也不应承载论文理解或研究判断逻辑。

### n8n-paper-pipeline

`n8n-paper-pipeline` 是 intake / detection / deduplication / routing / metadata / status / rough triage pipeline。

它负责自动化入库前处理和可解释状态输出，不是精读引擎，也不是高保真论文正文还原系统。

原 PDF 始终是证据源。`extract.md`、metadata、JSON、摘要或预读材料都只是派生工作材料。

### Docling

Docling 当前是 future sidecar structured parsing candidate。

当前不部署 Docling。

Docling 不得替换当前主路径。任何接入前必须先完成：

- API contract。
- Test plan。
- Sample validation。
- Failure / timeout policy。
- Stop conditions。
- Rollback plan。

### AI reading workflow

AI reading workflow 是后续精读层。

它应能回查原 PDF，也可以使用 pipeline 或 Docling 产生的结构化工件。它可以生成 preread outputs 或 draft notes，但不能替代用户正式研究笔记。

用户的人类研究笔记仍是最终知识资产。

## 6. 同步关系

同步顺序应为：

1. 本地工程仓库确认 canonical files。
2. 用户审查并确认。
3. 生成 Drive sync packet。
4. 更新 Google Drive / `Local-Ai-Lab`。
5. 回写 `docs/SYNC_CURSOR.md` 和 `docs/AI_WORKLOG.md`。

Google Drive 不能绕过 canonical state 单边成为新事实源。本地笔记仓库可以保存恢复提示词和人工桥接内容，但不覆盖工程 canonical files。

## 7. 当前下一步

1. 用户审查本地 canonical files。
2. 如通过，提交 canonical reconciliation commit。
3. 生成 Drive sync packet。
4. 更新 Google Drive / `Local-Ai-Lab`。
5. 回写 `docs/SYNC_CURSOR.md` 和 `docs/AI_WORKLOG.md`。
6. 再审阅 `docs/DOCLING_SERVICE_DESIGN.md`。
7. 再补 `docs/DOCLING_SERVICE_CONTRACT.md`。
8. 再补 `docs/DOCLING_SERVICE_TEST_PLAN.md`。
9. 不部署 Docling。
10. 不改变 `n8n-paper-pipeline` 主路径。
