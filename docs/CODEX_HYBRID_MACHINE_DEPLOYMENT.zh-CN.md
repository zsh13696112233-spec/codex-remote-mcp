# Codex 远程混合机部署与旧版升级执行手册

本文直接提供给目标机器上的 Codex 使用。目标是把一台 Windows 机器部署为“远程主监督 + 业务执行机”混合机器，或者把已经安装旧分支、旧 stdio MCP 的机器原地升级为当前 HTTP Sidecar 架构。

完整架构、中央服务和纯执行机部署仍以[完整部署指南](DEPLOYMENT_GUIDE.zh-CN.md)为准。本文只描述目标混合机以及中央机必须配合完成的最小操作。

## 一、Codex 执行约束

Codex 开始操作前必须遵守以下约束：

- 先阅读仓库根目录的 `AGENTS.md`、`README.md`、本文和 `services/python-workflow/README.md`。
- 不读取、显示、复制到聊天、写入日志或提交任何 Token 内容。
- 不把真实 Token 写入 JSON、TOML、PowerShell 脚本或 Markdown。
- 不提交 `*.token`、`config/workflow-service.json`、`.env` 或数据库文件。
- 不使用 `git reset --hard`、`git checkout --` 或其他会丢失旧部署修改的命令。
- 如果仓库有未提交修改，先停止升级并报告文件清单，等待用户决定如何处理。
- 不删除旧分支。切换新分支成功后保留旧分支作为回退入口。
- 不在仍有活动工作流时停止网关、Sidecar 或 app-server。
- 未完成中央登记、令牌同步和端到端验证时，不得声称部署成功。

## 二、开始前需要用户提供的信息

Codex 必须先取得以下信息。缺少任何必填项时只询问缺少的值，不猜测：

| 变量 | 示例 | 必填 |
| --- | --- | --- |
| `REPOSITORY_URL` | `https://git.example/codex-remote-mcp.git` | 是 |
| `TARGET_BRANCH` | `codex/webui-machine-registration` | 是 |
| `TARGET_COMMIT` | 完整 Git 提交号 | 是 |
| `CENTRAL_GATEWAY_URL` | `http://192.168.1.10:8080` | 是 |
| `GROUP_NAME` | `业务一组` | 是 |
| `MACHINE_IP` | `192.168.1.22` | 是 |
| `PROJECT_ROOT` | `D:\services\codex-remote-mcp` | 是 |
| `WORKSPACE_ROOT` | `D:\codex-workspaces\hybrid-02` | 是 |
| `APP_SERVER_TOKEN_FILE` | `C:\codex-secrets\app-server-shared.token` | 是 |
| `SIDECAR_TOKEN_FILE` | `C:\codex-secrets\hybrid-02-sidecar.token` | 是 |

`TARGET_COMMIT` 必须是已经推送到远程仓库的准确提交。不能只依赖分支当前最新位置，避免不同机器部署到不同版本。

## 三、最简令牌方案

系统存在两类用途不同的 Token：

1. app-server Token：连接每台机器的 `4500`。
2. Sidecar Token：Sidecar 调用中央网关 `8080` 的内部 API。

为了减少运维数量，可以让所有 `4500` app-server 使用同一个共享 app-server Token，但它仍必须保存在 Git 之外。共享意味着任意一台机器泄露后都要轮换所有 app-server，因此正式环境仍推荐每台机器独立。

每台远程主监督的 Sidecar Token 必须不同。中央网关使用 Token 唯一识别主监督；同一个 Sidecar Token 如果配置给多个 agent，认证会因为不能唯一匹配而失败。

因此，最简可用数量是：

- 一个全局共享 app-server Token。
- 每台远程主监督一个独立 Sidecar Token。

执行服务的客户端凭据引用路径必须在中央和派发方统一；服务端监听凭据文件及 Sidecar 身份凭据文件的路径可以不同，但对应内容必须一致：

| Token | 中央机 | 目标混合机 |
| --- | --- | --- |
| 共享 app-server Token | 网关读取的本地文件 | app-server 和本机 Sidecar 读取的本地文件 |
| 本机 Sidecar Token | 网关读取的该 agent 专属文件 | 本机 Sidecar 读取的本地文件 |

Token 文件必须是 UTF-8 单行文本。通过受控文件共享、密钥系统或人工安全复制同步；禁止通过 Git、聊天消息或命令行参数传递原文。

