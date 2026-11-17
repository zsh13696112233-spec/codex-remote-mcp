# 两个 Java 项目的代码目录与类职责

这份文档用于从业务功能定位代码、理解主要调用关系。配置中心负责配置与运行控制，监控中心负责单个工作流的展示和消息代理，实际工作流执行统一通过 Python 网关完成。

范围以 **2026-11-16 当前工作区** 为准，包含当时未提交的 Jira 缺任务提醒、文档继续沟通等代码，不等同于已部署版本。生产 Java 源文件逐项列出，内部记录类型随所属类说明；测试按功能归类，不逐个解释测试方法。类名均链接到源文件。

详细业务规则和接口契约仍以 [配置中心 README](../services/role-task-config-center/README.md)、[监控中心 README](../services/workflow-console/README.md) 及其引用文档为维护入口；本文维护代码导航，不复制完整字段与部署配置。

## 两个项目的分工

| 项目 | 配置中心 | 监控中心 |
| --- | --- | --- |
| 目录 | `services/role-task-config-center` | `services/workflow-console` |
| 默认端口 | 8091 | 8090 |
| 根包 | `com.codexflow.configcenter` | `com.codexflow.console` |
| 主要功能 | 角色、SOP、任务定义、定时、启动／取消／重试、机器、Jira、钉钉、安装管理 | URL 指定运行的进度、步骤结果、事件、讨论、文档与附件 |
| 数据存储 | 自己的 MySQL，使用 JPA 和 JDBC；Flyway 迁移 | 不连接配置中心 MySQL，数据通过网关查询 |
| 外部边界 | HTTP 调用 Python 网关；钉钉 SDK／API | HTTP 调用 Python 网关 |

```text
配置中心浏览器 → 8091 Web → 应用／领域服务 → MySQL
                                      └→ GatewayClient → 8080 Python 网关
钉钉平台 ⇄ Transport ⇄ Coordinator ─────┘

监控中心浏览器 → 8090 WorkflowController → GatewayClient → 8080 Python 网关
```

两个项目都使用 Java 17、Spring Boot 和原生 HTML/CSS/JavaScript。浏览器只请求所属 Java 服务的 `/api`；Java 不直接调用 MCP、Python 脚本或 app-server WebSocket。

## 公共目录读法

| 相对项目根目录的路径 | 用途 |
| --- | --- |
| `pom.xml` | Maven 依赖、编译、测试、格式检查和 Spring Boot 打包配置 |
| `src/main/java/` | 生产 Java 代码 |
| `src/main/resources/application.properties` | 服务配置入口；本地值可能不同，不应把实际凭据复制进文档 |
| `src/main/resources/static/` | 实际运行的网页与脚本，无 Node 构建步骤 |
| `src/test/java/` | Java 单元、Web 和数据库集成测试 |
| `src/test/js/` | 前端脚本回归测试，使用 Node 测试入口 |
| `src/test/resources/` | 测试配置及资源 |
| `target/` | Maven 构建产物，不是源码目录 |

配置中心另有 `src/main/resources/db/migration/` 的 SQL 迁移，以及 `src/main/java/db/migration/` 的 Java 迁移。没有实体类对应的功能也可能通过 `JdbcTemplate` 操作表，不能仅看 `Entity` 判断数据库覆盖范围。

## 配置中心的 Java 类

以下目录除特别注明外，均相对 `services/role-task-config-center/`。`web` 处理 HTTP，`application` 组织跨组件流程，`domain` 包含业务规则和持久化，`client` 负责网关通信，`integration/dingtalk` 负责钉钉集成。现有代码中部分控制器直接调用领域存储，应按实际调用关系阅读，而非假定所有请求都经过应用服务。

### 启动入口

目录：`src/main/java/com/codexflow/configcenter/`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [ConfigCenterApplication](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/ConfigCenterApplication.java) | Spring Boot 启动类；`main` 启动配置中心，启用调度并加载应用组件。 |

### Web 接口层

目录：`src/main/java/com/codexflow/configcenter/web`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [ApiController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/ApiController.java) | 角色、SOP、任务定义的 CRUD，以及启动、历史、详情、等待操作、取消、重试、机器列表和网关就绪入口；配置操作交给 `ConfigService`，运行操作交给 `WorkflowRunService`。 |
| [ApiExceptionHandler](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/ApiExceptionHandler.java) | 把领域不存在、冲突、参数校验及 `GatewayFailure` 映射为 HTTP 错误 JSON；是配置中心统一异常出口。 |
| [GroupController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/GroupController.java) | `/api/groups` 的查询、创建、重命名、删除与批量归组；调用 `GroupService` 和 `ConfigService.assignGroups`。 |
| [MachineController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/MachineController.java) | 机器登记、更新、删除、备注、模型查询、连接检测和重启确认；交给 `MachineService`。机器列表 GET 仍在 `ApiController`。 |
| [TaskScheduleController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/TaskScheduleController.java) | `/api/task-schedules` 的查询、创建、修改和删除；通过 `TaskScheduleStore` 保存独立定时规则。 |
| [RunCatalogController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/RunCatalogController.java) | `GET /api/task-runs` 的统一运行列表入口；将分组、状态等筛选交给 `RunCatalogService`。 |
| [JiraAccountController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/JiraAccountController.java) | 中央 Jira 账号的列表、保存、测试、选项读取和带版本删除；调用 `JiraAccountService`，不自行保存令牌。 |
| [JiraRunController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/JiraRunController.java) | Jira 请求状态、任务池、统计和运行详情入口；关联 `JiraPoolService` 与 `WorkflowRunService`。 |
| [JiraEmptyReminderController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/JiraEmptyReminderController.java) | `/api/jira-accounts/{id}/empty-reminder` 的读取与保存入口；调用 `JiraEmptyReminderService`。 |
| [SkillController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/SkillController.java) | Skill ZIP 上传、库存、机器目录检测、归组、下发及安装任务操作；校验请求格式并调用 `SkillService`。 |
| [McpController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/McpController.java) | MCP 安装包上传、库存、归组、下发及安装任务操作；校验请求格式并调用 `McpService`。 |
| [DingTalkConfigController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/DingTalkConfigController.java) | 机器人配置读取、保存和连接测试；调用 `DingTalkBotAdminService`。 |
| [DingTalkTargetController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/DingTalkTargetController.java) | 通知对象列表、组织树、人员同步、修改、删除和测试；调用 `DingTalkTargetAdminService`。 |
| [BlockedNotificationController](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/web/BlockedNotificationController.java) | 阻断通知模板配置、通知状态及测试发送入口；内嵌 `ConfigRequest`、`TestRequest` 为请求 DTO，调用 `BlockedNotificationService`。 |

