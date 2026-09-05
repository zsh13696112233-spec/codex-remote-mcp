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
- 左侧“运行状态”菜单可独立查看 Python 网关、主监督状态汇总和全部已登记主监督明细，不需要新建或打开 SOP；页面以绿、蓝、红、灰分别提示在线空闲、在线忙碌、离线和状态未知，并支持手动刷新。远程 `remote_sidecar` 主监督使用中央网关保存的 5 秒权威心跳，20 秒未续租即离线；本机兼容主监督继续使用轻量探测。
- 主监督执行机和步骤执行机都可以填写任意非空 ID，并由 `/api/agents` 提供建议列表；运行状态页和主监督下拉每 10 秒刷新一次。状态只作提示，网关离线或 ID 不在列表中仍可保存。
- SOP 默认模型以及单步骤模型覆盖。
- SOP 可配置单次工作流的人工重跑总额度，默认 `10`，范围 `0–100`。
- SOP 可选择 `automatic` 全自动或 `semi_automatic` 半自动流转；默认全自动。
- SOP 可选择 `legacy_text` 文字交接或 `cumulative_files` 文件交接；默认文字交接。
- 未被有效任务引用的 SOP 支持软删除；历史任务仍保留原 SOP 外键，删除后的 SOP 不再出现在配置列表中。
- 工作目录、执行机、节点权限档位、超时和 Skill/MCP 标签配置。
- 任务定义的新建、编辑、复制、搜索、软删除和重复运行。
- 任务定义可选择每天固定北京时间运行，或每隔 5–1440 分钟运行；错过时间不补跑，上一次仍未完成时跳过本次。同一任务定义同时只运行一个工作流，不同任务定义可以并行。
- 网页、定时和钉钉入口共用任务级运行槽；提交响应不明确时保留原 `workflowId` 和占用，由后台对账确认或幂等补交，不会贸然启动第二个工作流。
- 任务占用的申请、重跑恢复和终态释放统一由 `TaskLaunchStore` 在行锁内处理。钉钉目录只维护通知归属；两种关联不一致时保守保护现有运行，后台对账也检查仅剩钉钉关联的任务。旧运行终态只释放与自身编号匹配的关联，不覆盖新运行。同一事务内释放后可重新预约，任务占用与通知绑定一同提交或回滚；Python 运行时的执行互斥继续独立生效。
- 保存每次运行的不可变配置快照和完整提交 JSON。
- 使用最新配置运行、按原运行快照重试、取消运行和查看历史记录。
- 可选的钉钉长连接机器人，允许多个任务定义分别绑定一个已启用人员或群聊，不同绑定可并行运行。
- 钉钉人员页按同步到本地的部门父子关系展示默认收起、可展开的组织树；选择部门后显示直属人员，并可按姓名搜索，多部门人员会出现在每个所属部门中。人员的启用开关自动保存，不提供保存或删除按钮；人员生命周期由通讯录同步维护。部门只用于展示和筛选，不作为通知对象。

按原运行快照重试整项任务会生成新的 `workflowId`，沿用快照中的最大重跑额度、流转方式和结果交接方式，并从已使用 `0` 次开始计算。半自动模式在成功步骤与下一步骤之间等待确认，固定 30 秒后自动继续。

使用最新配置创建的新运行会把 SOP 的结果交接方式写入提交 JSON。`legacy_text` 把直接上一步的文字结果追加给下一步，适合纯文本串行任务；`cumulative_files` 累计交接前序当前有效文件且不传递前序文字结果，适合文件流水线。按历史快照重试保留原字段；旧快照如果缺失字段，仍使用兼容的 `legacy_text` 行为。

配置中心仍允许为远程主监督保存任一种交接方式，不依赖网关在线或能力状态。阶段 B 运行时只支持远程主监督的 `legacy_text`；如果不可变提交快照选择 `cumulative_files`，Python 网关会在持久化工作流前稳定拒绝，并提示改用文字交接。本系统不读取 Sidecar 令牌，也不直接调用 `/internal/v1`。

第一版失败策略固定为 `stop`。Skill 和 MCP 仅作为配置标签保存和展示，不影响真实执行权限。

新节点默认使用 `read_only`。权限档位为 `read_only`（只读、不审批）、`workspace_write`（工作区写入、不审批）、`auto_review`（工作区写入、越界请求交给 Auto-review）和 `full_access`（完全访问、不审批）。`full_access` 只有在执行机同时配置 `allow_write: true` 与 `allow_full_access: true` 时才显示；它不受文件系统和网络沙箱限制。页面从 `/api/agents` 的 `permissionProfiles` 限制选项；旧网关未返回该字段时仅保留原有只读/写入两种能力。

