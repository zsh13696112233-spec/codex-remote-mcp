# Python 工作流服务

钉钉普通过程消息使用 `GET /workflows/{workflowId}/events/history?view=bot`：除步骤与终态事件外，返回 `appserver.item/started` 和 `appserver.item/completed`。主监督和步骤沿用现有事件上报，任务助手新增 `source: assistant`，并在 payload 中携带对应提问的 `messageId`。助手中间事件先刷新入库，再保存最终回答；模型结构化答案和原始推理增量不作为公开过程消息发送。带事件回调的执行请求使用 `turn/start` 的 `summary: auto` 请求可读摘要，模型未提供时不编造。Java 按 SOP 详情开关展示工具状态、可读摘要和执行者／讨论过程说明，监督机自由进度文字始终隐藏。监控页面默认使用生命周期事件展示阶段变化；独立详情视图 `monitor_details` 在服务端投影公开摘要、执行说明和工具状态／成功文本结果，隐藏监督机自由播报及内部工具操作，不改变 `bot` 视图。详情展示和过滤边界见[监控中心 README](../workflow-console/README.md)。

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

- 配置中心提交携带可选 `taskDefinitionId`。中央运行库在同一写事务中校验同一任务只能有一个活动运行，不依赖主监督是否相同。
- `POST /workflow-task-bindings` 接收 `taskDefinitionId` 和最多 200 个 `workflowIds`，只用于配置中心补齐历史归属。重复绑定幂等、归属不可改、缺失运行不创建；原始快照不变。`POST /workflow-statuses` 按最多 200 个 `workflowIds` 批量返回 `{statuses: {workflowId: status}}`，不加载结果和提示词。
- `GET /workflows/{workflowId}` 返回 `revision` 和各步骤 `resultRevision`。可携带 `knownRevision` 与 JSON 数组形式的 `knownResults`（按步骤顺序，最多 100 项）。无状态变化时返回 `{unchanged: true, revision, lastEventSequence}`；步骤结果版本相同时省略该步骤的 `response/error/artifacts` 并返回 `resultUnchanged: true`，调用方复用缓存。缺省参数仍返回完整状态。
- 历史事件支持 `view=all|monitor|monitor_details|bot`，缺省仍为完整审计事件。`after` 向后增量读取，`tail=true` 取最近一页，`before` 向前回看；不能混用方向。响应增加 `nextCursor/hasMore/oldestCursor/hasOlder`，事件始终按序号升序返回。增量调用必须使用 `nextCursor`，即使过滤后为空也可前进，且不会越过尚未交付的符合条件事件。
- 较大的新事件正文采用透明无损压缩；旧数据库兼容升级，旧正文仍可读取。SQLite 操作在网关后台线程执行，避免写锁等待阻塞消息和心跳处理。升级时网关与本机 MCP 必须同步更新，旧版本无法读取新的压缩正文。

本机 MCP、网关和远程 Sidecar 共用事件批处理器：每批最多 64 条且只属于一个工作流，按入队顺序提交；事件编号与时间在入队时固定，失败重试复用编号。取消等待会先等待已开始的写入结束，关闭时刷新剩余事件；远程认证和租约校验仍由内部 API 客户端负责。

历史事件维护见部署指南，默认只统计、不删除数据。事件、步骤尝试和附件的审计保留语义不变。

升级前缺少任务归属的运行由配置中心后台补齐，不修改原快照；机器和任务占用保护继续保留。

## 机器业务删除

`DELETE /agents/{id}` 返回 `{deleted: true}`，中央 SQLite 为登记表兼容增加 `deleted_at`，只标记删除，不清除机器配置或历史记录。重复删除返回成功，找不到机器或存在未结束工作流、排队/执行中的 Skill 或 MCP 安装（含待核对占用）时返回 400。配置中心另行检查未删除 SOP 和提交中的运行引用。

已删除机器从登记目录、执行配置、同组机器列表及选择列表隐藏，禁止修改、检测和创建新任务；工作流及安装创建在写事务内再次检查，防止并发删除绕过预检。历史记录仍可查询。没有恢复接口，保留原 IP/端口唯一占用与分组历史引用，不能以重新登记覆盖旧机器。

## 机器备注

`POST /agents`、`PUT /agents/{id}` 接受可选 `remark`；新增、更新响应及 `GET /agents` 的机器条目返回该字段。备注去除首尾空白后最多 100 个 Unicode 字符，可重复；新建省略为空，更新省略保留原值，空字符串清空，非字符串（含 null）或超长文本返回 400 中文错误。

`PUT /agents/{id}/remark` 用于卡片自动保存，严格只接收 `{remark}`，返回 `{agentId, remark}`；校验规则与上文一致，缺少字段、额外字段及机器不存在返回 400。单条更新仅写备注，重复提交相同值安全，不同步部署默认值、不修改检测结果，也不受安装操作占用限制。

备注保存在中央 SQLite `registered_agents.remark`，启动时兼容增加列并默认置空，不删除已有数据。仅备注发生变化、连接地址/分组/能力/启用状态及安装设置均未变化时，不同步部署默认值，保留执行配置及连接检测结果；其他保存操作保持原同步规则。备注不进入执行配置或历史运行快照。

