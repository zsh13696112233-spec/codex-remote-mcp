# 任务运行监控中心

端口 `8090` 的 Java Web 监控系统，用于展示一项 Codex 工作流的实时执行过程，并与该工作流原有的主监督会话对话。浏览器访问 Java 服务，Java 服务代理端口 `8080` 的 Python 网关；本系统不连接配置中心的 MySQL 数据库。

## 技术栈

- Java 17
- Spring Boot 4.1
- Java `HttpClient`
- 原生 HTML、CSS 和 JavaScript

## 页面能力

- 每个页面只查看 URL 指定的一项任务。
- 展示任务名称、整体状态、当前步骤和完成进度。
- 展示每一步的名称、角色、状态、起止时间和结果。
- 展示主监督会话的用户可读消息时间线和最终总结。
- 在任务助手区域咨询最新进度，刷新页面后恢复完整对话。
- 通过聊天提议停止任务、重试或跳过未成功步骤；必须另发“确认执行”才会生效。
- 每两秒刷新一次；已完成任务且没有待处理消息后自动停止刷新。
- 将内部状态转换为普通用户容易理解的中文。
- 不展示内部事件 JSON、执行机信息或会话编号。

本系统不提供任务编辑、提交或直接控制接口。停止、重试和跳过只能通过聊天二次确认；添加、删除、修改步骤和重新执行已完成步骤不受支持。

## 启动依赖

启动前需要准备：

1. Java 17 和 Maven 3.9 或更高版本。
2. Python 工作流网关，默认地址为 `http://127.0.0.1:8080`。

监控中心可以在网关暂时离线时启动，但查询任务和连接状态会提示不可用。

## 开发启动

```powershell
cd C:\Users\13696\work\codex-remote-mcp\workflow-console

$env:CODEX_GATEWAY_URL = "http://127.0.0.1:8080"
$env:SERVER_ADDRESS = "127.0.0.1"
$env:SERVER_PORT = "8090"

mvn spring-boot:run
```

查看指定任务：

```text
http://127.0.0.1:8090/?workflowId=<workflowId>
```

URL 没有 `workflowId` 时，页面只显示任务编号输入框，不展示全局任务列表。

## 打包运行

```powershell
mvn package

$env:CODEX_GATEWAY_URL = "http://127.0.0.1:8080"
java -jar .\target\workflow-console-0.1.0.jar
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVER_PORT` | `8090` | 服务端口 |
| `SERVER_ADDRESS` | `127.0.0.1` | 监听地址 |
| `CODEX_GATEWAY_URL` | `http://127.0.0.1:8080` | Python 网关地址 |

## 业务接口

系统只提供三个读取接口和一个聊天消息接口：

```text
GET /api/gateway/ready
GET /api/workflows/{workflowId}
GET /api/workflows/{workflowId}/events?after=0&limit=200
POST /api/workflows/{workflowId}/messages
```

消息请求示例：

```json
{"messageId":"浏览器生成的 UUID","text":"第1步还没结束吗？"}
```

相同 `messageId` 的失败请求应复用原 UUID 重试。`completed` 后不再接收新消息；`failed` 或 `cancelled` 后仍可咨询并重试未成功步骤。

其中事件接口代理 Python 网关的历史事件查询，并支持通过 `after` 增量读取。`limit` 的允许范围为 `1` 到 `1000`。

## 状态显示

| 内部状态 | 页面显示 |
| --- | --- |
| `pending` | 尚未开始 |
| `queued` | 等待开始 |
| `running` | 正在进行 |
| `completed` | 已完成 |
| `skipped` | 已跳过 |
| `failed` | 未完成 |
| `cancelled` | 已停止 |
| `interrupted` | 已中断 |
| `cancelling` | 正在停止 |

## 测试

```powershell
mvn test
```

测试会启动 Spring 上下文，并验证 `/api` 下只有三个 GET 和一个消息 POST 路由，不存在直接提交、取消、重试、跳过或编辑接口。

## 安全说明

系统没有登录功能。默认只监听 `127.0.0.1`；如需通过可信内网访问，可以设置 `SERVER_ADDRESS=0.0.0.0`，但不得将本系统或 Python 网关直接暴露到公网。
