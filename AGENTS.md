# AGENTS.md

本文件适用于整个仓库，供 Codex 在本项目中进行分析、修改和验证时使用。进入子模块后，还应先阅读该模块的 `README.md`；业务边界和协议以本文件、根 README 及对应模块 README 为入口，部署与升级说明位于 `docs/`。

## 项目概览

这是一个 Codex 多执行机工作流编排平台，由三个可独立启动的服务组成：

- `services/python-workflow/`：Python 3.10+ 工作流网关、Codex Orchestrator MCP 和 SQLite 状态存储，默认端口 `8080`。
- `services/workflow-console/`：Java 17 / Spring Boot 监控中心，默认端口 `8090`。
- `services/role-task-config-center/`：Java 17 / Spring Boot 配置中心，使用 MySQL 8，默认端口 `8091`。

其他目录：

- `config/`：执行机配置模板；`agents.json` 是本机私有配置，不得提交。
- `docs/`：仅保留部署与升级文档。
- `scripts/`：端到端验证和运维辅助脚本。
- `prototypes/`：历史交互原型，不参与正式运行或构建。正式页面位于两个 Java 模块的 `src/main/resources/static/`。

## 开始工作前

1. 先运行 `git status --short`，把已有改动视为用户工作；不得覆盖、回退或顺手格式化无关文件。
2. 读取根目录 `history/` 中文件名符合 `YYYY-MM-DD_HH-mm-ss_主题.md` 的最新一条会话总结（按文件名时间降序，使用北京时间），再阅读根 `README.md` 和目标模块的 `README.md`。目录或总结不存在时跳过；需要追溯时再读取更早记录。总结只作为交接线索，先核对其中的分支、提交和当前代码，不能代替本次用户要求及代码事实。
3. 涉及工作流协议、状态、恢复或消息时，阅读 `services/python-workflow/README.md`，并核对实现和对应测试。
4. 涉及两个 Java 系统的职责、数据或页面能力时，阅读两个 Java 模块的 `README.md`，并遵守下述架构边界。
5. 先定位调用方、实现和测试，再修改跨服务接口。不要只改一端。

## 跨机器会话交接

- `history/` 纳入 Git 版本管理，用于 macOS 与 Windows 两台电脑之间交接。用户授权提交时，将相关会话总结一并提交；生成总结本身不代表可以擅自提交其他改动或推送。
- 换电脑前提交并推送需要交接的改动；另一台电脑开始前先同步同一分支，再读取最新总结。仅本地提交不会同步到另一台电脑；同步前检查未提交改动，不覆盖另一台电脑的工作。
- 只有用户明确要求“总结本次会话”或“写入 history”时，才新建 `history/YYYY-MM-DD_HH-mm-ss_主题.md`，不覆盖旧记录。普通问答、完成修改、提交代码、切换电脑或会话结束都不触发自动总结；不得自行新建或补写总结。
- 总结记录：本次目的、操作系统、当前分支及基线提交、实际修改及原因、测试命令和结果、已知问题、尚未完成的事项、下一步建议。明确区分已经完成、仅审查发现、尚未验证，注明未提交改动。
- `history/` 只保存 Markdown 会话总结，不放 Python、Java 等代码或其他附件。需要保留的复现代码放在对应模块测试目录或 `scripts/`，总结仅引用其路径。不复制完整对话、敏感配置、令牌、数据库内容或原始业务消息；路径优先使用仓库相对路径，命令注明 macOS/Windows 差异。开始会话只自动读取最新的日期命名 Markdown 总结。

## 不可破坏的架构边界