新建 SOP 的主监督默认填写 `local`，但可以修改。配置中心保存时只校验主监督 ID 非空且不超过 128 个字符，不访问网关校验在线、启停或能力；真正运行时由 Python 网关权威校验。不同步骤可以保存不同执行机和不同工作目录，目录留空继续表示继承执行机默认目录。使用最新配置运行和按历史快照重试都会冻结并复用对应的 `supervisorAgentId`。

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

### 启用钉钉机器人

1. 在钉钉开放平台创建企业内部应用并启用机器人，把应用发布到目标人员范围；群模式还要把机器人加入目标群。
2. 在机器人配置中将消息接收模式设为 Stream 模式，开通机器人接收消息、应用凭证发送群消息和单聊消息所需权限。需要同步公司人员时，还要给应用开通读取通讯录部门和成员的权限，并确保应用可见范围包含这些人员。Stream 模式由官方 SDK 主动连接钉钉，无需配置公网回调地址；运行环境只需允许访问钉钉 HTTPS/WSS `443`。
3. 进入 8091 的“钉钉机器人”页面，填写 Client ID、Client Secret 和轮询间隔；机器人可以在尚未绑定任务时连接，以便发现群聊。
4. 进入“钉钉通知对象”：人员由管理员点击“同步公司人员”手动拉取，首次同步默认停用；群聊在群内首次 `@机器人` 后自动登记，回到群聊页点击“刷新群聊”即可显示，默认停用。管理员确认名称、测试发送并启用对象。
5. 编辑任务定义，在“钉钉通知对象”中先选择“不绑定”“群聊”或“人员”，再从对应列表选择一个已启用对象。同一任务只能选择一个对象，同一对象也只能绑定一个任务；多个任务可以引用同一个 SOP。需要网页或定时运行主动通知时，同时勾选“网页或定时运行后推送钉钉”。
6. 如需 30 秒步骤确认，将 SOP 流转方式设为 `semi_automatic`。如需群聊互动卡片，再在钉钉互动卡片平台创建并发布模板，开通卡片创建和更新权限，记录模板 ID 并填回配置页面。可直接导入仓库中的 [`config/dingtalk-card-template.json`](../../config/dingtalk-card-template.json)；个人模式始终使用文本或 Markdown，不发送互动卡片。

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

钉钉通知与控制规则：

- 一个机器人可以处理多个任务定义；每个任务定义最多绑定一个人员或群聊，同一对象只能绑定一个任务定义。
- 同一任务定义同时最多运行一个任务，不同任务定义可以并行运行。上一个任务未结束时，网页重复运行会被拒绝，定时运行会跳过。暂停、继续、取消和任务助手对话只作用于消息来源对应的工作流。
- 从钉钉目标对象发起的运行始终建立钉钉会话绑定；任务定义勾选主动通知后，从 8091 页面或定时启动的运行也会建立绑定并回推消息。
- 群目标：顶层发送纯 `@机器人` 或 `@机器人 运行` 启动；不响应 `@所有人`。运行期间群内所有成员都可 `@机器人 + 问题或控制指令`，也可回复或引用启动消息、进度消息或进度卡。其他群不能启动、咨询或控制该 SOP。
- 人员目标：该人员可在机器人单聊中直接发送“运行”、问题或控制指令，无需 @；其他人员不能启动、咨询或控制。个人通知只使用文本或 Markdown。
- 支持半自动步骤确认、暂停、继续和 30 秒自动放行；任务助手提出的控制操作需要二次确认，运行终态释放任务占用。断线不会中止工作流，事件游标与待发送消息保存在 MySQL，连接恢复后由 Outbox 继续补发。无模板的 Markdown 模式回复“暂停”“继续”或“立即进入下一步”，互动卡片模式使用按钮。

页面配置保存在 MySQL 中并立即应用。Client Secret 只写不回显，后续留空保存表示继续使用已保存密钥。首次尚未保存页面配置时，可用 `DINGTALK_*` 环境变量作为启动默认值。

