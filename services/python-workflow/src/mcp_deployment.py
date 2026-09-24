"""独立 Agent 安装 MCP；配置注册和验收不信任模型自述。"""

import asyncio
import base64
import json
import logging
import os
import uuid
from pathlib import Path

from codex_orchestrator_mcp import AppServerClient, AppServerDisconnected, Orchestrator
from mcp_installation import RESULT_SCHEMA, installation_prompt, is_runtime, sandbox_policy, sandbox_rejection, validate_result
from mcp_packages import FILE_LIMIT, parse_package, windows_path
from mcp_store import McpStore
from mcp_diagnostics import diagnostic, tools_discovered
from mcp_file_transfer import DIRECT_LIMIT, McpFileTransfer
from mcp_terminal import install_terminal
from skill_deployment import RemoteFiles, storage_call
from skill_packages import SkillError
from workflow_service_config import setting

LOGGER = logging.getLogger(__name__)


class McpDeployment:
    def __init__(self, gateway, config=None, client_factory=AppServerClient):
        self.gateway = gateway
        self.store = McpStore(gateway.store)
        config = (config if config is not None else setting("mcp_deployment", {})) or {}
        self.root = Path(config["package_root"]) if config.get("package_root") else None
        self.client_factory = client_factory
        self.workers = []
        self.lock = None

    def enabled(self):
        if self.root is None:
            raise SkillError("请先配置 MCP 包存储目录。", 409)

    def upload(self, content, group):
        self.enabled()
        metadata, _ = parse_package(content)
        candidates = [entry["path"].split("/")[-1] for entry in metadata["manifest"]
                      if entry["path"].lower().endswith(".exe")]
        label = candidates[0] if candidates else metadata["manifest"][0]["path"].split("/")[0]
        metadata["name"] = label + " · " + metadata["id"][:12]
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / (metadata["id"] + ".zip")
        temp = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temp.write_bytes(content)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return self.store.add(metadata, group)

    def create(self, body):
        self.enabled()
        if not isinstance(body, dict) or set(body) != {"requestId", "packageId", "agentIds", "groupId"}:
            raise SkillError("请选择包、分组、执行机并提供请求编号。")
        ids = body["agentIds"]
        if not isinstance(body["packageId"], str) or len(body["packageId"]) != 64:
            raise SkillError("MCP 包编号不正确。")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(not isinstance(k, str) for k in ids) or len(set(ids)) != len(ids)):
            raise SkillError("请选择 1–100 台不同执行机。")
        self.store.package(body["packageId"])
        rows = self.gateway.registry.rows()
        snapshots = {}
        for key in ids:
            if key not in rows:
                raise SkillError("找不到执行机。", 404)
            row = rows[key]
            agent = self.gateway.registry.config_from_row(key, row)
            rule = self.gateway.registry.mcp_settings(key)
            if (not rule["enabled"] or not agent.allow_write or not agent.enabled
                    or "executor" not in agent.capabilities or row["test_status"] != "passed"
                    or not agent.allow_cwd_override):
                raise SkillError("执行机未授权 MCP 安装、写入或目录覆盖，或连接尚未检测通过。", 409)
            snapshots[key] = {"config": row["config"], "settings": rule,
                              "skill": self.gateway.registry.skill_settings(key)}
        return self.store.batch(self.store.create(body, snapshots))

    async def start(self):
        if self.root is None:
            return
        # 与 Skill 同样使用 OS 锁；进程崩溃不会自动释放远程安装占用记录。
        self.lock = open(str(self.gateway.store.path) + ".mcp.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.lock.seek(0, 2) == 0:
                    self.lock.write(b"0")
                    self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            await storage_call(self.store.recover)
        except BaseException:
            self.lock.close()
            self.lock = None
            raise
        self.workers = [asyncio.create_task(self.worker()) for _ in range(3)]

    async def stop(self):
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers = []
        if self.lock:
            self.lock.close()
            self.lock = None

    async def worker(self):
        while True:
            try:
                task = await storage_call(self.store.claim)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.error("MCP 领取任务失败，类型=%s", type(error).__name__)
                await asyncio.sleep(3)
                continue
            if not task:
                await asyncio.sleep(1)
                continue
            try:
                await asyncio.wait_for(self.run_with_reconnect(task), 1800)
            except asyncio.CancelledError:
                if task.get('_check_stage'):
                    await self.record_diagnostic(task, task['_check_stage'], error=InterruptedError())
                await self.interrupted(task, "安装中断，请核对远程状态。")
                raise
            except Exception as error:
                LOGGER.warning("MCP 安装未完成，类型=%s", type(error).__name__)
                if isinstance(error, AppServerDisconnected):
                    LOGGER.warning(
                        "MCP 安装连接断开，task_id=%s，execution_started=%s，execution_stopped=%s，close_code=%s",
                        task["id"], bool(task.get("execution_started")),
                        bool(task.get("execution_stopped")), error.code,
                    )
                if task.get('_check_stage'):
                    await self.record_diagnostic(task, task['_check_stage'], error=error)
                message = str(error) if isinstance(error, SkillError) else "安装未完成，请检测远程状态后重试。"
                await self.interrupted(task, message)

    async def interrupted(self, task, message):
        uncertain = task.get("execution_started", 1) and not task.get("execution_stopped", 0)
        await storage_call(self.store.update, task["id"], state="review" if uncertain else "failed",
                           occupied=1 if uncertain else 0, message=message)

    async def run_with_reconnect(self, task):
        for attempt in range(3):
            try:
                await self.run(task)
                return
            except (ConnectionError, TimeoutError):
                if task.get("execution_started", 1) or attempt == 2:
                    raise
                await asyncio.sleep(attempt + 1)

    async def run(self, task):
        if task.get('result'):
            task['_installation_kind'] = json.loads(task['result']).get('kind', 'mcp')
        if task['mode'] == 'check':
            await self.record_diagnostic(task, '连接执行服务', pending=True)
        snapshot = json.loads(task["snapshot"])
        rows = await storage_call(self.gateway.registry.rows)
        row = rows.get(task["agent_id"])
        if not row or row["config"] != snapshot["config"]:
            raise SkillError("执行机连接发生变化，停止恢复。", 409)
        rule = await storage_call(self.gateway.registry.mcp_settings, task["agent_id"])
        skill = await storage_call(self.gateway.registry.skill_settings, task["agent_id"])
        batch = await storage_call(self.store.batch, task["batch_id"])
        if (rule != snapshot["settings"] or row["group_id"] != batch["group_id"]
                or any(skill[key] != snapshot["skill"][key] for key in ("enabled", "root"))):
            raise SkillError("执行机安装授权、分组或目录已变化，停止恢复。", 409)
        agent = self.gateway.registry.config_from_row(task["agent_id"], row)
        token = await storage_call(Orchestrator._resolve_agent_token, agent)
        async with self.client_factory(agent.url, token=token) as client:
            if task["thread_id"]:
                thread = (await client.request("thread/read", {"threadId": task["thread_id"], "includeTurns": True}))["thread"]
                turns = thread.get("turns", [])
                if any(turn.get("status") == "inProgress" for turn in turns):
                    raise SkillError("远程安装仍在执行。", 409)
                task["execution_stopped"] = 1
                await storage_call(self.store.update, task["id"], execution_stopped=1)
                if not task["result"]:
                    value = self.result_from_turns(turns)
                    if value is None:
                        await storage_call(self.store.update, task["id"], state="failed", occupied=0,
                                           message="安装已停止，未取得有效结果；请核对文件后重新下发。")
                        return
                    task["result"] = json.dumps(value)
            if task["result"]:
                await self.register_and_check(client, task, json.loads(task["result"]))
                return
            if task["mode"] == "check":
                # 未拿到 thread ID 的不确定启动不能以“查不到”推断未执行。
                if task["occupied"]:
                    raise SkillError("远程启动结果未知，请维护人员核对。", 409)
                await storage_call(self.store.update, task["id"], state="failed", occupied=0, message="尚未执行安装。")
                return
            await self.install(client, task, snapshot, agent)

    @staticmethod
    def result_from_turns(turns):
        for turn in reversed(turns):
            if turn.get("status") != "completed":
                continue
            for item in reversed(turn.get("items", [])):
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    if len(item["text"]) > 20000:
                        raise SkillError("安装结果超过容量限制。", 409)
                    return json.loads(item["text"])
        return None

    async def install(self, client, task, snapshot, agent):
        package_id = self.store.batch(task["batch_id"])["package_id"]
        content = await asyncio.to_thread((self.root / (package_id + ".zip")).read_bytes)
        metadata, files = await asyncio.to_thread(parse_package, content)
        if metadata["id"] != package_id:
            raise SkillError("中央安装包发生变化，停止下发。", 409)
        transfer_files = {".mcp-package.zip": content, **files}
        merge_allowed = getattr(agent, "allow_write", False) and getattr(agent, "allow_full_access", False)
        if any(len(data) > DIRECT_LIMIT for data in transfer_files.values()) and not merge_allowed:
            raise SkillError("大文件合并需要执行机已授权完全访问；请更新机器权限后重新下发。", 409)
        rule = snapshot["settings"]
        temporary = windows_path(rule["temporaryRoot"]) / ((".mcp-tmp-" if rule.get("installRoot") else "") + task["id"])
        program = windows_path(rule["programRoot"]) / ("mcp-" + package_id[:16])
        skill_rule = snapshot["skill"]
        skill = windows_path(skill_rule["root"]) / ("mcp-" + package_id[:16]) if skill_rule["enabled"] else None
        fs = RemoteFiles(client, file_limit=FILE_LIMIT)
        marker = json.dumps({"taskId": task["id"], "packageId": package_id}, sort_keys=True).encode()
        for path in (temporary, program, skill):
            if path is None:
                continue
            await fs.ancestors(path.parent)
            names = await fs.names(path.parent)
            if path.name.casefold() in names:
                if await fs.read(path / ".mcp-install-owner.json") != marker:
                    raise SkillError("安装目录已存在且归属不匹配，禁止覆盖。", 409)
            else:
                await client.request("fs/createDirectory", {"path": str(path), "recursive": False})
                await fs.write(path / ".mcp-install-owner.json", marker)
        transfer = McpFileTransfer(client, fs, temporary, marker, allow_full_access=merge_allowed)
        for name, data in transfer_files.items():
            path = temporary.joinpath(*name.split("/"))
            await client.request("fs/createDirectory", {"path": str(path.parent), "recursive": True})
            await fs.ancestors(path.parent)
            if len(data) > DIRECT_LIMIT:
                await transfer.write_large(path, data)
                continue
            siblings = await fs.names(path.parent)
            if path.name.casefold() in siblings:
                await fs.metadata(path, False)
                original = await client.request("fs/readFile", {"path": str(path)})
                if original.get("dataBase64") != base64.b64encode(data).decode():
                    raise SkillError("待恢复包文件发生变化，禁止覆盖。", 409)
                continue
            await client.request("fs/writeFile", {"path": str(path), "dataBase64": base64.b64encode(data).decode()})
            if await fs.read(path) != data:
                raise SkillError("安装包传输内容不完整，请重试。", 409)
        policy = sandbox_policy(str(temporary), str(program), str(skill) if skill else None)
        config = (await client.request("config/read", {"includeLayers": False})).get("config", {})
        overrides = {"sandbox_mode": "workspace-write", "sandbox_workspace_write": {"writable_roots": policy["writableRoots"], "network_access": False,
                     "exclude_tmpdir_env_var": True, "exclude_slash_tmp": True}, "web_search": "disabled",
                     "apps": {"_default": {"enabled": False}}, "features": {"apps": False}}
        overrides["mcp_servers"] = {name: {"enabled": False} for name in config.get("mcp_servers", {})}
        overrides["plugins"] = {name: {"enabled": False} for name in config.get("plugins", {})}
        prompt = installation_prompt(str(temporary), str(program), str(skill) if skill else None, rule["runtimes"])
        # 先记录不确定启动阶段；任何失联都保留占用，不能自动重放安装。
        await storage_call(self.store.update, task["id"], state="installing")
        response = await client.request("thread/start", {"cwd": str(temporary), "model": agent.model,
                                        "approvalPolicy": "never", "sandbox": "workspace-write", "config": overrides,
                                        "developerInstructions": prompt})
        task["thread_id"] = response["thread"]["id"]
        await storage_call(self.store.update, task["id"], thread_id=task["thread_id"])
        rejection = sandbox_rejection(response, policy)
        if rejection:
            await storage_call(self.store.update, task["id"], state="failed", occupied=0, message=rejection)
            return
        task["execution_started"] = 1
        await storage_call(self.store.update, task["id"], execution_started=1)
        started = await client.request("turn/start", {"threadId": task["thread_id"], "input": [{"type": "text", "text": prompt}],
                                      "sandboxPolicy": policy, "approvalPolicy": "never", "outputSchema": RESULT_SCHEMA})
        task["turn_id"] = started["turn"]["id"]
        await storage_call(self.store.update, task["id"], turn_id=task["turn_id"])
        while True:
            try:
                message = await client.next_notification(timeout_sec=60)
            except TimeoutError:
                continue
            params = message.get("params", {})
            if message.get("method") == "turn/completed" and params.get("threadId") == task["thread_id"]:
                thread = (await client.request("thread/read", {"threadId": task["thread_id"], "includeTurns": True}))["thread"]
                if any(turn.get("status") == "inProgress" for turn in thread.get("turns", [])):
                    raise SkillError("远程安装尚未停止，暂停验收。", 409)
                task["execution_stopped"] = 1
                await storage_call(self.store.update, task["id"], execution_stopped=1)
                result = self.result_from_turns(thread.get("turns", []))
                result = validate_result(result, str(program), rule["runtimes"], str(skill) if skill else None)
                if (result.get("status") == "installed" and not result.get("skillPath")
                        and any(name.split("/")[-1] == "SKILL.md" for name in files)):
                    raise SkillError("包内含 Skill，但安装结果未提供 Skill 入口。", 409)
                await storage_call(self.store.update, task["id"], result=json.dumps(result))
                await self.register_and_check(client, task, result)
                return

    async def register_and_check(self, client, task, result):
        if result.get("status") != "installed":
            state = "unsupported" if result.get("status") == "unsupported" else "failed"
            await storage_call(self.store.update, task["id"], state=state, occupied=0,
                               message="安装包既无可用 MCP 入口，也不满足 CLI + Skill 安装要求。" if state == "unsupported" else "安装未完成。")
            return
        snapshot = json.loads(task["snapshot"])
        package = self.store.batch(task["batch_id"])["package_id"]
        program = str(windows_path(snapshot["settings"]["programRoot"]) / ("mcp-" + package[:16]))
        skill = str(windows_path(snapshot["skill"]["root"]) / ("mcp-" + package[:16])) if snapshot["skill"]["enabled"] else None
        result = validate_result(result, program, snapshot["settings"]["runtimes"], skill)
        task['_installation_kind'] = result.get('kind', 'mcp')
        manifest = (await storage_call(self.store.package, package))["manifest"]
        if not result["skillPath"] and any(entry["path"].split("/")[-1] == "SKILL.md" for entry in manifest):
            raise SkillError("附带 Skill 尚未安装或未报告入口。", 409)
        await storage_call(self.store.update, task["id"], result=json.dumps(result), program_verified=0, skill_verified=0)
        await self.record_diagnostic(task, '核对安装文件', pending=True)
        fs = RemoteFiles(client, file_limit=FILE_LIMIT)
        await fs.ancestors(windows_path(result["command"]).parent)
        await fs.metadata(result["command"], False)
        await fs.ancestors(windows_path(result["cwd"]))
        if is_runtime(result["command"], snapshot["settings"]["runtimes"]):
            await fs.ancestors(windows_path(result["args"][0]).parent)
            await fs.metadata(result["args"][0], False)
        if result.get('kind') == 'cli':
            await self.check_cli(client, fs, task, result, snapshot)
            return
        terminal_installed = await self.configure_terminal(client, fs, task, result, snapshot)
        desired = {"command": result["command"], "args": result["args"], "cwd": result["cwd"], "enabled": True}
        await self.record_diagnostic(task, '核对 MCP 注册配置', pending=True)
        await storage_call(self.store.update, task["id"], state="registering")
        config = await client.request("config/read", {"includeLayers": True})
        existing = config.get("config", {}).get("mcp_servers", {}).get(result["name"])
        saved = json.loads(task["registration"]) if task["registration"] else None
        if existing is not None:
            if (saved != desired or any(existing.get(k) != v for k, v in desired.items())
                    or any(existing.get(k) for k in ("url", "env", "env_vars", "bearer_token_env_var", "http_headers", "env_http_headers"))):
                raise SkillError("同名 MCP 配置冲突。", 409)
        else:
            layers = [layer for layer in config.get("layers", []) if layer.get("name", {}).get("type") == "user"
                      and not layer.get("name", {}).get("profile")]
            if len(layers) != 1 or not layers[0].get("version"):
                raise SkillError("执行服务不支持用户配置版本检查。", 409)
            await storage_call(self.store.update, task["id"], registration=json.dumps(desired))
            await client.request("config/batchWrite", {"edits": [{"keyPath": "mcp_servers." + result["name"],
                                 "value": desired, "mergeStrategy": "replace"}], "expectedVersion": layers[0]["version"],
                                 "filePath": layers[0]["name"]["file"]})
        await self.record_diagnostic(task, '刷新 MCP 配置', pending=True)
        await client.request("config/mcpServer/reload", {})
        await storage_call(self.store.update, task["id"], state="verifying", program_verified=0, skill_verified=0)
        await self.record_diagnostic(task, '创建检测会话', pending=True)
        verification = await client.request("thread/start", {"cwd": result["cwd"], "sandbox": "read-only",
                                            "approvalPolicy": "never", "ephemeral": True})
        verification_id = verification["thread"]["id"]
        try:
            await self.record_diagnostic(task, '连接与工具发现', pending=True)
            found = await self.discover(client, verification_id, result["name"])
            await self.record_diagnostic(task, '连接与工具发现', found=found)
        finally:
            try:
                await client.request("thread/unsubscribe", {"threadId": verification_id})
            except Exception as error:
                LOGGER.warning("MCP 验证会话释放失败，类型=%s", type(error).__name__)
        program_verified = tools_discovered(found)
        await storage_call(self.store.update, task["id"], program_verified=int(program_verified))
        await self.check_skill(fs, task, result)
        if not program_verified:
            await storage_call(self.store.update, task["id"], state="needs_configuration", occupied=0,
                               message="程序已注册，尚未发现工具；请检查服务地址、账号或启动环境后重新检测。" + self.terminal_notice(terminal_installed))
            return
        await storage_call(self.store.update, task["id"], state="completed", occupied=0,
                           message=("MCP 工具已识别，附带 Skill 检查通过。" if result["skillPath"] else "MCP 工具已识别。") + self.terminal_notice(terminal_installed))

    @staticmethod
    def terminal_notice(installed):
        return '终端入口已加入执行账号的用户 PATH；已有终端或执行服务需重启后生效。' if installed else ''

    async def configure_terminal(self, client, fs, task, result, snapshot):
        if result.get('kind') != 'cli' and result.get('terminal') is None:
            return False
        await self.record_diagnostic(task, '验证终端入口并写入用户 PATH', pending=True)
        package = self.store.batch(task['batch_id'])['package_id']
        program = str(windows_path(snapshot['settings']['programRoot']) / ('mcp-' + package[:16]))
        return await install_terminal(client, fs, result, program, package, task['id'],
                                      json.loads(snapshot['config']), snapshot['settings']['runtimes'])

    async def record_diagnostic(self, task, stage, **kwargs):
        task['_check_stage'] = stage if kwargs.get('pending') else None
        value = diagnostic(stage, **kwargs)
        if task.get('_installation_kind') == 'cli':
            value.update(kind='cli', found=None, connection=None, toolCount=None)
            if not kwargs.get('error'):
                value['reason'] = '' if kwargs.get('pending') else 'CLI 帮助命令与 Skill 识别检查通过，未执行业务命令。'
        await storage_call(self.store.update, task['id'], diagnostics=json.dumps(value))

    async def check_skill(self, fs, task, result):
        if not result['skillPath']:
            return
        await fs.ancestors(windows_path(result['skillPath']).parent)
        await fs.metadata(result['skillPath'], False)
        skills = await fs.skills(result['cwd'])
        matches = [entry for entry in skills['skills']
                   if entry.get('path') and windows_path(entry['path']) == windows_path(result['skillPath'])
                   and entry.get('enabled') is True]
        if (len(matches) != 1 or not matches[0].get('name') or skills['errors']
                or sum(entry.get('name') == matches[0]['name'] for entry in skills['skills']) != 1):
            raise SkillError('附带 Skill 尚未唯一识别或存在解析错误。', 409)
        await storage_call(self.store.update, task['id'], skill_verified=1)

    async def check_cli(self, client, fs, task, result, snapshot):
        await storage_call(self.store.update, task['id'], state='verifying')
        await self.record_diagnostic(task, '验证 CLI 帮助命令', pending=True)
        await self.configure_terminal(client, fs, task, result, snapshot)
        await storage_call(self.store.update, task['id'], program_verified=1)
        await self.record_diagnostic(task, '验证 CLI 配套 Skill', pending=True)
        await self.check_skill(fs, task, result)
        await self.record_diagnostic(task, 'CLI 与 Skill 验证完成')
        await storage_call(self.store.update, task['id'], state='completed', occupied=0,
                           message='CLI 帮助命令可运行，配套 Skill 已识别。' + self.terminal_notice(True))

    @staticmethod
    async def discover(client, verification_id, name):
        for attempt in range(30):
            found = await McpDeployment.discovery_page(client, verification_id, name)
            if found and found.get("runtimeStatus") not in ("starting", "notStarted"):
                return found
            if attempt < 29:
                await asyncio.sleep(2)
        return found

    @staticmethod
    async def discovery_page(client, verification_id, name):
        cursor, seen, found = None, set(), None
        for _ in range(100):
            page = await client.request("mcpServerStatus/list", {"cursor": cursor, "limit": 100, "threadId": verification_id})
            for row in page.get("data", []):
                if row.get("name") == name:
                    found = row
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise SkillError("执行服务返回重复工具游标。", 409)
            seen.add(cursor)
        else:
            raise SkillError("工具列表分页超过限制。", 409)
        return found