- `8091` 配置中心负责角色、SOP、任务定义、运行快照以及提交、取消、重试等控制操作；浏览器不得直接调用 Python 网关。
- `8090` 监控中心只展示 URL 中指定的单个工作流，并代理进度、事件和任务助手消息；不得增加任务编辑、直接提交、直接取消、直接重试、直接跳过等控制接口，也不得连接配置中心的 MySQL。
- `8080` Python 网关是 Java 系统访问工作流运行时的 HTTP 边界；Java 不直接调用 MCP、Python 脚本或 app-server WebSocket。
- 本机 `local_db` 模式的网关与 MCP 进程必须使用完全相同的 `CODEX_WORKFLOW_DB` 绝对路径；远程 `remote_sidecar` 通过带机器认证和租约的内部 HTTP API 访问中央状态，不配置或读取 SQLite。变更状态模型时，同时检查 `workflow_store.py`、网关、MCP、Java 客户端和前端状态映射。
- 节点执行顺序由 `dependsOn` 决定，不能依赖 JSON 数组顺序。依赖未全部完成或经确认跳过时不得启动后续节点。
- 主监督会话负责编排，不代替业务节点完成任务。用户可见消息应使用普通中文和“步骤”等业务说法，不暴露 MCP、thread、turn、agent、内部英文状态码或原始事件 JSON。
- Skill/MCP 字段当前只是配置标签：不得据此自动安装、启用、授予权限或注入提示词。
- 第一版配置中心生成严格串行 SOP，失败策略固定为 `stop`；除非需求明确变化，不要擅自扩展为通用 DAG 编辑器。

## 各模块修改约定

### Python 工作流服务

- 主要文件：
  - `workflow_gateway.py`：HTTP、SSE、主监督会话和聊天工作线程。
  - `codex_orchestrator_mcp.py`：MCP 工具、执行机配置和 app-server WebSocket 客户端。
  - `workflow_store.py`：SQLite 模型、状态转换、事件、聊天和控制动作。
- 保持 Python 3.10 兼容；沿用现有类型注解、异步模式、标准库 `unittest` 和中文业务错误信息。
- 所有外部输入都应在边界处验证，包括 ID、状态、超时、路径、写权限、消息长度和执行机 ID。
- 保持工作流与消息的幂等语义。尤其不要破坏 `workflowId` 唯一性、聊天 `messageId` 重试复用以及控制动作的二次确认机制。
- SQLite 是中央网关与本机兼容 MCP 的跨进程共享状态，远程 Sidecar 只能通过中央内部 API 读写；不要把关键持久状态只放在进程内存中。修改表结构时必须兼容已有数据库，并补充存储层测试。
- 保持结果与提示词容量限制：单结果 `20,000`、依赖结果合计 `40,000`、最终提示词 `100,000` 字符；截断必须有明确提示。
- 不要吞掉业务异常。协议读取循环中确需隔离的监控/回调异常，应保留现有“不能中断主流程”的边界并记录可诊断状态。

### Java 配置中心

- 根包为 `com.codexflow.configcenter`，按 `web`、`application`、`domain`、`client` 分层；新增代码放入职责匹配的包中。
- REST DTO 与 JPA 实体分离；跨服务 JSON 映射集中维护，不要把网关协议散落到控制器或前端。
- 数据库结构只通过 `src/main/resources/db/migration/` 下新的 Flyway 迁移演进。不要修改已应用迁移来修补正式数据库，不要启用 Hibernate 自动建表；保持 `ddl-auto=validate`。
- 运行记录和提交 JSON 是不可变快照。重试历史运行时使用原快照，使用最新配置运行时生成新快照和新 `workflowId`。
- 正式环境行为以 MySQL 8 为准；测试使用 H2 MySQL 兼容模式，涉及 SQL/Flyway 的改动需考虑两者差异。

### Java 监控中心

- 根包为 `com.codexflow.console`。控制器只暴露 README 记录的读取、消息代理和半自动等待确认/暂停接口。
- 保持“一个页面只查看一个 `workflowId`”的产品边界；没有 ID 时不要展示全局任务列表。
- 网关离线、工作流不存在或响应异常时，通过统一异常映射返回稳定且不泄露内部信息的结果。
- 前端状态文字、轮询停止条件和后端状态集合必须同步更新。

### 前端静态资源

- 两个 Java 应用都使用原生 HTML、CSS、JavaScript，没有 Node 构建步骤；沿用当前结构，不为小改动引入前端框架或打包器。
- 浏览器只访问所属 Java 服务的 `/api`，不要直连 `8080` 或数据库。
- 修改交互时同时检查加载、空状态、错误状态、终态、重复提交和刷新恢复行为。
- `prototypes/` 仅作视觉参考；除非任务明确要求，不要把修复只做在原型中。

