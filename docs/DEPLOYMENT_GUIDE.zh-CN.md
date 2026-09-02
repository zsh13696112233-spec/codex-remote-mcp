# Codex 工作流编排平台完整部署指南

本文说明当前单中央网关架构的完整部署方式，包括中央服务机、远程主监督机、业务执行机以及“主监督 + 业务执行”混合机器分别需要部署什么、如何配置、按什么顺序启动，以及如何从旧的 stdio MCP 迁移到 HTTP Sidecar。

如果要让目标机器上的 Codex 直接完成混合机部署或旧版升级，请使用[Codex 远程混合机部署与旧版升级执行手册](CODEX_HYBRID_MACHINE_DEPLOYMENT.zh-CN.md)。

本文中的主监督执行机、业务执行机都是逻辑角色。一台物理机可以只承担其中一种角色，也可以同时承担两种角色。

## 一、当前版本边界

- 全系统只有一个中央 Python 网关 `8080`，不支持多个网关实例竞争调度。
- 每个主监督同一时间最多运行一个工作流；同一主监督的后续工作流排队，不同主监督可以并行。
- 远程主监督必须使用 `legacy_text` 结果交接模式；当前版本不传输跨机器文件和二进制附件。
- 远程 Sidecar 每 5 秒向中央网关发送一次心跳，20 秒未收到心跳即判定离线。
- 远程主监督离线时，新工作流立即失败；运行中失联时，工作流失败并释放租约，不自动迁移。
- 网关重启会把遗留的 `running`、`cancelling` 工作流标记失败，不重新附着旧 Codex 会话。
- 不同 SOP 的工作目录隔离由部署约定保证；系统当前不做工作目录租约和执行机容量调度。

## 二、机器角色和服务清单

| 机器角色 | 必须部署的服务 | 监听端口 | 是否需要 MCP |
| --- | --- | --- | --- |
| 中央服务机 | Python 工作流网关 | `8080` | 只有本机 `local_db` 主监督才需要 stdio MCP |
| 中央服务机 | 任务运行监控中心 | `8090` | 不需要 |
| 中央服务机 | 角色任务配置中心 | `8091` | 不需要 |
| 中央服务机 | MySQL 8 | 通常 `3306` | 不需要 |
| 远程主监督机 | Codex app-server | 通常 `4500` | 通过 HTTP 连接本机 Sidecar |
| 远程主监督机 | Workflow Sidecar | 仅 `127.0.0.1:8082` | Sidecar 本身就是 MCP 服务 |
| 纯业务执行机 | Codex app-server | 通常 `4500` | 不需要 Orchestrator MCP |
| 主监督 + 执行混合机 | Codex app-server + Workflow Sidecar | `4500` + 仅本机 `8082` | app-server 通过 HTTP 连接 Sidecar |

MySQL 只属于配置中心。SQLite `workflows.db` 只属于中央 Python 网关；远程主监督机和业务执行机都不得复制、挂载或打开中央 SQLite。

## 三、网络调用方向

```text
浏览器 ───────────────▶ 配置中心 8091
浏览器 ───────────────▶ 监控中心 8090
配置中心 8091 ───────▶ 中央网关 8080
监控中心 8090 ───────▶ 中央网关 8080

中央网关 8080 ───────▶ 主监督 app-server 4500
主监督 app-server ───▶ 本机 Sidecar 127.0.0.1:8082/mcp
主监督 Sidecar ──────▶ 中央网关 8080/internal/v1
主监督 Sidecar ──────▶ 业务执行机 app-server 4500
```

防火墙至少需要允许：

- `8090`、`8091`：只允许业务内网用户或受保护反向代理访问。
- `8080`：只允许 `8090`、`8091` 所在机器和已登记的远程主监督机访问。
- 主监督机 `4500`：允许中央网关访问。
- 执行机 `4500`：允许需要向它派发步骤的主监督 Sidecar 访问。
- `8082`：只绑定 `127.0.0.1`，不开放任何防火墙入站规则。
- MySQL `3306`：只允许配置中心访问。

不要把 `8080`、`8090`、`8091` 或 app-server `4500` 直接暴露到公网。跨不可信网络使用 VPN、TLS 反向代理和 `wss://`。

## 四、通用软件准备