如果目标机缺少共享 app-server Token，Codex 不得自行生成一个不同的值。应停止并要求用户安全复制中央机正在使用的共享文件。

如果目标 agent 尚无 Sidecar Token，可以在目标机生成一个高熵随机值并只写入 `SIDECAR_TOKEN_FILE`，但必须通过安全渠道把同一内容同步到中央机对应文件后才能继续。生成过程不得把值打印到终端。

## 四、检查旧部署

在 `PROJECT_ROOT` 执行只读检查：

```powershell
git status --short --branch
git remote -v
git branch --show-current
git rev-parse HEAD
codex --version
```

同时检查但不要输出敏感内容：

- 端口 `4500`、`8082` 是否已监听。
- 是否存在旧 app-server、旧 MCP 或 Sidecar 进程。
- `~/.codex/config.toml` 是否存在 `codex_orchestrator`。
- 旧配置是否包含 `command`、`args`、`cwd` 或 `[mcp_servers.codex_orchestrator.env]`。
- 远程机器是否错误配置了 `CODEX_WORKFLOW_DB`。
- `APP_SERVER_TOKEN_FILE`、`SIDECAR_TOKEN_FILE` 是否存在、是普通文件且非空；只报告检查结果，不报告内容。

工作区不干净时停止，不要自动 stash、提交或丢弃用户修改。

## 五、切换到指定代码版本

确认工作区干净且当前没有活动工作流后：

```powershell
git fetch --all --prune
git switch <TARGET_BRANCH>
git pull --ff-only
git checkout --detach <TARGET_COMMIT>
git rev-parse HEAD
```

最后一个提交号必须与 `TARGET_COMMIT` 完全一致。使用 detached HEAD 是为了部署固定版本，不删除也不修改旧分支。

如果目标分支不存在、提交无法取得或 `pull --ff-only` 失败，立即停止并报告，不要合并旧分支。

## 六、准备运行环境和目录

仓库启动脚本固定使用根目录 `.venv`。在仓库根目录创建或同步该环境：

```powershell
if (-not (Test-Path -LiteralPath '.\.venv\Scripts\python.exe' -PathType Leaf)) {
  uv venv .venv --python 3.10
}
uv pip install --python .\.venv\Scripts\python.exe -e .\services\python-workflow
```

创建本机运行目录：

```powershell
New-Item -ItemType Directory -Force -Path '<WORKSPACE_ROOT>'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent '<APP_SERVER_TOKEN_FILE>')
New-Item -ItemType Directory -Force -Path (Split-Path -Parent '<SIDECAR_TOKEN_FILE>')
```

目录创建后限制 ACL，只允许运行服务的 Windows 账号和管理员读取令牌文件。不得修改或覆盖已经存在的令牌内容。

远程混合机不得部署以下组件：

- 中央工作流网关 `8080`。
- 配置中心 `8091`。
- 监控中心 `8090`。
- MySQL 或中央 SQLite。

远程混合机的服务配置不填写 `workflow_db`，不访问中央 SQLite。

## 七、准备目标机服务配置

将 `config/workflow-sidecar.example.json` 复制为目标机仓库的 `config/workflow-service.json`，填写中央地址及自身 Sidecar 凭据文件路径。该文件不纳入 Git；不配置 SQLite、不维护独立执行机清单。完整字段约定见[机器管理部署说明](WEBUI_MACHINE_REGISTRATION.zh-CN.md)。

## 八、把旧 stdio MCP 迁移为 HTTP Sidecar

编辑运行 app-server 的 Windows 账号所使用的 `~/.codex/config.toml`。

旧配置通常包含：

```toml
[mcp_servers.codex_orchestrator]
command = "uv"
args = ["run", "..."]
cwd = "..."

[mcp_servers.codex_orchestrator.env]
CODEX_WORKFLOW_DB = "..."
```

删除旧 `codex_orchestrator` 主表中的 `command`、`args`、`cwd`，并删除整个 `[mcp_servers.codex_orchestrator.env]` 子段。最终配置必须是：

```toml
[mcp_servers.codex_orchestrator]
url = "http://127.0.0.1:8082/mcp"
required = true
enabled_tools = ["dispatch_node", "wait_node", "node_status", "cancel_node", "workflow_status"]
default_tools_approval_mode = "approve"
```