### 应用编排层

目录：`src/main/java/com/codexflow/configcenter/application`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [WorkflowRunService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/WorkflowRunService.java) | 运行生命周期的主要入口。`runLatest`、`runScheduled`、`retry` 组织启动；`submitPrepared`、`submitReservation` 提交冻结载荷；`recoverSubmission`、`reconcileActiveRuns` 核对不确定提交和活动运行；还负责取消、等待动作、历史与运行时归属同步。依赖运行存储、启动预留、网关及主动通知服务。 |
| [RunCatalogService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/RunCatalogService.java) | 汇总运行列表；从 `RunCatalogStore` 取本地目录，结合网关状态及 `WorkflowRunStore` 更新展示所需信息。 |
| [MachineService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/MachineService.java) | 代理中央机器管理，在移动、删除和 SOP 校验前结合 `GroupService` 检查配置引用；实际机器数据通过 `GatewayClient` 管理。 |
| [SkillService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/SkillService.java) | 封装 Skill 网关路径和请求响应，提供 `list`、`upload`、`inventory`、`assign`、`checkDirectory`、`deployments`、`create`、`action`；Java 不直接执行安装命令。 |
| [McpService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/McpService.java) | 封装 MCP 安装包和部署网关请求；结构与 `SkillService` 类似，实际安装及验收在 Python 边界之后。 |
| [JiraPoolService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/JiraPoolService.java) | `enqueue` 将启动需求入库，定时处理请求并协调网关工单池、领取预留与工作流提交；`status`、`board`、`statistics` 提供请求、分发看板和统计查询。 |
| [DailyTaskScheduler](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/DailyTaskScheduler.java) | 北京时间每日及间隔任务调度。`tick` 领取到期规则并启动；Jira 走任务池，资源不足时延期；`reconcile` 核对活动运行，`synchronizeScopes` 同步历史运行归属。 |
| [JiraCollectionScheduler](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/JiraCollectionScheduler.java) | 周期调用 `JiraCollectionService.synchronize`，更新中央采集范围；独立于实际任务启动。 |
| [BlockedNotificationScheduler](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/BlockedNotificationScheduler.java) | 分别安排阻断通知的状态核对和投递，调用 `BlockedNotificationService.reconcile/deliver`。 |
| [JiraEmptyReminderScheduler](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/JiraEmptyReminderScheduler.java) | 周期调用 `JiraEmptyReminderService.reconcile` 检查缺任务提醒。 |
| [BoundedWork](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/application/BoundedWork.java) | 有界后台工作执行器。`submit(key, task)` 按键防止同一工作重入，限制线程和排队容量；`available` 供调度判断容量，`close` 释放执行器。 |

### 领域服务与存储

