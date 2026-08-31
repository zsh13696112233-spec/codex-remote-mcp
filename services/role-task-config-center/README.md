# 角色任务配置中心

端口 `8091` 的 Java Web 配置系统，用于维护角色、SOP、串行步骤和可重复执行的任务定义。浏览器只访问本系统；工作流由服务端提交给端口 `8080` 的 Python 网关，提交成功后打开端口 `8090` 的只读监控页面。

## 技术栈

- Java 17
- Spring Boot 4.1
- Spring Data JPA
- MySQL 8
- Flyway
- 原生 HTML、CSS 和 JavaScript

Java 根包为 `com.codexflow.configcenter`。代码按 Web 接口、应用编排、领域与持久化、
外部网关客户端划分；Flyway 继续作为唯一的数据库结构版本管理入口。

## 功能

- 角色的新建、编辑、搜索、启停和软删除。
- 被未删除 SOP 引用的角色禁止删除，只能停用；仅剩历史 SOP 引用时可删除并从列表隐藏。
- 可视化严格串行 SOP 编辑器：左侧选择工作流，从角色库拖入画布生成节点，并通过拖拽调整执行顺序。
- 页面右侧集中配置工作流参数和节点的模型、执行机、目录、超时、Skill/MCP 等属性。
- 主监督执行机固定为本地，期望输出由系统自动生成，配置界面不显示这两个内部字段。
- SOP 默认模型以及单步骤模型覆盖。
- SOP 可配置单次工作流的人工重跑总额度，默认 `10`，范围 `0–100`。
- SOP 可选择 `automatic` 全自动或 `semi_automatic` 半自动流转；默认全自动。
- SOP 可选择 `legacy_text` 文字交接或 `cumulative_files` 文件交接；默认文件交接。
- 未被有效任务引用的 SOP 支持软删除；历史任务仍保留原 SOP 外键，删除后的 SOP 不再出现在配置列表中。
- 工作目录、执行机、写权限、超时和 Skill/MCP 标签配置。
- 任务定义的新建、编辑、复制、搜索、软删除和重复运行。
- 保存每次运行的不可变配置快照和完整提交 JSON。
- 使用最新配置运行、按原运行快照重试、取消运行和查看历史记录。
- 可选的飞书或钉钉长连接机器人：固定绑定一个任务定义，在群聊中启动、查看进度、控制半自动流转并与任务助手对话；两个平台互斥启用。

按原运行快照重试整项任务会生成新的 `workflowId`，沿用快照中的最大重跑额度、流转方式和结果交接方式，并从已使用 `0` 次开始计算。半自动模式在成功步骤与下一步骤之间等待确认，固定 30 秒后自动继续。

使用最新配置创建的新运行会把 SOP 的结果交接方式写入提交 JSON。`legacy_text` 把直接上一步的文字结果追加给下一步，适合纯文本串行任务；`cumulative_files` 累计交接前序当前有效文件且不传递前序文字结果，适合文件流水线。按历史快照重试保留原字段；旧快照如果缺失字段，仍使用兼容的 `legacy_text` 行为。

第一版失败策略固定为 `stop`。Skill 和 MCP 仅作为配置标签保存和展示，不影响真实执行权限。

## 启动依赖

启动本系统前需要准备：

1. Java 17 和 Maven 3.9 或更高版本。
2. MySQL 8 数据库。
3. Python 工作流网关，默认地址为 `http://127.0.0.1:8080`。
4. 任务监控中心，默认地址为 `http://127.0.0.1:8090`。

首次准备数据库的示例：

```sql
CREATE DATABASE codex_config
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE USER 'codex'@'127.0.0.1' IDENTIFIED BY '请替换为强密码';
GRANT ALL PRIVILEGES ON codex_config.* TO 'codex'@'127.0.0.1';
FLUSH PRIVILEGES;
```

空数据库不需要手工建表。Flyway 会在首次启动时创建数据库结构，并初始化“策略负责人”“开发工程师”“质量审查员”三个默认角色。

如果已经手工执行了完整的 `V1__configuration_schema.sql`，启动时 Flyway 会将当前结构登记为版本 `1` 的基线，不会再次执行 V1。后续新增的 V2、V3 等迁移仍会正常执行。

## 开发启动

从仓库根目录执行：

```powershell
Set-Location .\services\role-task-config-center

$env:MYSQL_URL = "jdbc:mysql://127.0.0.1:3306/codex_config?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
$env:MYSQL_USERNAME = "codex"
$env:MYSQL_PASSWORD = "请填写数据库密码"
$env:CODEX_GATEWAY_URL = "http://127.0.0.1:8080"
$env:WORKFLOW_MONITOR_URL = "http://127.0.0.1:8090"

mvn spring-boot:run
```

