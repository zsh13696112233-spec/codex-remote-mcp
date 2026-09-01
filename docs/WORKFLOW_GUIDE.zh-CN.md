# Codex 主会话工作流调度与监控说明

本文档记录 Java 主机向 Codex 执行机发布工作流、由 Codex 主会话监督节点执行、并由 Java 查询进度的完整方案。

以后开始新会话时，可以直接告诉 Codex：

> 请先阅读项目内的 `docs/WORKFLOW_GUIDE.zh-CN.md`，然后继续处理工作流服务。

## 一、需求目标

系统需要满足以下要求：

1. Java 主机把一个完整任务以 JSON 形式发送到 Codex 执行机。
2. 一个任务包含多个 node 节点。
3. 每个节点可以在本机 Codex app-server 执行，也可以通过远程 app-server 执行。
4. 每个节点使用独立的 Codex thread。
5. 节点可以串行，也可以通过依赖关系组成 DAG。
6. 一个 Codex 主监督会话负责决定何时启动符合依赖条件的节点。
7. 节点完成后把结果返回给主监督会话，由主会话继续调度后续节点。
8. Java 可以随时查询当前执行到哪个节点，也可以实时接收主会话消息和 app-server 事件。

这里的 thread 指 Codex 会话线程，不是 Java 或 Python 的操作系统线程。

例如三个串行节点实际是：

```text
主监督 Codex thread
    │
    ├── node-a Codex thread：输出 a
    │       完成
    ├── node-b Codex thread：输出 b
    │       完成
    └── node-c Codex thread：输出 c
            完成
```

## 二、整体架构

```text
Java 业务主机
    │
    │ HTTP POST /workflows
    ▼
workflow_gateway.py（Codex 执行机，默认 8080）
    │
    ├── 创建主监督 Codex thread
    │
    ├── 写入 workflows.db
    │
    └── 主监督会话调用 Codex Orchestrator MCP
            │
            ├── dispatch_node
            ├── wait_node
            ├── node_status
            └── cancel_node
                    │
                    ├── 本机 app-server
                    └── 远程 app-server
```

各组件职责：

### Java 主机

- 生成完整工作流 JSON。
- 调用 HTTP 网关提交任务。
- 保存 `workflowId`。
- 使用 GET 轮询，或者使用 SSE 监听进度。
- 不直接调用 Python脚本、MCP 或 app-server WebSocket。

### `workflow_gateway.py`

- 对 Java 提供 HTTP API。
- 验证工作流 JSON 和执行机 ID。
- 创建主监督 Codex 会话。
- 把主会话、节点和事件写入 SQLite。
- 对外提供状态查询和 SSE 事件流。

### 主监督 Codex 会话

- 不亲自执行节点业务。
- 根据 `dependsOn` 决定可启动的节点。
- 调用 MCP 的 `dispatch_node` 派发节点。

主监督会话的用户可见更新必须面向完全不懂技术的用户：把节点称为“步骤”，
只说明当前第几步、是否完成、结果和下一步，不显示 MCP、thread、turn、agent、
英文状态码或内部错误。内部调度仍使用原有工具名称，但不会把这些术语展示给用户。
- 调用 `wait_node` 等待节点结束。
- 节点失败时根据 `failurePolicy` 决定是否继续。
- 输出可见的阶段说明，供 Java 或前端监控。

### `codex_orchestrator_mcp.py`

- 暴露节点调度工具给主监督会话。
- 从共享 SQLite 读取完整节点配置。
- 强制验证依赖，不允许提前启动后续节点。
- 根据 `agentId` 连接本机或远程 app-server。
- 为节点创建新的 Codex thread 和 turn。
- 将节点状态与原始 app-server 事件写回 SQLite。

### `workflows.db`

- 存储工作流定义。
- 存储主监督 thread/turn/job 状态。
- 存储每个节点的状态、结果和错误。
- 存储主会话及节点的 app-server 原始事件。
- HTTP 网关和 MCP 进程必须使用同一个数据库绝对路径。

## 三、Java 应该把节点发给谁

Java 不把单个 node 直接发给 app-server，也不直接发给 MCP。

Java 应当把包含全部 `nodes` 的完整工作流 JSON 发送给：

```text
POST http://<Codex执行机IP>:8080/workflows
Content-Type: application/json
```

