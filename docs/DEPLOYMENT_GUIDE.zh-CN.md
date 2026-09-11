# Codex 工作流编排平台完整部署指南

本文说明当前单中央网关架构的完整部署方式，包括中央服务机、远程主监督机、业务执行机以及“主监督 + 业务执行”混合机器分别需要部署什么、如何配置、按什么顺序启动，以及如何从旧的 stdio MCP 迁移到 HTTP Sidecar。

如果要让目标机器上的 Codex 直接完成混合机部署或旧版升级，请使用[Codex 远程混合机部署与旧版升级执行手册](CODEX_HYBRID_MACHINE_DEPLOYMENT.zh-CN.md)。

本文中的主监督执行机、业务执行机都是逻辑角色。一台物理机可以只承担其中一种角色，也可以同时承担两种角色。

## 一、当前版本边界

### 本次兼容升级与历史容量维护

升级顺序为 Python 网关及本机 MCP、配置中心、监控中心。保持中央 SQLite 路径一致，先备份数据库，并在没有活动任务时升级。配置中心自动执行 Flyway V19；历史运行的任务归属只由后台分批登记，不改变冻结快照。若提交时该任务仍有待登记历史，系统保留原运行编号和占用，后台登记完成后由对账自动补交，提交请求不再同步遍历全部历史。新的压缩事件要求网关与 MCP 运行同一版本，回退旧版本前应使用升级前备份，不混跑旧事件读取器。

较大新事件自动无损压缩。维护旧事件时，可以先运行只读容量统计（示例路径和日期需替换）：

```sh
python scripts/compact_workflow_events.py --db /absolute/path/workflows.db --before 2026-08-01T00:00:00+00:00
```

确认目标和备份后加 `--apply`，每批最多处理 100 条已结束运行的事件。该操作压缩正文而不删除事件、附件、快照或序号，完整历史仍可从 API 查询；默认不执行压缩。压缩释放的数据库页供后续写入复用，数据库文件不一定立即缩小。若需回收物理文件空间，应在维护窗口、停止所有数据库写入进程且备份后使用 SQLite `VACUUM`，脚本不会自动执行。

脚本仅输出记录数和容量，不输出业务内容。附件和运行次数没有自动删除策略，部署仍需为保留的审计数据规划磁盘容量及备份空间；不要通过清表或删库解决容量问题。

- 全系统只有一个中央 Python 网关 `8080`，不支持多个网关实例竞争调度。
- 每个主监督同一时间最多运行一个工作流；同一主监督的后续工作流排队，不同主监督可以并行。
- 远程主监督必须使用 `legacy_text` 结果交接模式；当前版本不传输跨机器文件和二进制附件。
- 远程 Sidecar 每 5 秒向中央网关发送一次心跳，20 秒未收到心跳即判定离线。
- 远程主监督离线时，新工作流立即失败；运行中失联时，工作流失败并释放租约，不自动迁移。
- 网关重启会把遗留的 `running`、`cancelling` 工作流标记失败，不重新附着旧 Codex 会话。
- 不同 SOP 的工作目录隔离由部署约定保证；系统当前不做工作目录租约和执行机容量调度。

### 飞书机器人退役

当前版本已移除飞书长连接、配置页面、配置接口和 SDK，只保留钉钉机器人。升级前先等待飞书发起的任务结束并处理待发送通知，再在旧版本停用飞书机器人；若需要停止任务，先通过现有控制入口完成停止。升级后旧飞书消息与卡片不再响应，待发送通知不会继续补发；历史运行仍可在配置中心及监控页面查看。

移除部署中的 `FEISHU_*` 环境变量，并在飞书管理端停用对应应用或撤销凭据。此次升级不删除历史业务数据：5 张飞书专属表及其中的数据保留，新程序不再映射或访问这些表。V5、V6、V17 等已发布 Flyway 脚本保持原样，保证旧版本和空库均可升级。不要手工删除这些脚本或清除 Flyway 记录；如以后需要清理旧表，另行安排数据保留与新的数据库迁移。

从源码构建配置中心时使用 `mvn -f services/role-task-config-center/pom.xml clean package`，避免旧编译目录中的飞书类进入新包。

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

### 1. 准备中央服务配置

本分支只支持网页登记，不读取旧机器清单。按[机器管理部署说明](WEBUI_MACHINE_REGISTRATION.zh-CN.md)复制中央服务示例，填写原数据库路径、统一工作目录及凭据引用。

