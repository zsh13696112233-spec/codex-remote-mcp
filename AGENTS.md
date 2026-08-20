# AGENTS.md

本文件适用于整个仓库，供 Codex 在本项目中进行分析、修改和验证时使用。进入子模块后，还应先阅读该模块的 `README.md`；如果任务涉及业务边界或协议，继续阅读 `docs/` 中对应文档。

## 项目概览

这是一个 Codex 多执行机工作流编排平台，由三个可独立启动的服务组成：

- `services/python-workflow/`：Python 3.10+ 工作流网关、Codex Orchestrator MCP 和 SQLite 状态存储，默认端口 `8080`。
- `services/workflow-console/`：Java 17 / Spring Boot 监控中心，默认端口 `8090`。
- `services/role-task-config-center/`：Java 17 / Spring Boot 配置中心，使用 MySQL 8，默认端口 `8091`。

其他目录：

- `config/`：执行机配置模板；`agents.json` 是本机私有配置，不得提交。
- `docs/`：架构、协议、业务边界和验收要求。
- `scripts/`：端到端验证和运维辅助脚本。
- `prototypes/`：历史交互原型，不参与正式运行或构建。正式页面位于两个 Java 模块的 `src/main/resources/static/`。

## 开始工作前

1. 先运行 `git status --short`，把已有改动视为用户工作；不得覆盖、回退或顺手格式化无关文件。
2. 阅读根 `README.md` 和目标模块的 `README.md`。
3. 涉及工作流协议、状态、恢复或消息时，阅读 `docs/WORKFLOW_GUIDE.zh-CN.md`。
4. 涉及两个 Java 系统的职责、数据或页面能力时，阅读 `docs/TWO_JAVA_WEB_SYSTEMS_REQUIREMENTS.zh-CN.md`。
5. 先定位调用方、实现和测试，再修改跨服务接口。不要只改一端。

## 不可破坏的架构边界

- `8091` 配置中心负责角色、SOP、任务定义、运行快照以及提交、取消、重试等控制操作；浏览器不得直接调用 Python 网关。
- `8090` 监控中心只展示 URL 中指定的单个工作流，并代理进度、事件和任务助手消息；不得增加任务编辑、直接提交、直接取消、直接重试、直接跳过等控制接口，也不得连接配置中心的 MySQL。
- `8080` Python 网关是 Java 系统访问工作流运行时的 HTTP 边界；Java 不直接调用 MCP、Python 脚本或 app-server WebSocket。
- 网关与 MCP 进程必须使用完全相同的 `CODEX_WORKFLOW_DB` 绝对路径。变更状态模型时，同时检查 `workflow_store.py`、网关、MCP、Java 客户端和前端状态映射。
- 节点执行顺序由 `dependsOn` 决定，不能依赖 JSON 数组顺序。依赖未全部成功时不得启动后续节点。
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
- SQLite 是网关与 MCP 的跨进程共享状态，不要把关键持久状态只放在进程内存中。修改表结构时必须兼容已有数据库，并补充存储层测试。
- 保持结果与提示词容量限制：单结果 `20,000`、依赖结果合计 `40,000`、最终提示词 `100,000` 字符；截断必须有明确提示。
- 不要吞掉业务异常。协议读取循环中确需隔离的监控/回调异常，应保留现有“不能中断主流程”的边界并记录可诊断状态。

### Java 配置中心

- 根包为 `com.codexflow.configcenter`，按 `web`、`application`、`domain`、`client` 分层；新增代码放入职责匹配的包中。
- REST DTO 与 JPA 实体分离；跨服务 JSON 映射集中维护，不要把网关协议散落到控制器或前端。
- 数据库结构只通过 `src/main/resources/db/migration/` 下新的 Flyway 迁移演进。不要修改已应用迁移来修补正式数据库，不要启用 Hibernate 自动建表；保持 `ddl-auto=validate`。
- 运行记录和提交 JSON 是不可变快照。重试历史运行时使用原快照，使用最新配置运行时生成新快照和新 `workflowId`。
- 正式环境行为以 MySQL 8 为准；测试使用 H2 MySQL 兼容模式，涉及 SQL/Flyway 的改动需考虑两者差异。

### Java 监控中心

- 根包为 `com.codexflow.console`。控制器只暴露 README 记录的读取和消息代理接口。
- 保持“一个页面只查看一个 `workflowId`”的产品边界；没有 ID 时不要展示全局任务列表。
- 网关离线、工作流不存在或响应异常时，通过统一异常映射返回稳定且不泄露内部信息的结果。
- 前端状态文字、轮询停止条件和后端状态集合必须同步更新。

### 前端静态资源

- 两个 Java 应用都使用原生 HTML、CSS、JavaScript，没有 Node 构建步骤；沿用当前结构，不为小改动引入前端框架或打包器。
- 浏览器只访问所属 Java 服务的 `/api`，不要直连 `8080` 或数据库。
- 修改交互时同时检查加载、空状态、错误状态、终态、重复提交和刷新恢复行为。
- `prototypes/` 仅作视觉参考；除非任务明确要求，不要把修复只做在原型中。

## API、状态与数据兼容性

- 跨服务字段名、状态值、HTTP 状态码、SSE 事件和错误语义属于公共契约。变更时同步更新生产代码、客户端映射、前端、测试和文档。
- 对外展示和内部状态要分层：内部可保留详细诊断信息，对普通用户的页面和助手消息不得泄露执行机地址、令牌、会话编号或原始异常细节。
- 保持时间、排序和增量游标的稳定性；事件查询的 `after` 是断点续传边界，不能因分页实现造成重复遗漏。
- 不要用破坏性方式“修复”本地数据库。任何删除库、清表、移除 Flyway 记录的操作都必须先确认目标是无业务数据的开发环境，并获得用户明确授权。

## 配置与安全

- 不读取、打印或提交 `config/agents.json`、`.env`、数据库文件、访问令牌或数据库密码。需要示例时只修改 `config/agents.example.json`，使用虚构值和 `token_env`。
- 不支持在执行机 JSON 中直接写 `token`；只允许通过 `token_env` 引用环境变量名。
- 服务默认监听 `127.0.0.1`。除非任务明确包含安全部署方案，不要改成公网监听，也不要建议直接暴露 `8080`、`8090` 或 `8091`。
- 尊重 `allow_write` 和 `allow_cwd_override`；不能通过请求参数绕过执行机侧限制。
- 不提交 `.venv/`、`.uv-cache/`、`target/`、`*.db*`、IDE 文件或其他 `.gitignore` 中的本地产物。

## 构建与测试

仓库没有根级聚合构建。优先运行与改动最接近的测试，再按影响范围扩大验证。

Python（从仓库根目录）：

```sh
uv run --project services/python-workflow \
  python -m unittest discover -s services/python-workflow/tests -v
```

长任务集成验证（需要可用的本地环境时）：

```sh
uv run --project services/python-workflow \
  python scripts/verify_long_job.py --delay-sec 3 --wait-sec 1
```

Java 监控中心：

```sh
mvn -f services/workflow-console/pom.xml test
```

Java 配置中心：

```sh
mvn -f services/role-task-config-center/pom.xml test
```

Java 构建会在 `validate` 阶段检查格式。仅格式化实际修改过的 Java 模块：

```sh
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