定时运行由 8091 服务端每 30 秒按 `Asia/Shanghai` 检查一次，浏览器只负责保存配置。`daily` 模式使用 `HH:mm` 时间并每天运行一次；`interval` 模式允许 5–1440 分钟，启用或修改间隔后等待完整间隔再首次执行。服务停机期间错过的触发点不会补跑；重复扫描通过数据库中的日期或下一次间隔时间去重。

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
| `DINGTALK_ENABLED` | `false` | 页面尚未保存配置时的钉钉长连接开关默认值 |
| `DINGTALK_CLIENT_ID` | 空 | 页面尚未保存配置时的钉钉 Client ID 默认值 |
| `DINGTALK_CLIENT_SECRET` | 空 | 页面尚未保存配置时的钉钉 Client Secret 默认值 |
| `DINGTALK_CARD_TEMPLATE_ID` | 空 | 页面尚未保存配置时的可选互动卡片模板 ID；留空使用内置 Markdown 进度消息 |
| `DINGTALK_EVENT_POLL_INTERVAL_MS` | `1000` | 页面尚未保存配置时的钉钉轮询间隔默认值，范围 250–60000 毫秒 |

## REST 接口

飞书机器人功能已移除，页面、配置接口和 `FEISHU_*` 环境变量不再生效。旧飞书表与 Flyway 迁移历史保留，运行记录和冻结快照不删除；升级前的处理步骤见[部署指南](../../docs/DEPLOYMENT_GUIDE.zh-CN.md#飞书机器人退役)。

运行记录页面使用 `GET /api/task-definitions/{id}/runs?summary=true&page=0&size=20`，只加载当前页摘要，不查询或传输大快照。页码从 0 开始，每页允许 1–100 条，按提交时间和运行编号倒序排列。完整快照按需通过 `GET /api/task-runs/{workflowId}` 读取；缺省 `summary=false` 保留旧接口兼容行为。

Flyway V19 增加运行历史索引和运行时归属登记标记。历史归属统一由后台补齐；提交入口只查询该任务是否还有其他未登记历史，不读取历史快照，也不循环迁移。尚未登记完成时保留本次运行的原编号、提交快照和运行槽，并提示登记完成后自动继续提交；后台对账随后使用同一编号补交，不创建第二次运行。新运行随网关提交原子登记归属，成功后标记已登记，不再进入历史迁移。新提交和旧监控页返工都受到运行库原子互斥约束，旧快照保持不变。需要先升级 Python 网关及 MCP，再升级两个 Java 服务。

配置中心每 15 秒后台补齐未登记历史归属，每轮最多 20 个任务、每个任务 200 条运行；单个任务失败不影响本轮其他任务，失败批次下轮重试。旧运行登记完成前返工会被拒绝并提示稍后重试，避免升级窗口内重复运行。若使用升级前的 SQLite 备份恢复中央运行库，应同时恢复对应的 MySQL 备份或重新执行归属登记，不能仅恢复一端的旧状态。

定时领取与网关对账分开执行。网络提交使用 4 个后台线程和最多 16 个等待位置，领取数量不超过可用位置；定时扫描不会等待网关对账或网络提交。超过运行能力的触发仍遵守错过不补跑规则。机器人事件按工作流隔离，使用有界后台执行器轮转处理，每个工作流每轮最多 200 条相关事件；被过滤的底层事件只批量推进游标。Outbox 保持独立、顺序发送和持久化重试。

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
GET    /api/task-runs/{workflowId}
POST   /api/task-runs/{workflowId}/cancel
POST   /api/task-runs/{workflowId}/retry

GET    /api/agents
GET    /api/gateway/ready

GET    /api/dingtalk/config
PUT    /api/dingtalk/config
POST   /api/dingtalk/config/test

GET    /api/dingtalk/targets
GET    /api/dingtalk/targets/directory
POST   /api/dingtalk/targets/sync-people
PUT    /api/dingtalk/targets/{id}
DELETE /api/dingtalk/targets/{id}
POST   /api/dingtalk/targets/{id}/test
```

任务定义的定时字段为 `scheduleEnabled`、`scheduleMode`、`scheduleTime` 和
`scheduleIntervalMinutes`。`scheduleMode` 只能是 `daily` 或 `interval`；响应中的
`nextScheduleAt` 统一返回下一次计划时间，未启用或当前不可调度时为 `null`。

钉钉配置接口只服务于受保护的 8091 内网页面。GET 和 PUT 的响应只返回 `secretConfigured`，不会返回 Client Secret；测试接口也只返回成功状态和普通中文提示。人员同步由管理员手动触发，不设置定时任务；同步成功后同时更新部门树和人员归属，部门树只用于配置页面展示。

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

钉钉入口不改变上述网络边界：SDK 只建立主动出站长连接。页面保存的机器人密钥存放在 MySQL 中，但不会通过查询接口、页面或日志回显；数据库备份和数据库账号必须按密钥级别保护。只有已启用且绑定任务的人员或群聊可以触发相应任务；群成员共享本群任务的控制权限，应通过钉钉应用可见范围和群成员管理限制使用人群。
