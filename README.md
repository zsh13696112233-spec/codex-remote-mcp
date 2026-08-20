# Codex 主会话工作流编排

完整中文架构、部署、Java 接入和监控说明见
[`WORKFLOW_GUIDE.zh-CN.md`](WORKFLOW_GUIDE.zh-CN.md)。

带节点编辑和实时监控界面的 Spring Boot 控制台见
[`workflow-console/README.md`](workflow-console/README.md)。

这个项目把 Codex `app-server` 包装成一个可由 Java 调用、可持续监控的工作流服务。

运行时有三个角色：

1. `workflow_gateway.py` 接收工作流 JSON，启动一个 Codex 主监督会话，并提供状态与 SSE 接口。
2. 主监督会话调用 `codex_orchestrator_mcp.py` 暴露的 `dispatch_node`、`wait_node` 等工具。
3. MCP 编排器为每个节点新建独立 Codex thread，连接本机或远程 `app-server`。节点依赖由 SQLite 强制检查，不只依赖模型自觉遵守。

`workflow_gateway.py` 和 MCP 进程必须使用同一个 `CODEX_WORKFLOW_DB` 绝对路径。主会话消息、节点状态、thread/turn ID，以及 app-server 原始通知都会进入这个数据库。

## 1. 配置执行机

复制 `agents.example.json` 为自己的配置。`local` 和 `remote-build` 只是业务名称；两者底层都通过 WebSocket 连接 app-server。

```json
{
  "agents": {
    "local": {
      "url": "ws://127.0.0.1:4500",
      "cwd": "C:\\work",
      "allow_write": true,
      "allow_cwd_override": true
    },
    "remote-build": {
      "url": "wss://worker.example.com/codex",
      "cwd": "/srv/work",
      "token_env": "REMOTE_CODEX_TOKEN",
      "allow_write": false,
      "allow_cwd_override": false
    }
  }
}
```

不要把 token 写进 JSON；只写环境变量名。生产环境建议用 WSS，或者把未加密 WebSocket 限制在回环地址/SSH 隧道中。

## 2. 把编排器注册给主会话

主监督会话所在的 Codex app-server 必须配置本项目的 MCP server。下面是 Codex `config.toml` 的示例；路径应改成部署机上的绝对路径。

```toml
[mcp_servers.codex_orchestrator]
command = "python"
args = ["C:\\services\\codex-remote-mcp\\codex_orchestrator_mcp.py"]
required = true

[mcp_servers.codex_orchestrator.env]
CODEX_AGENTS_FILE = "C:\\services\\codex-remote-mcp\\agents.json"
CODEX_WORKFLOW_DB = "C:\\services\\codex-remote-mcp\\workflows.db"
```

重启 app-server 使 MCP 配置生效。启动 HTTP 网关时必须传入同一份 agent 配置和同一个数据库：

主监督任务会由网关单独设置为 `approvalPolicy=on-request` 和
`approvalsReviewer=auto_review`，从而让 Codex 自动审核 `dispatch_node`、
`wait_node` 等 MCP 调度调用。普通节点任务仍保持 `approvalPolicy=never`。
不要把整个 MCP 永久配置成无条件批准；这样可以把自动审批限制在主监督任务内。

```powershell
$env:CODEX_WORKFLOW_DB = "C:\services\codex-remote-mcp\workflows.db"
python workflow_gateway.py --host 127.0.0.1 --port 8080 `
  --db $env:CODEX_WORKFLOW_DB --agents .\agents.json
```

默认只监听 `127.0.0.1`。如果 Java 主机跨机器访问，建议由带 TLS 和认证的反向代理暴露，不要直接把无认证的 8080 端口开放到网络。

## 3. 提交串行节点

下面的三个节点都会创建新的 Codex thread。依赖关系保证 `a -> b -> c` 串行；节点也可以分别指定不同的本地/远程 agent。

```json
{
  "workflowId": "demo-20260820-001",
  "name": "serial-a-b-c",
  "supervisorAgentId": "local",
  "failurePolicy": "stop",
  "nodes": [
    {
      "id": "node-a",
      "executor": {"type": "local", "agentId": "local"},
      "prompt": "只输出一个小写字母 a，不要输出其他内容",
      "dependsOn": []
    },
    {
      "id": "node-b",
      "executor": {"type": "local", "agentId": "local"},
      "prompt": "只输出一个小写字母 b，不要输出其他内容",
      "dependsOn": ["node-a"]
    },
    {
      "id": "node-c",
      "executor": {"type": "remote", "agentId": "remote-build"},
      "prompt": "只输出一个小写字母 c，不要输出其他内容",
      "dependsOn": ["node-b"]
    }
  ]
}
```

提交接口：

```text
POST /workflows                         提交，返回 202 和初始快照
GET  /workflows/{workflowId}            当前节点、进度、主会话消息和所有节点状态
GET  /workflows/{workflowId}/events     SSE 实时事件流，可用 ?after=序号 断点续传
GET  /workflows/{workflowId}/events/history?after=0&limit=200
POST /workflows/{workflowId}/cancel     请求取消主监督会话
GET  /readyz                            存活检查
```

Java 不需要再调用 Python。Java 直接向 HTTP 网关发送 JSON，再通过 `GET` 轮询或 SSE 监听即可。Python 只部署在 Codex 执行机上，充当协议适配和持久化服务。

失败工作流需要保留原记录并复制重试时，可以运行：

```powershell
.\.venv\Scripts\python.exe .\scripts\retry_workflow.py <原workflowId> `
  --db .\workflows.db --gateway-url http://127.0.0.1:8080
```

状态快照里最关键的字段：

```json
{
  "status": "running",
  "currentNodes": ["node-b"],
  "progress": {"completed": 1, "total": 3},
  "supervisor": {
    "threadId": "...",
    "turnId": "...",
    "status": "running",
    "lastMessage": "node-a 已完成，正在启动 node-b"
  },
  "nodes": [
    {"id": "node-a", "status": "completed", "response": "a"},
    {"id": "node-b", "status": "running"},
    {"id": "node-c", "status": "pending"}
  ]
}
```

SSE 不只包含汇总状态，也包含 `appserver.item/agentMessage/delta`、`appserver.item/completed`、`appserver.turn/completed`、MCP 工具调用等原始通知，所以可以展示主会话的实时对话过程。

## 4. Java 调用最小示例

```java
HttpClient client = HttpClient.newHttpClient();
HttpRequest request = HttpRequest.newBuilder()
    .uri(URI.create("http://127.0.0.1:8080/workflows"))
    .header("Content-Type", "application/json")
    .POST(HttpRequest.BodyPublishers.ofString(workflowJson))
    .build();
HttpResponse<String> response = client.send(
    request, HttpResponse.BodyHandlers.ofString());
```

Java 11 的 `HttpClient` 可以用 `BodyHandlers.ofLines()` 消费 SSE；也可以先用普通 GET 轮询状态。提交时自带唯一 `workflowId`，重复 ID 会返回 400，便于调用方实现幂等控制。

## 运行测试

```powershell
python -m unittest discover -s tests -v
```

这里的“thread”指 Codex 会话线程，不等同于 JVM/Python 操作系统线程。一个工作流通常包含一个主监督 Codex thread，加上每个节点一个独立 Codex thread。
