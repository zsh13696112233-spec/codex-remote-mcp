# Python 工作流服务

本模块同时提供 HTTP 工作流网关、Codex Orchestrator MCP 服务和 SQLite 状态存储，是整套平台的执行核心。

## 文件说明

```text
python-workflow/
├── src/
│   ├── workflow_gateway.py        HTTP、SSE、主监督和独立任务助手会话
│   ├── codex_orchestrator_mcp.py  MCP 工具及 app-server 客户端
│   ├── workflow_runtime_client.py 远程 Sidecar 到中央内部 API 的客户端
│   ├── workflow_sidecar.py        Streamable HTTP MCP 与权威心跳入口
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
| `CODEX_SIDECAR_AGENT_ID` | 远程 Sidecar 对应的稳定主监督 ID |
| `CODEX_GATEWAY_INTERNAL_URL` | Sidecar 访问的中央 `8080` 地址 |
| `CODEX_GATEWAY_TOKEN_ENV` / `CODEX_GATEWAY_TOKEN_FILE` | Sidecar 读取中央机器令牌的二选一来源 |
| `CODEX_SIDECAR_HOST` / `CODEX_SIDECAR_PORT` | Sidecar 监听地址和端口，默认 `127.0.0.1:8082` |

先从仓库根目录复制配置示例：

```powershell
Copy-Item .\config\agents.example.json .\config\agents.json
```

`agents.json` 是本机配置并已被 Git 忽略。令牌可以使用 `token_env` 引用环境变量，或使用 `token_file` 引用网关所在机器上的绝对文件路径；两者只能配置一个，不要把令牌明文写入配置文件。令牌文件使用 UTF-8 编码，只包含一行令牌且不能超过 8 KiB；网关在每次建立连接时重新读取，便于轮换令牌。请通过 Windows ACL 或 Linux 文件权限限制网关运行账号以外的访问。

每个执行机可以配置：

- `enabled`：是否允许新工作流使用，默认 `true`。
- `capabilities`：只允许 `supervisor` 和 `executor`。旧配置未声明时，`local` 默认同时具备两种能力，其他执行机默认仅具备 `executor`。
- `capacity`：具备 `supervisor` 能力时默认且仅允许为 `1`；纯执行机不配置该字段。
- `token_env` / `token_file`：可选的认证令牌来源，分别表示环境变量名和网关本机的绝对文件路径，严格二选一。
- `orchestration_mode`：`local_db`（默认）或 `remote_sidecar`。旧配置保持本机兼容行为。
- `sidecar_token_env` / `sidecar_token_file`：仅 `remote_sidecar` 主监督使用的中央机器令牌来源，严格二选一；与 app-server 的连接令牌彼此独立。

`GET /agents` 只返回脱敏后的 ID、默认目录/模型、启停、能力、主监督容量和权限上限。对于具备主监督能力的执行机，还返回 `connectionStatus`（`online`、`offline`、`unknown`）、基于持久租约计算的 `availability`（`idle`、`busy`）、`checkedAt` 和 `lastOnlineAt`；不返回地址、令牌或原始连接异常。`local_db` 每 10 秒执行轻量 WebSocket 探测；`remote_sidecar` 使用 SQLite 中的权威心跳，5 秒上报一次，20 秒未续租即离线。`POST /workflows` 在写入 SQLite 前校验主监督和每个步骤执行机是否存在、启用、能力匹配，并继续执行权限档位校验；不要求各步骤使用相同执行机或工作目录。

提交成功的工作流先进入 `queued`。调度器按主监督分别以 `created_at + workflow_id` 领取最早任务；同一主监督固定只运行一个工作流，不同主监督可以并行。远程租约包含不可预测令牌、Sidecar 实例 ID、续租时间和过期时间；写接口在同一个 SQLite 写事务中重新校验租约，旧实例和旧令牌不能回写。完成、失败和取消时与终态在同一事务释放，半自动暂停继续占用。远程主监督离线时新工作流立即失败；运行中失联、实例更换或心跳超时会失败并释放租约，不自动迁移。网关重启仍把遗留的 `running/cancelling` 工作流直接标记失败、清空租约并继续排队任务，不重新附着旧外部会话。

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

当前文件流水线要求编排器与 app-server 位于同一台机器，并在 `config/agents.json` 配置本机绝对路径 `artifact_root`。编排器直接在该根目录内为每次尝试创建 `inputs/step-N/` 和空 `output/`，提示词只交付绝对路径，不使用 Base64 传输，也不扫描业务工作区。所有步骤都允许只返回文字；任务本身需要发布文件时最多发布一个。步骤是否完成只取决于节点执行结果，不因没有附件而失败，后续步骤自行检查所需业务文件。`write` 只表示是否允许写入，不代表必须生成附件。`allow_write` 是执行机的工作区写入上限；`allow_full_access` 是独立的完全访问上限，只有两者都为 `true` 时才开放 `full_access`。前三档文件交接继续只开放受控写入根目录并关闭网络；`full_access` 会取消文件系统和网络隔离，但仍只从托管输出目录收集最多一个交付文件。前序文件仅作为可用输入；当前要求未明确要求使用时，Agent 不得打开或合并它们。阶段 B 的远程主监督只支持 `legacy_text`；提交 `cumulative_files` 会在持久化前拒绝，跨机器附件传输仍留到后续阶段。

节点权限映射遵循 OpenAI 的 [Sandboxing](https://learn.chatgpt.com/docs/sandboxing) 与 [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security) 语义：`read_only = read-only + never`，`workspace_write = workspace-write + never`，`auto_review = workspace-write + on-request + auto_review`，`full_access = danger-full-access + never`。启动节点前会读取 `configRequirements/read`；执行机管理策略明确不允许时不会启动 thread。旧 app-server 不支持该方法时保持兼容。

该约束不修改配置中心保存的原始提示词或不可变运行快照，也不在监控页面展示。它当前属于提示词约束，不在运行时拦截第二次工具调用。

## 启动网关

在仓库根目录执行：

```powershell
$env:CODEX_AGENTS_FILE = (Resolve-Path .\config\agents.json)
$env:CODEX_WORKFLOW_DB = "$PWD\workflows.db"