中央机和需要运行 Sidecar 的主监督机准备：

- Git。
- Python 3.10 或更高版本。
- `uv`。
- 当前仓库代码。

中央机另外准备：

- Java 17。
- Maven 3.9 或更高版本。
- MySQL 8。

所有需要运行 app-server 的机器准备：

- Codex CLI，并完成该操作系统账号的 Codex 登录或无人值守认证配置。
- 独立工作目录。
- 如开放非回环 WebSocket，配置 app-server 访问令牌和主机防火墙。

官方 Codex app-server 启动 WebSocket 的基本命令为：

```powershell
codex app-server --listen ws://127.0.0.1:4500
```

官方说明 WebSocket app-server 当前仍属于实验能力。非回环监听必须配置认证，推荐使用令牌文件：

```powershell
codex app-server `
  --listen ws://0.0.0.0:4500 `
  --ws-auth capability-token `
  --ws-token-file C:\codex-secrets\app-server.token
```

Linux 使用同样的 Codex 参数，只需要把文件路径改为 Linux 绝对路径。官方参考：

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

### 生成本机令牌文件

app-server 连接令牌和 Sidecar 机器令牌分别生成，不能复用。下面的 PowerShell 示例生成一个 UTF-8、单行、256 位随机令牌文件，执行时替换目标路径：

```powershell
$TokenPath = "C:\codex-secrets\supervisor-a-sidecar.token"
New-Item -ItemType Directory -Force -Path (Split-Path $TokenPath) | Out-Null

$Bytes = New-Object byte[] 32
$Rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $Rng.GetBytes($Bytes)
} finally {
    $Rng.Dispose()
}

$Token = [Convert]::ToBase64String($Bytes)
[IO.File]::WriteAllText(
    $TokenPath,
    $Token,
    [Text.UTF8Encoding]::new($false)
)

Remove-Variable Token, Bytes
```

不要在终端、日志、JSON、聊天或 Git 中输出令牌。使用操作系统 ACL 限制只有对应服务账号能够读取。令牌文件必须是 UTF-8 单行且不超过 8 KiB。

### 安装 Python 依赖

仓库提供的 PowerShell 启动脚本使用仓库根目录的 `.venv`：

```powershell
Set-Location C:\path\to\codex-remote-mcp

uv venv .venv --python 3.10
uv pip install --python .\.venv\Scripts\python.exe `
  -e .\services\python-workflow
```

Linux：

```bash
cd /opt/codex-remote-mcp
uv venv .venv --python 3.10
uv pip install --python .venv/bin/python -e services/python-workflow
```

升级代码后，重新执行 `uv pip install ... -e services/python-workflow`，确保新增依赖和入口已安装。

## 五、中央服务机部署

中央服务机可以同时部署 `8080`、`8090`、`8091` 和 MySQL。生产环境也可以把 MySQL 单独部署，但逻辑职责不变。

### 1. 准备中央执行机配置

```powershell
Copy-Item .\config\agents.example.json .\config\agents.json
```

`config/agents.json` 是中央权威执行机清单，不得提交 Git。它需要列出：

- 所有主监督 app-server。
- 所有可能被 SOP 步骤使用的业务执行机。
- 启停状态、能力、默认目录和权限上限。
- app-server 连接令牌来源。
- 远程主监督的 `remote_sidecar` 模式和独立 Sidecar 机器令牌来源。

完整字段示例见本文“中央 `agents.json` 配置”一节。

### 2. 启动 MySQL 和初始化配置中心数据库

```sql
CREATE DATABASE codex_config
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE USER 'codex'@'127.0.0.1' IDENTIFIED BY '请替换为强密码';
GRANT ALL PRIVILEGES ON codex_config.* TO 'codex'@'127.0.0.1';
FLUSH PRIVILEGES;
```

空数据库不需要手工创建表。配置中心首次启动时由 Flyway 自动迁移。

### 3. 启动中央 Python 网关 `8080`

Windows：

```powershell
Set-Location C:\path\to\codex-remote-mcp
.\scripts\start_workflow_gateway.ps1 -ListenHost 0.0.0.0 -Port 8080
```

Linux：

```bash
cd /opt/codex-remote-mcp
export CODEX_AGENTS_FILE=/opt/codex-remote-mcp/config/agents.json
export CODEX_WORKFLOW_DB=/opt/codex-remote-mcp/workflows.db

