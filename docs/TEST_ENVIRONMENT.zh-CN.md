# macOS 独立测试环境

本说明适用于 `codex/test-environment` worktree。正式目录的服务不参与测试环境启动、停止或迁移。

## 路径与连接

- 中央配置：`config/workflow-service.json`，仅本机保存。
- SQLite：根目录 `workflows.db`，中央网关与本机编排进程按相同服务配置解析路径。
- 包存储：根目录 `skill-packages/`、`mcp-packages/`，不使用正式环境的共享包目录。
- 日志：`logs/`；进程编号：`.runtime/run/`；临时文件：`.runtime/tmp/`。
- MySQL 凭据：`config/local/mysql.properties`，目录权限 700、文件权限 600，由 Spring 配置自动导入；实际凭据不提交 Git。`MYSQL_USERNAME`、`MYSQL_PASSWORD` 可覆盖本机文件。
- MySQL：`192.168.5.236:3306/codex_sop_dev`。
- 网关、监控、配置中心分别仅监听 `127.0.0.1:18080`、`127.0.0.1:18090`、`127.0.0.1:18091`。

运行数据、日志、临时文件和本机凭据已加入 Git 忽略。VS Code 的本地启动配置使用此 worktree 的绝对路径；换机器后须调整 Spring 凭据导入和 VS Code 路径。

## 启动与停止

从本 worktree 根目录执行：

```sh
python3 scripts/start_test_environment.py gateway
python3 scripts/start_test_environment.py console
python3 scripts/start_test_environment.py config-center
```

启动命令最后可以显式添加 `start`，省略时仍为启动。停止和状态查询示例：

```sh
python3 scripts/start_test_environment.py gateway stop
python3 scripts/start_test_environment.py gateway status
python3 scripts/start_test_environment.py console stop
python3 scripts/start_test_environment.py config-center stop
```

停止仅适用于该脚本记录的进程：核对 PID、测试端口及 worktree 工作目录后发送 SIGTERM，最多等待 30 秒，不自动强制结束。PID 过期且端口空闲时清理旧记录；记录与实际进程不符时拒绝停止。VS Code 启动的进程请在 VS Code 中停止。

端口被监听进程占用时脚本拒绝重复启动；端口预检允许复用已关闭连接的 TIME_WAIT 状态，正常停止后可立即重新启动。脚本当前只读借用原仓库已有 Python 虚拟环境的解释器和依赖，通过 `PYTHONPATH` 加载本 worktree 的源码，不写入该环境。Java 使用本 worktree 下打包产物；修改 Java 配置或代码后先使用 `mvnd -f services/<模块>/pom.xml -DskipTests package` 重新打包。

VS Code 可选择“测试配置中心（18091 / codex_sop_dev）”启动项；自动读取本机凭据，不再提示输入。已有进程占用 18091 时，先核实并停止该测试进程，再从 VS Code 启动。停服务前核对 PID、工作目录和测试监听端口，不使用按 Java/Python 名称批量结束进程的命令。

凭据已经配置；仅在本机凭据文件尚不存在时，可运行 `python3 scripts/configure_test_mysql.py` 创建，脚本不覆盖已有文件。

## 执行机边界

钉钉保持关闭。用户已允许准备接入正式执行机，但尚未指定或登记具体机器。网关启动脚本仅在进程环境存在 `CODEX_APP_SERVER_TOKEN` 时将其传给网关，不读取或输出令牌文件。

机器默认目录为 Windows `C:\codex-work`，模型为 `gpt-6-sol`，允许写入及完全访问。远程主监督使用 `remote_sidecar`；凭据路径模板和产物目录由本机服务配置指定。共享正式执行机时，远程工作目录并不因本地 worktree 独立而自动隔离。

当前网关只监听回环地址，远程 Sidecar 无法直接接入。实际接入前需确定机器、测试主监督实例及受限内网访问方式，不能改指正式 Sidecar 影响正式运行。
