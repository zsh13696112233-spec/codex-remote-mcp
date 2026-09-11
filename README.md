# Codex 工作流编排平台

本仓库是一套由 Python 工作流服务和两个 Java Web 应用组成的 Codex 多执行机编排平台。配置中心负责定义并提交任务，Python 服务负责调度 Codex，监控中心负责展示执行过程，并通过独立任务助手处理咨询和自然语言控制提议。

## 系统架构

```text
钉钉指定人员/群聊 ── HTTPS/WSS 443 长连接 ──▶ 角色任务配置中心（8091）
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

中央 Python 网关与本机兼容 MCP 使用同一 SQLite 工作流运行库；阶段 B 的远程 Sidecar 只使用带机器认证的中央内部 API，不读取 SQLite，也不配置数据库账号。配置中心另外使用 MySQL 保存角色、SOP、任务定义和运行快照。

网关支持本机和远程多个主监督 app-server。每个主监督固定容量为 `1`：同一主监督的工作流按 `created_at + workflowId` 排队，不同主监督可以并行。远程主监督由 Sidecar 每 5 秒上报权威心跳，20 秒未收到心跳即离线；本机兼容模式继续使用轻量连接探测。配置中心提供独立“运行状态”页面，并在 SOP 主监督建议列表中显示在线空闲、在线忙碌、离线或状态未知。配置中心根据已登记的主监督筛选同组执行机；保存和提交都需要网关在线并校验能力、分组，提交时额外要求启用且检测通过。步骤执行机和工作目录不要求一致，空目录继承执行机默认值。

配置中心可选接入一个全局钉钉机器人，使用官方 Java SDK 主动建立长连接，无需公网入站端口。所有能接触机器人的用户均可发送 `@机器人 任务定义名称` 启动任务，后续通过 `工作流ID + 问题` 或引用任务消息对话，回答在提问会话中 @ 提问人。启动不带图片，后续对话可以将原图交给 Codex；确认按图返工后，原图交给重跑步骤及其后续步骤。同一任务定义仍只允许一个未结束运行。通知对象仅用于已启用主动通知的网页、定时运行，允许多个任务使用同一对象；钉钉启动在启动会话按顺序发送普通进度消息、工具调用、可读思考摘要和最终结果，不额外推送给配置对象。部署要求和限制见 [钉钉统一入口升级说明](docs/DINGTALK_UNIFIED_ENTRY_UPGRADE.zh-CN.md)。

## 任务助手、节点重跑与重试额度

任务运行中的“编排”和“对话控制”使用两条独立链路：

- 主监督继续负责严格串行编排步骤，并生成“开始第几步、步骤完成、最终结果”等任务进度消息。
- 独立任务助手只在用户发送消息时启动一次 turn，回复完成后回到空闲状态。首次咨询创建独立 thread，后续咨询恢复同一个 thread，不再向主监督执行 `turn/steer`。
- 助手固定使用只读沙箱、`approvalPolicy=never` 和结构化输出，只能回答、澄清或提出 `stop`、`skip`、`restart_from` 控制意图。所有实际状态变更仍由 Python 网关确定性执行。
- “确认执行”和“取消操作”由网关直接识别，不再调用模型；控制提议继续使用独立确认消息、10 分钟有效期和 `stateVersion` 并发校验。
- 任务助手使用独立 Orchestrator，不受主监督的执行机串行锁阻塞；同一个工作流的用户消息仍按接收顺序逐条处理。助手回复期间，监控页面仍会继续展示主监督的实时进度。

现在可以在监控中心使用自然语言提出节点流转请求，例如“从第 2 步重新执行”或“从报告步骤重新开始”。`restart_from` 的固定语义是保留所选步骤之前的成功或已跳过结果，重新执行所选步骤以及全部后续步骤。确认执行时系统会：

1. 校验更早步骤均已完成或跳过。
2. 再次校验没有业务步骤正在执行；执行中的停止、跳过和返工请求会被拒绝，不中断业务步骤。
3. 将旧结果、错误、时间、会话信息和图片归档为历史尝试。
4. 清空尾部步骤的当前结果并恢复为待执行，重新打开工作流并恢复主监督。

如果状态校验或事务执行失败，节点不会被部分重置，也不会扣减重跑额度。主界面只显示最新一次执行结果和当前有效文件，旧尝试保存在 SQLite 审计表和事件中。

SOP 可选择结果交接方式。默认的 `handoffMode: "legacy_text"` 把直接上一步的文字结果追加给下一步，适合纯文本串行任务；`handoffMode: "cumulative_files"` 不传递前一步文字结果或返工要求，而是把所有更早步骤的当前有效文件按来源步骤累计交给下一步。文件交接时，每次步骤尝试都在本机 `artifact_root` 下使用独立的 `inputs/step-N/` 与空 `output/` 目录，Agent 只接收绝对路径，不传递 Base64 文件内容；业务工作区不会被扫描。目标步骤及后续步骤返工时，旧文件归档，新尝试生成新的当前文件，下游只会收到当前有效版本。缺少 `handoffMode` 的历史快照继续按 `legacy_text` 运行。

用户提出返工并说明修改意见时，独立任务助手会把意见总结为不超过 4000 字符的可执行返工要求，并在二次确认消息中展示。确认后，网关把总结持久化并追加到目标步骤实际提示词末尾；用户原话和原始步骤提示词保持不变。多次返工要求按确认顺序累积，较新要求与旧要求冲突时以较新要求为准，提示词中的返工上下文最多 20,000 字符。

每个 SOP 配置 `maxRetryCount`，默认值为 `10`，允许范围为 `0–100`，并冻结到每次运行快照。一次确认的“从某步重跑到末尾”只消耗 1 次共享额度，无论重跑多少个尾部步骤；停止、跳过、提议、取消、重复确认以及系统自动调度均不消耗额度。工作流状态返回：

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

SOP 还可选择全自动或半自动流转。全自动保持步骤成功后立即继续的原行为；半自动会在成功步骤与下一步骤之间创建持久化的两分钟等待，监控页可以点击“确认继续”，也可以选择“保持等待”取消自动放行，之后再人工继续。暂停本身不会触发返工；结果不符合要求时，应在任务助手中说明修改点并完成二次确认。等待期间收到任务回复会先保持等待；无人回复时由 Python 运行时到期自动放行。钉钉投递计时与异常兜底见 [步骤流转模式](services/python-workflow/README.md#步骤流转模式)。最后一步、失败步骤和跳过步骤不会创建等待。流转方式会写入不可变运行快照，按原快照重试时保持不变。

## 仓库结构

```text
.
├── services/
│   ├── python-workflow/          Python 网关、MCP 编排器和状态存储
│   ├── workflow-console/         Java 任务运行监控中心
│   └── role-task-config-center/  Java 角色任务配置中心
├── config/                       执行机配置示例和本机配置
├── docs/                         部署与升级文档
├── prototypes/                   不参与运行的历史交互原型
└── scripts/                      运维和端到端验证脚本
```

各模块的详细说明：

- [Python 工作流服务](services/python-workflow/README.md)
- [任务运行监控中心](services/workflow-console/README.md)
- [角色任务配置中心](services/role-task-config-center/README.md)
- [完整部署指南](docs/DEPLOYMENT_GUIDE.zh-CN.md)
- [Codex 远程混合机部署与旧版升级执行手册](docs/CODEX_HYBRID_MACHINE_DEPLOYMENT.zh-CN.md)

## 快速启动

本分支只支持网页登记机器，中央 SQLite 保存分组、机器和检测结果；不读取旧执行机清单，也不提供导入。完整参数及凭据约定见[机器管理部署说明](docs/WEBUI_MACHINE_REGISTRATION.zh-CN.md)。

### 1. 准备统一部署配置

在中央仓库复制示例（已有文件直接编辑，勿覆盖），填写实际目录、原数据库路径和凭据引用：

```powershell
Copy-Item .\config\workflow-service.example.json .\config\workflow-service.json
```

远程主监督使用 `config/workflow-sidecar.example.json` 生成其本机同名文件，填写中央网关地址和独立机器凭据文件路径。纯执行机不需要 Sidecar。实际令牌保存在独立文件，不写入 JSON。

### 2. 启动 Python 服务

中央 Windows：

```powershell
.\scripts\start_workflow_gateway.ps1
```

远程主监督 Windows：

```powershell
.\scripts\start_workflow_sidecar.ps1
```

其他系统使用已有 Python 环境执行对应 `workflow_gateway.py` / `workflow_sidecar.py`；均自动读取本机仓库固定位置的服务配置。远程主监督的执行服务仍通过本机 `http://127.0.0.1:8082/mcp` 访问编排服务。配置方法见 [Python README](services/python-workflow/README.md#启动远程-sidecar)。

