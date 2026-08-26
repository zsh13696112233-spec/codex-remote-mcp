# Python 工作流服务

本模块同时提供 HTTP 工作流网关、Codex Orchestrator MCP 服务和 SQLite 状态存储，是整套平台的执行核心。

## 文件说明

```text
python-workflow/
├── src/
│   ├── workflow_gateway.py        HTTP、SSE、主监督和独立任务助手会话
│   ├── codex_orchestrator_mcp.py  MCP 工具及 app-server 客户端
│   └── workflow_store.py          SQLite 状态与事件存储
├── tests/                         Python 自动化测试
├── pyproject.toml
└── uv.lock
```

## 配置

| 环境变量 | 说明 |
| --- | --- |
| `CODEX_AGENTS_FILE` | 执行机配置文件，推荐使用仓库的 `config/agents.json` |
| `CODEX_WORKFLOW_DB` | 网关与 MCP 共同使用的 SQLite 数据库绝对路径 |

先从仓库根目录复制配置示例：

```powershell
Copy-Item .\config\agents.example.json .\config\agents.json
```

`agents.json` 是本机配置并已被 Git 忽略。令牌使用 `token_env` 引用环境变量，不要写入配置文件。

图片生成步骤完成时，MCP 会在原始事件被截断前提取 PNG、JPEG、GIF 或 WebP 图片并作为工作流附件写入共享数据库。单张图片上限为 20 MB，每个工作流最多 50 张。网关通过 `GET /workflows/{workflowId}/artifacts/{artifactId}` 只读返回附件，不提供任意文件路径读取能力。

网关启动时还会回填步骤结果中仍然存在、且位于 `$CODEX_HOME/generated_images` 受信目录内的历史图片链接；目录外路径不会读取。

## 步骤流转模式

工作流提交可携带 `advanceMode`：

- `automatic`：默认值，成功步骤完成后立即派发下一步骤。
- `semi_automatic`：仅支持严格串行工作流。成功步骤完成且仍有下一步骤时，SQLite 中创建 30 秒持久化等待；调用 `POST /workflows/{workflowId}/advance/{gateId}/confirm` 可立即放行，调用 `POST /workflows/{workflowId}/advance/{gateId}/hold` 可持久化暂停并取消自动放行。暂停后再次调用确认接口即可继续；未暂停且未确认时由运行时到期自动放行。

状态接口同时返回 `advanceMode` 和 `pendingAdvance`；后者通过 `state` 区分 `countdown` 和 `held`，并在暂停时返回 `heldAt`。暂停期间工作流仍为 `running`，暂停时间不计入主监督最长运行时间。最后一步、失败步骤和跳过步骤不创建等待；取消、重跑和其他使等待失效的状态变化会关闭旧等待，防止过期按钮影响新一轮执行。

## 返工要求

任务助手识别到 `restart_from` 时，会把用户说明的问题和修改点总结为可执行的 `revisionInstruction`，并在用户发送“确认执行”前展示。确认后的总结与来源消息、目标步骤和重跑序号一起持久化，只追加到目标步骤的实际提示词末尾，不修改原始提示词或不可变运行快照。多轮要求按时间累积，最新要求优先；返工上下文最多 20,000 字符，超限时省略最旧内容，最终提示词仍不超过 100,000 字符。没有新增修改要求的普通重跑不会生成空返工段落。

## 启动网关

在仓库根目录执行：

```powershell
$env:CODEX_AGENTS_FILE = (Resolve-Path .\config\agents.json)
$env:CODEX_WORKFLOW_DB = "$PWD\workflows.db"

uv run --project .\services\python-workflow `
  python .\services\python-workflow\src\workflow_gateway.py `
  --host 127.0.0.1 --port 8080 `
  --db $env:CODEX_WORKFLOW_DB --agents $env:CODEX_AGENTS_FILE
```

MCP 进程需要运行 `src/codex_orchestrator_mcp.py`，并传入完全相同的 `CODEX_AGENTS_FILE` 和 `CODEX_WORKFLOW_DB`。

## 测试

```powershell
uv run --project .\services\python-workflow `
  python -m unittest discover -s .\services\python-workflow\tests `
  -t .\services\python-workflow -v
```

长任务 MCP 子进程验证从仓库根目录运行：

```powershell
uv run --project .\services\python-workflow `
  python .\scripts\verify_long_job.py --delay-sec 3 --wait-sec 1
```