例如 Codex 执行机 IP 是 `192.168.1.100`：

```text
POST http://192.168.1.100:8080/workflows
```

## 四、工作流 JSON 格式

三个节点按 `a → b → c` 串行执行：

```json
{
  "workflowId": "task-20260820-001",
  "name": "serial-a-b-c",
  "supervisorAgentId": "local",
  "failurePolicy": "stop",
  "handoffMode": "cumulative_files",
  "advanceMode": "semi_automatic",
  "nodes": [
    {
      "id": "node-a",
      "executor": {
        "type": "local",
        "agentId": "local"
      },
      "prompt": "只输出一个小写字母 a，不要输出其他内容",
      "dependsOn": [],
      "timeoutSec": 1800,
      "write": false,
      "permissionProfile": "read_only"
    },
    {
      "id": "node-b",
      "executor": {
        "type": "local",
        "agentId": "local"
      },
      "prompt": "只输出一个小写字母 b，不要输出其他内容",
      "dependsOn": ["node-a"],
      "timeoutSec": 1800,
      "write": false,
      "permissionProfile": "read_only"
    },
    {
      "id": "node-c",
      "executor": {
        "type": "remote",
        "agentId": "remote-build"
      },
      "prompt": "只输出一个小写字母 c，不要输出其他内容",
      "dependsOn": ["node-b"],
      "timeoutSec": 1800,
      "write": false,
      "permissionProfile": "read_only"
    }
  ]
}
```

主要字段：

| 字段 | 含义 |
|---|---|
| `workflowId` | Java 生成的工作流唯一 ID，重复提交同一 ID 会返回错误 |
| `name` | 可选的任务名称 |
| `supervisorAgentId` | 运行主监督会话的执行机 ID |
| `failurePolicy` | `stop` 表示节点失败后停止；`continue` 表示允许继续处理其他可运行节点 |
| `maxRetryCount` | 单个 `workflowId` 允许成功确认的尾部重跑总次数；默认 10，范围 0–100 |
| `advanceMode` | `automatic`（默认）或 `semi_automatic`；半自动仅支持严格串行工作流 |
| `handoffMode` | `cumulative_files` 累计交接前序当前文件；`legacy_text` 传递直接依赖的文字结果。字段缺失按 `legacy_text` 处理 |
| `nodes` | 节点数组 |
| `nodes[].id` | 工作流内唯一节点 ID |
| `nodes[].executor.type` | `local` 或 `remote`，用于表达执行位置 |
| `nodes[].executor.agentId` | `agents.json` 中配置的执行机 ID，真正决定连接哪个 app-server |
| `nodes[].prompt` | 节点收到的完整任务指令 |
| `nodes[].dependsOn` | 本节点依赖的节点 ID 数组；必须全部 `completed` 才允许启动 |
| `nodes[].timeoutSec` | 节点总超时，范围 10–7200 秒 |
| `nodes[].write` | 是否需要写权限；执行机配置也必须允许写入 |
| `nodes[].permissionProfile` | `read_only`、`workspace_write` 或 `auto_review`；旧请求缺省时由 `write` 派生 |
| `nodes[].cwd` | 可选的远程绝对工作目录；执行机必须允许覆盖 cwd |
| `nodes[].model` | 可选的模型覆盖值 |

仅把数组顺序写成 A、B、C 并不能代表串行。真正控制顺序的是：

```json
"dependsOn": ["前一个节点ID"]
```

## 五、节点状态

节点可能处于以下状态：

```text
pending       等待依赖
queued        已请求派发
running       正在执行
cancelling    正在取消
completed     成功完成
failed        执行失败
cancelled     已取消
interrupted   被中断
```

工作流主要状态：

```text
queued        已创建，主监督会话尚未正式运行
running       主监督会话或节点正在执行
completed     主监督会话成功结束，且所有节点都已完成
failed        主监督失败，或结束时仍有节点没有完成
cancelled     工作流被取消
```

## 六、HTTP API

### 1. 提交工作流

```text
POST /workflows
```

成功状态码：

```text
202 Accepted
```

### 2. 查询当前进度

```text
GET /workflows/{workflowId}
```

示例：

```text
GET http://192.168.1.100:8080/workflows/task-20260820-001
```

典型响应：