.venv/bin/python services/python-workflow/src/workflow_gateway.py \
  --host 0.0.0.0 --port 8080 \
  --db "$CODEX_WORKFLOW_DB" --agents "$CODEX_AGENTS_FILE"
```

检查：

```powershell
Invoke-WebRequest http://127.0.0.1:8080/readyz -UseBasicParsing
```

应返回 HTTP `200`。

### 4. 启动监控中心 `8090`

```powershell
Set-Location .\services\workflow-console

$env:CODEX_GATEWAY_URL = "http://127.0.0.1:8080"
$env:SERVER_ADDRESS = "0.0.0.0"
$env:SERVER_PORT = "8090"

mvn spring-boot:run
```

正式部署可以先执行 `mvn package`，再运行：

```powershell
java -jar .\target\workflow-console-0.1.0.jar
```

### 5. 启动配置中心 `8091`

```powershell
Set-Location .\services\role-task-config-center

$env:MYSQL_URL = "jdbc:mysql://127.0.0.1:3306/codex_config?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
$env:MYSQL_USERNAME = "codex"
$env:MYSQL_PASSWORD = "请从密钥系统注入"
$env:CODEX_GATEWAY_URL = "http://127.0.0.1:8080"
$env:WORKFLOW_MONITOR_URL = "http://127.0.0.1:8090"
$env:SERVER_ADDRESS = "0.0.0.0"
$env:SERVER_PORT = "8091"

mvn spring-boot:run
```

正式部署可以先执行 `mvn package`，再运行：

```powershell
java -jar .\target\role-task-config-center-0.1.0.jar
```

## 六、中央 `agents.json` 配置

下面示例同时包含纯主监督、纯执行机和混合机：

```json
{
  "agents": {
    "supervisor-a": {
      "url": "ws://supervisor-a.internal:4500",
      "cwd": "C:\\codex-workspaces\\supervisor-a",
      "enabled": true,
      "capabilities": ["supervisor"],
      "capacity": 1,
      "token_file": "C:\\codex-secrets\\supervisor-a-app-server.token",
      "orchestration_mode": "remote_sidecar",
      "sidecar_token_file": "C:\\codex-secrets\\supervisor-a-sidecar.token",
      "allow_write": false,
      "allow_cwd_override": false
    },
    "worker-a": {
      "url": "ws://worker-a.internal:4500",
      "cwd": "D:\\codex-workspaces\\worker-a",
      "enabled": true,
      "capabilities": ["executor"],
      "token_file": "C:\\codex-secrets\\worker-a-app-server.token",
      "allow_write": true,
      "allow_cwd_override": true
    },
    "hybrid-a": {
      "url": "ws://hybrid-a.internal:4500",
      "cwd": "D:\\codex-workspaces\\hybrid-a",
      "enabled": true,
      "capabilities": ["supervisor", "executor"],
      "capacity": 1,
      "token_file": "C:\\codex-secrets\\hybrid-a-app-server.token",
      "orchestration_mode": "remote_sidecar",
      "sidecar_token_file": "C:\\codex-secrets\\hybrid-a-sidecar.token",
      "allow_write": true,
      "allow_cwd_override": true
    }
  }
}
```

关键规则：

- `capabilities` 只允许 `supervisor` 和 `executor`。
- 具备 `supervisor` 能力时，`capacity` 当前固定为 `1`。
- 纯执行机不要配置 `capacity`、`orchestration_mode` 和 Sidecar 令牌。
- 远程主监督必须配置 `orchestration_mode: "remote_sidecar"`。
- `token_env` 与 `token_file` 二选一，用于连接 app-server。
- `sidecar_token_env` 与 `sidecar_token_file` 二选一，用于 Sidecar 调用中央 `8080`。
- app-server 令牌和 Sidecar 机器令牌必须相互独立，不得复用。
- `sidecar_token_file` 是中央网关机器上可读的绝对路径，不是远程主监督机上的路径。
- 使用环境变量时，变量必须存在于读取该配置的进程环境中。中央 `agents.json` 的 `token_env` 和 `sidecar_token_env` 由中央网关进程读取。
- 不允许在 JSON 中直接写明文令牌。

### 中央机兼任本地主监督或执行机（可选）

如果中央机还运行一个本机 app-server，可以保留现有的 `local_db` 兼容方式，不必为这个本地主监督部署 Sidecar：

```json
{
  "local": {
    "url": "ws://127.0.0.1:4500",
    "cwd": "C:\\codex-workspaces\\local",
    "enabled": true,
    "capabilities": ["supervisor", "executor"],
    "capacity": 1,
    "orchestration_mode": "local_db",
    "allow_write": true,
    "allow_cwd_override": true
  }
}
```

本机 app-server 使用 stdio MCP，并与中央网关指向同一个 SQLite 绝对路径：

```toml
[mcp_servers.codex_orchestrator]
command = "uv"
args = [
  "run", "--project", "C:\\path\\to\\codex-remote-mcp\\services\\python-workflow",
  "python", "C:\\path\\to\\codex-remote-mcp\\services\\python-workflow\\src\\codex_orchestrator_mcp.py"
]
required = true

