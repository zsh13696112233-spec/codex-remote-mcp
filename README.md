# Codex 工作流编排平台

本仓库是一套由 Python 工作流服务和两个 Java Web 应用组成的 Codex 多执行机编排平台。配置中心负责定义并提交任务，Python 服务负责调度 Codex，监控中心负责展示执行过程，并通过独立任务助手处理咨询和自然语言控制提议。

## 系统架构

```text
飞书或钉钉群聊 ── HTTPS/WSS 443 长连接 ──▶ 角色任务配置中心（8091）
                                                │ 提交工作流
          ▼
Python 工作流网关（8080）── 主监督 App Server（4500）
          │                         │ 本机 Streamable HTTP MCP
          │ 内部 API               ▼
          └────────────────── 远程 Sidecar（127.0.0.1:8082）── 执行机 App Server
          ▲
          │ 查询、事件和对话
任务运行监控中心（8090）
```

中央 Python 网关独占 SQLite 工作流运行库。阶段 A 的本机兼容 MCP 仍可与网关共享同一个数据库；阶段 B 的远程 Sidecar 只使用带机器认证的中央内部 API，不读取 SQLite，也不配置数据库账号。配置中心另外使用 MySQL 保存角色、SOP、任务定义和运行快照。

网关支持本机和远程多个主监督 app-server。每个主监督固定容量为 `1`：同一主监督的工作流按 `created_at + workflowId` 排队，不同主监督可以并行。远程主监督由 Sidecar 每 5 秒上报权威心跳，20 秒未收到心跳即离线；本机兼容模式继续使用轻量连接探测。配置中心提供独立“运行状态”页面，并在 SOP 主监督建议列表中显示在线空闲、在线忙碌、离线或状态未知。配置中心允许为 SOP 自由填写主监督和各步骤执行机 ID；保存不依赖网关在线，提交运行时才由网关校验执行机是否存在、启用且能力匹配。步骤执行机和工作目录不要求一致，空目录继承执行机默认值。

配置中心可选接入一个飞书机器人或钉钉机器人，两套适配均使用官方 Java SDK 主动建立长连接，不要求为 `8080`、`8090` 或 `8091` 配置公网域名或入站端口。同一部署只能启用一个平台，切换时必须先停用当前机器人。飞书在话题内继续对话；钉钉可在任务运行期间直接 `@机器人` 对话，也可回复或引用启动消息、进度消息或进度卡进入任务助手。两套适配都支持固定任务启动、可靠进度补发和任务助手完整回复，并共享“同时最多一个机器人任务”的限制；钉钉未配置卡片模板时使用内置 Markdown 进度消息和“暂停 / 继续”文字控制，配置模板后使用互动卡片按钮，并在同一卡片中动态展示当前步骤、最近产出和最新助手回复摘要。

## 本轮新增：独立任务助手、节点重跑与重试额度

本轮将任务运行中的“编排”和“对话控制”拆成两条独立链路：

- 主监督继续负责严格串行编排步骤，并生成“开始第几步、步骤完成、最终结果”等任务进度消息。
- 独立任务助手只在用户发送消息时启动一次 turn，回复完成后回到空闲状态。首次咨询创建独立 thread，后续咨询恢复同一个 thread，不再向主监督执行 `turn/steer`。
- 助手固定使用只读沙箱、`approvalPolicy=never` 和结构化输出，只能回答、澄清或提出 `stop`、`skip`、`restart_from` 控制意图。所有实际状态变更仍由 Python 网关确定性执行。
- “确认执行”和“取消操作”由网关直接识别，不再调用模型；控制提议继续使用独立确认消息、10 分钟有效期和 `stateVersion` 并发校验。
- 任务助手使用独立 Orchestrator，不受主监督的执行机串行锁阻塞；同一个工作流的用户消息仍按接收顺序逐条处理。助手回复期间，监控页面仍会继续展示主监督的实时进度。

现在可以在监控中心使用自然语言提出节点流转请求，例如“从第 2 步重新执行”或“从报告步骤重新开始”。`restart_from` 的固定语义是保留所选步骤之前的成功或已跳过结果，重新执行所选步骤以及全部后续步骤。确认执行时系统会：

1. 校验更早步骤均已完成或跳过。
2. 安全中止所选步骤及其后续仍在运行的 Codex turn；支持通过持久化的执行机、thread 和 turn 标识跨进程中止。
3. 将旧结果、错误、时间、会话信息和图片归档为历史尝试。
4. 清空尾部步骤的当前结果并恢复为待执行，重新打开工作流并恢复主监督。

如果中止、状态校验或事务执行失败，节点不会被部分重置，也不会扣减重跑额度。主界面只显示最新一次执行结果和当前有效文件，旧尝试保存在 SQLite 审计表和事件中。

