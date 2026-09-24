# Python 工作流服务

钉钉普通过程消息使用 `GET /workflows/{workflowId}/events/history?view=bot`：除步骤与终态事件外，返回 `appserver.item/started` 和 `appserver.item/completed`。主监督和步骤沿用现有事件上报，任务助手新增 `source: assistant`，并在 payload 中携带对应提问的 `messageId`。助手中间事件先刷新入库，再保存最终回答；模型结构化答案和原始推理增量不作为公开过程消息发送。带事件回调的执行请求使用 `turn/start` 的 `summary: auto` 请求可读摘要，模型未提供时不编造。Java 只提取工具状态、完整可读摘要和用户可见进度说明，普通监控页面的事件过滤保持兼容。

本模块同时提供 HTTP 工作流网关、Codex Orchestrator MCP 服务和 SQLite 状态存储，是整套平台的执行核心。

## 文件说明

钉钉图文对话新增 `POST /workflows/{workflowId}/input-images`（原始图片字节上传，返回 `imageId`）和 `GET /workflows/{workflowId}/input-images/{imageId}`（只读原图）。消息接口新增可选 `imageIds`、`actorId`、`expectedActionId`；原有纯文字调用兼容。调用者和确认操作由运行库事务校验。远程 Sidecar 通过带机器认证和当前租约的 `POST /internal/v1/workflows/{workflowId}/nodes/{nodeId}/input-images` 获取该步骤已确认的输入图。部署及容量限制见[钉钉图片升级说明](../../docs/DINGTALK_UNIFIED_ENTRY_UPGRADE.zh-CN.md)。

```text
python-workflow/
├── src/
│   ├── workflow_gateway.py        HTTP、SSE、主监督和独立任务助手会话
│   ├── codex_orchestrator_mcp.py  MCP 工具及 app-server 客户端
│   ├── workflow_event_batcher.py  本地与远程共用的异步事件批处理
│   ├── workflow_runtime_client.py 远程 Sidecar 到中央内部 API 的客户端
│   ├── workflow_sidecar.py        Streamable HTTP MCP 与权威心跳入口
│   └── workflow_store.py          SQLite 状态与事件存储
├── tests/                         Python 自动化测试
├── pyproject.toml
└── uv.lock
```

## 增量查询与任务互斥

- 配置中心提交携带可选 `taskDefinitionId`。中央运行库在同一写事务中校验同一任务只能有一个 `queued/running/cancelling` 运行；从监控页或机器人确认返工也遵守此约束，不依赖主监督是否相同。
- `POST /workflow-task-bindings` 接收 `taskDefinitionId` 和最多 200 个 `workflowIds`，只用于配置中心补齐历史归属。重复绑定幂等、归属不可改、缺失运行不创建；原始快照不变。`POST /workflow-statuses` 按最多 200 个 `workflowIds` 批量返回 `{statuses: {workflowId: status}}`，不加载结果和提示词。
- `GET /workflows/{workflowId}` 返回 `revision` 和各步骤 `resultRevision`。可携带 `knownRevision` 与 JSON 数组形式的 `knownResults`（按步骤顺序，最多 100 项）。无状态变化时返回 `{unchanged: true, revision, lastEventSequence}`；步骤结果版本相同时省略该步骤的 `response/error/artifacts` 并返回 `resultUnchanged: true`，调用方复用缓存。缺省参数仍返回完整状态。
- 历史事件支持 `view=all|monitor|bot`，缺省仍为完整审计事件。`after` 向后增量读取，`tail=true` 取最近一页，`before` 向前回看；不能混用方向。响应增加 `nextCursor/hasMore/oldestCursor/hasOlder`，事件始终按序号升序返回。增量调用必须使用 `nextCursor`，即使过滤后为空也可前进，且不会越过尚未交付的符合条件事件。
- 较大的新事件正文采用透明无损压缩；旧数据库兼容升级，旧正文仍可读取。SQLite 操作在网关后台线程执行，避免写锁等待阻塞消息和心跳处理。升级时网关与本机 MCP 必须同步更新，旧版本无法读取新的压缩正文。