目录：`src/main/java/com/codexflow/configcenter/domain`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [ConfigService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/ConfigService.java) | 角色、SOP、任务定义的业务 CRUD 和批量归组。检查名称、引用、启停、同组关系和步骤配置；通过 Repository 持久化，通过 `DomainJsonMapper` 返回页面 JSON。 |
| [GroupService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/GroupService.java) | 跨配置中心与网关的分组协调：`catalog/save/delete` 管理分组入口；`lock` 使用数据库锁协调配置变更；`require/same` 校验归属，检查机器移动、删除及模型能力。 |
| [WorkflowRunStore](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/WorkflowRunStore.java) | 运行快照和状态的事务存储。`prepareLatest/prepareBound` 冻结最新配置，`prepareRetry` 复制历史提交，`getPrepared` 读取已保存载荷；内部组装工作流 payload、步骤依赖和业务提示词；维护历史、网关响应、结束摘要及归属同步标记。 |
| [TaskLaunchStore](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/TaskLaunchStore.java) | 统一启动预留与并发占用。`reserveLatest/reserveBound/reserveRetry` 创建运行预留，`reserveJiraBatch` 处理 Jira 批次，`activeLaunches/release` 核对及释放；数据库锁保护网页、定时与钉钉来源的竞争。 |
| [TaskScheduleStore](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/TaskScheduleStore.java) | 独立定时规则的 JDBC 存储与领取。`save/list/delete` 管理规则，`claimDispatches` 领取到期任务，`requireEnabled` 校验有效性，`deferResources/abandonDispatch` 处理延期和放弃；内嵌 `Dispatch` 传递任务、令牌和分组。 |
| [RunCatalogStore](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/RunCatalogStore.java) | 用 `EntityManager` 查询统一运行目录；`list` 完成筛选、分页和本地记录映射，不承担工作流执行。 |
| [JiraRequestStore](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/JiraRequestStore.java) | Jira 启动请求的 JDBC 持久化；`enqueue/pending/status` 管理请求，`validate/prepare/reservation/dispatched` 协调冻结条件、启动预留和派发结果，`scheduleOverview` 提供定时概览。 |
| [JiraAccountService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/JiraAccountService.java) | Java 侧账号管理与分组引用校验，`list/save/test/options/delete` 调用中央网关；结合本地配置检查引用关系。账号令牌保存在中央 SQLite 专用表，Java MySQL 保存业务引用。 |
| [JiraCollectionService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/JiraCollectionService.java) | `synchronize` 从启用且有效的任务定义和 SOP 读取 Jira 查询范围，持配置写锁提交 `/jira-polling`；不创建运行或占用执行机。 |
| [JiraReminderWindow](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/JiraReminderWindow.java) | 缺任务提醒的北京时间周时间窗计算；`validate` 校验星期与时段，`current` 处理跨天并合并相邻、重叠窗口；`Window` 保存起止时刻。 |
| [BlockedNotificationStore](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/BlockedNotificationStore.java) | 阻断通知配置、接收对象冻结及投递状态存储；`reserve/poll/prepare/claim/finish/retry` 形成通知状态链，`Notice` 为内部记录。 |
| [DingTalkTargetDirectory](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/DingTalkTargetDirectory.java) | 维护人员、群和部门目录；`syncPeople/syncDirectory` 同步，`discoverGroup` 发现群，`directory/list/update/delete` 服务管理页面；内嵌远程目录和展示记录类型。 |
| [DingTalkTaskBindingDirectory](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/DingTalkTaskBindingDirectory.java) | 在任务定义与钉钉会话之间选择启动路由，管理主动通知目标和活动任务关联；`reserveNamed/reserveStart`、`reserveProactive`、`active/reconcile/releaseFinished` 复用 `TaskLaunchStore` 的启动约束。 |
| [DomainJsonMapper](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/DomainJsonMapper.java) | 实体到页面 JSON 的集中映射：`role/sop/task/run`；负责快照 JSON 读写、监控链接和展示用下次计划时间。不是整个网关协议的唯一组装点，提交 payload 在 `WorkflowRunStore`。 |
| [PreparedRun](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/PreparedRun.java) | 不可变记录，携带 `workflowId` 和已冻结的 `payload`，连接存储准备阶段与 HTTP 提交阶段。 |
| [Timestamped](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/Timestamped.java) | JPA 映射基类，统一创建、更新时间及生命周期回调。 |
| [NotFoundFailure](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/NotFoundFailure.java) | 领域对象不存在的业务异常，由 Web 异常处理器转为 404。 |
| [ConflictFailure](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/ConflictFailure.java) | 当前配置或运行状态不允许操作的业务异常，由 Web 异常处理器转为 409。 |

### 领域实体与数据访问接口

目录：`src/main/java/com/codexflow/configcenter/domain`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [RoleEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/RoleEntity.java) | 角色实体，对应 `codex_sop_roles`；保存分组、名称、职责、启停、软删除和乐观锁版本。 |
| [RoleRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/RoleRepository.java) | 角色 JPA 查询接口；按分组与名称筛选有效角色，检查同组未删除角色重名。 |
| [SopEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/SopEntity.java) | SOP 聚合根，对应 `codex_sop_sops`；保存调度模式、模型、等待策略、Jira 查询引用和有序步骤集合。 |
| [SopRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/SopRepository.java) | SOP JPA 查询接口；按名称、分组、未删除条件检索。 |
| [SopStepEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/SopStepEntity.java) | 步骤实体，对应 `codex_sop_steps`；关联角色，保存顺序、业务要求、模型覆盖、权限、超时与 Skill/MCP 标签。 |
| [SopStepRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/SopStepRepository.java) | 步骤 JPA 接口；查询角色是否仍被有效 SOP 引用，支撑删除保护。 |
| [TaskDefinitionEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/TaskDefinitionEntity.java) | 可重复执行的任务定义，对应 `codex_sop_task_definitions`；保存目标、SOP、分组、通知目标、活动运行关联及历史定时兼容字段。 |
| [TaskDefinitionRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/TaskDefinitionRepository.java) | 任务定义检索、同名检查、锁定查询及运行／定时相关查询；供配置、启动预留和钉钉路由使用。 |
| [TaskRunEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/TaskRunEntity.java) | 一次运行的本地记录，对应 `codex_sop_task_runs`；保存冻结快照、提交 JSON、来源、状态、网关响应及结束信息。配置快照保持不变，状态与响应可更新。 |
| [TaskRunRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/TaskRunRepository.java) | 运行 JPA 接口；按任务检索历史、分页摘要、活动运行和待同步归属，内嵌 `Summary` 投影避免列表加载完整快照。 |
| [DingTalkTargetEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/DingTalkTargetEntity.java) | 人员或群通知目标，对应 `codex_sop_dingtalk_targets`；保存外部身份、展示名、启停、可用性和软删除状态，关联部门。 |
| [DingTalkTargetRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/DingTalkTargetRepository.java) | 通知目标查询和按 ID 加锁；支持目标同步、选择及引用校验。 |
| [DingTalkDepartmentEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/DingTalkDepartmentEntity.java) | 通讯录部门节点，对应 `codex_sop_dingtalk_departments`；保存父部门、外部编号、名称和同步状态。 |
| [DingTalkDepartmentRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/domain/DingTalkDepartmentRepository.java) | 按机器人应用查询部门、按外部编号匹配部门，供通讯录同步和组织树展示。 |

