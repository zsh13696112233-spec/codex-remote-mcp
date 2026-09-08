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
- 节点的 Skill/MCP 标签收纳在默认折叠的“高级设置”中，展开后可编辑；折叠不影响已有标签的保存。
- 节点实际执行目标由“执行机”决定，页面不再提供“执行位置（本机/远程）”选项；历史 `executorType` 字段继续保留以兼容已有配置和运行快照。
- 任务定义的新建、编辑、复制、搜索、软删除和重复运行。
- 任务定义可选择每天固定北京时间运行，或每隔 5–1440 分钟运行；错过时间不补跑，上一次仍未完成时跳过本次。同一任务定义同时只运行一个工作流，不同任务定义可以并行。
- 网页、定时和钉钉入口共用任务级运行槽；提交响应不明确时保留原 `workflowId` 和占用，由后台对账确认或幂等补交，不会贸然启动第二个工作流。
- 任务占用的申请、重跑恢复和终态释放统一由 `TaskLaunchStore` 在行锁内处理。钉钉目录只维护通知归属；两种关联不一致时保守保护现有运行，后台对账也检查仅剩钉钉关联的任务。旧运行终态只释放与自身编号匹配的关联，不覆盖新运行。同一事务内释放后可重新预约，任务占用与通知绑定一同提交或回滚；Python 运行时的执行互斥继续独立生效。
- 保存每次运行的不可变配置快照和完整提交 JSON。
- 使用最新配置运行、按原运行快照重试、取消运行和查看历史记录。
- 可选的全局钉钉长连接机器人，按完整任务定义名称启动，不需要绑定启动人员或群；所有能接触机器人的用户均可查询、咨询或提出控制操作。
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

本项目 macOS 使用 `mvnd`，Windows 使用 `mvn`。下方 PowerShell 示例用于 Windows；macOS 启动、测试、打包和格式化均使用 `mvnd`，路径使用 `/`。

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
5. 如需网页或定时运行主动通知，在任务定义中选择一个通知群或人员并勾选“网页或定时运行后推送钉钉”。多个任务可以使用同一通知对象。此设置不影响谁能从机器人启动任务；钉钉启动不向配置对象推送。
6. 如需 30 秒步骤确认，将 SOP 流转方式设为 `semi_automatic`。当前统一使用普通文本消息，不需要卡片模板或卡片权限。通过工作流编号或引用消息发送“暂停”“继续”或“立即进入下一步”。

Flyway V22 为消息顺序检查、待处理入站消息判断和活动绑定轮询增加索引，兼容已有记录。Outbox 按同一工作流、同一会话的发送序号依次领取；没有工作流编号的回复按会话排序。前序消息待发送、正在发送或等待重试时，后序消息不会越过；不同工作流和会话互不阻塞。发送成功后同轮继续领取后续消息，每轮累计最多 50 条。沿用现有重试策略：普通正文失败后等待重试，临时会话回复失败后放弃并允许后续消息继续；历史卡片仍保留过期更新跳过规则。

消息收尾只查询当前工作流是否还有待处理入站消息；轮询直接查询活动绑定及仍等待助手的终态绑定，不加载全部历史消息或已结束绑定。这不改变终态工作流的重新激活规则。

普通消息版通过 Flyway V21 保存启动会话回复地址，并给已有 Outbox 增加数据库生成的发送序号。钉钉启动的任务在启动会话接收过程和结果，不额外发送给配置的通知对象；任意会话提问的处理过程和答案只回复该次提问会话。

进度、工具开始/完成状态、模型提供的完整可读思考摘要、最终结果按事件顺序发送。没有摘要则不发送，不转发原始推理、工具参数和原始终端输出。长消息分段，每段携带工作流编号；任务持续过程与结果正文通过应用 OpenAPI 发送，不依赖临时会话回复地址。群聊关键状态另发简短 @ 提醒，临时地址失效时提醒可能失败，正文不受影响。任务助手仍按本次提问会话回复。沿用现有 Outbox，不新增恢复或重试框架；会话回复失败后由用户重试。普通公开交付链接保留，内部地址和带凭据的链接隐藏。

历史卡片发送记录、模板配置及按钮回调兼容保留；新运行和新过程消息不再创建或更新卡片。下面仅记录旧卡片协议，供历史兼容维护。

历史钉钉卡片模板变量约定：

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