本机 MCP、网关和远程 Sidecar 共用事件批处理器：每批最多 64 条且只属于一个工作流，按入队顺序提交；事件编号与时间在入队时固定，失败重试复用编号。取消等待会先等待已开始的写入结束，关闭时刷新剩余事件；远程认证和租约校验仍由内部 API 客户端负责。

历史事件维护见部署指南，默认只统计、不删除数据。事件、步骤尝试和附件的审计保留语义不变。

升级前缺少任务归属的运行，在配置中心后台登记完成前拒绝返工，防止两个旧监控页绕过互斥。配置中心每 15 秒处理一批未登记历史运行；提交入口不承担历史迁移，该任务尚有待登记历史时保留原编号和占用，登记完成后由后台对账补交。不经配置中心创建的旧独立工作流，需要维护人员通过归属登记接口为其登记独立任务编号后才能返工。新独立工作流可不传 `taskDefinitionId`，继续按原方式使用。

## 统一分组协议

分组目录返回 `mcpCount`，按 `mcp_package_groups` 中各组收录的 MCP 包计数，同一包在每个所属组各计一次；旧库尚未建立 MCP 表时返回 0。配置中心透传该统计，用于分组管理和 MCP 分组导航。

中央 SQLite 的机器分组为全系统唯一分组目录，配置中心角色、SOP、任务及运行引用相同编号。机器分组编号与已有登记保持不变。

`POST /workflows` 支持可选 `groupId`，提供时须为有效 UUID，且主监督和全部步骤执行机属于该组；新配置中心必传，旧独立调用省略时保持兼容。该字段保存在原始运行规格中。网关预检后，创建运行的 SQLite 写事务再次校验分组，机器改组也在写事务中拒绝活动运行引用，避免并发改组绕过检查。

`POST /agents/validate` 同样接受可选 `groupId`，用于 SOP 保存的同组验证。配置中心负责业务引用保护和跨实例分组写入互斥，不把角色或任务数据复制到中央 SQLite；浏览器仍通过配置中心访问。先升级中央网关再升级配置中心，详见[统一分组升级说明](../../docs/SHARED_GROUPS_UPGRADE.zh-CN.md)。

## Skill 压缩包下发

中央网关新增 `/skills`、`/skills/machines`、`/skill-deployments` 和 `/skill-deployment-tasks/{id}/retry|check` 管理接口，经配置中心代理使用。ZIP 与解包文件保存到专用目录，包清单、逐机任务、安装归属及进度保存在同一中央 SQLite。使用独立执行服务文件连接，不经过业务模型会话，纯执行机无需 Sidecar。

默认关闭，需配置 `skill_deployment.package_root`，再在机器管理网页保存逐机安装目录和授权（中央 SQLite 持久化、立即生效）；同时尊重机器 `allow_write`。只支持首次安装，不覆盖同名不同内容，不执行安装脚本、不自动安装依赖或启用能力。根 `SKILL.md` 最后发布，回读校验及 `skills/list` 刷新通过后才报告已安装。配置、容量、接口状态和重启恢复边界见 [Skill 下发部署说明](../../docs/SKILL_DEPLOYMENT.zh-CN.md)。


