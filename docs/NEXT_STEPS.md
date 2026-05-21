# 下一阶段迁移计划

更新时间：2026-05-21

## 背景

当前 `n8n-paper-pipeline` 已迁入：

```text
/Users/zeyuan/Projects/local-ai-lab/services/n8n-paper-pipeline
```

`local-ai-python-worker` 已挂载该路径到容器内：

```text
/pipelines/n8n-paper-pipeline
```

当前 `paper-intake` 链路已经验证通过。本次文档更新不修改运行代码、不重启服务、不部署 Docling。

## 第一阶段：设计 docling-service 服务边界

先明确 `docling-service` 是独立解析服务，而不是直接嵌入现有 `n8n-paper-pipeline` 的一段脚本。

需要定义：

- 输入格式：PDF、HTML、浏览器打印 PDF、未来可能的网页快照。
- 输出格式：结构化 Markdown、JSON metadata、质量标记、错误类型。
- API 边界：HTTP endpoint、请求体、响应体、超时和错误码。
- 文件边界：是否接收文件路径、文件上传，或由 worker 负责传递。
- 失败策略：不可解析、需 OCR、HTML 伪装 PDF、权限不足等场景如何返回。

## 第二阶段：部署 docling-service 最小可运行版本

部署一个最小版本，只证明服务可以启动、健康检查可用、单文件解析接口可调用。

原则：

- 不替换现有 `paper-intake` 主路径。
- 不修改 n8n workflow。
- 不清理 legacy 代码。
- 不引入大规模业务重构。

## 第三阶段：让 n8n-paper-pipeline 调用 docling-service

在 `n8n-paper-pipeline` 中新增可选调用路径，让 pipeline 能调用 `docling-service`。

要求：

- 旧 endpoint 和旧解析路径继续保留。
- 新路径先作为实验分支或显式配置项。
- 默认行为不破坏当前 `paper-intake`。
- 错误时可以回退到当前粗文本抽取逻辑。

## 第四阶段：少量样本解析质量测试

使用少量样本测试，不扩大数据范围。

建议样本：

- 普通 PDF。
- 双栏论文 PDF。
- 扫描版或图片型 PDF。
- HTML 下载结果。
- 浏览器打印 PDF。

评估维度：

- 文本完整性。
- 标题、章节、段落结构。
- 表格和图注处理。
- 是否误判 PDF/HTML。
- 是否能稳定输出质量标记。
- 失败时是否有清晰错误类型。

## 第五阶段：决定是否切换 paper-intake 主路径

只有在 `docling-service` 的质量、速度、错误处理和回滚路径都清晰后，才考虑切换 `paper-intake` 主路径。

切换前必须确认：

- n8n workflow 是否需要修改。
- worker endpoint 是否保持兼容。
- 旧路径是否还能快速恢复。
- 运行时状态和输出目录是否不会污染 Git。

## 第六阶段：稳定后归档旧路径

当新路径稳定运行，并确认不需要从旧路径回滚时，再处理：

```text
/Users/zeyuan/Projects/n8n-paper-pipeline
```

归档前不要删除旧目录。建议先记录：

- 最后一次验证时间。
- 新路径运行结果。
- 回滚窗口是否结束。
- 旧目录中是否还有未迁移内容。

## 当前禁止事项

- 不部署 Docling。
- 不修改 n8n 容器。
- 不修改 n8n workflow。
- 不重启任何 Docker 容器。
- 不修改 `local-ai-python-worker`。
- 不删除旧目录。
- 不提交 inputs、outputs、PDF、env、token、数据库、日志或缓存。