- 群聊发送 `@机器人 完整任务定义名称` 启动；单聊不需要 @。重名时要求管理员调整，不按模糊匹配选择任务。启动不带图片。
- 同一任务定义同时最多运行一个工作流；多人、多群同时启动时只创建一个运行。正在运行时返回编号和“本次未启动”，不干扰已有运行。
- 后续发送 `工作流ID + 问题或需求`，或引用关联任务消息省略编号。显式编号与引用冲突时拒绝处理；没有名称、编号或有效引用时提示补充，不推测最近任务。
- 图文允许“编号、图片、下方配文”的排列，图片位置作为文字分隔保留；群聊回调若保留了编号前的 `@名称`，也可识别，包括名称与编号之间没有空格的情况。正文中的 Unicode 分隔符（包括 U+2005、全角空格和不换行空格）按普通空格处理，不从任意问题内容中猜测工作流编号。定位失败时仅记录长度、图片数、编号/引用存在性和分隔字符编码等格式诊断，不记录正文或下载信息。
- 回答发送到本次提问会话并 @ 提问人，不修改该任务的主动通知对象。可以从其他会话提问，无须原发起人或原群授权。
- 启用主动通知的网页和定时运行发送到配置对象；钉钉启动的进度、工具调用、可读思考摘要和结果持续回复启动会话，不额外发送给配置对象。
- 钉钉消息过滤主监督的等待步骤、查询步骤进度和查询任务进度工具调用（开始及完成均不发送），保留必要进度说明和其他操作。业务步骤的工具调用保留并显示步骤名称；任务助手不受此过滤影响。原始运行事件仍保留。
- 主监督启动、停止步骤仅显示中文操作及步骤名称，不发送调用参数或结果 JSON；“已启动步骤”表示派发完成，不代表业务步骤执行完成，停止操作显示“已提交停止请求”。业务步骤的同名工具仍展示详情。
- 工具详情按原内容展示，不做路径、命令、参数或文本结果脱敏：文件操作展示路径与操作类型，完成时附增删行数和差异节选；命令展示工作目录、退出码和输出节选；其他调用展示参数、结果和耗时（事件提供时）。单字段最多 1500 字符、单次工具通知最多 4000 字符（不含截断提示），文件最多 10 个，超限明确标注并沿用普通消息分段。已被网关裁剪的超大事件标注详情已截断；不还原丢失内容，不转发图片内容或钉钉图片传输字段。
- 停止、返工等操作由提议人携带编号或引用有效确认消息确认；网关同时校验调用者及具体操作，旧确认消息不能确认新的提议。
- 后续图文消息支持最多 5 张 PNG/JPEG/WebP 原图，合计不超过 20 MB。图片仅供问答时交给任务助手；确认按图返工后，原图交给重跑步骤及其后续步骤。只有图片时提示引用图片消息补充需求，不自动执行。
- 图片下载仅使用钉钉已认证下载接口返回的 `downloadUrl`，兼容 HTTP/HTTPS 并保持签名参数原样；缺少地址、格式错误和不支持的协议分别提示，不输出临时地址。
- 支持半自动步骤确认、暂停、继续和 30 秒自动放行；任务助手提出的控制操作需要二次确认，运行终态释放任务占用。带工作流编号或引用消息发送“暂停”“继续”或“立即进入下一步”。现有主动通知 Outbox 行为保留，不为新增会话过程消息扩展恢复或重试系统。

页面配置保存在 MySQL 中并立即应用。Client Secret 只写不回显，后续留空保存表示继续使用已保存密钥。首次尚未保存页面配置时，可用 `DINGTALK_*` 环境变量作为启动默认值。

定时运行由 8091 服务端每 30 秒按 `Asia/Shanghai` 检查一次，浏览器只负责保存配置。`daily` 模式使用 `HH:mm` 时间并每天运行一次；`interval` 模式允许 5–1440 分钟，启用或修改间隔后等待完整间隔再首次执行。服务停机期间错过的触发点不会补跑；重复扫描通过数据库中的日期或下一次间隔时间去重。

启动后访问：

```text
http://127.0.0.1:8091
```

## macOS 本机数据库配置

公共 `application.properties` 继续纳入版本管理。Mac 使用单独的
`application-mac.properties`，仅覆盖数据库连接配置，默认连接
`127.0.0.1:3306/codex_sop`，用户名默认为 `root`，密码必须通过
`MYSQL_PASSWORD` 环境变量提供，不写入配置文件。

从仓库根目录启动（macOS 的 zsh）：

```zsh
cd services/role-task-config-center
read -s 'MYSQL_PASSWORD?请输入 MySQL 密码: '
echo
export MYSQL_PASSWORD
mvnd spring-boot:run -Dspring-boot.run.profiles=mac
```

IDE 启动时将 Active profiles 设置为 `mac`，并配置 `MYSQL_PASSWORD` 环境变量。
打包后使用 `java -jar target/role-task-config-center-0.1.0.jar --spring.profiles.active=mac`，
同样需要先设置密码环境变量。

请先准备好对应的 MySQL 8 数据库；若使用前文示例创建的 `codex_config` 和 `codex`
账号，请设置 `MYSQL_URL` 和 `MYSQL_USERNAME` 覆盖默认值。
`MYSQL_URL` 环境变量优先于 Mac 配置中的默认地址；若终端已有远程数据库的
`MYSQL_URL`，需先执行 `unset MYSQL_URL`，才能使用默认的本机连接。
未启用 `mac` 时继续使用原有配置。

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
| `DINGTALK_CARD_TEMPLATE_ID` | 空 | 历史卡片兼容字段；新消息始终使用普通文本 |
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

钉钉入口不改变上述网络边界：SDK 只建立主动出站长连接。页面保存的机器人密钥及临时回复地址不通过页面或日志回显，数据库备份和账号应妥善保护。机器人不再按人员或群限制任务使用；钉钉应用可见范围决定谁能接触机器人。对话发送失败不增加补发机制；已有主动通知 Outbox 行为保留。升级见[统一入口升级说明](../../docs/DINGTALK_UNIFIED_ENTRY_UPGRADE.zh-CN.md)。