```json
{
  "workflowId": "task-20260820-001",
  "status": "running",
  "advanceMode": "semi_automatic",
  "pendingAdvance": {
    "gateId": "...",
    "completedNodeId": "node-a",
    "nextNodeId": "node-b",
    "state": "countdown",
    "heldAt": null,
    "expiresAt": "2026-08-26T10:00:30+00:00"
  },
  "currentNodes": ["node-b"],
  "progress": {
    "completed": 1,
    "total": 3
  },
  "retryPolicy": {
    "maxRetries": 10,
    "usedRetries": 3,
    "remainingRetries": 7
  },
  "supervisor": {
    "agentId": "local",
    "jobId": "...",
    "threadId": "...",
    "turnId": "...",
    "status": "running",
    "lastMessage": "node-a 已完成，正在启动 node-b"
  },
  "nodes": [
    {
      "id": "node-a",
      "status": "completed",
      "response": "a"
    },
    {
      "id": "node-b",
      "status": "running"
    },
    {
      "id": "node-c",
      "status": "pending"
    }
  ]
}
```

判断执行到哪一步，主要读取：

- `status`
- `currentNodes`
- `progress`
- `supervisor.lastMessage`
- `nodes[].status`
- `nodes[].response`
- `nodes[].error`

半自动模式只在成功步骤与下一步骤之间等待。等待固定 30 秒并持久化在 SQLite 中，不依赖监控页面是否打开；立即继续或恢复暂停使用：

```text
POST /workflows/{workflowId}/advance/{gateId}/confirm
```

在倒计时结束前取消自动放行使用：

```text
POST /workflows/{workflowId}/advance/{gateId}/hold
```

暂停后 `pendingAdvance.state` 为 `held`，并返回 `heldAt`；原始 `expiresAt` 仅供审计，页面和运行时不再按其自动放行。确认和暂停均为幂等操作。未暂停且到期未确认时运行时自动放行；最后一步、失败步骤和跳过步骤不等待。取消、尾部重跑或其他使当前等待失效的状态变化会关闭旧等待。

“暂停，暂不进入下一步”只控制步骤流转，不代表审核不通过，也不会自动返工。需要返工时，用户应在任务助手中说明问题和修改要求。

### 3. 实时 SSE 事件

```text
GET /workflows/{workflowId}/events
```

支持使用事件序号断点续传：

```text
GET /workflows/{workflowId}/events?after=123
```

事件包括：

- 工作流创建、完成、失败和取消事件。
- 节点请求派发、开始、完成和失败事件。
- 主监督会话的 `item/agentMessage/delta`。
- 完整 agent message。
- `turn/started`、`turn/completed`。
- MCP 工具调用、命令执行和其他 app-server 原始通知。
- 半自动等待创建、人工暂停、暂停后继续、到期自动放行和等待失效事件。

### 4. 查询历史事件

```text
GET /workflows/{workflowId}/events/history?after=0&limit=200
```

### 4.1 查询步骤文件附件

工作流快照的 `nodes[].artifacts` 返回文件名、MIME 类型、大小等元数据，二进制正文通过以下只读接口获取：

```text
GET /workflows/{workflowId}/artifacts/{artifactId}
```

接口仅返回已持久化且属于该工作流的文件，不接受文件路径参数。单文件最多 `20 MB`，单个工作流的当前和历史文件合计最多 `50` 个。图片生成事件产物与 `output/` 文件按 SHA-256 合并去重。图片可内嵌预览，其他类型强制附件下载并设置 `nosniff`。

`cumulative_files` 下，第 N 步的每次尝试会在本机 `artifact_root` 下获得独立目录，其 `inputs/step-01/` 至 `inputs/step-(N-1)/` 保存前序步骤的当前有效文件，`output/` 在开始时为空。编排器直接读写本机文件，提示词只包含步骤来源、文件名、大小、MIME 和绝对路径，不传递 Base64 文件内容；没有文件的历史步骤明确标记“无文件”。所有步骤都可以只返回文字；`write` 只控制写权限，不强制生成附件。节点正常结束后即视为完成，需要业务文件的后续步骤自行检查文件是否存在并决定是否失败。前序文件只是可用输入，当前要求未明确要求使用时不得打开或合并。当前文件流水线要求编排器与 app-server 同机，远程固定目录上传协议不在本版本范围内。