[mcp_servers.codex_orchestrator.env]
CODEX_AGENTS_FILE = "C:\\path\\to\\codex-remote-mcp\\config\\agents.json"
CODEX_WORKFLOW_DB = "C:\\path\\to\\codex-remote-mcp\\workflows.db"
```

这种方式只适用于与中央 `workflows.db` 同机的 app-server。中央机如果同时作为执行机，`capabilities` 保留两种能力即可；远程机器不得照搬这段 stdio/SQLite 配置。

## 七、纯远程主监督机部署

纯主监督机运行两个进程：

1. Codex app-server `4500`。
2. Workflow Sidecar `127.0.0.1:8082`。

不部署中央网关、监控中心、配置中心、MySQL 或 SQLite。

### 1. 准备主监督机本地执行机清单

```powershell
Copy-Item .\config\agents.remote-sidecar.example.json `
  .\config\agents.sidecar.json
```

主监督机本地的 `agents.sidecar.json` 至少应列出：

- 自己的逻辑 ID。
- 该主监督可能派发的所有业务执行机。
- 从这台主监督机访问各 app-server 时应使用的 URL 和令牌来源。

注意中央和远程机器的 URL 视角可能不同。例如中央通过内网域名连接 `hybrid-a`，而 `hybrid-a` 本机 Sidecar 连接自己的 app-server 时可以使用 `ws://127.0.0.1:4500`。

### 2. 准备独立 Sidecar 机器令牌

每个远程主监督使用一个独立随机令牌。中央机和对应主监督机各保存一份内容相同的令牌；文件路径可以不同。

```text
中央机：C:\codex-secrets\supervisor-a-sidecar.token
主监督机：C:\codex-secrets\supervisor-a-sidecar.token
```

中央 `agents.json` 的 `sidecar_token_file` 指向中央机文件；启动 Sidecar 时的 `-TokenFile` 指向主监督机文件。

### 3. 配置主监督 app-server 的 HTTP MCP

编辑运行 app-server 的操作系统账号所使用的 `~/.codex/config.toml`：

```toml
[mcp_servers.codex_orchestrator]
url = "http://127.0.0.1:8082/mcp"
required = true
```

Streamable HTTP MCP 只使用 `url`。不要在该表下面保留 stdio 的 `command`、`args`、`cwd` 或 `[mcp_servers.codex_orchestrator.env]`。

### 4. 启动 Sidecar `8082`

Windows：

```powershell
Set-Location C:\path\to\codex-remote-mcp

.\scripts\start_workflow_sidecar.ps1 `
  -AgentId supervisor-a `
  -GatewayUrl http://central.internal:8080 `
  -TokenFile C:\codex-secrets\supervisor-a-sidecar.token `
  -AgentsFile .\config\agents.sidecar.json
```

也可以使用环境变量：

```powershell
$env:SUPERVISOR_A_SIDECAR_TOKEN = "由密钥系统注入"

.\scripts\start_workflow_sidecar.ps1 `
  -AgentId supervisor-a `
  -GatewayUrl http://central.internal:8080 `
  -TokenEnv SUPERVISOR_A_SIDECAR_TOKEN `
  -AgentsFile .\config\agents.sidecar.json
```

