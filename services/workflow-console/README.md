# 任务运行监控中心

端口 `8090` 的 Java Web 监控系统，用于展示一项 Codex 工作流的实时执行过程，并与该工作流的独立任务助手对话。浏览器访问 Java 服务，Java 服务代理端口 `8080` 的 Python 网关；本系统不连接配置中心的 MySQL 数据库。

## 技术栈

- Java 17
- Spring Boot 4.1
- Java `HttpClient`
- 原生 HTML、CSS 和 JavaScript

Java 根包为 `com.codexflow.console`，Web 控制器、异常映射和
工作流网关客户端分别维护。

## 页面能力

- 每个页面只查看 URL 指定的一项任务。
- 展示任务名称、整体状态、当前步骤和完成进度。
- 展示每一步的名称、角色、状态、起止时间和结果。
- 在对应步骤结果中展示工作流生成的图片，并支持打开原图。
- 将主监督的用户可读消息标记为“任务进度”，将咨询回复标记为“任务助手”。
- 在任务助手区域咨询最新进度，刷新页面后恢复完整对话。
- 通过聊天提议停止任务、跳过步骤，或从指定步骤重跑到末尾；任务助手会展示 AI 总结的返工要求，必须另发“确认执行”才会生效。
- 半自动 SOP 在步骤成功后显示 30 秒倒计时、“立即进入下一步”和“暂停，暂不进入下一步”按钮；暂停后不再自动放行，并显示“继续进入下一步”，刷新页面可从网关状态恢复等待或暂停。暂停不会触发返工，结果不符合要求时需在任务助手中说明修改点。
- 展示当前工作流的重跑上限、已用和剩余额度。
- 每两秒刷新一次；终态任务且没有待处理消息后自动停止刷新，发送消息或任务重新运行后恢复。
- 将内部状态转换为普通用户容易理解的中文。
- 不展示内部事件 JSON、执行机信息或会话编号。

本系统不提供任务编辑、提交、停止、尾部重跑或跳过的直接接口；这些控制仍只能通过任务助手二次确认。仅允许暂停或继续半自动 SOP 当前等待；添加、删除和修改步骤不受支持。

## 启动依赖

启动前需要准备：

1. Java 17 和 Maven 3.9 或更高版本。
2. Python 工作流网关，默认地址为 `http://127.0.0.1:8080`。

监控中心可以在网关暂时离线时启动，但查询任务和连接状态会提示不可用。

## 开发启动

从仓库根目录执行：

```powershell
Set-Location .\services\workflow-console

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

系统提供四个读取接口、一个聊天消息接口和两个半自动流转接口：

```text
GET /api/gateway/ready
GET /api/workflows/{workflowId}
GET /api/workflows/{workflowId}/events?after=0&limit=200
GET /api/workflows/{workflowId}/artifacts/{artifactId}
POST /api/workflows/{workflowId}/messages
POST /api/workflows/{workflowId}/advance/{gateId}/confirm
POST /api/workflows/{workflowId}/advance/{gateId}/hold
```

消息请求示例：

```json
{"messageId":"浏览器生成的 UUID","text":"第1步还没结束吗？"}
```

相同 `messageId` 的失败请求应复用原 UUID 重试。所有终态仍可继续咨询；也可在额度允许时请求从某一步重新执行到末尾。

其中事件接口代理 Python 网关的历史事件查询，并支持通过 `after` 增量读取。`limit` 的允许范围为 `1` 到 `1000`。

图片接口只代理 Python 网关已经归属到该工作流的图片附件，不接受本机文件路径。浏览器不会直接访问 Python 网关或执行机文件系统。

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

构建会自动检查 Java 格式。需要修复格式时执行 `mvn fmt:format`。

测试会启动 Spring 上下文，并验证 `/api` 下只有四个 GET、一个消息 POST 和暂停、继续两个半自动流转 POST 路由，不存在直接提交、取消、重试、跳过或编辑接口。

## 安全说明

系统没有登录功能。默认只监听 `127.0.0.1`；如需通过可信内网访问，可以设置 `SERVER_ADDRESS=0.0.0.0`，但不得将本系统或 Python 网关直接暴露到公网。