不要同时保留 stdio 和 HTTP 两种同名配置。迁移不需要重新安装另一套 MCP；只是把 Codex 的连接方式由本地 stdio 子进程改成常驻 HTTP Sidecar。`enabled_tools` 把主监督限制在五个编排工具内，`default_tools_approval_mode = "approve"` 只预批准这些工具，避免无人值守任务为每次派发和等待启动一次 Auto-review。

修改前为 `config.toml` 创建带时间戳的本机备份，但不得提交备份。

## 九、中央网页登记

中央准备服务配置后，在 8091 “机器管理”建立组，按目标 IP、执行服务端口登记机器，同时选择主监督和执行机能力。中央凭据路径模板解析出的文件内容需与目标 Sidecar 身份凭据一致。启动目标服务后手动检测，检测通过才能运行。

机器编号由中央生成，无需复制到远程；远程按认证身份获取同组清单。不支持旧机器导入。

## 十、停止旧服务并启动新服务

确认没有活动工作流后，停止当前机器上的旧 app-server、旧 stdio MCP 和旧 Sidecar。只按已确认的进程 ID 停止，不按模糊进程名批量终止。

先在独立的受控后台进程、服务或终端中启动 Sidecar：

```powershell
.\scripts\start_workflow_sidecar.ps1
```

Sidecar 必须只监听 `127.0.0.1:8082`，不得对内网或公网开放 `8082`。

确认 `8082` 已监听后，再在另一个独立的受控后台进程、服务或终端中启动 app-server：

```powershell
codex app-server `
  --listen ws://0.0.0.0:4500 `
  --ws-auth capability-token `
  --ws-token-file '<APP_SERVER_TOKEN_FILE>'
```

OpenAI 官方文档要求非回环 WebSocket 监听配置鉴权，并建议通过 `--ws-token-file` 读取令牌，避免把原始值放在命令行中。参见 [Codex App Server](https://learn.chatgpt.com/zh-Hans/docs/app-server)。

目标机防火墙只允许中央网关以及需要向本机派发业务步骤的已登记主监督访问 `4500`。可信内网使用明文 WebSocket 是当前部署约定；如果链路离开可信内网，应改为 VPN、TLS 反向代理或 `wss://`。

长期运行时应把两个启动命令注册为受控服务或计划任务，使用固定服务账号并配置失败重启；不要依赖临时终端窗口。

## 十一、验证

Codex 必须依次完成以下验证：

1. `TARGET_COMMIT` 与 `git rev-parse HEAD` 完全一致。
2. `config/workflow-service.json` 能解析，只包含凭据文件引用，中央已完成网页登记和检测。
3. `~/.codex/config.toml` 只保留 HTTP `url`，不存在旧 stdio 字段和旧 MCP `env` 子段。
4. 当前机器没有 `CODEX_WORKFLOW_DB`。
5. `GET http://127.0.0.1:4500/readyz` 返回成功。
6. `127.0.0.1:8082` 正在监听，其他网卡没有监听 `8082`。
7. `GET <CENTRAL_GATEWAY_URL>/readyz` 返回成功。
8. 中央 `GET /agents` 显示 `<AGENT_ID>` 为 `online` 和 `idle`。
9. 重启 Codex 后，`codex_orchestrator` MCP initialize 成功。
10. MCP 工具列表包含 `dispatch_node`、`wait_node`、状态和取消相关工具。
11. 提交一个只读、单步骤、`handoffMode: "legacy_text"` 的测试工作流，预期返回 `PHASE_B_OK`。
12. 测试工作流进入完成终态，并且中央租约已释放。

远程主监督当前不支持 `cumulative_files`。不要使用带跨机器文件交接的工作流作为验收用例。

## 十二、失败处理与最终报告

任何一步失败时保留现场，收集不含秘密的状态和错误摘要。不要通过打印配置文件或 Token 排查认证问题；只比较文件是否存在、长度是否合理，并可在两端比较安全哈希是否一致，禁止输出原文。

Codex 最终必须报告：

- 升级前分支和提交号。
- 部署后的固定提交号。
- 工作区是否干净。
- 是否发现并移除了旧 stdio MCP 配置。
- 是否确认两类 Token 文件存在且未进入 Git；不得报告其内容。
- `4500`、`8082`、中央 `8080` 的检查结果。
- 中央显示的 agent 在线和空闲状态。
- MCP 工具加载结果。
- 最小 `legacy_text` 工作流结果。
- 尚需中央管理员或用户处理的事项。

只有上述验证全部成功，才能报告这台混合机部署完成。