## 统一分组协议

分组目录返回 `mcpCount`，按 `mcp_package_groups` 中各组收录的 MCP 包计数，同一包在每个所属组各计一次；旧库尚未建立 MCP 表时返回 0。配置中心透传该统计，用于分组管理和 MCP 分组导航。

中央 SQLite 的机器分组为全系统唯一分组目录，配置中心角色、SOP、任务及运行引用相同编号。机器分组编号与已有登记保持不变。分组删除在 SQLite 写事务内设置 `agent_groups.deleted_at`，启动时自动兼容增加此列；目录只返回未删除分组。有效机器及 Skill/MCP 收录阻止删除，已删除机器和下发历史不阻止；原记录、名称占用和历史引用保留，重复删除安全。登记机器、包收录及新下发均拒绝已删除分组。配置中心另校验有效业务配置和未结束运行。

`POST /workflows` 支持可选 `groupId`，提供时须为有效 UUID，且主监督和全部步骤执行机属于该组；新配置中心必传，旧独立调用省略时保持兼容。该字段保存在原始运行规格中。网关预检后，创建运行的 SQLite 写事务再次校验分组，机器改组也在写事务中拒绝活动运行引用，避免并发改组绕过检查。

`POST /agents/validate` 同样接受可选 `groupId`，用于 SOP 保存的同组验证。配置中心负责业务引用保护和跨实例分组写入互斥，不把角色或任务数据复制到中央 SQLite；浏览器仍通过配置中心访问。先升级中央网关再升级配置中心，详见[统一分组升级说明](../../docs/SHARED_GROUPS_UPGRADE.zh-CN.md)。

## Skill 压缩包下发

中央网关新增 `/skills`、`/skills/machines`、`/skill-deployments` 和 `/skill-deployment-tasks/{id}/retry|check` 管理接口，经配置中心代理使用。ZIP 与解包文件保存到专用目录，包清单、逐机任务、安装归属及进度保存在同一中央 SQLite。使用独立执行服务文件连接，不经过业务模型会话，纯执行机无需 Sidecar。

默认关闭，需配置 `skill_deployment.package_root`，再在机器管理网页保存逐机安装目录和授权（中央 SQLite 持久化、立即生效）；同时尊重机器 `allow_write`。只支持首次安装，不覆盖同名不同内容，不执行安装脚本、不自动安装依赖或启用能力。根 `SKILL.md` 最后发布，回读校验及 `skills/list` 刷新通过后才报告已安装。配置、容量、接口状态和重启恢复边界见 [Skill 下发部署说明](../../docs/SKILL_DEPLOYMENT.zh-CN.md)。


