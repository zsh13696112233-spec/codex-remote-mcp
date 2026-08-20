# Codex 工作流编排平台

本仓库是一套由 Python 工作流服务和两个 Java Web 应用组成的 Codex 多执行机编排平台。配置中心负责定义并提交任务，Python 服务负责调度 Codex，监控中心负责展示执行过程并与主监督会话交互。

## 系统架构

```text
角色任务配置中心（8091）
          │ 提交工作流
          ▼
Python 工作流网关（8080）── Codex Orchestrator MCP ── Codex App Server
          ▲
          │ 查询、事件和对话
任务运行监控中心（8090）
```

Python 网关与 MCP 进程通过同一个 SQLite 数据库共享工作流、节点、消息和事件状态。配置中心另外使用 MySQL 保存角色、SOP、任务定义和运行快照。

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
- [完整工作流指南](docs/WORKFLOW_GUIDE.zh-CN.md)

## 快速启动

### 1. 准备执行机配置

```powershell
Copy-Item .\config\agents.example.json .\config\agents.json
```

编辑 `config/agents.json`，为每个执行机配置 Codex app-server WebSocket 地址、默认工作目录和权限。访问令牌只填写环境变量名，不要直接写入 JSON。

### 2. 启动 Python 网关

网关和 MCP 必须使用同一个 `CODEX_WORKFLOW_DB` 绝对路径。

```powershell
$ProjectRoot = (Resolve-Path .).Path
$env:CODEX_AGENTS_FILE = (Resolve-Path .\config\agents.json).Path
$env:CODEX_WORKFLOW_DB = Join-Path $ProjectRoot "workflows.db"

uv run --project .\services\python-workflow `
  python .\services\python-workflow\src\workflow_gateway.py `
  --host 127.0.0.1 --port 8080 `
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

## 测试

Python：

```powershell
uv run --project .\services\python-workflow `
  python -m unittest discover -s .\services\python-workflow\tests -v
```

Java：

```powershell
mvn -f .\services\workflow-console\pom.xml test
mvn -f .\services\role-task-config-center\pom.xml test
```

## 运行与安全边界

- 三个 HTTP 服务默认仅监听本机回环地址，不能直接暴露到公网。
- `config/agents.json`、SQLite 数据库、IDE 配置及构建产物均不提交 Git。
- 当前任务运行中的异步作业仍保存在 Python 进程内存；进程重启后历史状态仍在，但不会自动重新附着到运行中的 Codex turn。
- `prototypes/` 仅保存早期页面方案，不是生产入口。