### 启用飞书机器人

1. 在飞书开放平台创建企业自建应用并启用机器人能力，把机器人加入需要使用的群聊。
2. 在权限管理中开通“接收群聊中 @ 机器人消息”和“以应用身份发送消息”等消息读写权限；在事件订阅中启用“接收消息”和“卡片交互”事件。应用版本需发布到目标用户或群可见范围。
3. 事件接收方式选择[长连接](https://open.feishu.cn/document/ukTMukTMukTM/uETO1YjLxkTN24SM5UjN)。此模式由官方 SDK 主动连接飞书，无需填写本系统公网回调地址、注册公网域名或把 `8091` 暴露到公网；运行环境只需允许访问飞书 HTTPS/WSS `443`。
4. 先在配置中心创建并启用任务定义及其 SOP。需要飞书卡片出现 30 秒确认按钮时，将 SOP 流转方式设为 `semi_automatic`。
5. 启动配置中心并访问 `http://127.0.0.1:8091`，进入“飞书机器人”页面，填写 App ID、App Secret、固定任务和事件轮询间隔。先点击“测试连接”，成功后勾选启用并保存。

页面配置保存在配置中心 MySQL 中，保存后立即应用，无需重启。App Secret 是只写字段：查询接口和页面不会返回原值，后续留空保存表示继续使用已保存密钥。页面显示“已连接”即表示 SDK 长连接可用；“测试连接”只验证候选凭据，不保存参数或启用机器人。

首次尚未保存页面配置时，仍可用下面的 `FEISHU_*` 环境变量作为启动默认值。数据库中存在配置后，以数据库配置为准。

群聊使用规则：

- 顶层发送纯 `@机器人` 或 `@机器人 运行` 启动固定任务；不响应 `@所有人`，不支持单聊。
- 同一时间全局只允许一个机器人任务。运行中再次顶层 @ 只返回当前任务编号和进度，不创建新运行。
- 启动后在同一话题中直接询问状态，或提出“停止”“跳过”“从某步返工”等请求；实际控制沿用任务助手的“确认执行 / 取消操作”二次确认。
- 半自动步骤卡片可选择“立即进入下一步”或“暂停”，暂停后可继续。无人操作时 30 秒后自动流转。
- 群内所有成员都能咨询并操作按钮。任务终态会释放执行槽，但旧话题会保留，以便继续咨询或在空闲时确认返工。
- 飞书断线不会中止工作流。事件游标与待发送消息保存在 MySQL，连接恢复后继续补发。

### 启用钉钉机器人

1. 在钉钉开放平台创建企业内部应用并启用机器人，把应用发布到测试范围并将机器人加入目标群。
2. 在机器人配置中将消息接收模式设为 Stream 模式，并开通机器人接收群消息和应用凭证发送群消息所需权限。Stream 模式由官方 SDK 主动连接钉钉，无需配置公网回调地址；运行环境只需允许访问钉钉 HTTPS/WSS `443`。
3. 先在配置中心创建并启用固定任务及其 SOP；需要 30 秒步骤确认时，将 SOP 流转方式设为 `semi_automatic`。
4. 进入 8091 的“钉钉机器人”页面，填写 Client ID、Client Secret、固定任务和轮询间隔。互动进度卡模板 ID 可以留空；先测试连接，成功后再启用并保存。
5. 如需使用互动卡片，再在钉钉互动卡片平台创建并发布模板，开通卡片创建和更新权限，记录模板 ID并填回配置页面。可直接导入仓库中的 [`docs/card_1787896598119.json`](../../docs/card_1787896598119.json) 作为进度卡模板；模板需提供下表中的变量，并将按钮回调类型设为 Stream。

如果此前已经导入过旧版示例模板，需要重新导入当前 JSON 并发布一个新版本，才能获得精简版任务卡片。旧变量全部保留，模板 ID 不变时无需重新配置代码或机器人。

卡片模板 ID 留空时，机器人使用钉钉内置 `sampleMarkdown` 展示启动、步骤进度和最终状态，不需要自定义卡片模板；任务助手的普通回答仍以文本发送。两类消息都通过 Outbox 持久化并在断线恢复后补发。半自动等待期间可回复“暂停”“继续”或“立即进入下一步”。填写模板 ID 后才会创建和更新带 Stream 回调按钮的互动卡片；任务运行期间不能切换有无模板。

互动卡片会复用同一个卡片实例动态刷新，而不是每次进度变化都创建新卡。为避免钉钉提示组件过多，每个普通状态只使用任务标题和一个合并正文组件，等待状态额外保留按钮组，四个状态合计 9 个组件。正文使用“步骤状态”和“最终结果”两个紧凑分区，展示状态与步骤数、最多 6 项的步骤轨迹，以及当前阶段必要的产出、结果或等待操作；卡片总状态使用红黄绿圆点：绿色表示完成，黄色表示处理中、等待或准备，红色表示失败、停止或超时。步骤行使用 `✅` 表示完成、`•` 表示处理中、`❌` 表示失败、停止或超时。助手对话不再重复放进进度卡，完整回复仍会单独发送。执行中、完成和失败状态不渲染空按钮区域。为避免网络恢复后旧更新覆盖新进度，过期的卡片 Outbox 更新会直接淘汰。

钉钉卡片模板变量约定：

| 变量 | 用途 |
| --- | --- |
| `title`、`markdown`、`status` | 动态任务名、Markdown 进度与回复摘要、带图标的当前状态 |
| `progressText` | 简短的完成比例和步骤数，兼容旧版模板 |
| `currentStep`、`stepTimeline` | 当前步骤详情和最多 6 项的紧凑步骤轨迹 |
| `latestOutput`、`latestReply` | 最近步骤产出和最新助手回复的引用式摘要 |
| `result`、`notice` | 最终结果或失败说明，以及等待确认和最近状态提示 |
| `cardBody` | 精简后的状态、步骤、产出或等待操作，供当前模板展示 |
| `flowStatus` | 卡片视觉状态：等待 `1`、完成 `3`、执行中 `4`、失败 `5` |
| `workflowId`、`gateId` | 工作流和半自动等待标识，按钮回调时必须原样带回 |
| `showConfirm`、`showHold` | 控制“立即进入下一步”和“暂停”按钮是否显示 |
| `confirmText`、`holdText` | 两个按钮的动态文案 |
| `confirmAction`、`holdAction` | 回调动作值，分别为 `advance_confirm` 和 `advance_hold` |

按钮回调参数必须包含 `action`、`workflowId` 和 `gateId`；`action` 使用对应的 `confirmAction` 或 `holdAction`。应用使用 Client ID 作为机器人编码，并通过应用访问令牌创建和更新卡片，因此长期进度补发不依赖会过期的临时会话 Webhook。

钉钉群聊使用规则：

- 顶层发送纯 `@机器人` 或 `@机器人 运行` 启动固定任务；不响应 `@所有人`，不支持单聊。
- 任务运行期间，在启动任务的同一群内直接发送 `@机器人 + 问题或控制指令` 即可进入当前任务助手；也可以回复或引用该任务的启动指令、机器人进度消息或进度卡。未 @ 机器人且未回复、未引用的普通群消息不会进入任务助手。任务结束后如需继续咨询，应回复或引用该任务的历史消息，以便准确定位工作流。
- 半自动确认、暂停、继续、30 秒自动放行、二次确认、终态释放和可靠 Outbox 补发语义与飞书版本一致。无模板的 Markdown 模式回复“暂停”“继续”或“立即进入下一步”，互动卡片模式使用按钮。
- 同一部署只能启用飞书或钉钉其中一个。保存启用配置时若另一平台仍启用，系统会拒绝并提示先停用，不会自动切换连接。

页面配置保存在 MySQL 中并立即应用。Client Secret 只写不回显，后续留空保存表示继续使用已保存密钥。首次尚未保存页面配置时，可用 `DINGTALK_*` 环境变量作为启动默认值。

启动后访问：

```text
http://127.0.0.1:8091
```

## 打包运行

```powershell
mvn package
java -jar .\target\role-task-config-center-0.1.0.jar
```

运行 JAR 前同样需要设置 MySQL 和网关环境变量。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVER_PORT` | `8091` | 服务端口 |
| `SERVER_ADDRESS` | `0.0.0.0` | 监听地址 |
| `MYSQL_URL` | 本机 `codex_config` JDBC 地址 | MySQL JDBC URL |
| `MYSQL_USERNAME` | `codex` | 数据库用户名 |
| `MYSQL_PASSWORD` | 无 | 数据库密码，必须显式设置 |
| `CODEX_GATEWAY_URL` | `http://127.0.0.1:8080` | Python 网关地址 |
| `WORKFLOW_MONITOR_URL` | `http://127.0.0.1:8090` | 监控中心地址 |
| `DEFAULT_STEP_MODEL` | `gpt-5.6-sol` | SOP 默认步骤模型 |
| `FEISHU_ENABLED` | `false` | 页面尚未保存配置时的长连接开关默认值 |
| `FEISHU_APP_ID` | 空 | 页面尚未保存配置时的 App ID 默认值 |
| `FEISHU_APP_SECRET` | 空 | 页面尚未保存配置时的 App Secret 默认值 |
| `FEISHU_TASK_DEFINITION_ID` | 空 | 页面尚未保存配置时的固定任务默认值 |
| `FEISHU_EVENT_POLL_INTERVAL_MS` | `1000` | 页面尚未保存配置时的轮询间隔默认值，范围 250–60000 毫秒 |
| `DINGTALK_ENABLED` | `false` | 页面尚未保存配置时的钉钉长连接开关默认值 |
| `DINGTALK_CLIENT_ID` | 空 | 页面尚未保存配置时的钉钉 Client ID 默认值 |
| `DINGTALK_CLIENT_SECRET` | 空 | 页面尚未保存配置时的钉钉 Client Secret 默认值 |
| `DINGTALK_TASK_DEFINITION_ID` | 空 | 页面尚未保存配置时的钉钉固定任务默认值 |
| `DINGTALK_CARD_TEMPLATE_ID` | 空 | 页面尚未保存配置时的可选互动卡片模板 ID；留空使用内置 Markdown 进度消息 |
| `DINGTALK_EVENT_POLL_INTERVAL_MS` | `1000` | 页面尚未保存配置时的钉钉轮询间隔默认值，范围 250–60000 毫秒 |

## REST 接口

```text
GET    /api/roles
POST   /api/roles
PUT    /api/roles/{id}
DELETE /api/roles/{id}

GET    /api/sops
GET    /api/sops/{id}
POST   /api/sops
PUT    /api/sops/{id}
DELETE /api/sops/{id}

GET    /api/task-definitions
GET    /api/task-definitions/{id}
POST   /api/task-definitions
PUT    /api/task-definitions/{id}
DELETE /api/task-definitions/{id}
POST   /api/task-definitions/{id}/copy

POST   /api/task-definitions/{id}/runs
GET    /api/task-definitions/{id}/runs
POST   /api/task-runs/{workflowId}/cancel
POST   /api/task-runs/{workflowId}/retry

GET    /api/agents
GET    /api/gateway/ready

GET    /api/feishu/config
PUT    /api/feishu/config
POST   /api/feishu/config/test

GET    /api/dingtalk/config
PUT    /api/dingtalk/config
POST   /api/dingtalk/config/test
```

飞书和钉钉配置接口只服务于受保护的 8091 内网页面。GET 和 PUT 的响应只返回 `secretConfigured`，不会返回 App Secret 或 Client Secret；测试接口也只返回成功状态和普通中文提示。两个平台的启用状态互斥。

角色、SOP 和任务定义列表接口支持 `q` 查询参数，例如：

```text
GET /api/roles?q=开发
```

## 测试

```powershell
mvn test
```

构建会自动检查 Java 格式。需要修复格式时执行：

```powershell
mvn fmt:format
```

自动化测试使用 H2 的 MySQL 兼容模式，并验证 Flyway 建表和默认数据。正式验收仍应连接真实 MySQL 8，验证数据持久化和服务重启后的恢复情况。

## Flyway 首次初始化失败后的处理

如果首次启动曾在 `V1__configuration_schema.sql` 中失败，MySQL 可能保留 Flyway 的失败记录。对于尚未存放业务数据的新数据库，最安全的处理方式是删除并重新创建 `codex_config`，然后重新启动服务。不要在已有正式数据的数据库中执行删除操作。

也可以先检查迁移记录：

```sql
SELECT installed_rank, version, description, success
FROM flyway_schema_history
ORDER BY installed_rank;
```

确认只有失败的首次初始化记录且没有业务数据后，可以删除失败记录，再重新启动：

```sql
DELETE FROM flyway_schema_history WHERE success = 0;
```

## 安全说明

系统第一版没有登录和多用户权限，默认监听 `0.0.0.0` 以支持可信内网访问；必须通过主机防火墙限制来源，或部署在受保护的反向代理后，不得直接暴露到公网。如只需本机访问，可设置 `SERVER_ADDRESS=127.0.0.1`。

飞书入口不改变上述网络边界：SDK 只建立主动出站长连接。页面保存的机器人密钥存放在 MySQL 中，但不会通过查询接口、页面或日志回显；数据库备份和数据库账号必须按密钥级别保护。机器人加入的所有群均可触发固定任务，且群成员共享控制权限，因此应通过飞书应用可见范围和群成员管理限制使用人群。