`-TokenEnv` 和 `-TokenFile` 必须二选一。

Linux：

```bash
cd /opt/codex-remote-mcp

.venv/bin/python services/python-workflow/src/workflow_sidecar.py \
  --host 127.0.0.1 --port 8082 \
  --agent-id supervisor-a \
  --gateway-url http://central.internal:8080 \
  --token-file /etc/codex/secrets/supervisor-a-sidecar.token \
  --agents /etc/codex/agents.sidecar.json
```

Sidecar 必须保持常驻。生产环境使用 Windows 服务、systemd 或其他进程管理器拉起，并配置失败重启。

### 5. 启动主监督 app-server `4500`

Sidecar 已监听后再启动或重启 app-server，使 HTTP MCP 配置生效：

```powershell
codex app-server `
  --listen ws://0.0.0.0:4500 `
  --ws-auth capability-token `
  --ws-token-file C:\codex-secrets\supervisor-a-app-server.token
```

中央 `agents.json` 中对应主监督的 `token_file` 或 `token_env` 必须能解析出同一个 app-server 连接令牌。

## 八、纯业务执行机部署

纯业务执行机只运行 Codex app-server，不运行 Sidecar，也不配置 Orchestrator MCP。

### 1. 准备工作目录

例如：

```text
D:\codex-workspaces\sop-a
D:\codex-workspaces\sop-b
```

不同 SOP 不在同一目录同时运行。可以为同一台物理机配置多个逻辑执行机 ID，并分别指定不同默认目录。

### 2. 启动 app-server

```powershell
codex app-server `
  --listen ws://0.0.0.0:4500 `
  --ws-auth capability-token `
  --ws-token-file C:\codex-secrets\worker-a-app-server.token
```

### 3. 配置调用方

- 中央 `agents.json` 需要有该执行机 ID，以便提交工作流时校验。
- 每台可能向它派发步骤的主监督机，其 `agents.sidecar.json` 也需要有同一个执行机 ID。
- Sidecar 本地配置中的 URL 必须从主监督机网络视角可达。
- Sidecar 本地配置中的 `token_file` 是主监督机本地保存的客户端令牌文件，不是执行机上的路径。

纯执行机不需要：

- `8080`、`8090`、`8091`。
- MySQL 或 SQLite。
- `workflow_sidecar.py`。
- `[mcp_servers.codex_orchestrator]`。
- `CODEX_WORKFLOW_DB`。

## 九、主监督和业务执行混合机部署

混合机仍然只需要一套 Codex app-server，再加一个 Sidecar：

```text
Codex app-server 4500：承载主监督会话，也承载业务步骤会话
Workflow Sidecar 8082：给主监督提供工作流工具，并派发业务步骤
```

不需要为监督角色和执行角色分别启动两个 app-server。一个 app-server 可以承载多个独立 Codex 会话；主监督容量 `1` 只限制活动工作流数量，不等同于执行步骤容量。

中央配置：

```json
{
  "hybrid-a": {
      "url": "ws://hybrid-a.internal:4500",
    "cwd": "D:\\codex-workspaces\\hybrid-a",
    "enabled": true,
    "capabilities": ["supervisor", "executor"],
    "capacity": 1,
    "token_file": "C:\\codex-secrets\\hybrid-a-app-server.token",
    "orchestration_mode": "remote_sidecar",
    "sidecar_token_file": "C:\\codex-secrets\\hybrid-a-sidecar.token",
    "allow_write": true,
    "allow_cwd_override": true
  }
}
```

混合机本地 `agents.sidecar.json`：

```json
{
  "agents": {
    "hybrid-a": {
      "url": "ws://127.0.0.1:4500",
      "cwd": "D:\\codex-workspaces\\hybrid-a",
      "enabled": true,
      "capabilities": ["supervisor", "executor"],
      "capacity": 1,
      "allow_write": true,
      "allow_cwd_override": true
    },
    "worker-a": {
      "url": "ws://worker-a.internal:4500",
      "cwd": "D:\\codex-workspaces\\worker-a",
      "enabled": true,
      "capabilities": ["executor"],
      "token_file": "C:\\codex-secrets\\worker-a-app-server.token",
      "allow_write": true,
      "allow_cwd_override": true
    }
  }
}
```