SOP 可选择结果交接方式。默认的 `handoffMode: "cumulative_files"` 不传递前一步文字结果或返工要求，而是把所有更早步骤的当前有效文件按来源步骤累计交给下一步；`handoffMode: "legacy_text"` 把直接上一步的文字结果追加给下一步，适合纯文本串行任务。文件交接时，每次步骤尝试都在本机 `artifact_root` 下使用独立的 `inputs/step-N/` 与空 `output/` 目录，Agent 只接收绝对路径，不传递 Base64 文件内容；业务工作区不会被扫描。目标步骤及后续步骤返工时，旧文件归档，新尝试生成新的当前文件，下游只会收到当前有效版本。缺少 `handoffMode` 的历史快照继续按 `legacy_text` 运行。

用户提出返工并说明修改意见时，独立任务助手会把意见总结为不超过 4000 字符的可执行返工要求，并在二次确认消息中展示。确认后，网关把总结持久化并追加到目标步骤实际提示词末尾；用户原话和原始步骤提示词保持不变。多次返工要求按确认顺序累积，较新要求与旧要求冲突时以较新要求为准，提示词中的返工上下文最多 20,000 字符。

每个 SOP 新增 `maxRetryCount`，默认值为 `10`，允许范围为 `0–100`，并冻结到每次运行快照。一次确认的“从某步重跑到末尾”只消耗 1 次共享额度，无论重跑多少个尾部步骤；停止、跳过、提议、取消、重复确认以及系统自动调度均不消耗额度。工作流状态返回：

```json
{
  "retryPolicy": {
    "maxRetries": 10,
    "usedRetries": 3,
    "remainingRetries": 7
  }
}
```

配置中心通过 Flyway `V3__add_sop_max_retry_count.sql` 保存 SOP 上限。使用原运行快照重试整项任务时会生成新的 `workflowId`，继承上限但已使用次数从 0 开始。监控中心增加“剩余重跑次数”，并用“任务进度”和“任务助手”区分消息来源；已完成或失败的任务仍可继续咨询和提出重跑，重新运行后自动恢复轮询。监控中心仍只代理查询、事件和消息，不新增直接停止、跳过或重试接口。

SOP 还可选择全自动或半自动流转。全自动保持步骤成功后立即继续的原行为；半自动会在成功步骤与下一步骤之间创建持久化的 30 秒等待，监控页可以点击“立即进入下一步”，也可以选择“暂停，暂不进入下一步”取消自动放行，之后再人工继续。暂停本身不会触发返工；结果不符合要求时，应在任务助手中说明修改点并完成二次确认。未暂停且无人确认时由 Python 运行时到期自动放行。最后一步、失败步骤和跳过步骤不会创建等待。流转方式会写入不可变运行快照，按原快照重试时保持不变。

## 仓库结构

```text
.
├── services/
│   ├── python-workflow/          Python 网关、MCP 编排器和状态存储
│   ├── workflow-console/         Java 任务运行监控中心
│   └── role-task-config-center/  Java 角色任务配置中心
├── config/                       执行机配置示例和本机配置
├── docs/                         架构、协议和部署文档
├── prototypes/                   不参与运行的历史交互原型
└── scripts/                      运维和端到端验证脚本
```

各模块的详细说明：

- [Python 工作流服务](services/python-workflow/README.md)
- [任务运行监控中心](services/workflow-console/README.md)
- [角色任务配置中心](services/role-task-config-center/README.md)
- [完整部署指南](docs/DEPLOYMENT_GUIDE.zh-CN.md)
- [Codex 远程混合机部署与旧版升级执行手册](docs/CODEX_HYBRID_MACHINE_DEPLOYMENT.zh-CN.md)
- [完整工作流指南](docs/WORKFLOW_GUIDE.zh-CN.md)
- [多主监督机工作流设计](docs/MULTI_SUPERVISOR_WORKFLOW_DESIGN.zh-CN.md)

## 快速启动

### 1. 准备执行机配置

```powershell
Copy-Item .\config\agents.example.json .\config\agents.json
```

编辑 `config/agents.json`，为每个执行机配置 Codex app-server WebSocket 地址、默认工作目录、`enabled`、`capabilities` 和权限上限。能力只允许 `supervisor`、`executor`；具备主监督能力时 `capacity` 固定为 `1`。旧配置中未声明能力的 `local` 同时具备两种能力，其他执行机默认为 `executor`。业务步骤支持 `read_only`、`workspace_write`、`auto_review` 三档；分别对应只读且不审批、工作区写入且不审批、工作区写入并由 Auto-review 审核越界请求。访问令牌通过 `token_env` 引用环境变量，或通过 `token_file` 引用网关本机的绝对文件路径，两者只能配置一个；不要直接把令牌写入 JSON。

远程主监督在中央配置中增加 `"orchestration_mode": "remote_sidecar"`，并使用 `sidecar_token_env` 或 `sidecar_token_file` 配置独立机器令牌。该令牌只认证 Sidecar 到中央 `8080`，不能与中央网关连接主监督 app-server 的 `token_env`/`token_file` 混用。完整示例见 `config/agents.example.json`；远程机本地执行机清单见 `config/agents.remote-sidecar.example.json`。

