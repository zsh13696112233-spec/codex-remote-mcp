# 助手查询步骤记录与独立咨询

用户仍通过监控页或钉钉提问。进度问题直接查询；历史问题由助手选取步骤记录；需要核实实现时，在已结束步骤所在执行机创建独立只读咨询。实际修改仍需返工二次确认。功能、容量和恢复语义统一见 [Python README](../services/python-workflow/README.md#步骤记录查询与独立咨询)。

## 升级

1. 避开业务运行升级中央 Python 网关；继续使用原 SQLite 路径。新增表兼容旧库，不删除原表或迁移历史数据。
2. 更新监控中心 8090 和配置中心 8091，以展示和转发安全查询进度。两个 Java 系统不增加数据库结构或直接控制接口。
3. 本机 MCP 与远程 Sidecar 同步更新代码，开始保存新事件的版本关联及步骤实际目录/模型。旧记录不推测缺失的历史字段；无需调整 MCP 工具白名单。
4. 验证中央能连接目标机器的登记地址，机器须启用、具备执行能力且检测通过。不自动升级执行服务，不读取或展示凭据内容。

执行服务需支持 `thread/read`、创建/恢复会话、`config/read`、`mcpServerStatus/list` 和只读沙箱。支持 `thread/fork` 的 `lastTurnId` 时采用分支，否则用选取记录建立新咨询。没有隔离确认、机器离线或会话缺失时，不扩大权限，也不回到原业务会话继续执行。

未知启动状态的咨询不会自动重发。若启动响应丢失且没有轮次编号，该咨询会话保持不可继续；仍可查记录，需运维核实远端状态后再处理，第一版不提供强制解除接口。不要以重试业务步骤的方式修复咨询记录。

## 验收

Windows 使用根目录已有虚拟环境：

```powershell
$env:PYTHONPATH="$PWD/services/python-workflow/src;$PWD/services/python-workflow"
.venv/Scripts/python.exe -m unittest tests.test_workflow_consultation tests.test_workflow_gateway tests.test_workflow_store -q
node --test services/workflow-console/src/test/js/consultation.test.cjs
mvn -f services/role-task-config-center/pom.xml -Dtest=DingTalkBotCoordinatorTest test
```

macOS 使用 `.venv/bin/python`、冒号分隔 `PYTHONPATH` 和 `mvnd`，具体入口见根 README。

可选无账号隔离协议验收：将 `CONSULTATION_TEST_CODEX` 设为本机 Codex 可执行文件绝对路径，运行 `tests.test_consultation_app_server`。该用例使用临时工作区和空配置，仅验证只读沙箱响应及外部工具清单，不调用模型，不证明完整咨询或实际操作系统写入拦截已通过。

上线前需另行用测试任务验收真实模型：完成三个步骤后追问第一步、返工后查询首次版本、连续追问、跨步骤比较、故意请求咨询员写文件、断线恢复、网页刷新以及钉钉跨会话回传。核查实际代码时确认原业务会话和文件未被改变；覆盖 Windows/macOS 目标机器。未运行这些实机项时不能用模拟协议测试代替。