配置文件名为 `config/workflow-service.json`：中央填写 `workflow_db`、`machine_defaults`；远程主监督及监督执行一体机填写 `sidecar`；纯执行机只需执行服务启动配置。各机器令牌文件放置位置、两种认证用途和缺少文件的处理步骤见[一体机配置与两种凭据](WEBUI_MACHINE_REGISTRATION.zh-CN.md#一体机配置与两种凭据)。

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

Windows 从仓库根目录执行 `scripts/start_workflow_gateway.ps1`。Linux/macOS 使用已有 Python 环境执行 `services/python-workflow/src/workflow_gateway.py`。进程读取本机仓库的 `config/workflow-service.json`，不再通过清单参数启动。

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

## 六、中央网页登记

启动中央和 8091 后，在“机器管理”建立分组并登记机器。表单只填写 IP、执行服务端口、分组、能力。检测通过后选用于 SOP；无旧配置导入。详细流程及本机 `local_db` 配置见[机器管理部署说明](WEBUI_MACHINE_REGISTRATION.zh-CN.md)。

## 七、纯远程主监督机部署

远程使用 `config/workflow-sidecar.example.json` 生成本机 `config/workflow-service.json`，填写中央地址及独立 Sidecar 令牌文件路径。无需维护本地执行机清单，按中央认证接口获取同组机器。

执行服务的 HTTP MCP 配置及 Sidecar 启动命令见 [Python README](../services/python-workflow/README.md#启动远程-sidecar)。执行服务端口通常为 `4500`；Sidecar 仅监听回环地址 `8082`，不要对外开放。启动执行服务仍使用部署好的 app-server 认证规则，凭据必须与中央及派发方解析出的执行服务凭据匹配。

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

在网页将执行机登记到主监督所在分组，选择执行机能力并手动检测。统一执行凭据引用必须在中央与所有派发方可解析。同一登记 IP／端口必须从组内派发方可达，不维护不同来源的独立地址。

纯执行机无需网关、Java 服务、SQLite 或 Sidecar。

## 九、主监督和业务执行混合机部署

混合机运行一套执行服务和一个 Sidecar，网页同时勾选主监督、执行机两种能力。主监督容量仍为 1；监督和业务步骤使用独立会话。无需额外维护机器清单，执行凭据与 Sidecar 身份凭据仍是两种用途。

## 十、使用本分支替换旧部署

等待活动任务结束，备份中央运行库后更新代码。按[机器管理部署说明](WEBUI_MACHINE_REGISTRATION.zh-CN.md#首次登记与升级)准备新服务配置并重新网页登记，不自动迁移旧机器。不删除旧配置文件或业务数据。

远程主监督将原 stdio 编排配置替换为本机 HTTP Sidecar 入口；中央本机主监督可继续使用读取同一中央 SQLite 的 `local_db`。

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
5. 混合机同时作为主监督和步骤执行机，确认步骤能通过登记 IP 和端口派回本机执行服务。

## 十三、常见故障

### 主监督在列表中离线

检查：

- Sidecar 是否监听 `127.0.0.1:8082`。
- Sidecar 能否访问中央 `8080`。
- 机器是否已网页登记，Sidecar 凭据是否唯一匹配该主监督。
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

网页必须登记该步骤执行机，具备执行能力、与主监督同组、启用且检测通过；中央及主监督均须能访问登记的 IP 和端口。

## 十四、运维检查清单

上线前逐项确认：

- [ ] 中央只运行一个 `8080` 网关实例。
- [ ] `workflows.db` 只存在于中央机，且已纳入备份。
- [ ] 配置中心 MySQL 已备份，Flyway 迁移成功。
- [ ] 每个主监督 ID 唯一，`capacity` 为 `1`。
- [ ] 每个远程主监督有独立 Sidecar 令牌。
- [ ] app-server 连接令牌与 Sidecar 令牌没有复用。
- [ ] 实际服务配置、令牌文件、数据库和日志未提交 Git。
- [ ] `8082` 只监听回环地址。
- [ ] `8080`、`8090`、`8091` 和 `4500` 已设置内网防火墙规则。
- [ ] 远程主监督没有 `CODEX_WORKFLOW_DB`。
- [ ] 远程 SOP 使用 `legacy_text`。
- [ ] 不同 SOP 的业务目录按部署约定隔离。
- [ ] 已完成单步骤、并行、排队和 Sidecar 失联验收。