Skill 包通过中央 `skill_group_packages` 关联表加入一个或多个现有机器分组；上传及新下发必须提供 `groupId`，查询可按组筛选。下发在写事务内校验组、包收录和全部机器归属，新批次冻结分组编号及名称。机器有待处理 Skill 操作时禁止改组；存在收录或新批次历史的组禁止删除。安装汇总按机器当前组展示完整平台记录，不扫描人工安装内容。旧包手动归组，旧批次不推测归属；迁移、旧队列恢复和接口细节见 [Skill 下发部署说明](../../docs/SKILL_DEPLOYMENT.zh-CN.md#统一分组升级)。

## 执行机配置

带图咨询所用的主监督机器以及接收图片的步骤执行机需要配置 `machine_defaults.artifact_root` 和 `allow_write: true`。已有机器缺少图片目录时，在中央服务配置中补齐目标执行机的绝对目录，待无活动工作流后升级并重启网关，再在机器管理中编辑保存对应机器；保存同步目录并保留机器编号和 SOP 引用，远程通过同组清单获取。省略目录配置保留已有值，目录不能配置为空字符串。任务助手自身仍以只读沙箱执行。

机器只能通过 8091 网页登记，中央 SQLite 为唯一机器清单来源。没有来源开关、旧机器导入、独立远程清单或机器配置环境变量回退。统一目录、协议、模型和凭据引用保存在 `config/workflow-service.json`；示例、字段约定、本机与远程执行方式见[机器管理部署说明](../../docs/WEBUI_MACHINE_REGISTRATION.zh-CN.md)。

本机编排进程与网关读取同一配置和同一 SQLite；远程 Sidecar 通过中央认证接口按需获取同组机器。SOP 保存时校验登记、能力和分组；提交及中央步骤派发时额外校验启用和手动检测资格。登记的 cwd 是默认目录：节点留空时继承，填写绝对目录时覆盖，已有登记同样生效。本机读取和远程清单下发使用相同规则；绝对路径、写权限和完全访问权限仍在执行边界校验。

统一部署配置支持 `machine_defaults.allow_write` 和 `machine_defaults.allow_full_access`，均默认 false；完全访问必须同时允许写入。权限上限在登记或网页保存机器时持久化，并通过同组清单下发；修改配置后重启网关，已登记机器需编辑保存应用。节点必须另行选择 full_access，配置上限不会自动提升所有节点权限。

凭据支持 `token_file`（绝对路径）或 `token_env`（环境变量名称），严格二选一。这里的环境变量仅用于解析凭据，不切换机器来源。令牌文件为 UTF-8 单行、最多 8 KiB，每次连接重新读取；实际值不写入登记字段或网页响应。

`GET /agents` 返回登记 ID、IP、端口、分组、检测记录、默认目录/模型、启停、能力、主监督容量和权限上限。对于具备主监督能力的执行机，还返回 `connectionStatus`（`online`、`offline`、`unknown`）、基于持久租约计算的 `availability`（`idle`、`busy`）、`checkedAt` 和 `lastOnlineAt`；不返回令牌或原始连接异常。`local_db` 每 10 秒执行轻量 WebSocket 探测；`remote_sidecar` 使用 SQLite 中的权威心跳，5 秒上报一次，20 秒未续租即离线。`POST /workflows` 在写入 SQLite 前校验主监督和每个步骤执行机是否存在、启用、能力匹配，并继续执行权限档位校验；不要求各步骤使用相同执行机或工作目录。

提交成功的工作流先进入 `queued`。调度器按主监督分别以 `created_at + workflow_id` 领取最早任务；同一主监督固定只运行一个工作流，不同主监督可以并行。远程租约包含不可预测令牌、Sidecar 实例 ID、续租时间和过期时间；写接口在同一个 SQLite 写事务中重新校验租约，旧实例和旧令牌不能回写。完成、失败和取消时与终态在同一事务释放，半自动暂停继续占用。远程主监督离线时新工作流立即失败；运行中失联、实例更换或心跳超时会失败并释放租约，不自动迁移。网关重启仍把遗留的 `running/cancelling` 工作流直接标记失败、清空租约并继续排队任务，不重新附着旧外部会话。

步骤发布的任意格式文件会作为工作流附件写入共享数据库；图片生成事件也会在事件截断前被合并。单文件上限为 20 MB，每个工作流的当前与历史文件合计最多 50 个，同一 SHA-256 内容自动去重。网关继续通过 `GET /workflows/{workflowId}/artifacts/{artifactId}` 只读返回附件，不提供任意文件路径读取能力。非图片响应强制附件下载并设置 `nosniff`。

网关启动时还会回填步骤结果中仍然存在、且位于 `$CODEX_HOME/generated_images` 受信目录内的历史图片链接；目录外路径不会读取。

## 步骤流转模式

工作流提交可携带 `advanceMode`：

- `automatic`：默认值，成功步骤完成后立即派发下一步骤。
- `semi_automatic`：仅支持严格串行工作流。成功步骤完成且仍有下一步骤时，SQLite 中创建两分钟持久化等待；调用 `POST /workflows/{workflowId}/advance/{gateId}/confirm` 可立即放行，调用 `POST /workflows/{workflowId}/advance/{gateId}/hold` 可持久化暂停并取消自动放行。暂停后再次调用确认接口即可继续；未暂停且未确认时由运行时到期自动放行。

状态接口同时返回 `advanceMode` 和 `pendingAdvance`；后者通过 `state` 区分 `countdown` 和 `held`，并在暂停时返回 `heldAt`。暂停期间工作流仍为 `running`，暂停时间不计入主监督最长运行时间。最后一步、失败步骤和跳过步骤不创建等待；取消、重跑和其他使等待失效的状态变化会关闭旧等待，防止过期按钮影响新一轮执行。

全自动、半自动均禁止在业务步骤为排队派发、执行中或停止中时执行用户停止、跳过、返工操作；咨询不受影响。控制在提议、确认及执行时校验，派发与已确认控制互斥。执行期间收到的控制请求不能在稍后的等待中自动执行，需用户重新提出。任务失败、完成后的原有重试、返工保持不变，系统超时和故障清理不受此限制。

半自动等待期间，新任务消息在入站事务中先保持等待，再交给助手；回复“确认继续”（兼容“继续”）直接放行，不需要模型判断。重复消息绑定首次接收时的等待和控制许可，不影响后续新等待。保持等待仍占用运行名额；沿用网关重启将遗留运行标记失败的策略，不增加恢复机制。

配置中心在下载图片前调用 `POST /workflows/{workflowId}/input-observations`，请求为 `{"messageId":"有效 UUID"}`，返回首次入站对应的 `gateId`（可为空）和 `controlAllowed`；同一编号与后续 `/messages` 复用。该服务端入口不增加监控中心路由。

钉钉明确确认继续时，该接口传 `hold: false`，只记录消息首次对应的等待，不保持等待、不暂停主监督，再通过原确认接口放行。省略 `hold` 默认仍为 `true`，提问、图片和暂停请求保留先保持的行为；重复消息仍绑定首次等待。升级时先更新 Python 网关，再更新配置中心。

钉钉通知成功后调用 `POST /workflows/{workflowId}/advance/{gateId}/notified`，请求为 `{"sentAt":"带时区的发送成功时间"}`。仅尚未过期的倒计时等待接受首次回执，将 `expiresAt` 调整为发送时间后120秒，并返回 `updated: true`；重复、迟到、已保持或已关闭等待返回 `updated: false`。`pendingAdvance` 新增可空 `notifiedAt`。通知失败、回执未能在原截止前送达或没有钉钉通知时，仍按等待创建后120秒放行。旧等待保留原截止时间。

## 步骤记录查询与独立咨询

任务助手支持在现有聊天入口查询任意步骤及历史执行版本，并对已结束步骤发起独立只读咨询。助手通过结构化 `tool_request` 请求平台内部的 `list_step_attempts`、`read_step_records`、`consult_step`，由网关绑定当前任务并校验参数；不增加浏览器调用执行服务的接口。执行版本内部从 0 开始，对用户显示为第 1 次、第 2 次执行。未指定版本时选择当前版本，开始查询后固定；运行中的目标步骤只查已产生记录，不预约咨询。

记录优先读取原执行轮次，连接不可用时使用中央已有事件、指令和结果，并明确标注不完整。分页冻结一次查询的来源与顺序，不能把后续返工记录混入旧版本。每个查询最多保留 200,000 字符，单条显示最多 8,000 字符，超限标注；单次工具结果最多 20,000、累计证据最多 40,000、最终提示词最多 100,000 字符。每条消息最多 8 次工具调用、总时限 600 秒，超限不无限重试。

咨询在原执行机上优先通过 `thread/fork` 和 `lastTurnId` 继承截至目标轮次的上下文；不支持时使用选取记录建立新会话，并声明上下文有限。后续按任务、步骤、执行版本和提问人复用咨询会话；网页未提供提问人时使用该任务的网页咨询上下文。咨询不恢复原业务会话、不发布产物、不更新步骤结果或消耗重跑额度。

咨询固定只读、禁止审批、关闭网络及 MCP/应用/插件，检查返回的沙箱及工具清单；执行服务无法确认隔离时拒绝启动。咨询只查看代码、测试文本及已有日志，不执行构建测试。原始图片不会转交咨询会话，回答不能声称已查看参考图片。核查当前文件不能还原历史代码；尤其后续步骤可能已修改同一工作目录。

SQLite 自动兼容新增运行目录/模型快照、咨询会话、工具调用和查询分页快照。重复消息复用已保存结果；未知启动结果不会重发，已知咨询轮次先查询终态，不能确认时保持该咨询会话不可继续并返回明确限制。后续提问会再次尝试核对已知轮次。远程模式仍由中央保存状态，中央直接使用已登记的管理连接访问执行服务，不增加 Sidecar 数据库。升级及验收见[咨询功能部署说明](../../docs/ASSISTANT_NODE_CONSULTATION.zh-CN.md)。

## 返工要求

返工保留所选步骤的原会话编号，派发时使用 app-server `thread/resume` 后开启新一轮执行；后续重跑步骤仍新建会话。旧尝试照常归档，新一轮提示要求基于上一版产物修改，保留未要求改变的内容，并允许本轮交付一个版本。目标步骤从未建立会话时才正常新建；恢复失败按执行失败处理，不静默重新设计。中央及远程执行机均需更新 Python 代码。

有效待确认操作及已确认、执行中的控制操作会在中央确认事务中阻止普通“继续执行”，包括旧卡片和其他确认入口；取消提议不放行原等待，之后仍需明确继续。

任务助手识别到 `restart_from` 时，会把用户说明的问题和修改点总结为可执行的 `revisionInstruction`，并在用户发送“确认执行”前展示。确认后的总结与来源消息、目标步骤和重跑序号一起持久化，只追加到目标步骤的实际提示词末尾，不修改原始提示词或不可变运行快照。多轮要求按时间累积，最新要求优先；返工上下文最多 20,000 字符，超限时省略最旧内容，最终提示词仍不超过 100,000 字符。没有新增修改要求的普通重跑不会生成空返工段落。

## 单次产物约束

运行时会在每个步骤的实际执行提示词末尾追加隐藏约束：每次步骤尝试只允许生成或修改一个面向用户交付的产物版本。首次产物完成后可以只读检查并报告问题，但不得自行重绘、重写、修正、覆盖或生成备选版本；产物交由人工审核。用户确认返工并开始新的步骤尝试后，才重新获得一次生成机会。

## 文件交接模式

- `handoffMode: "legacy_text"`：保留历史行为，把直接依赖步骤的文字结果追加到下一步。字段缺失时使用此模式。
- `handoffMode: "cumulative_files"`：不传递任何前序文字结果，返工要求也只属于目标步骤。第 N 步获得第 1 至 N-1 步的全部当前有效文件。

当前文件流水线要求编排器与 app-server 位于同一台机器，并在统一部署配置中设置本机绝对路径 `machine_defaults.artifact_root`。编排器直接在该根目录内为每次尝试创建 `inputs/step-N/` 和空 `output/`，提示词只交付绝对路径，不使用 Base64 传输，也不扫描业务工作区。所有步骤都允许只返回文字；任务本身需要发布文件时最多发布一个。步骤是否完成只取决于节点执行结果，不因没有附件而失败，后续步骤自行检查所需业务文件。`write` 只表示是否允许写入，不代表必须生成附件。`allow_write` 是执行机的工作区写入上限；`allow_full_access` 是独立的完全访问上限，只有两者都为 `true` 时才开放 `full_access`。前三档文件交接继续只开放受控写入根目录并关闭网络；`full_access` 会取消文件系统和网络隔离，但仍只从托管输出目录收集最多一个交付文件。前序文件仅作为可用输入；当前要求未明确要求使用时，Agent 不得打开或合并它们。阶段 B 的远程主监督只支持 `legacy_text`；提交 `cumulative_files` 会在持久化前拒绝，跨机器附件传输仍留到后续阶段。

节点权限映射遵循 OpenAI 的 [Sandboxing](https://learn.chatgpt.com/docs/sandboxing) 与 [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security) 语义：`read_only = read-only + never`，`workspace_write = workspace-write + never`，`auto_review = workspace-write + on-request + auto_review`，`full_access = danger-full-access + never`。启动节点前会读取 `configRequirements/read`；执行机管理策略明确不允许时不会启动 thread。旧 app-server 不支持该方法时保持兼容。

该约束不修改配置中心保存的原始提示词或不可变运行快照，也不在监控页面展示。它当前属于提示词约束，不在运行时拦截第二次工具调用。

## 启动网关

已登记机器修改执行服务凭据引用时，先修改中央 `config/workflow-service.json` 并重启网关，再在机器管理中“编辑 → 保存”。保存同步 `machine_defaults.token_env` / `token_file`，移除旧来源；引用变化时清除检测结果，需要重新检测，机器编号和 SOP 引用保留。Sidecar 身份凭据不随此操作更新；部署步骤见[机器管理部署说明](../../docs/WEBUI_MACHINE_REGISTRATION.zh-CN.md)。

机器“在线”与“检测通过”独立：远程主监督在线表示心跳有效，手动检测还会连接执行服务并完成认证和初始化。检测接口保持返回 `passed` 和 `message`；凭据文件不存在、不可读、为空、格式错误，以及连接超时或心跳缺失会返回具体中文提示。页面和网关日志不输出实际凭据、文件路径或原始异常内容。修复部署问题后须重新点击检测，心跳恢复不会自动改写检测结果。

先按部署说明准备 `config/workflow-service.json`，在仓库根目录执行：

```powershell
.\scripts\start_workflow_gateway.ps1
```

也可使用已有 Python 环境运行 `src/workflow_gateway.py --host 0.0.0.0 --port 8080`。数据库路径由服务配置提供；本机编排进程运行 `src/codex_orchestrator_mcp.py`，必须使用同一配置文件和同一运行库。不再接受 `--agents`。

网关默认监听所有网络接口以支持可信内网中的 Java 服务访问，但没有内置用户认证。必须通过主机防火墙限制 `8080` 的访问来源，不得直接暴露到公网；只需本机访问时可显式传入 `--host 127.0.0.1`。

## 启动远程 Sidecar

远程主监督机只运行执行服务与 Sidecar，不运行完整网关，不填写 SQLite 路径。将 `config/workflow-sidecar.example.json` 复制为本机仓库的 `config/workflow-service.json`，填写中央地址和凭据文件路径后启动：

```powershell
.\scripts\start_workflow_sidecar.ps1
```

机器身份由中央根据独立凭据识别；未登记时心跳会重试。登记后从中央按需获取同组清单，不需要复制机器编号或执行机文件。

Codex app-server 使用 Streamable HTTP MCP：

```toml
[mcp_servers.codex_orchestrator]
url = "http://127.0.0.1:8082/mcp"
required = true
enabled_tools = ["dispatch_node", "wait_node", "node_status", "cancel_node", "workflow_status"]
default_tools_approval_mode = "approve"
```

仅预批准上述主监督编排工具，可以避免 `dispatch_node` 和 `wait_node` 逐次进入 Auto-review。主监督仍使用只读沙箱，业务步骤继续使用各自选择的权限档位。

Sidecar 启动时先确认 `8082` 已监听，再向中央登记上线，之后每 5 秒心跳。中央 `/internal/v1` 提供心跳、工作流/步骤上下文、原子准备派发、步骤状态同步和最多 64 项的事件批量上报。Bearer Token 唯一映射到一个启用的 `remote_sidecar` 主监督；所有写操作还必须携带 `X-Workflow-Lease`。认证失败、越权、未找到和租约冲突分别返回 `401`、`403`、`404`、`409`。事件使用工作流内幂等键，网络重试不会重复写入。

机器令牌可以通过 `sidecar.token_env` 间接引用环境变量，也可以用 `sidecar.token_file` 指向绝对文件。两者严格二选一；文件必须是 UTF-8 单行且不超过 8 KiB。Sidecar 每次请求重新读取令牌以支持轮换。机器令牌不能复用 app-server 的连接令牌，响应不会回传令牌；机器管理接口展示人工登记的 IP 和端口，监控页面仍隐藏机器连接信息。

`8082` 只能绑定回环地址，不需要也不应开放防火墙。中央 `8080` 只允许 `8090`、`8091` 和已登记主监督机访问；跨机器链路应位于可信内网、VPN 或 TLS 反向代理之后。

## 测试

从仓库根目录执行，统一命令见[根 README 的测试与格式化](../../README.md#测试与格式化)，包含 Windows、macOS/Linux、现有 `.venv` 优先规则、`uv` 回退和长任务 MCP 子进程验证。

## MCP ZIP 下发

安装结果的 `skillPath` 应为完整 `SKILL.md` 路径；兼容助手只返回本次授权的包级 Skill 根目录时补上 `SKILL.md`，随后仍验证文件存在、非链接及 Skill 识别，不扩大授权目录。已安装文件但因该路径错误失败的旧任务，升级网关后使用“重新检测”读取原安装结果并继续验收；不要先点“重试”，以免清除尚未验收的安装会话后重新安装。

同一入口支持 MCP + Skill 与 CLI + Skill，保留无 Skill 的 MCP 兼容模式。CLI 不注册 MCP，由平台检查正式入口的 `--help` 和 Skill 识别；此固定帮助检查要求机器已授权写入及完全访问，安装助手仍限制目录。持久化结果增加 `kind`，旧结果默认 MCP，旧 unsupported 重试重新分析包。详细验收、权限和升级步骤见 [权限与验收](../../docs/MCP_DEPLOYMENT.zh-CN.md#权限与验收)。

新增独立安装会话、中央持久任务与业务互斥，通过配置中心管理。默认关闭，安装使用限定目录写入和关闭网络策略；完整配置、接口及真实协议验收限制见 [MCP 下发说明](../../docs/MCP_DEPLOYMENT.zh-CN.md)。

机器安装配置支持统一 `installRoot`，临时和程序子目录仍独立授权；Python/Node 路径由安装助手发现，平台检查启动文件及脚本入口。已有双目录配置和冻结任务保持兼容，不自动迁移。

MCP / CLI 原 ZIP 和解压文件超过 8 MiB 时自动按 4 MiB 分块下发，平台固定 PowerShell 代码在执行机合并并校验 SHA-256，不整文件回传。大文件要求机器已登记允许写入及完全访问；传输阶段的完全访问只用于合并代码，安装助手权限不变。分块归属、断连恢复、缓存保留及升级步骤见 [MCP 下发说明](../../docs/MCP_DEPLOYMENT.zh-CN.md#大文件分块传输)。

MCP 逐机任务持久化 `diagnostics` 检测摘要，由安装汇总和批次查询返回；含时间、阶段、是否发现服务、连接状态、工具数量及安全分类原因，不存储原始执行服务错误。数据库自动兼容增加此字段，旧记录保持为空直到重新检测。

## 原会话串行验收

业务步骤可携带可选 `acceptance: {name, criteria, maxRepairs}`，名称 1–200 字符，条件 1–10000 字符，自动修复次数 0–10（默认 2）。含验收的运行只接受严格串行依赖。首次执行结束后在原执行机、原会话中检查；结构化结果为 `passed`、`failed`、`needs_input`，附 `reason` 与 `issues`。验收通过前不将业务步骤标记完成，后续依赖不能启动，判断结果不覆盖业务产出。

运行状态在步骤的 `acceptance` 中展示阶段、修复次数、原因及历史。自动修复用独立额度；超过上限、格式无效或执行异常进入持久化等待，步骤状态为 `acceptance_held`，工作流继续占用任务。半自动两分钟等待仅在验收通过后创建。连接中断或网关重启时，已进入验收的步骤保持暂停；续接前读取原会话，不能确认已结束时不启动新轮次。

用户通过任务助手补充要求并独立确认 `repair_acceptance`，追加一次修复和复检，消耗一次共享重跑额度；未通过再次暂停，不允许直接放行或跳过。验收修复固定针对紧邻前一步，不支持跨步骤自动回退。中央 SQLite 新增验收状态表，Sidecar 经带租约的内部 `/internal/v1/workflows/{workflow_id}/nodes/{node_id}/acceptance` 操作状态。旧快照不带验收配置时保持旧行为。