### 请求 DTO

目录：`src/main/java/com/codexflow/configcenter/dto`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [RoleSaveRequest](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/dto/RoleSaveRequest.java) | 角色保存请求：名称、职责、启停、版本和分组；Bean Validation 校验基础长度及必填项。 |
| [SopSaveRequest](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/dto/SopSaveRequest.java) | SOP 保存请求：模型、步骤、运行／等待设置、分组与 Jira 条件；历史构造器保留调用兼容。 |
| [SopStepRequest](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/dto/SopStepRequest.java) | 单步骤保存请求：角色、自定义要求、执行配置、模型／思考强度、权限、超时、标签等。 |
| [TaskDefinitionSaveRequest](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/dto/TaskDefinitionSaveRequest.java) | 任务定义保存请求：名称、目标、SOP、补充说明、通知与分组等；保留历史字段和构造器兼容。 |
| [TaskScheduleRequest](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/dto/TaskScheduleRequest.java) | 独立定时规则请求：名称、SOP／任务引用、每日或间隔模式、启停和版本。 |
| [JiraDispatchRequest](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/dto/JiraDispatchRequest.java) | Jira 查询配置：`jql`、中央账号 `connectionId` 与历史账号别名 `account`；不携带令牌。 |
| [DingTalkConfigSaveRequest](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/dto/DingTalkConfigSaveRequest.java) | 机器人配置保存／测试请求，承载应用配置、卡片模板和版本等输入；不作为页面回显对象。 |

### Python 网关客户端

目录：`src/main/java/com/codexflow/configcenter/client`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [GatewayClient](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/client/GatewayClient.java) | 配置中心统一 HTTP 客户端；`get/post/put/delete` 处理 JSON，`skillExchange` 支持安装包及部署响应，`uploadImage` 上传对话图片；封装连接、编码与错误转换。 |
| [GatewayFailure](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/client/GatewayFailure.java) | 网关访问异常，保留 HTTP 状态码和原因；由业务层进行已知失败判断，再由 Web 层映射响应。 |

### 钉钉接入与业务协调

目录：`src/main/java/com/codexflow/configcenter/integration/dingtalk`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [DingTalkBotCoordinator](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkBotCoordinator.java) | 机器人运行协调核心，实现启动／停止生命周期；接收消息与卡片回调、选择工作流、转发讨论、轮询事件和消费发件箱。`handleMessage`、`handleAction`、`pollBinding`、`deliver` 是主要内部路径，调用工作流服务、网关、存储和传输层。 |
| [DingTalkTransport](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkTransport.java) | 外部平台传输接口，定义长连接、消息／卡片投递、通讯录查询、图片下载等能力，便于测试替换。 |
| [OfficialDingTalkTransport](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/OfficialDingTalkTransport.java) | 官方 SDK 与 HTTP API 实现；建立 Stream 长连接，解析消息／引用／回调，管理访问令牌，发送和更新卡片，读取通讯录及图片，核对投递回执。 |
| [DingTalkStore](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkStore.java) | 钉钉事务状态核心；持久化工作流绑定、入站去重、事件游标、操作上下文和有序发件箱。`reserveStart/registerInbound/recordProcess/recordWaitingCard/claimDue/markOutboxSent` 连接接收、运行与投递阶段。 |
| [DingTalkModels](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkModels.java) | 内部数据记录集合，包括 `Message`、`CardAction`、`Binding`、`Inbound`、`Outbox`、`StartReservation`、`SendResult`；携带各层交换的数据及引用指纹。 |
| [DingTalkProperties](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkProperties.java) | Spring 钉钉配置属性和启用校验；提供数据库尚未配置时的启动默认值。 |
| [DingTalkSettingsStore](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkSettingsStore.java) | 读取、保存和应用机器人配置；`current/applyPersisted/save/forTest` 协调数据库设置与启动默认值，内嵌 `Settings`。 |
| [DingTalkBotAdminService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkBotAdminService.java) | 面向管理页的配置编排；`current/save/test` 返回受控视图，检查活动任务期间关键配置变更，必要时重建长连接。 |
| [DingTalkTargetAdminService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkTargetAdminService.java) | 面向管理页的通讯录／通知目标服务；调用传输层同步人员部门，通过目录存储更新目标并发送测试通知。 |
| [DingTalkProactiveNotificationService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkProactiveNotificationService.java) | 网页与定时任务的主动通知衔接；`validate/reserve/submitted/submissionFailed` 管理通知目标、会话预留及启动结果通知。 |
| [BlockedNotificationService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/BlockedNotificationService.java) | Jira 正式阻断后的开发人通知；`reconcile` 核对运行结果与接收人，`deliver` 领取发送，`config/configure/status/test` 服务配置页；依赖 `BlockedNotificationStore` 和传输层。 |
| [JiraEmptyReminderService](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/JiraEmptyReminderService.java) | 账号缺任务提醒的配置、时间窗判断与发送状态机；`get/save/reconcile` 结合中央采集状态、目标目录、机器人设置和 JDBC 状态记录决定提醒及恢复。 |
| [DingTalkDatabaseRetry](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkDatabaseRetry.java) | 对指定数据库并发冲突提供有限重试；`execute/run` 包裹事务调用，不是任意业务失败的通用重试器。 |

