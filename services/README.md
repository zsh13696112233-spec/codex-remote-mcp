# 服务模块

本目录包含三个可独立启动的服务：

- `python-workflow`：工作流网关、MCP 编排器和 SQLite 状态存储。
- `workflow-console`：端口 8090 的任务运行监控中心。
- `role-task-config-center`：端口 8091 的角色、SOP 和任务配置中心。

整体启动顺序和依赖关系见仓库根目录 `README.md`，具体配置见各模块自己的 `README.md`。