如果业务步骤选择 `hybrid-a`，Sidecar 通过回环地址把步骤派回同一 app-server；如果步骤选择 `worker-a`，Sidecar通过内网连接远程执行机。

混合机启动顺序：

1. 确认中央 `8080` 已启动。
2. 启动本机 Sidecar `8082`。
3. 启动或重启本机 app-server `4500`。
4. 从中央 `GET /agents` 确认 `hybrid-a` 为 `online`。

## 十、从旧 stdio MCP 迁移到 Sidecar

### 哪些机器需要迁移

- 中央机上继续使用 `local_db` 的本地主监督可以保留 stdio MCP。
- 任何作为远程主监督的机器，都应迁移到 HTTP Sidecar。
- 纯业务执行机不应该有 Orchestrator MCP；如果以前误配，可以删除整个 `codex_orchestrator` MCP 配置。

### 迁移前准备

1. 停止提交新工作流。
2. 等待当前工作流结束；不要在活动工作流中重启中央网关或主监督。
3. 部署当前代码和 Python 依赖。
4. 在中央 `agents.json` 给目标主监督增加 `remote_sidecar` 和独立机器令牌。
5. 在远程主监督机准备 `agents.sidecar.json` 和相同内容的 Sidecar 令牌。

### 删除旧 stdio 配置

旧配置通常类似：

```toml
[mcp_servers.codex_orchestrator]
command = "uv"
args = ["run", "..."]
required = true

[mcp_servers.codex_orchestrator.env]
CODEX_AGENTS_FILE = "..."
CODEX_WORKFLOW_DB = "..."
```

迁移后必须替换为：

```toml
[mcp_servers.codex_orchestrator]
url = "http://127.0.0.1:8082/mcp"
required = true
```

必须同时删除：

- `command`。
- `args`。
- `cwd`。
- 整个 `[mcp_servers.codex_orchestrator.env]` 子段。
- 远程机器服务配置中的 `CODEX_WORKFLOW_DB`。
- 远程机器上的中央 SQLite 副本或共享挂载配置。

只把 `command` 改成 `url` 而保留旧 `env` 子段会导致 app-server 报错：

```text
env is not supported for streamable_http
```

此时 Codex 会把 MCP 配置视为无效，主监督无法取得工作流工具。

### 迁移启动顺序

1. 重启中央网关，让 `remote_sidecar` 配置生效。
2. 启动远程 Sidecar，确认 `8082` 监听且中央显示主监督在线。
3. 重启远程主监督 app-server，让新的 HTTP MCP 配置生效。
4. 提交一个只读、单步骤、`legacy_text` 测试工作流。
5. 测试通过后恢复业务提交。

迁移不是“重新安装 MCP”。旧 stdio 和新 Sidecar 提供的是同一组工作流工具；改变的是 Codex 连接 MCP 的方式，以及 MCP 访问中央运行状态的方式。

## 十一、推荐的全系统启动和停机顺序

### 冷启动

1. MySQL。
2. 中央 Python 网关 `8080`。
3. 纯业务执行机 app-server `4500`。
4. 每台远程主监督或混合机的 Sidecar `8082`。
5. 每台远程主监督或混合机的 app-server `4500`。
6. 监控中心 `8090`。
7. 配置中心 `8091`。
8. 检查 `/agents`，再允许用户提交任务。

如果中央数据库中已有排队工作流，启动过程中先禁止新提交，避免 Sidecar 已上线但主监督 app-server 尚未启动时中央立即调度任务。

### 计划停机

1. 停止新任务提交。
2. 等待活动工作流进入终态。
3. 停止配置中心和监控中心。
4. 停止主监督 Sidecar 和 app-server。
5. 停止业务执行机 app-server。
6. 最后停止中央网关和 MySQL。

不要在活动工作流中直接重启网关；当前版本会把活动工作流标记失败。

## 十二、部署验证

### 1. 中央服务

```powershell
Invoke-WebRequest http://127.0.0.1:8080/readyz -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8090/api/gateway/ready -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8091/api/gateway/ready -UseBasicParsing
```

### 2. app-server

在 app-server 本机检查：

