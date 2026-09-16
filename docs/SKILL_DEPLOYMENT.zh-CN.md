# Skill 压缩包下发

## 首版范围

配置中心“Skill 管理”支持上传一个 Skill ZIP，选择一台或多台执行机，后台下发文件并检查执行服务是否识别。支持提示词、Python 脚本、参考资料与二进制资源；不会执行压缩包中的脚本、安装依赖、启用 MCP 或提升权限。

纯执行机不需要 Sidecar。浏览器只访问 8091；8091 通过 HTTP 把 ZIP 转交 8080；中央网关通过已有认证 WebSocket 的文件接口逐文件写入目标执行服务。主监督与业务步骤的会话不参与安装。

只支持首次安装。同名不同内容、同名人工安装目录均拒绝覆盖；同包重复下发会重新核对平台已安装内容。没有升级、卸载和自动删除功能。包与安装记录存放在中央，不连接配置中心 MySQL，也不改变 SOP 现有标签语义。

## 部署准备

1. 更新中央 Python 网关环境，在原环境中安装项目依赖（新增 `PyYAML>=6.0.2,<7`）；不要复制其他机器的虚拟环境。Windows 使用根 `.venv\Scripts\python.exe -m pip install -e services/python-workflow`，macOS 使用 `.venv/bin/python -m pip install -e services/python-workflow`。有锁定部署流程时按现有流程使用更新后的 `uv.lock`。
2. 在中央服务配置中合并以下 **新增段落**，保留原数据库路径、机器默认配置和凭据引用。示例为虚构路径：

```json
{
  "skill_deployment": {
    "package_root": "C:\\codex-data\\skill-packages"
  }
}
```

`package_root` 是中央部署机保存 ZIP 的专用绝对目录，建议位于仓库外；只需由维护人员统一配置一次。省略时关闭上传和下发。

逐机授权在 **机器管理 → 编辑 → Skill 安装设置** 中完成：勾选“允许向这台执行机下发 Skill”，填写执行机上的绝对安装目录并保存。不需要手填机器编号。设置保存至中央 SQLite 的 `agent_skill_settings` 表，网页保存后立即生效，刷新和重启均保留；无需编辑逐机 JSON 或重启服务。Java 不增加数据库副本。

Windows 示例目录为 `C:\Users\codex-worker\.agents\skills`，macOS 示例为 `/Users/codex-worker/.agents/skills`。必须使用目标执行服务运行账号实际可识别的目录，预先创建；不接受网络共享、设备路径、链接目录或相对路径。不要照搬中央机器账号的路径。

保存后可在执行机卡片点击“检测 Skill 目录”。检测只使用已保存目录，检查目录及祖先类型、目录读取和 Skill 刷新接口，不写测试文件、不执行脚本；结果和时间持久化展示。检测通过不代表已验证写权限或该目录一定属于 Skill 搜索范围，实际写入和唯一识别仍在下发时验证。

机器仍须启用、具备执行能力、连接检测通过并已有 `allow_write: true`。网页授权不会提高写权限或业务步骤权限。旧版本服务配置的 `skill_deployment.agents` 仅在升级首次为已有机器建立设置记录时导入，之后数据库设置优先，包括明确关闭的授权；可移除旧配置，避免误解。新登记机器默认未授权。

更换连接地址后必须重新检测连接；更改安装目录会清除旧目录检测结果。存在排队或运行中的 Skill 任务时，拒绝修改分组、安装设置和连接地址。关闭授权不删除已安装文件；改目录不迁移旧 Skill，旧安装记录仍受目标路径和机器身份校验保护。

3. 核对目标版本支持 `fs/getMetadata`、`fs/readDirectory`、`fs/createDirectory`、`fs/writeFile`、`fs/readFile` 和 `skills/list` 的 `forceReload` 参数。接口不可用或目录策略禁止时安装失败，不退回模型安装或 SSH。写入与回读只针对授权目录内的包文件和归属标记，不读取凭据正文。
4. 先重启 8080，再更新并启动 8091。无需修改 8090 或给纯执行机增加 Sidecar。中央网关一个运行库只允许一个 Skill 后台处理进程；运行库旁的 `.skills.lock` 是 OS 进程锁，进程退出自动释放，不要运行时删除它。

执行服务版本间的 Skill 搜索位置可能不同。当前官方文档列出用户级 `.agents/skills`；已有使用 `.codex/skills` 的部署应按实际版本验证。安装结束以目标服务在机器默认工作目录下返回的唯一名称、准确路径和启用状态为准。仓库级 Skill 仅保证对应仓库范围可见；希望不同步骤目录都可引用时，使用该运行账号支持的用户级目录。