本机主监督保留 `local_db` 执行方式：统一配置 `machine_defaults.orchestration_mode=local_db`，本机编排进程运行 `services/python-workflow/src/codex_orchestrator_mcp.py`，与网关读取同一服务配置及同一 SQLite。无需机器清单环境变量。

启动 8091 后，在“机器管理”建立分组、登记 IP／端口／能力并手动检测，随后在 SOP 中选择同组机器。远程只支持 `legacy_text` 交接；`cumulative_files` 仍要求本机执行。

### 3. 启动两个 Java Web 应用

本项目 macOS 使用 `mvnd`，Windows 使用 `mvn`。下方 PowerShell 示例用于 Windows；macOS 启动、测试、打包和格式化时使用 `mvnd`，路径使用 `/`。

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
如需启用钉钉机器人，请先按配置中心 README 创建并发布对应企业内部应用、配置事件与权限，再在 8091 的机器人页面测试并保存配置。步骤间等待通知和等待期间正式回答使用已发布的单按钮卡片，其余消息沿用普通文本；模板及权限要求见配置中心 README。`DINGTALK_*` 环境变量只作为数据库尚无页面配置时的启动默认值。

## 文档维护

- 根 README 维护项目概览、快速启动和统一测试入口；模块 README 维护对应功能、接口、配置和当前限制。部署与升级步骤放在 `docs/`。
- [AGENTS.md](AGENTS.md) 维护 AI 修改仓库时的工作流程、架构约束、安全要求和交付检查。功能、接口或配置变化时更新对应 README；工作规则变化时更新 AGENTS。详细说明只维护一处，其他位置使用链接，关键约束可以简短重复。

