"""确定性 Skill 文件下发；不创建模型会话、不执行包内程序。"""

import asyncio
import base64
import json
import logging
import os
import uuid
from pathlib import Path

from codex_orchestrator_mcp import AppServerClient, AppServerRpcError, Orchestrator
from skill_packages import (FILE_LIMIT, OWNER_FILE, SkillError, digest, parse_package,
                            remote_root, safe_relative)
from skill_store import SkillStore
from workflow_service_config import setting

LOGGER = logging.getLogger(__name__)


async def storage_call(function, *args, **kwargs):
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        # 释放进程锁前等待已开始的数据库操作，避免关闭后旧写入覆盖恢复状态。
        try:
            await operation
        except Exception as error:
            LOGGER.error("Skill 关闭时持久化失败，类型=%s", type(error).__name__)
        raise


class RemoteFiles:
    def __init__(self, client, file_limit=FILE_LIMIT):
        self.client = client
        self.file_limit = file_limit

    async def call(self, method, **params):
        try:
            return await self.client.request(method, params)
        except AppServerRpcError as error:
            LOGGER.warning("Skill 文件接口失败，method=%s，code=%s", method, error.code)
            raise SkillError("执行服务接口不兼容、目录不可访问或被策略禁止，请检查执行服务版本和权限。", 409) from error

    async def metadata(self, path, directory):
        value = await self.call("fs/getMetadata", path=str(path))
        if (not isinstance(value, dict) or value.get("isSymlink") is not False
                or value.get("isDirectory" if directory else "isFile") is not True):
            raise SkillError("目标包含链接、特殊文件或类型不匹配，已停止安装。", 409)

    async def ancestors(self, path):
        for parent in [*reversed(path.parents), path]:
            await self.metadata(parent, True)

    async def names(self, path):
        await self.metadata(path, True)
        value = await self.call("fs/readDirectory", path=str(path))
        if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
            raise SkillError("执行服务目录响应不兼容。", 409)
        result = {}
        for entry in value["entries"]:
            name = entry.get("fileName") if isinstance(entry, dict) else None
            if not isinstance(name, str) or "/" in name or "\\" in name or name in {".", ".."}:
                raise SkillError("执行服务目录响应不兼容。", 409)
            if name.casefold() in result:
                raise SkillError("目标目录存在大小写冲突。", 409)
            result[name.casefold()] = name
        return result

    async def read(self, path):
        await self.metadata(path, False)
        response = await self.call("fs/readFile", path=str(path))
        encoded = response.get("dataBase64") if isinstance(response, dict) else None
        if not isinstance(encoded, str) or len(encoded) > ((self.file_limit + 2) // 3) * 4:
            raise SkillError("远程文件响应过大或格式不兼容。", 409)
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise SkillError("远程文件响应格式不兼容。", 409) from error

    async def write(self, path, content):
        await self.ancestors(path.parent)
        await self.call("fs/writeFile", path=str(path), dataBase64=base64.b64encode(content).decode("ascii"))
        if await self.read(path) != content:
            raise SkillError("文件回读校验不一致，已停止安装。", 409)

    async def skills(self, cwd):
        value = await self.call("skills/list", cwds=[cwd], forceReload=True)
        rows = value.get("data") if isinstance(value, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise SkillError("执行服务 Skill 列表响应不兼容。", 409)
        if not isinstance(rows[0].get("skills"), list) or not isinstance(rows[0].get("errors"), list):
            raise SkillError("执行服务 Skill 列表响应不兼容。", 409)
        return rows[0]


class SkillDeployment:
    def __init__(self, gateway, config=None, client_factory=AppServerClient):
        self.gateway = gateway
        self.store = SkillStore(gateway.store)
        self.config = config if config is not None else setting("skill_deployment", {})
        self.root = Path(self.config["package_root"]) if self.config.get("package_root") else None
        self.client_factory = client_factory
        self.workers = []
        self.lock_file = None

    def enabled(self):
        if self.root is None:
            raise SkillError("Skill 下发未启用，请先配置包存储目录和允许安装的执行机。", 409)

    def eligibility(self, agent_id):
        result = {"agentId": agent_id}
        try:
            self.enabled()
            rows = self.gateway.registry.rows()
            if agent_id not in rows:
                raise SkillError("找不到执行机。", 404)
            agent = self.gateway.registry.config_from_row(agent_id, rows[agent_id])
            if not agent.enabled or "executor" not in agent.capabilities or rows[agent_id]["test_status"] != "passed":
                raise SkillError("执行机未启用、缺少执行能力或尚未检测通过。", 409)
            rule = self.gateway.registry.skill_settings(agent_id)
            if not agent.allow_write or rule.get("enabled") is not True:
                raise SkillError("执行机未允许写入或未授权 Skill 安装。", 409)
            root = remote_root(rule.get("root"))
            result.update(root=str(root), identity=digest((agent.url + "\n" + agent.cwd).encode()), eligible=True)
        except SkillError as error:
            result.update(eligible=False, error=str(error))
        return result

    def machines(self, group_id=None):
        if group_id:
            self.store.validate_group(group_id)
        return [{**row, **{k: v for k, v in self.eligibility(row["agentId"]).items() if k != "identity"}}
                for row in self.gateway.registry.public() if not group_id or row["groupId"] == group_id]

    async def check_directory(self, agent_id):
        registry = self.gateway.registry
        rows = await storage_call(registry.rows)
        if agent_id not in rows:
            raise SkillError("找不到执行机。", 404)
        rule = await storage_call(registry.skill_settings, agent_id)
        root = remote_root(rule["root"])
        agent = registry.config_from_row(agent_id, rows[agent_id])
        if not agent.enabled or "executor" not in agent.capabilities:
            raise SkillError("请先启用执行机。", 409)
        passed = False
        try:
            if not agent.allow_write:
                raise SkillError("执行机未允许写入，请维护人员配置写权限。", 409)
            async def probe():
                token = await storage_call(Orchestrator._resolve_agent_token, agent)
                async with self.client_factory(agent.url, token=token) as client:
                    fs = RemoteFiles(client)
                    await fs.ancestors(root)
                    await fs.names(root)
                    await fs.skills(agent.cwd)
            await asyncio.wait_for(probe(), timeout=20)
            passed = True
            message = "目录读取和 Skill 刷新接口检测通过；实际写入及安装识别将在下发时验证。"
        except SkillError as error:
            message = str(error)
        except Exception as error:
            LOGGER.warning("Skill 目录检测失败，类型=%s", type(error).__name__)
            message = "目录检测失败，请检查执行服务连接、凭据和权限。"
        await storage_call(registry.record_skill_check, agent_id, rows[agent_id]["config"], rule["root"], message)
        return {"passed": passed, "message": message}

    def upload(self, content, group_id):
        self.enabled()
        self.store.validate_group(group_id)
        meta, files = parse_package(content)
        directory = self.root / meta["id"]
        directory.mkdir(parents=True, exist_ok=True)
        # 哈希目录内使用原子替换，重复上传不产生另一份包。
        def save(path, data):
            temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temp.write_bytes(data)
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        save(directory / "package.zip", content)
        for name, data in files.items():
            path = directory / "files" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            save(path, data)
        return self.store.add_package(meta, group_id)

    def create(self, body):
        self.enabled()
        if not isinstance(body, dict) or set(body) != {"requestId", "packageId", "agentIds", "groupId"}:
            raise SkillError("请提供包编号、执行机列表和请求编号。")
        agents = body["agentIds"]
        if (not isinstance(agents, list) or not 1 <= len(agents) <= 100
                or any(not isinstance(a, str) or not 1 <= len(a) <= 128 for a in agents)
                or len(set(agents)) != len(agents) or not isinstance(body["packageId"], str)):
            raise SkillError("请选择 1–100 台不重复的执行机。")
        package = self.store.package(body["packageId"])
        targets = []
        for agent in agents:
            target = self.eligibility(agent)
            if target.get("eligible"):
                target["target"] = str(remote_root(target["root"]) / package["name"])
            targets.append(target)
        key = self.store.create(body["requestId"], package, targets, body["groupId"])
        return self.store.batch(key)

    async def start(self):
        if self.root is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        # OS 锁随进程退出释放；只有持锁进程可恢复 running 任务，避免租约过期后双写。
        lock = open(str(self.gateway.store.path) + ".skills.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if lock.seek(0, 2) == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError("已有 Skill 下发进程使用此中央运行库。") from None
        self.lock_file = lock
        try:
            await storage_call(self.store.recover)
        except BaseException:
            lock.close()
            self.lock_file = None
            raise
        self.workers = [asyncio.create_task(self.worker(), name=f"skill-installer-{i}") for i in range(3)]

    async def stop(self):
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers = []
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    async def worker(self):
        while True:
            try:
                task = await storage_call(self.store.claim)
                if task is None:
                    await asyncio.sleep(1)
                    continue
                await self.run(task)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.error("Skill 后台任务处理失败，类型=%s", type(error).__name__)
                await asyncio.sleep(1)

    async def run(self, task):
        for attempt in range(task["attempts"], 4):
            await storage_call(self.store.attempt, task)
            try:
                await asyncio.wait_for(self.install(task), timeout=600)
                await storage_call(self.store.finish, task)
                return
            except (ConnectionError, TimeoutError, OSError) as error:
                if attempt < 3:
                    await asyncio.sleep(min(2 ** attempt, 4))
                    continue
                message = "传输连接失败或超时，自动重连已用尽；可重试并先核对已有文件。"
            except SkillError as error:
                message = str(error)
            except Exception as error:
                LOGGER.error("Skill 安装失败，类型=%s", type(error).__name__)
                message = "安装检测失败，请检查服务日志或重新检测。"
            await storage_call(self.store.finish, task, message)
            return
        await storage_call(self.store.finish, task, "自动重连次数已用尽，请手动重试。")

    async def install(self, task):
        await storage_call(self.store.validate_execution, task)
        install = await storage_call(self.store.installation, task)
        package = await storage_call(self.store.package, install["package_id"])
        eligible = await storage_call(self.eligibility, task["agent_id"])
        if not eligible["eligible"]:
            raise SkillError(eligible["error"], 409)
        target = remote_root(eligible["root"]) / package["name"]
        if str(target) != install["target"] or eligible["identity"] != install["identity"]:
            raise SkillError("目标执行机配置已变化，已停止安装。", 409)
        agent = (await storage_call(self.gateway.registry.configs))[task["agent_id"]]
        token = await storage_call(Orchestrator._resolve_agent_token, agent)
        async with self.client_factory(agent.url, token=token) as client:
            fs = RemoteFiles(client)
            await fs.ancestors(target.parent)
            before = await fs.skills(agent.cwd)
            for item in before["skills"]:
                if str(item.get("name", "")).casefold() == package["name"].casefold():
                    if type(target)(item.get("path", "")) != target / "SKILL.md":
                        raise SkillError("执行服务已发现其他同名 Skill，不能覆盖或创建歧义引用。", 409)
            siblings = await fs.names(target.parent)
            existing = siblings.get(target.name.casefold())
            marker = json.dumps({"owner": install["owner"], "packageId": package["id"]}, sort_keys=True).encode()
            if existing is not None:
                if existing != target.name:
                    raise SkillError("安装目录存在大小写冲突。", 409)
                children = await fs.names(target)
                if children.get(OWNER_FILE) != OWNER_FILE or await fs.read(target / OWNER_FILE) != marker:
                    raise SkillError("同名目录已存在且无法确认平台归属，已停止安装。", 409)
            else:
                if install["claimed"] or task["mode"] == "check":
                    raise SkillError("已安装目录缺失，请人工检查。", 409)
                await fs.call("fs/createDirectory", path=str(target), recursive=False)
                # 创建响应丢失且没有归属标记时，不认领未知空目录。
                if await fs.names(target):
                    raise SkillError("新建目录出现未知文件，已停止安装。", 409)
                await fs.write(target / OWNER_FILE, marker)
            await storage_call(self.store.owned, install["owner"])
            paths = {entry["path"] for entry in package["manifest"]}
            allowed = {OWNER_FILE, *paths}
            for path in paths:
                parts = path.split("/")
                allowed.update("/".join(parts[:i]) for i in range(1, len(parts)))

            async def inspect(folder, prefix=""):
                for name in (await fs.names(folder)).values():
                    path = prefix + name
                    if path not in allowed:
                        raise SkillError("安装目录出现不属于此包的内容，已停止安装。", 409)
                    is_dir = path != OWNER_FILE and path not in paths
                    await fs.metadata(folder / name, is_dir)
                    if is_dir:
                        await inspect(folder / name, path + "/")
            await inspect(target)
            complete = []
            await storage_call(self.store.progress, task, complete)
            for entry in sorted(package["manifest"], key=lambda e: (e["path"] == "SKILL.md", e["path"])):
                relative = safe_relative(entry["path"])
                local = self.root / package["id"] / "files" / relative
                data = await storage_call(local.read_bytes)
                if len(data) != entry["size"] or digest(data) != entry["sha256"]:
                    raise SkillError("中央包文件校验失败，已停止安装。", 409)
                destination = target / relative
                folder = target
                for part in relative.split("/")[:-1]:
                    names = await fs.names(folder)
                    if part.casefold() not in names:
                        if task["mode"] == "check":
                            raise SkillError("已安装文件目录缺失。", 409)
                        await fs.call("fs/createDirectory", path=str(folder / part), recursive=False)
                    elif names[part.casefold()] != part:
                        raise SkillError("安装目录大小写不一致。", 409)
                    folder = folder / part
                    await fs.metadata(folder, True)
                names = await fs.names(folder)
                if destination.name.casefold() in names:
                    if names[destination.name.casefold()] != destination.name or await fs.read(destination) != data:
                        raise SkillError("远程文件与包内容不同，首版禁止覆盖。", 409)
                elif task["mode"] == "check":
                    raise SkillError("已安装文件缺失。", 409)
                else:
                    await fs.write(destination, data)
                complete.append(relative)
                await storage_call(self.store.progress, task, complete)
            await inspect(target)
            result = await fs.skills(agent.cwd)
            expected = target / "SKILL.md"
            if any(type(target)(e.get("path", "")) == expected for e in result["errors"]):
                raise SkillError("文件已传输，但执行服务解析 Skill 失败。", 409)
            matches = [s for s in result["skills"] if s.get("name") == package["name"]]
            if len(matches) != 1 or type(target)(matches[0].get("path", "")) != expected:
                raise SkillError("文件已传输，但 Skill 未被唯一识别；请检查安装目录与执行服务版本。", 409)
            if matches[0].get("enabled") is not True:
                raise SkillError("文件已传输，但 Skill 被禁用；平台不会自动修改启用策略。", 409)