### 5. 发送任务助手消息

```text
POST /workflows/{workflowId}/messages
Content-Type: application/json
```

```json
{
  "messageId": "912f53bd-7044-4f89-9228-c036ef16f6b3",
  "text": "第1步还没结束吗？"
}
```

- `messageId` 由前端生成 UUID。同一 ID 和相同文本重复提交只处理一次；失败重试必须复用原 ID。
- 文本去除首尾空格后不能为空，最长 4000 字符。
- 用户消息先写入 SQLite，再由独立任务助手处理；助手首次咨询创建专用 thread，后续咨询恢复同一 thread，每条消息单独启动 turn，回答后回到空闲状态。
- 助手使用只读沙箱、`approvalPolicy=never` 和结构化输出，只能回答、澄清或产生受限控制意图，不再向主监督 `turn/steer`。
- `completed`、`failed`、`cancelled` 后仍可咨询，也可在额度允许时提出尾部重跑。
- 内容完全为 `确认执行` 或 `取消操作` 的消息由网关直接处理，不调用模型。

聊天允许提议停止任务、跳过步骤，或通过 `restart_from` 从指定步骤重新执行到最后一步。第一次请求只产生待确认操作，助手说明影响后，用户必须另发一条内容完全为：

```text
确认执行
```

才能执行。回复 `取消操作` 可取消待确认操作。确认十分钟后过期；任务状态在确认前发生变化时必须重新提议和确认。用户提供修改意见时，助手在结构化结果的 `revisionInstruction` 中生成不超过 4000 字符的独立返工要求并在确认消息中展示；确认后，该总结与来源消息、目标步骤和重跑序号一起持久化，并追加在目标步骤实际提示词最后。用户原话和原始提示词不被改写。多轮返工要求按时间累积，较新要求优先，最多向提示词传入 20,000 字符，超限时省略最旧要求。一次成功的尾部重跑只消耗一次全局额度，并保留目标步骤之前的结果；目标步骤及后续步骤的旧结果和图片归档后重新执行。提议、取消、重复确认、校验失败和中止失败都不消耗额度。聊天不允许修改工作流、添加或删除步骤。

聊天事件包括：

```text
chat.user.accepted
chat.user.forwarded
chat.assistant.delta
chat.assistant.completed
chat.message.failed
chat.control.proposed
chat.control.confirmed
chat.control.completed
chat.control.failed
chat.control.cancelled
node.restart_from_requested
workflow.retry_budget.updated
```

每个聊天事件都包含 `messageId`，回复事件还包含 `assistantMessageId`，控制事件包含 `actionId`。工作流快照中的 `pendingChatCount` 用于判断终态后是否仍需轮询，`stateVersion` 用于发现生成回复期间的状态变化。

### 6. 请求取消

```text
POST /workflows/{workflowId}/cancel
```

### 7. 健康检查

```text
GET /readyz
```

正常响应：

```json
{"ready": true}
```

## 七、Java 调用示例

### 提交工作流

```java
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

HttpClient client = HttpClient.newHttpClient();

HttpRequest request = HttpRequest.newBuilder()
    .uri(URI.create("http://192.168.1.100:8080/workflows"))
    .header("Content-Type", "application/json")
    .POST(HttpRequest.BodyPublishers.ofString(workflowJson))
    .build();

HttpResponse<String> response = client.send(
    request,
    HttpResponse.BodyHandlers.ofString()
);

System.out.println(response.statusCode());
System.out.println(response.body());
```

正常情况下状态码是 `202`。

### 查询进度

```java
String workflowId = "task-20260820-001";

HttpRequest statusRequest = HttpRequest.newBuilder()
    .uri(URI.create(
        "http://192.168.1.100:8080/workflows/" + workflowId))
    .GET()
    .build();

HttpResponse<String> statusResponse = client.send(
    statusRequest,
    HttpResponse.BodyHandlers.ofString()
);
```

Java 可以每隔一到数秒轮询一次。需要实时界面时，再使用 SSE。

## 八、执行机配置

`agents.json` 决定 `agentId` 对应哪个 app-server。