## 跨机器会话记录

`history/` 只保存 Markdown 会话总结，并随 Git 提交，用于 macOS 与 Windows 之间交接。只有用户明确要求总结或写入 history 时才生成记录，不自动总结；复现代码放在模块测试目录或 `scripts/`，不放在 history。换电脑前提交并推送，另一台电脑同步同一分支后，先读取最新的日期命名总结，再核对当前分支和代码。命名、内容与读取规则见 [AGENTS.md](AGENTS.md#跨机器会话交接)。

## 测试与格式化

仓库没有根级聚合构建。优先运行与改动最接近的测试，再按影响范围扩大验证。

Python（从仓库根目录）：

优先检查并使用当前机器已有的根目录 `.venv`，不要假定操作系统、Python 小版本或依赖一定已安装。若未以 editable 方式安装本模块，显式设置 `PYTHONPATH`，避免测试找不到 `src` 中的模块。Windows 示例：

```powershell
$env:PYTHONPATH = "$PWD\services\python-workflow\src"
.\.venv\Scripts\python.exe -m unittest discover `
  -s services/python-workflow/tests `
  -t services/python-workflow -v
```

macOS/Linux（从仓库根目录）：

```sh
PYTHONPATH=services/python-workflow/src .venv/bin/python -m unittest discover \
  -s services/python-workflow/tests -t services/python-workflow -v
```

只有根目录 `.venv` 不存在或不可用时，才回退到以下跨平台 `uv` 命令；不要仅为了运行测试擅自安装 `uv` 或重建虚拟环境：

```sh
uv run --project services/python-workflow python -m unittest discover -s services/python-workflow/tests -t services/python-workflow -v
```

长任务集成验证需要可用的本地环境，同样优先使用根目录虚拟环境。Windows：

```powershell
.\.venv\Scripts\python.exe scripts/verify_long_job.py --delay-sec 3 --wait-sec 1
```

macOS/Linux：

```sh
.venv/bin/python scripts/verify_long_job.py --delay-sec 3 --wait-sec 1
```

根目录 `.venv` 不可用时再回退到：

```sh
uv run --project services/python-workflow python scripts/verify_long_job.py --delay-sec 3 --wait-sec 1
```

Java 命令按当前操作系统选择：macOS 使用 `mvnd`，Windows 使用 `mvn`；不要把另一台电脑的命令直接照搬到当前环境。

macOS（从仓库根目录）：

```sh
mvnd -f services/workflow-console/pom.xml test
mvnd -f services/role-task-config-center/pom.xml test
```

Windows（从仓库根目录）：

```powershell
mvn -f services/workflow-console/pom.xml test
mvn -f services/role-task-config-center/pom.xml test
```

Java 构建会在 `validate` 阶段检查格式。仅格式化实际修改过的 Java 模块；macOS：

```sh
mvnd -f services/workflow-console/pom.xml fmt:format
mvnd -f services/role-task-config-center/pom.xml fmt:format
```

Windows：

```powershell
mvn -f services/workflow-console/pom.xml fmt:format
mvn -f services/role-task-config-center/pom.xml fmt:format
```

不要为了通过格式检查而格式化整个仓库或改动无关文件。需要真实 MySQL、Codex app-server 或远程执行机的测试，如果环境不可用，应明确说明未运行原因，不能声称已验证。

## 运行与安全边界

- 三个服务（8080、8090、8091）默认监听 `0.0.0.0` 以支持可信内网访问；必须通过主机防火墙限制来源，三者都不能直接暴露到公网。只需本机访问时应显式改为 `127.0.0.1`。
- 钉钉机器人只需要服务端主动访问平台 HTTPS/WSS `443`；无需给本系统开放公网入站接口。
- 实际 `config/workflow-service.json`、SQLite 数据库、IDE 配置及构建产物均不提交 Git。
- 工作流队列和主监督租约持久化在 SQLite。网关重启时不会重新附着旧 Codex 会话：遗留的运行中或取消中工作流直接标记失败并清除租约，然后继续调度排队任务。
- 远程 Sidecar 令牌按主监督独立配置；内部 API 认证失败返回 `401`，跨主监督访问返回 `403`，对象不存在返回 `404`，旧实例、旧租约或状态冲突返回 `409`。停止 Sidecar 后，活动工作流会在 20 秒内失败并释放租约，不自动迁移。
- 远程机不得设置 `CODEX_WORKFLOW_DB` 或复制中央 SQLite；`8082` 不开放防火墙，`8080` 只允许 Java 服务和已登记的主监督机访问，并应位于可信内网、VPN 或 TLS 反向代理之后。
- `prototypes/` 仅保存早期页面方案，不是生产入口。