Skill 包通过中央 `skill_group_packages` 关联表加入一个或多个现有机器分组；上传及新下发必须提供 `groupId`，查询可按组筛选。下发在写事务内校验组、包收录和全部机器归属，新批次冻结分组编号及名称。机器有待处理 Skill 操作时禁止改组；存在收录的组禁止删除，仅有批次历史时允许逻辑删除。安装汇总按机器当前组展示完整平台记录，不扫描人工安装内容。旧包手动归组，旧批次不推测归属；迁移、旧队列恢复和接口细节见 [Skill 下发部署说明](../../docs/SKILL_DEPLOYMENT.zh-CN.md#统一分组升级)。

## 执行机配置

定时任务执行前检查使用中央网关 `POST /schedule-checks`，仅由配置中心后台调用；请求字段为 `groupId`、`agentId`、`scriptPath`、`arguments`、`timeoutSeconds`，响应为 `{status, reason}`。中央从机器登记校验同组、执行能力、检测及权限后，用独立执行服务连接运行 Windows BAT/CMD，不经过业务模型或 Sidecar 状态接口，不读取远程 SQLite。参数、结果及部署约定统一维护在[配置中心 REST 接口说明](../role-task-config-center/README.md#rest-接口)的执行前检查段落。脚本输出只用于判断，不写入事件或返回原文；断连及超时不自动重试命令。

带图咨询所用的主监督机器以及接收图片的步骤执行机需要配置 `machine_defaults.artifact_root` 和 `allow_write: true`。已有机器缺少图片目录时，在中央服务配置中补齐目标执行机的绝对目录，待无活动工作流后升级并重启网关，再在机器管理中编辑保存对应机器；保存同步目录并保留机器编号和 SOP 引用，远程通过同组清单获取。省略目录配置保留已有值，目录不能配置为空字符串。任务助手自身仍以只读沙箱执行。

机器只能通过 8091 网页登记，中央 SQLite 为唯一机器清单来源。没有来源开关、旧机器导入、独立远程清单或机器配置环境变量回退。统一目录、协议、模型和凭据引用保存在 `config/workflow-service.json`；示例、字段约定、本机与远程执行方式见[机器管理部署说明](../../docs/WEBUI_MACHINE_REGISTRATION.zh-CN.md)。

本机编排进程与网关读取同一配置和同一 SQLite；远程 Sidecar 通过中央认证接口按需获取同组机器。SOP 保存时校验登记、能力和分组；提交及中央步骤派发时额外校验启用和手动检测资格。登记的 cwd 是默认目录：节点留空时继承，填写绝对目录时覆盖，已有登记同样生效。本机读取和远程清单下发使用相同规则；绝对路径、写权限和完全访问权限仍在执行边界校验。

统一部署配置支持 `machine_defaults.allow_write` 和 `machine_defaults.allow_full_access`，均默认 false；完全访问必须同时允许写入。权限上限在登记或网页保存机器时持久化，并通过同组清单下发；修改配置后重启网关，已登记机器需编辑保存应用。节点必须另行选择 full_access，配置上限不会自动提升所有节点权限。

凭据支持 `token_file`（绝对路径）或 `token_env`（环境变量名称），严格二选一。这里的环境变量仅用于解析凭据，不切换机器来源。令牌文件为 UTF-8 单行、最多 8 KiB，每次连接重新读取；实际值不写入登记字段或网页响应。

`GET /agents` 返回登记 ID、IP、端口、分组、检测记录、默认目录/模型、启停、能力、主监督容量和权限上限。对于具备主监督能力的执行机，还返回 `connectionStatus`（`online`、`offline`、`unknown`）、基于持久租约计算的 `availability`（`idle`、`busy`）、`checkedAt` 和 `lastOnlineAt`；不返回令牌或原始连接异常。`local_db` 每 10 秒执行轻量 WebSocket 探测；`remote_sidecar` 使用 SQLite 中的权威心跳，5 秒上报一次，20 秒未续租即离线。`POST /workflows` 在写入 SQLite 前校验主监督和每个步骤执行机是否存在、启用、能力匹配，并继续执行权限档位校验；不要求各步骤使用相同执行机或工作目录。

提交成功的工作流先进入 `queued`。调度器按主监督分别以 `created_at + workflow_id` 领取最早任务；同一主监督固定只运行一个工作流，不同主监督可以并行。远程租约包含不可预测令牌、Sidecar 实例 ID、续租时间和过期时间；写接口在同一个 SQLite 写事务中重新校验租约，旧实例和旧令牌不能回写。完成、失败和取消时与终态在同一事务释放，半自动暂停继续占用。远程主监督离线时新工作流立即失败；运行中失联、实例更换或心跳超时会结束并释放租约（已保存提前结束决定时保留该结果，其余标记失败），不自动迁移。网关重启保留已持久化的提前结束决定，其余遗留的 `running/cancelling` 工作流标记失败、清空租约并继续排队任务，不重新附着旧外部会话。

已有任意格式工作流附件保留在共享数据库中；新的图片生成事件会在事件截断前被合并并保存为附件。业务文档通过独立的文档采集与展示接口处理，不再从托管输出目录发布附件。单文件上限为 20 MB，每个工作流的当前与历史文件合计最多 50 个，同一 SHA-256 内容自动去重。网关继续通过 `GET /workflows/{workflowId}/artifacts/{artifactId}` 只读返回附件，不提供任意文件路径读取能力。非图片响应强制附件下载并设置 `nosniff`。

网关启动时还会回填步骤结果中仍然存在、且位于 `$CODEX_HOME/generated_images` 受信目录内的历史图片链接；目录外路径不会读取。

## 步骤流转模式

`supervisorTimeoutSec` 支持 10～43200 秒（最长 12 小时），省略时仍为 7200 秒；节点 `timeoutSec` 仍为 10～7200 秒。网关派发主监督使用独立的较长超时上限，普通执行及 MCP 单任务派发保持原上限。已有运行使用提交时的时长快照，修改 SOP 不会延长正在执行的运行；按原快照重试也沿用原值。

消息图片上传等入队前处理通过现有 `POST /workflows/{workflowId}/input-observations` 传入 `receiving: true`，同一等待在接收结束前禁止继续；入队事务自动清除此标记，接收失败用同一接口传入 `hold: false, receiving: false` 解除接收阻塞而不恢复倒计时。字段省略保留原行为。`discussionBusy` 同时包含接收、排队和执行阶段。

以下两分钟及通知回执规则适用于没有 `waitPolicy` 的历史快照；新提交规则见本文“半自动等待策略”。

工作流提交可携带 `advanceMode`：

- `automatic`：默认值，成功步骤完成后立即派发下一步骤。
- `semi_automatic`：仅支持严格串行工作流。成功步骤完成且仍有下一步骤时，SQLite 中创建两分钟持久化等待；调用 `POST /workflows/{workflowId}/advance/{gateId}/confirm` 可立即放行，调用 `POST /workflows/{workflowId}/advance/{gateId}/hold` 可持久化暂停并取消自动放行。暂停后再次调用确认接口即可继续；未暂停且未确认时由运行时到期自动放行。

状态接口同时返回 `advanceMode` 和 `pendingAdvance`；后者通过 `state` 区分 `countdown` 和 `held`，并在暂停时返回 `heldAt`。暂停期间工作流仍为 `running`，暂停时间不计入主监督最长运行时间。最后一步、失败步骤和跳过步骤不创建等待；取消、重跑和其他使等待失效的状态变化会关闭旧等待，防止过期按钮影响新一轮执行。

全自动、半自动均不支持通过聊天停止、跳过、返工或继续。配置中心取消入口保留，业务步骤执行中拒绝用户取消。

半自动等待期间，新任务消息在入站事务中先保持等待，再交给刚完成步骤的原执行者；只有按钮可以提前放行。重复消息绑定首次等待，不影响后续步骤。保持等待仍占用运行名额。

配置中心在下载图片前调用 `POST /workflows/{workflowId}/input-observations`，请求为 `{"messageId":"有效 UUID"}`，返回首次入站对应的 `gateId`（可为空）和 `controlAllowed`；同一编号与后续 `/messages` 复用。该服务端入口不增加监控中心路由。

钉钉普通消息及图片下载前仍通过 input-observations 保持等待；继续按钮直接调用确认接口。hold 参数保留协议兼容，不再用于文字继续。

钉钉通知成功后调用 `POST /workflows/{workflowId}/advance/{gateId}/notified`，请求为 `{"sentAt":"带时区的发送成功时间"}`。仅尚未过期的倒计时等待接受首次回执，将 `expiresAt` 调整为发送时间后120秒，并返回 `updated: true`；重复、迟到、已保持或已关闭等待返回 `updated: false`。`pendingAdvance` 新增可空 `notifiedAt`。通知失败、回执未能在原截止前送达或没有钉钉通知时，仍按等待创建后120秒放行。旧等待保留原截止时间。

## 步骤记录查询与独立咨询

非等待期间，任务助手支持查询任意步骤及历史执行版本，并对已结束步骤发起独立只读咨询。平台校验 `list_step_attempts/read_step_records/consult_step` 请求，不开放浏览器直连执行服务。等待期间改走下文原执行者讨论链路。

记录优先读取原执行轮次，连接不可用时使用中央已有事件、指令和结果，并明确标注不完整。分页冻结一次查询的来源与顺序，不能把后续返工记录混入旧版本。每个查询最多保留 200,000 字符，单条显示最多 8,000 字符，超限标注；单次工具结果最多 20,000、累计证据最多 40,000、最终提示词最多 100,000 字符。每条消息最多 8 次工具调用、总时限 600 秒，超限不无限重试。

咨询在原执行机上优先通过 `thread/fork` 和 `lastTurnId` 继承截至目标轮次的上下文；不支持时使用选取记录建立新会话，并声明上下文有限。后续按任务、步骤、执行版本和提问人复用咨询会话；网页未提供提问人时使用该任务的网页咨询上下文。咨询不恢复原业务会话、不发布产物、不更新步骤结果或消耗重跑额度。

咨询固定只读、禁止审批、关闭网络及 MCP/应用/插件，检查返回的沙箱及工具清单；执行服务无法确认隔离时拒绝启动。咨询只查看代码、测试文本及已有日志，不执行构建测试。原始图片不会转交咨询会话，回答不能声称已查看参考图片。核查当前文件不能还原历史代码；尤其后续步骤可能已修改同一工作目录。

SQLite 自动兼容新增运行目录/模型快照、咨询会话、工具调用和查询分页快照。重复消息复用已保存结果；未知启动结果不会重发，已知咨询轮次先查询终态，不能确认时保持该咨询会话不可继续并返回明确限制。后续提问会再次尝试核对已知轮次。远程模式仍由中央保存状态，中央直接使用已登记的管理连接访问执行服务，不增加 Sidecar 数据库。升级及验收见[咨询功能部署说明](../../docs/ASSISTANT_NODE_CONSULTATION.zh-CN.md)。

## 等待讨论与交接总结

等待期间的消息绑定首次入站时的等待和刚完成步骤，恢复该步骤原业务会话，使用原实际目录、模型和权限。明确修改、采纳或要求整理时，返回完整新版总结；普通解释只回答，不改总结。每个回答与总结最多 20,000 字符，格式或容量无效会明确失败，不静默截断新版总结。文字交接沿用依赖合计 40,000、提示词 100,000 字符限制。

最新总结直接保存为当前步骤结果，更新结果修订号，回答及更新在同一事务提交。不增加执行次数、不重新创建等待、不重跑尾部。原步骤迟到轮询不能覆盖新总结。共享目录由使用者保证；平台只交接文字总结，不搬运前序业务文件，历史附件仍可读取。

SQLite 兼容增加 `workflow_discussions` 保存消息对应的步骤、执行版本、原会话、讨论轮次和处理状态，避免重复执行写操作。本地执行报错后，若核对原轮次确认已完成且回答格式有效，会直接保存回答和总结，无需用户再次重试。已知轮次重试先查询结果，未知启动结果不重发；无法确认时持续阻止放行，并提示核对执行状态。重启保留已持久化的提前结束决定，其余遗留运行标记失败，不新增自动恢复。

状态接口新增 `discussionBusy`；聊天完成事件可附带 `nodeId/stepNumber/gateId/summaryUpdated`，已有事件名及普通咨询字段兼容。完成回复明确说明是否更新总结。使用原消息与等待接口；文字不触发继续或取消。配置中心取消会先中止并核对活动讨论，不能确认远端终态时返回冲突，不报告取消成功。

聊天控制提议、二次确认、跳过和尾部返工入口已移除；`pendingControl` 固定为 null，历史额度及动作字段仅保留兼容，不产生新操作。历史表和审计不删除。升级前结束活动运行，部署步骤见 [等待讨论升级说明](../../docs/SOP_WAIT_DISCUSSION_UPGRADE.zh-CN.md)。

## 步骤产物与修改

运行时不再统一追加“系统单次产物约束”。步骤可在任务要求和原权限范围内修改多个文件、验证并修复问题；需要限制生成次数的任务应在该步骤执行要求中明确。等待期间仍只有用户明确要求修改或采纳时，才在原会话修订产物和交接总结，普通提问不构成修改授权。已有执行会话中的历史提示词不会被撤回。

## 步骤结果交接

所有步骤通过共用实际工作目录中的文件及直接依赖步骤的最新版文字总结协作。执行顺序仍由 `dependsOn` 决定，目录共享由使用者保证；不同机器上的同名路径不代表同一份文件。

累计文件交接模式已移除：不再创建步骤输入/输出托管目录、复制前序文件、收集托管输出或限制交接文件数量。`handoffMode` 不再出现在运行状态与新提交快照中；旧请求中的该字段被忽略，不恢复旧文件模式的执行或重试行为。SQLite 遗留列和历史附件保留，不进行数据清理。

图片输入仍使用 `machine_defaults.artifact_root` 暂存；生成图片、附件读取、计划文档采集及全自动文档汇总保持独立。步骤可按任务要求和原权限修改多个业务文件；`allow_write` 是写入上限，`full_access` 仍要求同时授权 `allow_write` 和 `allow_full_access`。

升级时等待活动运行结束，同步更新中央网关、本机编排进程/远程 Sidecar 和配置中心；不支持运行中的旧文件交接任务跨版本继续执行。部署不需要删除数据库或历史附件。

节点权限映射遵循 OpenAI 的 [Sandboxing](https://learn.chatgpt.com/docs/sandboxing) 与 [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security) 语义：`read_only = read-only + never`，`workspace_write = workspace-write + never`，`auto_review = workspace-write + on-request + auto_review`，`full_access = danger-full-access + never`。启动节点前会读取 `configRequirements/read`；执行机管理策略明确不允许时不会启动 thread。旧 app-server 不支持该方法时保持兼容。

## 启动网关

中央网关可部署在 macOS，主监督和执行机可继续使用 Windows。`workflow_db`、两个包存储 `package_root` 和凭据文件引用使用中央机器路径；`machine_defaults.cwd` 与 `machine_defaults.artifact_root` 使用执行机路径，支持 Windows 盘符、UNC 和 Unix 绝对路径，不应因中央迁移到 macOS 而改成 Mac 路径。

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

需要终端调用的 CLI（包括兼具 CLI 的 MCP）另由平台固定代码将入口目录加入执行账号的用户 PATH，并检查命令名解析；纯 MCP 和 Skill 目录不加入。安装结果新增可空 `terminal` 入口，MCP 注册继续使用绝对路径。已有 CLI 记录可通过“重新检测”补做 PATH；已有终端或执行服务需要重启取得新环境，不自动重启服务。范围、旧 MCP 记录兼容及脚本启动器限制见上述下发说明。

新增独立安装会话、中央持久任务与业务互斥，通过配置中心管理。默认关闭，安装使用限定目录写入和关闭网络策略；完整配置、接口及真实协议验收限制见 [MCP 下发说明](../../docs/MCP_DEPLOYMENT.zh-CN.md)。

机器安装配置支持统一 `installRoot`，临时和程序子目录仍独立授权；Python/Node 路径由安装助手发现，平台检查启动文件及脚本入口。已有双目录配置和冻结任务保持兼容，不自动迁移。

MCP / CLI 原 ZIP 和解压文件超过 8 MiB 时自动按 4 MiB 分块下发，平台固定 PowerShell 代码在执行机合并并校验 SHA-256，不整文件回传。大文件要求机器已登记允许写入及完全访问；传输阶段的完全访问只用于合并代码，安装助手权限不变。分块归属、断连恢复、缓存保留及升级步骤见 [MCP 下发说明](../../docs/MCP_DEPLOYMENT.zh-CN.md#大文件分块传输)。

MCP 逐机任务持久化 `diagnostics` 检测摘要，由安装汇总和批次查询返回；含时间、阶段、是否发现服务、连接状态、工具数量及安全分类原因，不存储原始执行服务错误。数据库自动兼容增加此字段，旧记录保持为空直到重新检测。

## 交付文档展示

仅半自动流程的首个业务步骤且存在下一步时自动展示一份主文档，无可配置开关。结构化结果返回总结及可为空的 `document: {name, path}`；半自动单步骤及后续步骤不采集；新版全自动另按版本 3 清单采集。中央 SQLite 保存正文，远程通过带认证与租约的步骤状态接口提交单个 `document`，不读取 SQLite。讨论明确修改才同步，缺失或失败保留旧正文并提示，允许继续；改名和重试沿用稳定链接。历史开关字段保留兼容但不起作用。容量、只读接口及部署见[文档展示说明](../../docs/SOP_DOCUMENT_DISPLAY.zh-CN.md)。


## 业务阻断与无任务提前结束

平台统一追加的协议提示明确：阻断但未锁定唯一工单、无权限或开发人读取失败时，开发人状态使用 `unavailable`，评论状态独立填写，不需要修改 SOP 角色职责。模型结果解析仅兼容一种版本 2 误填：`blocked` 搭配开发人 `not_applicable`，人员账号类型、账号、显示名全部为空且说明为字符串（允许为空）时，规范为 `unavailable` 后再完整校验。原始回答仍保留在执行事件中，成功规范化记录无业务正文的诊断日志；不改变阻断决定、不自动重发业务操作。其余非法结果仍失败，执行诊断通过 `error_kind=invalid_business_result`、`error_stage=result/validate` 和 `error_details.validation_error` 保留校验原因，普通页面仍使用稳定中文错误。版本 1 及旧运行语义不变；中央状态写入仍校验字段、类型、容量与账号状态一致性。

既有网关调用未显式指定版本时仍默认冻结 `resultProtocolVersion=2`，正式步骤使用统一结构化结果：`summary`、`outcome`、`reason`、`jiraComment`、`document`、`jiraDeveloper`。提交可显式指定版本 1 用于原快照兼容，已有版本 1 运行保持原五字段协议。`outcome` 只允许 `success/no_task/blocked`，新协议缺失或非法结果按执行失败处理；旧运行没有版本字段，保留旧协议，不扫描旧回复中的关键词。单主文档仍仅在半自动首步且有下一步时采集。配置中心新全自动运行显式冻结版本 3，以 `documents: [{name, path}]` 替代 `document`，每步采集全部交付文档；汇总元数据位于工作流顶层 `documents`，其余结果及 Jira 语义不变。旧快照不升级，详见[文档展示说明](../../docs/SOP_DOCUMENT_DISPLAY.zh-CN.md)。

未配置新等待策略的运行，步骤判定阻断后停止 SVN 提交、方案移动及正常业务流转，仅在原授权内对已锁定的唯一 Jira 执行阻断收尾：先写入说明原因、已完成/未执行事项和恢复条件的评论并回查确认成功，再转交负责人。唯一开发人使用 `Public.Jira.Fields.Developer` 的实际读取结果；为空或多人使用 `Public.Jira.Manager`，读取失败重试一次，仍失败使用 Manager。Manager 无法确认或无权限时不猜测人员，记录待人工处理。转交前后回查负责人、Jira 状态及处理角色；已是目标负责人不重复写入，结果不明先查不盲目重发，并发变更不覆盖。Jira 状态和处理角色保持不变，此规则优先于旧职责的 Natural Person 收尾要求，转交不代表审核通过。评论未确认成功不得转交，最终始终返回 `blocked`。`summary`、`jiraComment.detail`、业务日志及职责要求的总结文档记录前后负责人、目标与实际结果，无法写入日志或文档时在总结披露。`jiraComment.status` 仅表示评论结果；`jiraDeveloper` 只保留开发人读取结果，不填入兜底 Manager。平台注入稳定评论去重标记，存在则复用，提交不明先核查，不能确认不盲目重发；此模型侧约定不提供严格的一次性写入保证。`jiraComment` 包含 `status`（`succeeded/failed/unknown/not_applicable`）、`issueKey`、`reference` 和 `detail`。没有唯一编号或没有授权工具时说明原因，收尾失败或未知不阻止流程结束。平台不新增 Jira 凭据或连接。

正式结束决定形成后，中央事务保存 `termination`（`outcome/reason/nodeId/jiraComment`，版本 2、3 同时保存 `jiraDeveloper`），立即关闭等待并拒绝后续派发。阻断步骤标记失败，无任务步骤标记完成，其余未启动步骤标记跳过、显示“未执行”。收尾期间保持运行占用，网关等待主监督退出及已派发步骤结束后写入终态并释放租约。阻断映射 `failed`，无任务映射 `completed`；结果文字分别为“因阻断结束”和“无待处理任务，已结束”。重复与迟到结果不能覆盖决定，旧继续按钮无效。重启保留已持久化的提前结束决定，不恢复旧执行会话。

`GET /workflows/{workflowId}` 新增可空 `termination`；`POST /workflow-statuses` 保留原 `statuses` 字符串映射，新增同编号的 `terminations` 映射；终态事件附加 `termination`。内部步骤快照增加 `businessResult`，本机和远程使用相同校验，远程继续校验机器认证与租约。旧客户端可继续识别原终态，但须升级展示才能区分业务结束原因。

仅正式业务步骤可控制提前结束。等待期间讨论、普通咨询仍不能停止流程。已结束运行的问题解决后通过配置中心创建重试运行，不支持从聊天恢复终态。带版本 2 `waitPolicy` 的新半自动运行持续等待人工处理，未结束时可通过卡片重新执行当前遇阻步骤。升级说明见 [阻断结束升级与验收](../../docs/SOP_BLOCKED_STOP.zh-CN.md)。

### Jira 开发人结果（版本 2）

`jiraDeveloper` 严格包含 `status/accountType/accountId/displayName/detail`。凡正式步骤处理唯一 Jira，成功或阻断均返回 `jiraComment.issueKey` 并实际读取该 Jira 的 `Public.Jira.Fields.Developer`，不因评论为 `not_applicable` 省略编号或开发人，不为展示新增评论或修改 Jira：唯一人员为 `resolved`，账号优先使用实际返回的 `accountId`，其次 `key`、`name`，`accountType` 标明所选字段，`accountId` 保留原值；禁止使用姓名、经办人或发起人推断。账号上限 256、显示名 160、说明 2000 字符。

空值、多人、无法读取分别为 `empty/multiple/unavailable`，账号类型与账号必须为空；未处理 Jira 的非阻断步骤可用 `not_applicable`。多人可在显示名内返回实际姓名列表，不选择其中一人。建议在 `detail` 说明读取情况，但允许空字符串或纯空白，不因开发人或说明为空把业务结果判为格式无效，也不猜测或补填人员。开发人读取结果不单独阻断业务，已有业务阻断决定仍保留；`resolved` 仍必须提供唯一 Jira 与稳定账号。中央根据冻结版本严格校验，本机和远程共用规则；旧运行没有开发人信息，不从正文提取或补发通知。负责人转交结果放在 `jiraComment.detail`，其 `status` 仅表示评论结果。

Python 不持有钉钉凭据或发送通知；8091 在确认阻断终态后按冻结群配置通知开发人。升级顺序和验收见 [Jira 阻断通知部署说明](../../docs/JIRA_BLOCKED_NOTIFICATION.zh-CN.md)。

## 步骤思考强度

工作流提交节点增加可选 `reasoningEffort`，仅接受 null 或 `none/minimal/low/medium/high/xhigh/max/ultra`；缺失或 null 保留执行服务默认行为，`none` 是需要模型明确支持的具体档位。配置中心负责计算流程默认与节点覆盖，Python 只接收有效节点值。值随提交快照和 SQLite 节点列持久化，旧库自动补充可空列，旧运行不补填具体强度。

`GET /agents/{agent_id}/models` 通过已登记执行机的管理连接读取 `model/list` 全部分页，仅返回 `models: [{model, supportedReasoningEfforts, defaultReasoningEffort}]`，其中支持档位为字符串数组。不返回连接、认证或工作目录；无效/不可用执行机返回 400，能力读取失败返回 503 和稳定中文提示。查询只读取能力，不创建执行会话，最多等待八秒。

提交前检查明确配置的模型与强度组合；本机及远程 Sidecar 在启动步骤前再次读取能力校验，再通过 `turn/start.effort` 传递。模型不支持或能力查询失败时不会启动该步骤，不静默降级。未配置强度时不查询能力、不发送 effort，兼容旧执行服务。半自动等待讨论继续使用该步骤冻结的强度；主监督和独立任务助手不受此设置影响。远程链路经中央内部节点派发接口传递该字段，不引入 Sidecar SQLite。

上线前结束活动运行，更新中央网关、本机编排或远程 Sidecar，再升级配置中心；仅新增字段，不改变步骤顺序、权限或讨论放行规则。

## 半自动等待策略

新提交的半自动运行携带 `waitPolicy: {"version":2}`，不接受等待时长字段。配置中心创建运行时冻结；每一步成功后必须手动确认，最后一步（含单步骤）必须确认结束，遇阻一直等待人工重新执行或终止任务。全自动不携带此策略。历史版本 1 策略仍按冻结的 `confirmationMinutes` / `blockedMinutes` 计时，缺少策略的旧快照保持原行为；原快照重试不自动升级。

复用中央等待记录，`pendingAdvance.kind` 为 `advance`（中间成功）、`blocked`（业务遇阻）、`finish`（最后成功）；旧等待为 `legacy`。增加 `nodeId`、`availableActions`，最后一步 `nextNodeId` 为空。步骤遇阻使用 `waiting`，显示“等待人工处理”，此时无 `termination`，后续步骤仍未开始。执行等待动作时 `pendingAdvance.state=busy`。

版本 2 的三类等待创建时直接持久化为 `held`，设置 `heldAt`，不进入到期扫描；沿用历史表中非空的截止时间列保存创建时间，但 `held` 状态下该值无计时意义。消息、通知回执和机器人回复都不会启动倒计时；接收图片、排队和讨论期间不得确认。中央网关继续事务领取人工操作，定时到期处理仅用于历史计时策略。Sidecar 仍只访问中央 API。等待期间暂停空闲主监督，不消耗其执行超时，但继续占用运行名额；网关重启及租约失效仍按既有中断规则处理。

确认接口按等待类型处理下一步或最终结束，拒绝遇阻放行。新增 `POST /workflows/{workflowId}/advance/{gateId}/retry` 和 `/stop`，只处理当前有效新等待；重复领取不重复执行，旧卡片不能影响后续步骤。`retry` 在原会话重新处理当前步骤并归档旧执行版本，不重跑其他步骤；沿用原工作目录及权限；成功后重新等待确认，再次遇阻继续等待人工处理；版本 1 历史策略才重新计时。讨论本身不执行流程控制。`stop` 为人工取消，不回滚已有写入，不执行阻断 Jira 收尾或开发人通知。

首次遇阻禁止停止评论和转交 Jira 处理角色；超时后平台关闭交互，再授权原执行者仅执行阻断收尾。收尾结果不允许改变工单身份和原阻断决定；失败或未知仍结束并明确披露，不自动重发。重新执行遇到已确认的技术错误按失败结束；外部写入终态不明时保持禁止放行并记录诊断，不盲目重复操作。

SQLite 兼容新增等待字段和索引，不恢复已结束任务。网关重启和远程租约失效仍沿用原中断规则，不支持重启恢复。最终确认只延后平台运行终态，不撤回或延后原步骤已完成的文件、提交及 Jira 操作。部署见[升级说明](../../docs/SOP_SEMI_AUTOMATIC_WAIT.zh-CN.md)。

## 主监督异常收尾

远程 Sidecar 的监控事件上报遇到租约不可用时，先以成功的中央心跳确认该实例已不再持有对应工作流租约；单独的 HTTP 409、网络失败或心跳失败不作为丢弃事件的依据。确认后停止该工作流旧事件的重试，移出发送队列，并对后续旧事件累计未送达数量（进程内 `AsyncEventBatcher.rejected_event_counts`），首次记录带工作流编号的警告，避免旧事件阻塞其他运行。该处理不视为上报成功，不绕过授权，也不恢复已经结束的流程或补回业务结果；未送达事件正文不额外落盘，计数在进程重启后清空。升级需更新并重启远程 Sidecar；本改动不改变主监督异常时现有的流程终止规则。

App Server 协议读取与监控事件写入分开处理：读取循环持续接收通知、RPC 响应和心跳，通知保留接收时间并通过现有队列按序交给执行消费端，再执行监控回调。监控批量落库或远程上报变慢时，不阻塞 WebSocket 读取，避免命令大量输出触发接收背压后连心跳回应也无法读取。连接关闭时补齐尚未消费的监控事件；事件内容、顺序和批量写入的幂等标识不变，心跳间隔及超时仍为 20 秒。升级需同步中央网关、本机 MCP 和实际运行步骤的远程 Sidecar；已经失败的运行不会自动续跑。

主监督失败（包括上游限流重试耗尽）时，网关通过已保存的原执行标识向活动步骤发送限时中断请求，适用于本机 MCP 和远程 Sidecar 持有的步骤。随后同一事务保存工作流终态、将未结束步骤标为中断、未启动步骤标为未执行，并释放运行名额；已完成结果保留。迟到结果和绑定不能重新打开终态步骤。中断请求成功不等于已确认外部操作停止；步骤提示保留结果待核实说明，连接失败仅记录错误类型，不自动重发业务操作。该修复不回写历史终态记录，上线前仍需核实旧运行执行端及外部写入结果。


## 精简消息的事件与提示词

监督提示词要求等待超时静默继续，不猜测正常推进或正在收尾；最终以“本轮流程已结束”区分执行结束与业务验收结论。执行者减少内部操作流水，正式结果仍须保留阻断原因和未验证项，结果协议不变。

`view=monitor` 返回聊天与受理、步骤启动、失败／超时／取消、等待和终态事件，不返回监督机原始播报；`view=all` 原始审计保持完整。`node.started` 新增 `payload.attemptNumber`，用于历史重放时区分首次启动与重新执行，旧事件由消费者兼容。新的监控部分索引与旧索引并存，已有数据库无需清理或导出；分页与增量游标语义不变。

部署时同步中央网关、实际执行编排的本机 MCP／远程 Sidecar 和两个 Java 服务；不修改运行快照或业务流转规则。


## 已移除的历史重跑额度

提交中的旧 `maxRetryCount` 字段被忽略，不再校验、写入新运行规格或参与执行；状态接口、主监督查询工具及助手上下文不再返回 `retryPolicy`（含 `maxRetries/usedRetries/remainingRetries`）。SQLite 的历史额度列和原有规格快照保留，不清理业务数据。半自动“重新执行”的步骤版本计数继续保留，历史整项重试仍创建新工作流，不增加次数限制；消息投递及数据库技术重试保持原规则。升级需同步 Python 网关、MCP／Sidecar 和 Java 配置中心，无需删列或修改已应用迁移。

### 结束卡片 Jira 展示信息

正式步骤结果通过既有校验后，在同一中央事务保存 `jira_context_json`，查询快照新增可空 `jiraContext: {issueKey, developerStatus, developerName}`，不包含账号、原始说明或工具输出。SQLite 自动新增可空列，旧记录不从总结回填。后续步骤未返回编号时保留已知信息；同一编号的 `not_applicable` 不覆盖已保存信息，实际读取结果覆盖旧值；不同编号不继承前一工单开发人。重复、迟到及无效结果沿用原拒绝规则。本机与远程 Sidecar 共用中央保存链路，不改变流程状态、等待或阻断通知规则。

需同步升级中央 Python 与实际执行的 MCP／Sidecar，使成功步骤按提示返回已有 Jira 字段；旧协议继续兼容，缺少展示信息不单独导致业务失败。卡片展示规则见配置中心 README。