### 钉钉消息与卡片生成

目录：`src/main/java/com/codexflow/configcenter/integration/dingtalk`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [DingTalkProgressCard](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkProgressCard.java) | 将工作流快照转换为进度卡片、文本或 Markdown；`render/renderText/renderMarkdown` 组织当前步骤、进度、结果和业务状态。 |
| [DingTalkWaitingCard](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkWaitingCard.java) | 半自动等待卡片的数据生成；根据等待种类、可用操作和当前快照生成内容及按钮状态，供协调器投递／刷新。 |
| [DingTalkDocumentSummaryCard](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkDocumentSummaryCard.java) | 判断是否适用结束文档汇总并生成卡片；`applicable/render` 组织交付文档和查看链接，内嵌发送失败类型区分可重试情况。 |
| [DingTalkExecutionNotice](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkExecutionNotice.java) | 将执行事件转为业务进度和可选工具详情，含 `execution`、`stepLabel`、`jiraStart` 等。当前工具详情会包含命令、路径、参数和文本结果；图片字段过滤不能等同于完整脱敏。 |
| [DingTalkResultText](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkResultText.java) | 从结果文本中提取适合展示的结论、原因和进度摘要；`extract/progress/display/toolResult` 服务不同消息形态，识别归一化 Codex 失败提示。 |
| [DingTalkJiraContext](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkJiraContext.java) | 把 Jira 上下文整理成卡片中的编号、开发人等简短提示。 |
| [DingTalkMonitorLink](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkMonitorLink.java) | 生成指定工作流的监控页面 Markdown 入口，供消息与卡片复用。 |

### 钉钉实体与数据访问接口

目录：`src/main/java/com/codexflow/configcenter/integration/dingtalk`。

| 类或接口 | 职责与关键入口 |
| --- | --- |
| [DingTalkSettingsEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkSettingsEntity.java) | 机器人持久配置，对应 `codex_sop_dingtalk_bot_settings`；包含版本和历史兼容字段。 |
| [DingTalkSettingsRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkSettingsRepository.java) | 机器人配置的 JPA 数据访问接口。 |
| [DingTalkWorkflowBindingEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkWorkflowBindingEntity.java) | 工作流与会话的绑定，对应 `codex_sop_dingtalk_workflow_bindings`；保存触发来源、目标会话、事件游标、回复和卡片状态。 |
| [DingTalkWorkflowBindingRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkWorkflowBindingRepository.java) | 绑定锁定、触发消息／引用定位、待轮询运行与等待回答查询。 |
| [DingTalkInboundMessageEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkInboundMessageEntity.java) | 入站消息处理记录，对应 `codex_sop_dingtalk_inbound_messages`；关联平台消息与工作流消息，支撑去重及恢复。 |
| [DingTalkInboundMessageRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkInboundMessageRepository.java) | 入站消息查询，按工作流、消息编号和处理状态定位记录。 |
| [DingTalkOutboxEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkOutboxEntity.java) | 待发消息及投递记录，对应 `codex_sop_dingtalk_outbox`；保存去重键、投递顺序、类型、载荷、重试及等待卡片状态。 |
| [DingTalkOutboxRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkOutboxRepository.java) | 发件箱领取和锁定、前序阻塞检查、引用消息定位及最新卡片查询；保持同一会话运行的投递顺序。 |
| [DingTalkActionContextEntity](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkActionContextEntity.java) | 卡片操作上下文，对应 `codex_sop_dingtalk_action_contexts`；保存操作身份及关联信息，供后续结果通知使用。 |
| [DingTalkActionContextRepository](../services/role-task-config-center/src/main/java/com/codexflow/configcenter/integration/dingtalk/DingTalkActionContextRepository.java) | 卡片操作上下文的 JPA 数据访问接口。 |

### Java 数据库迁移类

目录为 `src/main/java/db/migration/`，不在业务根包下，由 Flyway 发现并执行。

| 类 | 职责 |
| --- | --- |
| [V26__backfill_run_catalog](../services/role-task-config-center/src/main/java/db/migration/V26__backfill_run_catalog.java) | 分批从历史冻结快照回填运行名称，从明确的钉钉绑定来源回填触发来源；无法确认的来源保留为未知。 |
| [V29__release_deleted_role_names](../services/role-task-config-center/src/main/java/db/migration/V29__release_deleted_role_names.java) | 调整历史角色名称唯一约束，让已删除角色释放名称，保留历史记录；兼容 MySQL 与 H2 旧约束形式。后续同组名称约束由 V45 继续演进。 |

## 配置中心的前端与数据库资源

### 前端文件

目录为 [src/main/resources/static](../services/role-task-config-center/src/main/resources/static)。这些文件是实际页面入口，`prototypes/` 不参与运行。