机器登记 `POST /api/agents` 和编辑 `PUT /api/agents/{id}` 接受可选 `skillInstallation: {enabled: true, root: "目标绝对目录"}`。省略该字段保留已有设置，新机器默认为关闭。机器响应同时返回 `checkedAt`、`checkMessage`；检测结果只能由服务端写入。

## 压缩包与操作

一个 ZIP 只包含一个 Skill，入口为根目录的 `SKILL.md`，或外包一层目录，例如 `demo/SKILL.md`。所有其他文件必须在同一 Skill 目录。`SKILL.md` 为 UTF-8，开头示例：

```markdown
---
name: sales-report
description: 在需要生成销售报表时使用。
---

按此目录下的说明处理输入文件，输出到任务指定的产物目录。
```

名称最多 64 字符，仅小写字母、数字及单连字符；说明为 1–1024 字符。YAML 元数据最多 16 KiB，不接受别名、重复键和对象构造标签。ZIP 最大 20 MiB，解压后最大 50 MiB，单文件最大 10 MiB，最多 500 个文件。拒绝越界路径、链接、加密 ZIP、重复路径、大小写冲突及 Windows 非法名称；仅忽略 `__MACOSX` 和 `.DS_Store` 打包元数据。

1. 在统一分组栏选择分组，点击 Skill 库页签中的“＋ 上传”，在弹窗中选择 ZIP 并上传；成功后自动选中该包，查看名称、说明、文件数量、内容哈希和脚本提示。校验失败时弹窗保留文件并展示原因。
2. 点击包卡片的“下发”，在弹窗勾选本组执行机并提交。全部视图需要先选择该包已加入的组；未归组包需先加入分组。每台机器串行安装，同时最多三台；单台失败不影响其他目标。
3. 在“执行机”页签中选择机器查看安装结果、重试或重新检测；“下发记录”页签按当时分组展示最近 100 个批次。机器列表仅展示平台下发记录，不扫描人工安装内容。资源和脚本先写入，根 `SKILL.md` 最后发布；每个文件回读校验后更新进度。
4. “已安装并识别”后复制 `$sales-report` 到 SOP 的“执行要求”。**不要填在 Skill 标签中**，标签不会下发到提示词。可写“使用 $sales-report 处理本步骤输入，输出销售日报”。

首版不会强制步骤使用 Skill，也不会在每次业务派发时检查安装状态。新启动步骤的引用效果需要实际验收；不保证已有会话即时刷新。带脚本时始终显示“运行环境未验证”，由执行机预装 Python、第三方库及外部工具。包应可移植，不携带开发机绝对路径或虚拟环境。产物应写到业务输出目录，避免修改安装文件；重检遇到未知额外文件（包括脚本产生的缓存）会报告目录变化，不自动清理。

## 失败与恢复

- 首次连接加最多三次自动重连，每次总处理上限十分钟；用尽后页面手动重试。
- 请求重试复用 UUID；相同编号不同请求内容返回 409。页面在当前浏览器会话保留未确认提交编号；若浏览器禁止会话存储，将拒绝提交而非丢失幂等保护。
- 中央保存安装归属、文件进度和尝试次数。网关重启后恢复未结束任务，继续前重新回读，不信任旧进度。
- `.codex-platform-owner.json` 为平台保留标记。目录存在但标记缺失或不匹配时停止，不认领未知空目录。若刚创建目录就断线、尚未写入标记，可能需要维护人员核实后处理；平台不自动删除。
- 机器地址、默认工作目录或安装根目录变化后，旧安装记录拒绝继续，避免向另一目标恢复。首版不提供安装记录迁移。
- “重新检测”只读取并核对文件与识别状态，不补文件；“重试”可继续本次已确认归属的未完成安装，但不覆盖内容不同的文件。
- 文件传输完成但未识别、解析错误或已禁用时，不报告成功，也不自动启用。检查目录与目标版本后重新检测。
- 此协议没有跨文件系统原子事务；本机管理员须避免安装期间并发替换目录、修改包文件或改变执行服务账号。平台对自己的并发安装做互斥和校验。

## HTTP 契约

浏览器路径带 `/api`，网关路径去掉该前缀：

