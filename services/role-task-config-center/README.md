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

- 角色的新建、编辑、搜索、启停和删除。
- 被 SOP 引用的角色禁止删除，只能停用。
- 可视化严格串行 SOP 编辑器：左侧选择工作流，从角色库拖入画布生成节点，并通过拖拽调整执行顺序。
- 页面右侧集中配置工作流参数和节点的模型、执行机、目录、超时、Skill/MCP 等属性。
- 主监督执行机固定为本地，期望输出由系统自动生成，配置界面不显示这两个内部字段。
- SOP 默认模型以及单步骤模型覆盖。
- SOP 可配置单次工作流的人工重跑总额度，默认 `10`，范围 `0–100`。
- 工作目录、执行机、写权限、超时和 Skill/MCP 标签配置。
- 任务定义的新建、编辑、复制、搜索、软删除和重复运行。
- 保存每次运行的不可变配置快照和完整提交 JSON。
- 使用最新配置运行、按原运行快照重试、取消运行和查看历史记录。

按原运行快照重试整项任务会生成新的 `workflowId`，沿用快照中的最大重跑额度，并从已使用 `0` 次开始计算。

第一版失败策略固定为 `stop`。Skill 和 MCP 仅作为配置标签保存和展示，不影响真实执行权限。

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

启动后访问：

```text
http://127.0.0.1:8091
```

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
| `SERVER_ADDRESS` | `127.0.0.1` | 监听地址 |
| `MYSQL_URL` | 本机 `codex_config` JDBC 地址 | MySQL JDBC URL |
| `MYSQL_USERNAME` | `codex` | 数据库用户名 |
| `MYSQL_PASSWORD` | 无 | 数据库密码，必须显式设置 |
| `CODEX_GATEWAY_URL` | `http://127.0.0.1:8080` | Python 网关地址 |
| `WORKFLOW_MONITOR_URL` | `http://127.0.0.1:8090` | 监控中心地址 |
| `DEFAULT_STEP_MODEL` | `gpt-5.6-sol` | SOP 默认步骤模型 |

## REST 接口

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
POST   /api/task-runs/{workflowId}/cancel
POST   /api/task-runs/{workflowId}/retry

GET    /api/agents
GET    /api/gateway/ready
```

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

系统第一版没有登录和多用户权限。默认仅监听回环地址；如需设置 `SERVER_ADDRESS=0.0.0.0`，必须部署在可信内网或受保护的反向代理后，不得直接暴露到公网。