## 已知实现与安全约定的差异

- 钉钉 v2 当前会直接展示业务工具的路径、命令、参数和文本结果（见配置中心 README 的工具详情说明），只过滤图片传输字段；这不等于已经满足下文“不泄露令牌、执行机地址和原始内部信息”的约定。涉及消息展示时必须核对这项差异，不得仅凭工具名或方法名宣称已脱敏。

## API、状态与数据兼容性

- 跨服务字段名、状态值、HTTP 状态码、SSE 事件和错误语义属于公共契约。变更时同步更新生产代码、客户端映射、前端、测试和文档。
- 对外展示和内部状态要分层：内部可保留详细诊断信息，对普通用户的页面和助手消息不得泄露执行机地址、令牌、会话编号或原始异常细节。
- 保持时间、排序和增量游标的稳定性；事件查询的 `after` 是断点续传边界，不能因分页实现造成重复遗漏。
- 不要用破坏性方式“修复”本地数据库。任何删除库、清表、移除 Flyway 记录的操作都必须先确认目标是无业务数据的开发环境，并获得用户明确授权。

## 配置与安全

- 不读取、打印或提交 `config/agents.json`、`.env`、数据库文件、访问令牌或数据库密码。需要示例时只修改 `config/agents.example.json`，使用虚构值和 `token_env`。
- 不支持在执行机 JSON 中直接写 `token`；使用 `token_env` 引用环境变量名，或 `token_file` 引用服务所在机器上的绝对文件路径，严格二选一。远程机器认证另用 `sidecar_token_env` / `sidecar_token_file`；不得读取或回显实际令牌文件内容。
- 三个服务默认监听 `0.0.0.0` 以支持可信内网访问，必须通过主机防火墙限制来源。不要建议将 `8080`、`8090` 或 `8091` 直接暴露到公网；只需本机访问时应显式改为 `127.0.0.1`。
- 尊重 `allow_write` 和 `allow_cwd_override`；不能通过请求参数绕过执行机侧限制。
- 不提交 `.venv/`、`.uv-cache/`、`target/`、`*.db*`、IDE 文件或其他 `.gitignore` 中的本地产物。

## 构建与测试

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

仅限 Windows：如果根目录 `.venv\Scripts\python.exe` 存在，但 Codex 沙箱因无法执行用户 `AppData` 下的基础 Python 而报“拒绝访问”或 `Unable to create process`，应申请在沙箱外执行同一条 `.venv` 命令。不要把 Codex 缓存目录中的内置 Python 路径写死到项目文档或脚本中。此规则不适用于 macOS；macOS 继续使用其本机项目环境和下述跨平台回退方式。

只有根目录 `.venv` 不存在或不可用时，才回退到以下跨平台 `uv` 命令；不要仅为了运行测试擅自安装 `uv` 或重建虚拟环境：

```sh
uv run --project services/python-workflow \
  python -m unittest discover -s services/python-workflow/tests \
  -t services/python-workflow -v
```

长任务集成验证（需要可用的本地环境时）同样优先使用根目录虚拟环境：

```powershell
.\.venv\Scripts\python.exe scripts/verify_long_job.py --delay-sec 3 --wait-sec 1
```

根目录 `.venv` 不可用时再回退到：

```sh
uv run --project services/python-workflow \
  python scripts/verify_long_job.py --delay-sec 3 --wait-sec 1
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

## 完成标准

- 实现保持上述服务职责与安全边界。
- 新行为有针对性测试；修复缺陷时优先增加能复现问题的回归测试。
- 相关测试通过，或清楚列出未运行项及原因。
- 接口、配置、启动方式或用户行为变化时，同步更新对应 README/`docs/`。
- 最后检查 `git diff --check` 和 `git status --short`，确认没有秘密、本地产物或无关改动。
- 向用户交付时简述改了什么、验证了什么以及仍存在的环境限制；不要自动提交或推送，除非用户明确要求。
