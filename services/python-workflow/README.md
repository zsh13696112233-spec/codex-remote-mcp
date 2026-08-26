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
