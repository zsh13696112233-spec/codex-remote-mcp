# 执行机配置

`agents.example.json` 是中央网关可提交的配置模板。将其复制为 `agents.json` 后填写本机或远程 Codex app-server 信息；`agents.json` 已被 Git 忽略。

`agents.remote-sidecar.example.json` 是远程主监督机本地 Sidecar 使用的执行机清单模板。

app-server 访问令牌通过 `token_env` 或 `token_file` 配置，远程主监督的独立机器令牌通过 `sidecar_token_env` 或 `sidecar_token_file` 配置；每一组都严格二选一，不要把令牌值直接写入 JSON。

中央模板中的 `supervisor-b` 演示环境变量令牌，`remote-build` 演示文件令牌。`token_file` 必须是中央网关机器可读取的绝对路径；如果改用 `token_env`，应删除同一执行机的 `token_file`，反之亦然。模板中的完全访问权限默认关闭，需要时必须同时显式开启 `allow_write` 和 `allow_full_access`。

中央配置、远程 Sidecar 配置和各机器部署步骤见[完整部署指南](../docs/DEPLOYMENT_GUIDE.zh-CN.md)。

## 等待互动卡片模板

停止提议新增第三种互斥模式：`stopMode=true`、`waitingMode=false`、`restartMode=false`，只显示“确认停止／取消停止”，动作分别为 `stop_confirm/stop_cancel`，回传 `workflowId/controlId`。取消停止只撤销提议，不自动继续。模板须重新导入、保存、预览三种模式后发布；如果新建模板产生新编号，需要更新配置中心模板常量。

[`sop等待选择卡片_1788953254627.json`](sop等待选择卡片_1788953254627.json) 用于步骤间等待通知、回答正文和返工确认。普通等待只显示“继续执行”；返工提议只显示“确认返工／取消返工”，不显示普通等待指引。没有自定义“回复”按钮。修改后的模板需重新导入、保存、预览和发布；预览切换 `waitingMode=true, restartMode=false` 与 `waitingMode=false, restartMode=true`，检查按钮组互斥。建议更新原模板，保留当前模板编号；若创建新模板，需同步修改后端模板编号。

在钉钉卡片搭建器中导入后保存，以重新生成渲染布局，再预览及发布。`widgetInfo` 缓存为空，布局由搭建器生成。

| 变量 | 说明 |
| --- | --- |
| `title` | 动态任务名称 |
| `markdown` | 中文等待状态、等待次数、步骤、通知或本次实际回答及等待规则；使用真实换行 |
| `workflowId`、`gateId` | 任务与本轮等待标识，继续回调原样传回 |
| `confirmStatus` | `normal` 或 `disabled`；后端确认本轮有效后才启用 |
| `confirmRequestSucceeded` | 继续请求成功判定，布尔值，示例默认 `false`；回调按本次网关确认结果返回 |
| `waitingMode`、`restartMode` | 普通等待与返工模式，两者互斥 |
| `controlId` | 本次返工操作编号，返工按钮回传；不使用等待编号代替 |

预览数据使用虚构标识，继续按钮默认禁用，回传动作保持 `advance_confirm`。成功判定不可使用恒定 true 或上一次成功值；正式接入须核对本次事件返回值及业务结果，网络成功不代表业务成功。成功 Toast 为空，实际是否继续由权威状态展示。

此次按用户要求改为测试钉钉原生右键“回复”。仍需验证客户端是否支持引用该卡片发送文字、图片，以及入站引用标识能否准确关联任务；不得以最近任务或用户下一条任意消息猜测。仅打开原生输入框不保证后端能观察到，因此不能承诺输入期间倒计时停止；须在消息送达并成功保持等待后取消自动继续。本模板不增加暂停按钮或专属回复页面。

后端接入与当前限制以[配置中心等待单按钮卡片说明](../services/role-task-config-center/README.md#等待单按钮卡片)为准。真实群内 @ 由发送接口设置接收人参数。每次正式回答新建卡片保留历史；等待结束后将本轮全部卡片的 `confirmStatus` 设为 `disabled`，后端同时拒绝过期操作。回答正文的等待规则须按实际状态生成。

返工按钮动作分别为 `restart_confirm`、`restart_cancel`；后端校验已投递卡片、本次操作编号、提议人和有效期。成功提示“已提交，请查看处理结果”表示请求已受理，不表示返工已执行完成。取消返工只撤销提议，不取消整个任务、不自动放行原等待。重复点击同一按钮复用消息编号。操作处理或过期后按钮禁用。

配置中心需重新构建并重启后生效。导入保存时重新生成布局；模板预览和真实钉钉投递仍需客户端验收。旧卡片不转换为新的返工提议，使用发布后新生成的卡片测试。