| 文件 | 主要功能与后端关联 |
| --- | --- |
| `index.html` | 单页管理入口，提供侧栏、各功能容器和弹窗，加载各业务脚本。 |
| `app.js` | 公共页面状态、导航、API 请求、角色管理、SOP 编辑、任务定义和运行操作；对应 `ApiController`，并组织其他页面模块。 |
| `groups.js` | 公共分组选择及分组管理、归组交互；对应 `GroupController`。 |
| `machines.js` | 机器列表、详情、检测、登记、备注、启停、删除及安装相关展示；对应 `MachineController` 和机器查询入口。 |
| `allocation.js` | 运行的机器分配详情，展示排队、实际机器和占用情况；从运行详情读取网关分配信息。 |
| `run-catalog.js` | 统一运行列表、筛选与分页；对应 `RunCatalogController`。 |
| `task-schedules.js` | 每日／间隔定时规则管理；对应 `TaskScheduleController`。 |
| `jira-accounts.js` | Jira 账号管理、连接测试及缺任务提醒设置；对应账号和提醒控制器。 |
| `jira-sop-options.js` | SOP 中 Jira 账号、项目和类型等选项联动；调用账号选项接口。 |
| `jira-runs.js` | Jira 任务池／分发中心与工单历次运行详情；对应 `JiraRunController`。 |
| `jira-statistics.js` | Jira 结果统计、条件筛选与明细；对应 Jira 统计接口。 |
| `blocked-notifications.js` | 开发人阻断通知模板及测试管理；对应 `BlockedNotificationController`。 |
| `skills.js` | Skill 包、库存与下发管理；对应 `SkillController`。 |
| `mcps.js` | MCP 包、库存与安装任务管理；对应 `McpController`。 |
| `styles.css`、`management.css`、`sync-wait.css` | 公共布局、管理页样式和加载等待层。 |
| `roles.css`、`sop-editor.css`、`machines.css` | 角色编辑、SOP 画布与机器页面样式。 |
| `groups.css`、`group-filter.css`、`run-catalog.css`、`skills.css` | 分组控件、运行目录和安装管理相关样式。 |

钉钉配置和通知对象的页面逻辑也位于公共页面代码中，并非每个后端控制器都有同名 JavaScript 文件。

### SQL 迁移目录

[src/main/resources/db/migration](../services/role-task-config-center/src/main/resources/db/migration) 保存按版本执行的数据库演进。阅读某项功能时，应同时看当前实体／SQL 查询及对应迁移，不修改已应用版本。

| 版本范围 | 主要主题 |
| --- | --- |
| V1–V6 | 基础角色、SOP、任务、运行结构，早期监督／重试／流转设置及历史飞书结构。历史迁移存在不代表仍有对应运行入口。 |
| V7–V16 | 钉钉接入、卡片与通知对象、部门目录、软删除、步骤权限和交接模式兼容。 |
| V17–V24 | 定时与间隔运行、运行归属、统一会话、文本进度顺序、投递索引与回执、等待卡片状态。 |
| V25–V32 | 运行目录与历史回填、独立定时规则、共享分组、角色名称复用、SOP 编辑／验收和长职责文本。 |
| V33–V42 | 钉钉详情开关、步骤文档、文档版本、提前结束、阻断通知、思考强度、等待策略、调度预检查、卡片上下文及字符集。 |
| V43–V51 | 分组调度、Jira 分发与任务池、同组角色名约束、中央账号、运行 SOP 上下文、默认类型及 Jira 请求字符集。 |
| V52–V53 | 当前工作区新增的 Jira 缺任务提醒，以及 Jira 请求规格去重迁移。 |

生产以 MySQL 8 为准，测试使用 H2 MySQL 模式。定时、Jira 请求和提醒等逻辑使用 JDBC，不应为了理解它们而寻找不存在的同名 JPA 实体。

## 监控中心的 Java 类

目录相对 `services/workflow-console/`。当前只有五个生产 Java 源文件，没有独立 `application`、`domain` 或 Repository 层。

| 类 | 职责与关键入口 |
| --- | --- |
| [WorkflowConsoleApplication](../services/workflow-console/src/main/java/com/codexflow/console/WorkflowConsoleApplication.java) | Spring Boot 启动入口，加载监控 API 和静态资源。 |
| [WorkflowController](../services/workflow-console/src/main/java/com/codexflow/console/web/WorkflowController.java) | `/api` HTTP 入口。`ready/status/events` 查询状态和事件，`artifact/inputImage/document` 读取文件、图片和交付文档；`sendMessage` 发送任务讨论，`confirmAdvance/holdAdvance` 处理等待；`documentMessages/documentMessage/documentWorkspace` 代理结束后的文档沟通、资料与版本。校验分页、版本参数和上传容量，设置文件响应头。 |
| [GatewayClient](../services/workflow-console/src/main/java/com/codexflow/console/client/GatewayClient.java) | 封装对应 Python 网关 HTTP 请求、路径编码、超时及错误转换；`exchange` 处理 JSON，`binary` 和 `documentWorkspace` 处理字节响应；内嵌 `BinaryResponse` 携带正文、类型和文件名。 |
| [GatewayException](../services/workflow-console/src/main/java/com/codexflow/console/client/GatewayException.java) | 保存上游状态及响应正文，或网络异常说明，供统一错误出口使用。 |
| [ApiExceptionHandler](../services/workflow-console/src/main/java/com/codexflow/console/web/ApiExceptionHandler.java) | 参数错误映射为 400，已知上游 4xx 映射为对应状态，其余转为 502。当前错误 JSON 包含 `upstreamStatus` 和 `upstreamBody`，不能把统一映射直接等同于已经隐藏所有内部信息。 |