```json
{
  "agents": {
    "local": {
      "url": "ws://127.0.0.1:4500",
      "cwd": "C:\\work",
      "artifact_root": "C:\\codex-workflow-artifacts",
      "allow_write": true,
      "allow_cwd_override": true
    },
    "remote-build": {
      "url": "wss://worker.example.com/codex",
      "cwd": "/srv/work",
      "artifact_root": "/srv/codex-workflow-artifacts",
      "token_env": "REMOTE_CODEX_TOKEN",
      "allow_write": false,
      "allow_cwd_override": false
    }
  }
}
```

注意事项：

- JSON 中的 `agentId` 必须存在于 `agents.json`。
- 文件流水线的本机执行机必须配置绝对路径 `artifact_root`；该配置只授权编排器在此目录内暂存工作流文件。
- 当前编排器直接用该路径读写文件，不通过 app-server 传输文件内容；远程文件流水线暂不支持。
- `allow_write` 仍只决定 Agent 是否能写业务工作区，不代替 `artifact_root` 授权。
- `allow_write=false` 时只允许 `read_only`；为 `true` 时三档均可用。`read_only = read-only + never`，`workspace_write = workspace-write + never`，`auto_review = workspace-write + on-request + auto_review`。
- 节点启动前调用 `configRequirements/read` 检查执行机管理策略；明确禁止所需审批策略或沙箱时直接失败，旧 app-server 不支持该方法时兼容继续。
- 当前项目实际配置中还需要确认是否存在名为 `local` 的执行机。
- token 不要直接写进 JSON，只写环境变量名称。
- 明文 `ws://` 建议只用于回环地址、可信内网或 SSH 隧道。
- 跨不可信网络时使用 `wss://`。

## 九、主监督 app-server 的 MCP 配置

主监督会话所在的 Codex app-server 必须加载本项目 MCP。下面的 `<PROJECT_ROOT>` 必须替换为仓库的真实绝对路径；Windows TOML 双引号字符串中的反斜杠需要写成 `\\`，Linux/macOS 直接使用 `/absolute/path`：

```toml
[mcp_servers.codex_orchestrator]
command = "uv"
args = [
  "run", "--project", "<PROJECT_ROOT>\\services\\python-workflow",
  "python", "<PROJECT_ROOT>\\services\\python-workflow\\src\\codex_orchestrator_mcp.py"
]
required = true

[mcp_servers.codex_orchestrator.env]
CODEX_AGENTS_FILE = "<PROJECT_ROOT>\\config\\agents.json"
CODEX_WORKFLOW_DB = "<PROJECT_ROOT>\\workflows.db"
```

修改 MCP 配置后需要重启主监督 app-server。

### 主监督 MCP 审批策略

MCP 工具调用不能在 `approvalPolicy = "never"` 的主监督 turn 中直接运行，
否则会出现 `MCP tool call requires approval, but approval policy is never`。

当前网关只对主监督任务发送：

```text
approvalPolicy = on-request
approvalsReviewer = auto_review
```

Codex 会逐次自动审核主监督发起的 MCP 调度调用；普通节点仍使用 `never`。
这比在 `config.toml` 中把整个 Orchestrator MCP 永久设成无条件批准更安全，
也适合无人值守的 Java/Python 工作流。

最重要的要求是：

```text
workflow_gateway.py 使用的 CODEX_WORKFLOW_DB
                      必须等于
codex_orchestrator_mcp.py 使用的 CODEX_WORKFLOW_DB
```

如果路径不同，主监督会话会找不到 Java 提交的工作流和节点。

## 十、启动顺序

推荐按以下顺序启动：

1. 启动本机和远程的 Codex app-server。
2. 确认 `agents.json` 中的 WebSocket 地址可以连接。
3. 在主监督 app-server 中注册 Codex Orchestrator MCP。
4. 重启主监督 app-server。
5. 启动 `workflow_gateway.py`。
6. 调用 `/readyz` 检查网关。
7. 由 Java 提交工作流 JSON。
8. Java 保存 `workflowId`，开始轮询或监听 SSE。

Windows 启动示例：

```powershell
$ProjectRoot = (Resolve-Path .).Path
$env:CODEX_WORKFLOW_DB = Join-Path $ProjectRoot "workflows.db"
$env:CODEX_AGENTS_FILE = (Resolve-Path .\config\agents.json).Path

uv run --project .\services\python-workflow `
  python .\services\python-workflow\src\workflow_gateway.py `
  --host 0.0.0.0 `
  --port 8080 `
  --db $env:CODEX_WORKFLOW_DB `
  --agents $env:CODEX_AGENTS_FILE