```powershell
Invoke-WebRequest http://127.0.0.1:4500/readyz -UseBasicParsing
```

### 3. Sidecar

Windows：

```powershell
netstat -ano | Select-String ":8082"
```

应看到 `127.0.0.1:8082` 处于 `LISTENING`。不要把浏览器直接访问 `/mcp` 的普通 GET 结果当作健康检查；MCP 需要 Streamable HTTP 协议握手。

### 4. 中央主监督状态

```powershell
Invoke-RestMethod http://127.0.0.1:8080/agents
```

远程主监督应显示：

```text
connectionStatus = online
availability = idle 或 busy
```

### 5. 最小工作流

验收工作流应满足：

- `supervisorAgentId` 选择待测远程主监督。
- `handoffMode` 使用 `legacy_text`。
- 只有一个只读步骤。
- 步骤提示词只要求返回固定文本，不写文件。
- 工作流和步骤最终均为 `completed`。

完成最小验证后，再验收：

1. 两个不同主监督同时运行，确认可以并行。
2. 同一主监督连续提交两个工作流，确认第二个排队并在第一个终态后启动。
3. 运行中停止 Sidecar，确认 20 秒内任务失败并释放租约。
4. 恢复 Sidecar 后提交新任务，确认可以重新运行。
5. 混合机同时作为主监督和步骤执行机，确认步骤能通过回环 app-server 执行。

## 十三、常见故障

### 主监督在列表中离线

检查：

- Sidecar 是否监听 `127.0.0.1:8082`。
- Sidecar 能否访问中央 `8080`。
- `AgentId` 是否与中央 `agents.json` 完全一致。
- Sidecar 机器令牌内容是否与中央配置解析出的令牌一致。
- 中央目标执行机是否启用并具有 `supervisor` 能力。

### app-server 启动后 MCP 无效

检查 `~/.codex/config.toml`：

- HTTP 模式只有 `url`，没有 `command` 和 `args`。
- 已删除整个 `[mcp_servers.codex_orchestrator.env]`。
- Sidecar 已经先于 app-server 启动。
- URL 是 `http://127.0.0.1:8082/mcp`。

### 内部 API 返回 `401`

Sidecar 令牌不匹配或中央找不到该令牌对应的唯一主监督。检查中央和远程两份令牌内容，不要在日志或聊天中打印令牌。

### 内部 API 返回 `403`

该 Sidecar 正在访问其他主监督拥有的工作流。检查 `AgentId`、SOP 的 `supervisorAgentId` 和中央配置。

### 内部 API 返回 `409`

通常表示旧 Sidecar 实例、旧租约或状态冲突。确认没有启动两个相同 `AgentId` 的 Sidecar，停止旧进程并等待中央状态更新后重新提交新任务。

### 远程工作流提交时拒绝 `cumulative_files`

这是当前版本的预期限制。把 SOP 的结果交接方式改为 `legacy_text`；跨机器文件交接将在后续版本按实际需要实现。

### 步骤执行机未知或能力不匹配

中央 `agents.json` 必须存在该步骤的执行机 ID，且包含 `executor` 能力；远程主监督的 `agents.sidecar.json` 也必须存在相同 ID，并配置从主监督机视角可达的 app-server 地址。

## 十四、运维检查清单

上线前逐项确认：

- [ ] 中央只运行一个 `8080` 网关实例。
- [ ] `workflows.db` 只存在于中央机，且已纳入备份。
- [ ] 配置中心 MySQL 已备份，Flyway 迁移成功。
- [ ] 每个主监督 ID 唯一，`capacity` 为 `1`。
- [ ] 每个远程主监督有独立 Sidecar 令牌。
- [ ] app-server 连接令牌与 Sidecar 令牌没有复用。
- [ ] 私有 `agents.json`、令牌文件、数据库和日志未提交 Git。
- [ ] `8082` 只监听回环地址。
- [ ] `8080`、`8090`、`8091` 和 `4500` 已设置内网防火墙规则。
- [ ] 远程主监督没有 `CODEX_WORKFLOW_DB`。
- [ ] 远程 SOP 使用 `legacy_text`。
- [ ] 不同 SOP 的业务目录按部署约定隔离。
- [ ] 已完成单步骤、并行、排队和 Sidecar 失联验收。