监控中心不提供任务定义编辑、直接提交、取消、重试或跳过入口。当前代码已增加文档消息和资料代理，因而“只读监控”指运行控制职责的限制，不代表所有请求都是 GET：讨论和资料操作仍通过 Python 网关处理，不由 Java 直接操作执行机文件。

## 监控中心的前端资源

目录为 [src/main/resources/static](../services/workflow-console/src/main/resources/static)。

| 文件 | 主要功能 |
| --- | --- |
| `index.html` | URL 指定 `workflowId` 的监控页面；没有 ID 时不展示全局任务列表。 |
| `app.js` | 状态和事件轮询、增量游标、步骤与结果展示、任务聊天、等待确认／暂停、终态与刷新恢复；业务显示逻辑主要集中在此文件。 |
| `styles.css` | 监控主页面、步骤区、讨论和等待区域样式。 |
| `document.html` | 交付文档查看与继续沟通页面，以工作流和文档 ID 定位内容。 |
| `document.js` | 读取最新文档，渲染 Markdown，展示步骤、版本和同步状态，限制不安全内容与链接。 |
| `document-chat.js` | 文档继续沟通的消息加载、提交、增量刷新及修改结果展示，与资料区联动。 |
| `document-workspace.js` | 文档沟通资料区：已有 Jira 信息、附件上传／预览／下载／引用、版本列表和新版本提示；调用 `workspace` 代理。 |
| `document.css` | 文档正文、沟通界面、资料栏与预览布局。 |
| `vendor/marked-17.0.1.js` | 本地托管的 Markdown 解析库。 |
| `vendor/purify-3.3.3.min.js` | 本地托管的 HTML 清理库，为文档安全渲染提供支持。 |

## 主要业务调用链

### 保存角色和 SOP

`index.html/app.js → ApiController → ConfigService → Repository → MySQL`。

请求 DTO 做基础校验，`ConfigService` 处理引用、同组关系及业务限制，`DomainJsonMapper` 形成页面响应。机器和分组能力检查会经过 `GroupService`／网关。SOP 编辑器保存严格串行步骤；运行提交时由 `WorkflowRunStore` 生成明确的 `dependsOn`，Python 按依赖调度，不能只依赖 JSON 数组排列。

### 网页启动和历史重试

普通任务主链为 `ApiController → WorkflowRunService → TaskLaunchStore → WorkflowRunStore → GatewayClient → Python 网关`。

启动预留和冻结快照先持久化，再提交网关。`WorkflowRunService` 保存提交结果并在响应不确定时核对恢复；不能把 HTTP 超时直接当作未受理。使用最新配置启动会重新冻结配置，历史重试从原快照生成新的工作流 ID。主动通知经 `DingTalkProactiveNotificationService` 建立通知记录，后续由钉钉发件箱投递。Jira 任务启动还需经过请求池及工单领取。

### 定时运行

`TaskScheduleController → TaskScheduleStore` 保存规则；后台由 `DailyTaskScheduler → claimDispatches → WorkflowRunService` 执行。

调度器先按容量领取任务；普通任务走常规启动路径，Jira 任务进入 `JiraPoolService` 等待工单与资源。失败或容量不足时处理领取状态，数据库保存调度信息，不能只用进程内定时器保证去重。另有后台核对活动运行和同步历史归属的任务。

### Jira 采集与派发

采集范围：`JiraCollectionScheduler → JiraCollectionService → GatewayClient /jira-polling → Python`。

实际派发：启动入口 `→ JiraPoolService → JiraRequestStore → 网关工单池／预留 → WorkflowRunService → 工作流提交`。

两条链独立：配置中心从业务配置导出采集范围，Python 承担中央 Jira 访问及池状态；启动请求在本地持久化后等待可用工单与资源，不等同于每次采集都启动任务。缺任务提醒另由 `JiraEmptyReminderScheduler → JiraEmptyReminderService` 结合采集状态和 `JiraReminderWindow` 判断。

### 钉钉启动与讨论

`钉钉平台 → OfficialDingTalkTransport → DingTalkBotCoordinator → DingTalkStore/任务路由 → WorkflowRunService 或 GatewayClient`。

启动消息预留运行；讨论通过消息编号或引用定位工作流，登记入站消息后转发。协调器轮询网关事件，渲染为进度文本、等待卡片或文档汇总，再由 `DingTalkStore` 写入有序发件箱，传输层发送并保存回执。消息去重、游标和投递状态必须持久化，不能只看协调器内存中的工作队列。

### 监控与文档沟通

`index.html/app.js → WorkflowController → GatewayClient → Python 状态／事件／消息接口`。

`document.html/document*.js → WorkflowController 文档相关方法 → GatewayClient → Python 文档与沟通接口`。

Java 保留单工作流边界并代理请求；步骤执行者恢复、文档修改、版本和附件归属由 Python 服务处理。确认等待走专用按钮接口，聊天文本不直接推进或停止工作流。

### Skill 和 MCP 安装管理

`skills.js/mcps.js → SkillController/McpController → SkillService/McpService → GatewayClient → Python 安装管理`。

这是显式安装管理链路，与 `SopStepEntity` 中的 Skill/MCP 标签不同；步骤标签本身不会安装组件、注入提示词或授予权限。包传输、路径校验、执行机授权和安装验收需要继续看 [Skill 部署说明](SKILL_DEPLOYMENT.zh-CN.md) 与 [MCP 部署说明](MCP_DEPLOYMENT.zh-CN.md)。