```

如果 Java 和 Codex 服务位于同一台机器，优先监听 `127.0.0.1`。只有跨机器访问时才监听 `0.0.0.0`。

## 十一、安全要求

当前 HTTP 网关本身没有内置用户认证。

不要把裸露的 `8080` 端口直接开放到公网。跨机器部署建议：

1. 只允许 Java 主机 IP 访问该端口。
2. 在网关前部署 Nginx、Caddy 或其他反向代理。
3. 使用 HTTPS。
4. 增加 API Key、JWT 或 mTLS 认证。
5. 对提交接口设置请求大小、并发和速率限制。
6. 节点写权限默认关闭，只为必要的执行机显式启用。

## 十二、SQLite 说明

SQLite 用于 HTTP 网关和 MCP 进程之间共享状态。

当前实现会在每次数据库操作后显式关闭连接。原因是 Python `sqlite3.Connection` 的事务上下文只保证提交或回滚，不保证关闭连接。

- Windows 上未关闭连接通常会直接造成数据库文件被占用。
- Linux 允许删除仍然打开的文件，因此问题可能不容易立即出现，但连接和文件描述符仍可能泄漏。
- 当前修复对 Windows 和 Linux 都适用。

SQLite 已启用 WAL 模式，适合当前单机网关和 MCP 进程共享。如果未来需要多台网关、高可用或大量并发，应考虑迁移到 PostgreSQL 等服务型数据库。

## 十三、异常与恢复边界

当前状态和事件已经持久化，但正在运行的 Python `asyncio` job 仍保存在对应进程内存中。

因此：

- 普通查询和事件历史在进程重启后仍然存在。
- 如果网关或 MCP 进程在节点执行中途崩溃，SQLite 会保留最后状态，但当前版本不会自动重新附着到原来的内存 job。
- 生产环境建议先使用进程守护工具保证服务稳定。
- 如果后续需要进程崩溃自动恢复，需要增加启动时状态核对、租约、心跳和重新附着/重试机制。

保留失败记录并以新 `workflowId` 复制重试：

```powershell
uv run --project .\services\python-workflow `
  python .\scripts\retry_workflow.py <原workflowId> `
  --db .\workflows.db `
  --gateway-url http://127.0.0.1:8080
```

## 十四、项目关键文件

```text
services/python-workflow/src/codex_orchestrator_mcp.py  app-server 客户端和 MCP 节点工具
services/python-workflow/src/workflow_gateway.py        HTTP/SSE 网关和主监督会话
services/python-workflow/src/workflow_store.py          SQLite 工作流、节点和事件存储
services/python-workflow/tests/                         Python 自动化测试
config/agents.json                                      当前执行机配置（不提交 Git）
config/agents.example.json                              执行机配置示例
workflows.db                                            运行时数据库（不提交 Git）
README.md                                               项目总览与快速启动
docs/WORKFLOW_GUIDE.zh-CN.md                            本文档
scripts/retry_workflow.py                               复制已有工作流并以新 ID 重新提交
```

## 十五、测试命令

```powershell
uv run --project .\services\python-workflow `
  python -m unittest discover -s .\services\python-workflow\tests -v
```

目前测试覆盖：

- app-server 连接和断开处理。
- RPC 超时和总任务超时。
- token 环境变量读取。
- 原始 app-server 事件回调。
- 工作流依赖环检查。
- 节点依赖强制拦截。
- 重复派发的幂等行为。
- 节点 MCP 派发新 thread。
- 节点状态和事件写入 SQLite。
- 主监督的作用域审批策略会同时传给 thread/start 和 turn/start。

## 十六、最简记忆版本

只需要记住以下流程：

```text
Java 提交完整 nodes JSON
    → POST /workflows
    → 网关创建主监督 Codex thread
    → 主会话通过 MCP 调用 dispatch_node
    → MCP 连接对应 agentId 的 app-server
    → 每个节点使用独立 Codex thread
    → 节点完成后主会话继续调度
    → Java 使用 GET 查询或 SSE 实时监听
```

Java 只对接 HTTP 网关；主会话、MCP、Python 和 app-server 的内部调度由 Codex 执行机负责。