| 方法与路径 | 含义 |
| --- | --- |
| `GET /api/skills?groupId=` | 按组筛选后最近 200 个包及上传是否启用；省略分组为全部，包增加 groups 列表 |
| `POST /api/skills/groups/assign` | `{groupId, packageIds}`，整批加入分组，最多 200 个包 |
| `POST /api/skills/machines/{id}/check` | 检测已保存目录，返回 passed/message，不接受临时路径 |
| `GET /api/skills/{id}` | 按内容哈希读取包详情，供恢复较早的选择和未确认请求 |
| `POST /api/skills?groupId=必填分组编号` | 原始 ZIP 字节，Content-Type 为 `application/zip` 或 `application/octet-stream`，返回 201 |
| `GET /api/skills/inventory?groupId=` | 全量记录按机器和名称汇总，返回 items；优先保留原安装包，并显示其活动任务或最近更新状态 |
| `GET /api/skills/machines?groupId=` | 分组、机器及数据库安装授权和资格；远程接口与权限在安装时检测 |
| `GET /api/skill-deployments?groupId=` | 最近 100 个批次 |
| `POST /api/skill-deployments` | `{groupId, requestId, packageId, agentIds}`，最多 100 台，返回 202 |
| `GET /api/skill-deployments/{id}` | 批次详情及逐机进度 |
| `POST /api/skill-deployment-tasks/{id}/retry` | `{requestId}`，只重试失败任务，返回 202 |
| `POST /api/skill-deployment-tasks/{id}/check` | `{requestId}`，重新核对终态任务，返回 202 |

错误统一为 `{error: "中文说明"}`；400 参数错误、404 不存在、409 安装资格或状态冲突、413 超限、415 类型不支持。配置中心保留 Skill 网关业务状态码，网关不可用或响应非法返回固定 502，不改变其他接口的异常映射。

## 验证命令与限制

按根 README 设置 `PYTHONPATH` 并使用已有 `.venv`：

```powershell
.venv/Scripts/python.exe -m unittest tests.test_skills tests.test_workflow_service_config -v
mvn -f services/role-task-config-center/pom.xml test -Dtest=SkillControllerTest
node --test services/role-task-config-center/src/test/js/skills.test.cjs
```

真实协议测试需显式设置 `SKILL_TEST_CODEX` 为本机 Codex 可执行文件绝对路径，再运行 `tests.test_skill_app_server`；测试仅创建临时目录，验证真实文件接口、Skill 识别及无第三方依赖脚本，不创建模型会话、不读取部署凭据。默认测试集跳过此项。macOS 使用 `.venv/bin/python` 和 `mvnd`。

`python -m tests.preview_skills` 启动 127.0.0.1:18191 的一次性 UI 验收环境，使用临时数据库和内存执行机；它不是生产启动入口。浏览器访问 `/?page=skills`。关闭进程后数据丢弃。

正式上线前还应在实际 Windows/macOS 执行机验证网络传输、用户级搜索目录、SOP 新步骤的 `$skill-name` 引用、脚本依赖及步骤权限。真实协议测试和模拟 UI 成功不能替代这些业务联调。

官方参考：[Skill 目录与结构](https://learn.chatgpt.com/docs/build-skills)、[执行服务接口](https://learn.chatgpt.com/docs/app-server)。

## 统一分组升级

Skill 复用中央机器分组。一个包可加入多个组，组与包以组合唯一键持久化，加入操作只追加收录，不复制文件、不自动安装、不改变其他组。页面“加入分组”支持多选包，逐次加入目标组；本次不提供移除收录、升级或卸载。分组管理统计 Skill 数量并可跳转。分组是管理归属，不是用户权限或运行环境隔离。

新增 `POST /api/skills/groups/assign`，请求 `{groupId, packageIds}`，1–200 个不重复包，整批事务校验；重复加入不产生重复记录。上传通过查询参数提供 groupId，下发请求改为 `{groupId, packageId, agentIds, requestId}`，同一请求编号不能改组或更换目标。后端在同一写事务校验包收录与机器归属，重试和后台执行再次检查。Java 代理到同名中央路径（去掉 /api 前缀）；浏览器不直连中央。查询的 groupId 省略或为空表示全部，非法或已删除的分组返回稳定错误。

中央 SQLite 自动追加 skill_group_packages 关联表，以及 skill_batches 的可空 group_id/group_name，保留包内容、安装记录和任务进度。新批次冻结分组名称；组改名或机器改组不改历史。机器有待处理 Skill 操作时禁止改组；没有待处理操作时沿用原有 SOP/运行保护，改组不会移动或删除远程文件。新组仍展示实际平台安装记录，未加入本组库时标注；检测不要求收录且不会补写缺失文件。旧组批次不能跨组重试，应先在新组收录后重新下发。

旧包仍在全部列表，由用户手动批量归组，不按试装记录推断。旧批次显示“历史未归组”，仅在全部历史可见；升级前已接受的排队任务按原语义恢复。旧失败任务不再直接重试，先归组再创建新下发。Skill 收录关系和新下发历史均阻止组删除。

先备份中央库并按原运行维护流程升级 8080，再升级 8091 静态资源和代理；8090 不变。短暂版本不一致期间，旧页面因缺少 groupId 不能创建上传或下发，应在两端升级后刷新页面。无需 MySQL 新迁移，不自动重启任何正式服务。