## 测试代码导航

测试用于定位已有约束与回归场景；下面列举主要测试类和文件，不表示本文编写时重新执行过这些测试。

| 项目与测试目录 | 主要覆盖与入口 |
| --- | --- |
| 配置中心 `src/test/java/com/codexflow/configcenter/` | `ConfigCenterApplicationTest` 验证应用接口，`RoleNameReuseIntegrationTest`、`SopDeletionIntegrationTest` 验证角色名复用和 SOP 删除；`GroupedFixtureSupport` 为分组测试提供共享准备代码。 |
| 配置中心 `application/` | `WorkflowRunServiceTest`、`DailyTaskSchedulerTest` 验证运行和调度，`MachineServiceTest` 验证机器业务，`JiraPoolServiceTest` 验证工单请求池；运行列表、Skill、MCP、定时控制器另有对应测试。 |
| 配置中心 `domain/` 的配置测试 | `RoleGroupNameIntegrationTest`、`GroupConcurrencyTest`、`SharedGroupsIntegrationTest` 验证分组及并发；`SopInstructionTest`、`SopReasoningEffortTest`、`SopTimeoutIntegrationTest`、`SopWaitPolicyTest` 等验证步骤保存与运行载荷。 |
| 配置中心 `domain/` 的运行测试 | `WorkflowSubmissionRecoveryTest` 验证提交恢复，`TaskScheduleIntegrationTest` 验证调度，`JiraDispatchIntegrationTest`、`JiraAccountServiceTest` 验证 Jira 关联，`BlockedNotificationSnapshotTest` 验证冻结通知信息，`JiraReminderWindowTest` 验证提醒时间窗。 |
| 配置中心 `domain/` 的迁移测试 | `BranchMigrationCompatibilityTest`、`RunCatalogMigrationTest`、`TaskScheduleMigrationTest`、`JiraRequestCharsetMigrationTest`、`JiraEmptyReminderMigrationTest`、`JiraRequestSpecMigrationTest` 等验证旧数据迁移、约束和兼容。H2 验证不能代替真实 MySQL 验收。 |
| 配置中心 `integration/dingtalk/` | `DingTalkBotCoordinatorTest`、`DingTalkStoreIntegrationTest`、`DingTalkProactiveNotificationServiceTest` 验证协调和持久化；各 `*CardTest`、`*TransportTest` 验证渲染、请求和回执；`BlockedNotificationTest`、`JiraEmptyReminderTest` 验证专项通知；`DingTalkDatabaseRetryTest` 验证数据库重试边界。 |
| 配置中心 `src/test/js/` | `shared-groups`、`machine-grid`、`allocation`、`run-history`、`task-schedules`、`jira-accounts`、`jira-runs`、`jira-statistics`、`skills`、`mcps` 等 `.test.cjs` 覆盖页面行为；还有加载、导航弹窗、网关状态和 SOP 编辑专项测试。 |
| 监控中心 Java 测试 | `WorkflowConsoleApplicationTest` 验证路由和代理；`MonitorDetailsTest` 验证详情展示边界；`client/ArtifactResponseTest` 验证文件响应。 |
| 监控中心 `src/test/js/` | `consultation.test.cjs` 验证讨论，`refresh.test.cjs` 验证刷新恢复，`document-chat.test.cjs` 验证文档沟通交互。 |

测试与格式化命令统一见 [根 README](../README.md#测试与格式化)。Java 使用 `mvnd`；两个项目各自构建，没有根级聚合构建。仅查文档或调整说明无需启动 MySQL、Python 或执行机。

## 修改功能时从哪里开始

| 想了解或修改的功能 | 建议阅读顺序 |
| --- | --- |
| 角色、职责、SOP 步骤编辑 | `app.js → 请求 DTO → ConfigService → Entity/Repository → WorkflowRunStore` |
| 启动失败、重复启动、提交恢复 | `ApiController → WorkflowRunService → TaskLaunchStore/WorkflowRunStore → GatewayClient`，再核对 Python 运行状态 |
| 定时未触发或资源等待 | `TaskScheduleStore → DailyTaskScheduler → WorkflowRunService/JiraPoolService` |
| 机器登记、改组与删除 | `machines.js → MachineController → MachineService/GroupService → Python 机器管理` |
| Jira 没有任务、池状态或分发问题 | `JiraCollectionService → JiraPoolService/JiraRequestStore → Python Jira 接口`；提醒问题再看 `JiraEmptyReminderService` |
| 钉钉消息重复、丢失或顺序错误 | `DingTalkBotCoordinator → DingTalkStore → Outbox/Inbound/Binding Repository → OfficialDingTalkTransport` |
| 等待卡片按钮、结果或文档链接 | `DingTalkWaitingCard/DingTalkDocumentSummaryCard → Coordinator/Transport → 网关等待／文档接口` |
| 监控进度或聊天显示 | 监控中心 `app.js → WorkflowController → GatewayClient → Python 事件／消息接口` |
| 结束后的文档继续沟通 | `document-chat.js/document-workspace.js → WorkflowController → GatewayClient → Python 文档沟通模块` |

当前源码还存在两处容易误读的地方：监控中心 README 早期“接口数量”段落没有涵盖后来增加的文档相关代理；钉钉工具详情输出与监控页详情过滤也不是同一套实现。接口范围应结合实际路由阅读，安全要求与当前实现应分别核对，不据类名、注释或统一异常处理器推断已经完全脱敏。
