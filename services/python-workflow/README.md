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

步骤发布的任意格式文件会作为工作流附件写入共享数据库；图片生成事件也会在事件截断前被合并。单文件上限为 20 MB，每个工作流的当前与历史文件合计最多 50 个，同一 SHA-256 内容自动去重。网关继续通过 `GET /workflows/{workflowId}/artifacts/{artifactId}` 只读返回附件，不提供任意文件路径读取能力。非图片响应强制附件下载并设置 `nosniff`。

网关启动时还会回填步骤结果中仍然存在、且位于 `$CODEX_HOME/generated_images` 受信目录内的历史图片链接；目录外路径不会读取。

## 步骤流转模式

工作流提交可携带 `advanceMode`：

- `automatic`：默认值，成功步骤完成后立即派发下一步骤。
- `semi_automatic`：仅支持严格串行工作流。成功步骤完成且仍有下一步骤时，SQLite 中创建 30 秒持久化等待；调用 `POST /workflows/{workflowId}/advance/{gateId}/confirm` 可立即放行，调用 `POST /workflows/{workflowId}/advance/{gateId}/hold` 可持久化暂停并取消自动放行。暂停后再次调用确认接口即可继续；未暂停且未确认时由运行时到期自动放行。

状态接口同时返回 `advanceMode` 和 `pendingAdvance`；后者通过 `state` 区分 `countdown` 和 `held`，并在暂停时返回 `heldAt`。暂停期间工作流仍为 `running`，暂停时间不计入主监督最长运行时间。最后一步、失败步骤和跳过步骤不创建等待；取消、重跑和其他使等待失效的状态变化会关闭旧等待，防止过期按钮影响新一轮执行。

## 返工要求

任务助手识别到 `restart_from` 时，会把用户说明的问题和修改点总结为可执行的 `revisionInstruction`，并在用户发送“确认执行”前展示。确认后的总结与来源消息、目标步骤和重跑序号一起持久化，只追加到目标步骤的实际提示词末尾，不修改原始提示词或不可变运行快照。多轮要求按时间累积，最新要求优先；返工上下文最多 20,000 字符，超限时省略最旧内容，最终提示词仍不超过 100,000 字符。没有新增修改要求的普通重跑不会生成空返工段落。

## 单次产物约束

运行时会在每个步骤的实际执行提示词末尾追加隐藏约束：每次步骤尝试只允许生成或修改一个面向用户交付的产物版本。首次产物完成后可以只读检查并报告问题，但不得自行重绘、重写、修正、覆盖或生成备选版本；产物交由人工审核。用户确认返工并开始新的步骤尝试后，才重新获得一次生成机会。

## 文件交接模式

- `handoffMode: "legacy_text"`：保留历史行为，把直接依赖步骤的文字结果追加到下一步。字段缺失时使用此模式。
- `handoffMode: "cumulative_files"`：不传递任何前序文字结果，返工要求也只属于目标步骤。第 N 步获得第 1 至 N-1 步的全部当前有效文件。

当前文件流水线要求编排器与 app-server 位于同一台机器，并在 `config/agents.json` 配置本机绝对路径 `artifact_root`。编排器直接在该根目录内为每次尝试创建 `inputs/step-N/` 和空 `output/`，提示词只交付绝对路径，不使用 Base64 传输，也不扫描业务工作区。`write=true` 必须发布恰好一个文件；`write=false` 允许只返回文字，如果发布文件则最多一个。`allow_write` 仍只控制 Agent 对业务工作区的写权限。前序文件仅作为可用输入；当前要求未明确要求使用时，Agent 不得打开或合并它们。远程文件上传将在远程固定目录协议完成后另行接入。

该约束不修改配置中心保存的原始提示词或不可变运行快照，也不在监控页面展示。它当前属于提示词约束，不在运行时拦截第二次工具调用。

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