uv run --project .\services\python-workflow `
  python .\services\python-workflow\src\workflow_gateway.py `
  --host 0.0.0.0 --port 8080 `
  --db $env:CODEX_WORKFLOW_DB --agents $env:CODEX_AGENTS_FILE
```

MCP 进程需要运行 `src/codex_orchestrator_mcp.py`，并传入完全相同的 `CODEX_AGENTS_FILE` 和 `CODEX_WORKFLOW_DB`。

网关默认监听所有网络接口以支持可信内网中的 Java 服务访问，但没有内置用户认证。必须通过主机防火墙限制 `8080` 的访问来源，不得直接暴露到公网；只需本机访问时可显式传入 `--host 127.0.0.1`。

## 启动远程 Sidecar

远程主监督机只运行 app-server `4500` 和 Sidecar `127.0.0.1:8082`，不运行完整网关，也不得配置 `CODEX_WORKFLOW_DB`。先准备该机器可见的执行机清单，再启动 Sidecar：

```powershell
Copy-Item .\config\agents.remote-sidecar.example.json .\config\agents.sidecar.json
$env:SUPERVISOR_B_SIDECAR_TOKEN = "请通过密钥系统注入"
.\scripts\start_workflow_sidecar.ps1 `
  -AgentId supervisor-b `
  -GatewayUrl http://central.internal:8080 `
  -TokenEnv SUPERVISOR_B_SIDECAR_TOKEN `
  -AgentsFile .\config\agents.sidecar.json
```

Codex app-server 使用 Streamable HTTP MCP：

```toml
[mcp_servers.codex_orchestrator]
url = "http://127.0.0.1:8082/mcp"
required = true
enabled_tools = ["dispatch_node", "wait_node", "node_status", "cancel_node", "workflow_status"]
default_tools_approval_mode = "approve"
```

仅预批准上述主监督编排工具，可以避免 `dispatch_node` 和 `wait_node` 逐次进入 Auto-review。主监督仍使用只读沙箱，业务步骤继续使用各自选择的权限档位。

Sidecar 启动时先确认 `8082` 已监听，再向中央登记上线，之后每 5 秒心跳。中央 `/internal/v1` 提供心跳、工作流/步骤上下文、原子准备派发、步骤状态同步和最多 64 项的事件批量上报。Bearer Token 唯一映射到一个启用的 `remote_sidecar` 主监督；所有写操作还必须携带 `X-Workflow-Lease`。认证失败、越权、未找到和租约冲突分别返回 `401`、`403`、`404`、`409`。事件使用工作流内幂等键，网络重试不会重复写入。

机器令牌可以通过 `CODEX_GATEWAY_TOKEN_ENV` 间接引用环境变量，也可以用 `CODEX_GATEWAY_TOKEN_FILE` 指向绝对文件。两者严格二选一；文件必须是 UTF-8 单行且不超过 8 KiB。Sidecar 每次请求重新读取令牌以支持轮换。机器令牌不能复用 app-server 的连接令牌，响应和公开执行机接口也不会回传令牌、网络地址或底层 thread/turn 标识。

`8082` 只能绑定回环地址，不需要也不应开放防火墙。中央 `8080` 只允许 `8090`、`8091` 和已登记主监督机访问；跨机器链路应位于可信内网、VPN 或 TLS 反向代理之后。

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