### 2. 启动 Python 网关

网关和 MCP 必须使用同一个 `CODEX_WORKFLOW_DB` 绝对路径。

```powershell
$ProjectRoot = (Resolve-Path .).Path
$env:CODEX_AGENTS_FILE = (Resolve-Path .\config\agents.json).Path
$env:CODEX_WORKFLOW_DB = Join-Path $ProjectRoot "workflows.db"

uv run --project .\services\python-workflow `
  python .\services\python-workflow\src\workflow_gateway.py `
  --host 0.0.0.0 --port 8080 `
  --db $env:CODEX_WORKFLOW_DB --agents $env:CODEX_AGENTS_FILE
```

主监督 app-server 的 MCP 配置示例。`<PROJECT_ROOT>` 必须替换为仓库的真实绝对路径；Windows TOML 双引号字符串中的反斜杠需要写成 `\\`，Linux/macOS 直接使用 `/absolute/path`：

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

远程主监督不使用上面的 stdio/SQLite 配置。在远程机启动 app-server `4500` 和 Sidecar `8082` 后，为该 app-server 配置官方 Streamable HTTP MCP：

```toml
[mcp_servers.codex_orchestrator]
url = "http://127.0.0.1:8082/mcp"
required = true
```

Sidecar 默认且只允许监听回环地址。示例启动命令如下；`SUPERVISOR_B_SIDECAR_TOKEN` 的值必须与中央配置解析出的机器令牌一致：

```powershell
Copy-Item .\config\agents.remote-sidecar.example.json .\config\agents.sidecar.json
$env:SUPERVISOR_B_SIDECAR_TOKEN = "请通过密钥系统注入"
.\scripts\start_workflow_sidecar.ps1 `
  -AgentId supervisor-b `
  -GatewayUrl http://central.internal:8080 `
  -TokenEnv SUPERVISOR_B_SIDECAR_TOKEN `
  -AgentsFile .\config\agents.sidecar.json
```

远程主监督当前只接受 `handoffMode: "legacy_text"`。提交 `cumulative_files` 会在写入运行库前稳定拒绝；跨机器附件传输留到后续阶段。

### 3. 启动两个 Java Web 应用

```powershell
cd .\services\workflow-console
mvn spring-boot:run
```

另一个终端：

```powershell
cd .\services\role-task-config-center
mvn spring-boot:run
```

默认访问地址：

- 配置中心：`http://127.0.0.1:8091`
- 监控中心：`http://127.0.0.1:8090/?workflowId=<workflowId>`
- Python 网关健康检查：`http://127.0.0.1:8080/readyz`

配置中心还需要 MySQL 8；数据库初始化和环境变量见其模块 README。
如需启用飞书或钉钉机器人，请先按配置中心 README 创建并发布对应企业内部应用、配置事件与权限，再在 8091 的机器人页面测试并保存配置。钉钉互动卡片模板为可选项，留空即可使用钉钉内置 Markdown 进度消息。`FEISHU_*` 和 `DINGTALK_*` 环境变量只作为数据库尚无页面配置时的启动默认值。

## 测试

Python：

```powershell
uv run --project .\services\python-workflow `
  python -m unittest discover -s .\services\python-workflow\tests `
  -t .\services\python-workflow -v
```

Java：

```powershell
mvn -f .\services\workflow-console\pom.xml test
mvn -f .\services\role-task-config-center\pom.xml test
```

## 运行与安全边界

- 三个服务（8080、8090、8091）默认监听 `0.0.0.0` 以支持可信内网访问；必须通过主机防火墙限制来源，三者都不能直接暴露到公网。只需本机访问时应显式改为 `127.0.0.1`。
- 飞书和钉钉机器人都只需要服务端主动访问平台 HTTPS/WSS `443`；无需给本系统开放公网入站接口，且同一部署只能启用其中一个平台。
- `config/agents.json`、SQLite 数据库、IDE 配置及构建产物均不提交 Git。
- 工作流队列和主监督租约持久化在 SQLite。网关重启时不会重新附着旧 Codex 会话：遗留的运行中或取消中工作流直接标记失败并清除租约，然后继续调度排队任务。
- 远程 Sidecar 令牌按主监督独立配置；内部 API 认证失败返回 `401`，跨主监督访问返回 `403`，对象不存在返回 `404`，旧实例、旧租约或状态冲突返回 `409`。停止 Sidecar 后，活动工作流会在 20 秒内失败并释放租约，不自动迁移。
- 远程机不得设置 `CODEX_WORKFLOW_DB` 或复制中央 SQLite；`8082` 不开放防火墙，`8080` 只允许 Java 服务和已登记的主监督机访问，并应位于可信内网、VPN 或 TLS 反向代理之后。
- `prototypes/` 仅保存早期页面方案，不是生产入口。
