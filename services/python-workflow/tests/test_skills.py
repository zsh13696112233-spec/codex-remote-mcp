import asyncio
import base64
import io
import json
import stat
import tempfile
import unittest
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from starlette.testclient import TestClient
from skill_packages import OWNER_FILE, SkillError, parse_package, remote_root
from skill_deployment import SkillDeployment
from skill_store import SkillStore
from agent_registry import AgentRegistry
from workflow_gateway import create_app
from workflow_store import WorkflowStore
from codex_orchestrator_mcp import AgentConfig, AppServerRpcError


SKILL = b'---\nname: demo\ndescription: "A demo skill"\n---\nUse scripts/main.py if available.\n'


def bundle(files=None, prefix=""):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (files or {"SKILL.md": SKILL}).items():
            archive.writestr(prefix + name, content)
    return stream.getvalue()


class PackageTests(unittest.TestCase):
    def test_two_structures_have_same_version_and_keep_binary(self):
        files = {"SKILL.md": SKILL, "scripts/main.py": b'print("hello")', "assets/a.bin": bytes(range(256))}
        first, contents = parse_package(bundle(files))
        second, wrapped = parse_package(bundle(files, "demo/"))
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["hasScripts"])
        self.assertEqual(contents, wrapped)
        self.assertEqual(contents["assets/a.bin"], bytes(range(256)))

    def test_reject_bad_paths_and_structure(self):
        raw = bundle({"SKILL.md": SKILL, "a/b": b"x"}).replace(b"a/b", b"a\\b")
        with self.assertRaises(SkillError): parse_package(raw)
        for path in ("../x", "/x", "C:/x", "nul.py", "foo.", "foo ", "x:y", "scripts/../a"):
            with self.subTest(path=path), self.assertRaises(SkillError):
                parse_package(bundle({"SKILL.md": SKILL, path: b"x"}))
        for files in ({"readme": b"x"}, {"skill.md": SKILL}, {"a/b/SKILL.md": SKILL},
                      {"SKILL.md": SKILL, "b/SKILL.md": SKILL},
                      {"a/SKILL.md": SKILL, "outside": b"x"},
                      {"SKILL.md": SKILL, "A/x": b"1", "a/y": b"2"},
                      {"SKILL.md": SKILL, "file": b"1", "file/x": b"2"},
                      {"SKILL.md": SKILL, OWNER_FILE: b"x"}):
            with self.subTest(files=list(files)), self.assertRaises(SkillError):
                parse_package(bundle(files))

    def test_safe_yaml(self):
        for meta in (b'!!python/object/apply:os.system ["echo bad"]', b'name: demo\nname: other\ndescription: x',
                     b'name: &a demo\ndescription: *a', b'name: demo\ndescription: 123', b'- demo'):
            with self.subTest(meta=meta), self.assertRaises(SkillError):
                parse_package(bundle({"SKILL.md": b'---\n'+meta+b'\n---\n'}))
        meta, _ = parse_package(bundle({"SKILL.md": b'---\nname: demo\ndescription: |\n  Multi\n  line\n---\n'}))
        self.assertEqual(meta["description"], "Multi\nline")

    def test_limits_and_corruption(self):
        with self.assertRaises(SkillError): parse_package(b"not zip")
        with patch("skill_packages.ZIP_LIMIT", 5), self.assertRaises(SkillError): parse_package(bundle())
        with patch("skill_packages.FILE_LIMIT", 5), self.assertRaises(SkillError): parse_package(bundle())
        with patch("skill_packages.TOTAL_LIMIT", 5), self.assertRaises(SkillError): parse_package(bundle())
        with patch("skill_packages.FILE_COUNT", 1), self.assertRaises(SkillError):
            parse_package(bundle({"SKILL.md": SKILL, "a": b"x"}))

    def test_links_duplicates_and_macos(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("SKILL.md", SKILL)
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "elsewhere")
        with self.assertRaises(SkillError): parse_package(stream.getvalue())
        with self.assertRaises(SkillError): parse_package(bundle({"SKILL.md": SKILL, "A": b"1", "a": b"2"}))
        self.assertEqual(parse_package(bundle({"SKILL.md": SKILL, "__MACOSX/._demo": b"x", ".DS_Store": b"x"}))[0]["fileCount"], 1)

    def test_remote_roots(self):
        self.assertEqual(str(remote_root("/Users/worker/.agents/skills")), "/Users/worker/.agents/skills")
        self.assertEqual(str(remote_root("C:/Users/worker/.agents/skills")), "C:\\Users\\worker\\.agents\\skills")
        for root in ("relative", "/", "C:/", "C:/a/../b", "//server/share/x", None):
            with self.subTest(root=root), self.assertRaises(SkillError): remote_root(root)


class MemoryRemote:
    def __init__(self):
        self.dirs = {"/", "/work", "/work/skills"}
        self.files = {}
        self.links = set()
        self.writes = []
        self.enabled = True
        self.discover = True
        self.disconnect_after = None
        self.unsupported = None

    def __call__(self, *args, **kwargs): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    async def request(self, method, params):
        if self.unsupported == method: raise AppServerRpcError("private path and token", code=-32601)
        path = params.get("path")
        if method == "fs/getMetadata":
            if path not in self.dirs and path not in self.files: raise AppServerRpcError("missing", code=-1)
            return {"isDirectory": path in self.dirs, "isFile": path in self.files, "isSymlink": path in self.links}
        if method == "fs/readDirectory":
            entries = [p for p in self.dirs | self.files.keys() if str(PurePosixPath(p).parent) == path and p != path]
            return {"entries": [{"fileName": PurePosixPath(p).name, "isDirectory": p in self.dirs, "isFile": p in self.files} for p in entries]}
        if method == "fs/createDirectory": self.dirs.add(path); return {}
        if method == "fs/writeFile":
            self.files[path] = base64.b64decode(params["dataBase64"])
            self.writes.append(path)
            if self.disconnect_after == path:
                self.disconnect_after = None
                raise ConnectionError("lost response")
            return {}
        if method == "fs/readFile": return {"dataBase64": base64.b64encode(self.files[path]).decode()}
        if method == "skills/list":
            skills = []
            if self.discover and "/work/skills/demo/SKILL.md" in self.files:
                skills.append({"name": "demo", "path": "/work/skills/demo/SKILL.md", "enabled": self.enabled})
            return {"data": [{"cwd": params["cwds"][0], "skills": skills, "errors": []}]}
        raise AssertionError(method)


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = WorkflowStore(self.root / "runtime.db")
        with patch("workflow_service_config._load", return_value={}):
            registry = AgentRegistry(self.db)
        self.group = registry.save_group({"name": "测试组"})["id"]
        with self.db._connect() as db:
            db.execute("INSERT INTO registered_agents(id,group_id,ip,port,config) VALUES(?,?,?,?,?)", ("machine-1", self.group, "192.0.2.1", 4500, "{}"))
        self.agent = AgentConfig("machine-1", "ws://127.0.0.1:4500", "/work", allow_write=True)
        self.registry = SimpleNamespace(rows=lambda: {"machine-1": {"test_status": "passed"}},
            config_from_row=lambda *args: self.agent, configs=lambda: {"machine-1": self.agent})
        self.remote = MemoryRemote()
        self.config = {"package_root": str(self.root / "packages"), "agents": {"machine-1": {"enabled": True, "root": "/work/skills"}}}
        self.registry.skill_settings = lambda key: self.config["agents"].get(key, {})
        self.manager = SkillDeployment(SimpleNamespace(store=self.db, registry=self.registry), self.config, self.remote)
        self.package = self.upload(bundle({"SKILL.md": SKILL, "scripts/main.py": b'print("hello")'}))

    def upload(self, content):
        return self.manager.upload(content, self.group)

    def create(self, package=None):
        batch = self.manager.create({"groupId": self.group, "requestId": str(uuid.uuid4()), "packageId": (package or self.package)["id"], "agentIds": ["machine-1"]})
        return self.manager.store.claim(), batch["id"]

    async def test_install_and_check_no_rewrite(self):
        task, batch = self.create()
        await self.manager.run(task)
        result = self.manager.store.batch(batch)["tasks"][0]
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(self.remote.writes[-1], "/work/skills/demo/SKILL.md")
        writes = list(self.remote.writes)
        self.manager.store.action(task["id"], str(uuid.uuid4()), "check")
        await self.manager.run(self.manager.store.claim())
        self.assertEqual(self.remote.writes, writes)

        task2, batch2 = self.create()
        await self.manager.run(task2)
        self.assertEqual(self.manager.store.batch(batch2)["tasks"][0]["state"], "completed")
        self.assertEqual(self.remote.writes, writes)

    async def test_machine_inventory_preserves_install_across_conflicts_and_old_batches(self):
        task, batch = self.create()
        await self.manager.run(task)
        replacement = self.upload(bundle({"SKILL.md": SKILL + b"changed"}))
        for _ in range(101):
            self.manager.create({"groupId": self.group, "requestId":str(uuid.uuid4()), "packageId":replacement["id"], "agentIds":["machine-1"]})
        self.assertNotIn(batch, [b["id"] for b in self.manager.store.batches()])
        items = SkillStore(self.db).inventory()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["packageId"], self.package["id"])
        self.assertEqual(items[0]["state"], "completed")
        self.manager.store.action(task["id"], str(uuid.uuid4()), "check")
        self.assertEqual(self.manager.store.inventory()[0]["state"], "queued")
        self.remote.discover = False
        await self.manager.run(self.manager.store.claim())
        self.assertEqual(self.manager.store.inventory()[0]["state"], "failed")

    async def test_response_lost_restart_verifies_before_resuming(self):
        task, batch = self.create()
        self.remote.disconnect_after = "/work/skills/demo/scripts/main.py"
        with self.assertRaises(ConnectionError): await self.manager.install(task)
        replacement = SkillStore(self.db)
        replacement.recover()
        await self.manager.run(replacement.claim())
        self.assertEqual(replacement.batch(batch)["tasks"][0]["state"], "completed")
        self.assertEqual(self.remote.writes.count("/work/skills/demo/scripts/main.py"), 1)

    async def test_unknown_directory_and_changed_files_not_overwritten(self):
        self.remote.dirs.add("/work/skills/demo")
        task, batch = self.create()
        await self.manager.run(task)
        self.assertEqual(self.manager.store.batch(batch)["tasks"][0]["state"], "failed")
        self.assertFalse(self.remote.writes)

    async def test_corruption_detected_and_no_overwrite(self):
        task, batch = self.create(); await self.manager.run(task)
        self.remote.files["/work/skills/demo/scripts/main.py"] = b"modified"
        writes = list(self.remote.writes)
        self.manager.store.action(task["id"], str(uuid.uuid4()), "check")
        await self.manager.run(self.manager.store.claim())
        self.assertEqual(self.manager.store.batch(batch)["tasks"][0]["state"], "failed")
        self.assertEqual(self.remote.writes, writes)

    async def test_not_discovered_disabled_and_unsupported(self):
        self.remote.discover = False
        task, batch = self.create(); await self.manager.run(task)
        self.assertIn("未被唯一识别", self.manager.store.batch(batch)["tasks"][0]["error"])
        self.remote.discover = True; self.remote.enabled = False
        self.manager.store.action(task["id"], str(uuid.uuid4()), "check")
        await self.manager.run(self.manager.store.claim())
        self.assertIn("禁用", self.manager.store.batch(batch)["tasks"][0]["error"])
        self.remote.unsupported = "skills/list"
        self.manager.store.action(task["id"], str(uuid.uuid4()), "check")
        await self.manager.run(self.manager.store.claim())
        message = self.manager.store.batch(batch)["tasks"][0]["error"]
        self.assertIn("不兼容", message); self.assertNotIn("private", message)

    async def test_links_stop_before_write(self):
        self.remote.links.add("/work/skills")
        task, batch = self.create(); await self.manager.run(task)
        self.assertFalse(self.remote.writes)
        self.assertEqual(self.manager.store.batch(batch)["tasks"][0]["state"], "failed")

    def test_idempotent_requests_conflict_and_single_machine_claim(self):
        request = {"groupId": self.group, "requestId": str(uuid.uuid4()), "packageId": self.package["id"], "agentIds": ["machine-1"]}
        first = self.manager.create(request); self.assertEqual(first["id"], self.manager.create(request)["id"])
        request["agentIds"] = ["unknown"]
        with self.assertRaises(SkillError): self.manager.create(request)
        changed = self.upload(bundle({"SKILL.md": SKILL+b"changed"}))
        result = self.manager.create({"groupId": self.group, "requestId": str(uuid.uuid4()), "packageId": changed["id"], "agentIds": ["machine-1"]})
        self.assertEqual(result["tasks"][0]["state"], "failed")
        self.manager.create({"groupId": self.group, "requestId": str(uuid.uuid4()), "packageId": self.package["id"], "agentIds": ["machine-1"]})
        self.assertIsNotNone(self.manager.store.claim()); self.assertIsNone(self.manager.store.claim())

    async def test_machine_change_stops_install(self):
        task, batch = self.create()
        self.config["agents"]["machine-1"]["root"] = "/work/other"
        await self.manager.run(task)
        self.assertFalse(self.remote.writes)
        self.assertIn("配置已变化", self.manager.store.batch(batch)["tasks"][0]["error"])

    async def test_worker_lock_and_shutdown(self):
        await self.manager.start()
        other = SkillDeployment(self.manager.gateway, self.config, self.remote)
        try:
            with self.assertRaises(RuntimeError): await other.start()
        finally: await self.manager.stop()
        await other.start(); await other.stop()

    async def test_unknown_target_rejects_entire_batch(self):
        with self.assertRaises(SkillError):
            self.manager.create({"groupId": self.group, "requestId":str(uuid.uuid4()), "packageId":self.package["id"], "agentIds":["unknown", "machine-1"]})
        self.assertEqual(self.manager.store.batches(), [])

    async def test_reconnect_budget_and_manual_retry(self):
        task, batch = self.create()
        with patch.object(self.manager, "install", new=AsyncMock(side_effect=ConnectionError("private"))) as install, patch("skill_deployment.asyncio.sleep", new=AsyncMock()):
            await self.manager.run(task)
            self.assertEqual(install.await_count, 4)
        result = self.manager.store.batch(batch)["tasks"][0]
        self.assertEqual(result["attempts"], 4)
        self.assertNotIn("private", result["error"])
        request = str(uuid.uuid4())
        self.manager.store.action(task["id"], request, "retry")
        self.manager.store.action(task["id"], request, "retry")
        await self.manager.run(self.manager.store.claim())
        self.assertEqual(self.manager.store.batch(batch)["tasks"][0]["state"], "completed")

    async def test_check_does_not_restore_missing_file(self):
        task, batch = self.create(); await self.manager.run(task)
        del self.remote.files["/work/skills/demo/scripts/main.py"]
        writes = list(self.remote.writes)
        self.manager.store.action(task["id"], str(uuid.uuid4()), "check")
        await self.manager.run(self.manager.store.claim())
        self.assertEqual(self.remote.writes, writes)
        self.assertEqual(self.manager.store.batch(batch)["tasks"][0]["state"], "failed")

    async def test_credential_and_install_permissions_are_not_overridden(self):
        self.agent = replace(self.agent, allow_write=False)
        task, batch = self.create()
        self.assertIsNone(task)
        self.assertEqual(self.manager.store.batch(batch)["tasks"][0]["state"], "failed")
        self.assertFalse(self.remote.writes)

    async def test_lost_final_response_and_unknown_extra_file(self):
        task, batch = self.create()
        self.remote.disconnect_after = "/work/skills/demo/SKILL.md"
        with self.assertRaises(ConnectionError): await self.manager.install(task)
        self.manager.store.recover()
        await self.manager.run(self.manager.store.claim())
        self.assertEqual(self.remote.writes.count("/work/skills/demo/SKILL.md"), 1)
        self.remote.files["/work/skills/demo/unexpected.txt"] = b"other work"
        self.manager.store.action(task["id"], str(uuid.uuid4()), "check")
        await self.manager.run(self.manager.store.claim())
        self.assertEqual(self.remote.files["/work/skills/demo/unexpected.txt"], b"other work")
        self.assertEqual(self.manager.store.batch(batch)["tasks"][0]["state"], "failed")


class SkillRouteTests(unittest.TestCase):
    def test_upload_errors_and_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as root, patch("workflow_service_config._load", return_value={}):
            app = create_app(db_path=Path(root)/"state.db")
            with TestClient(app) as client:
                self.assertFalse(client.get("/skills").json()["enabled"])
                self.assertEqual(client.post("/skills", content=bundle(), headers={"Content-Type":"application/zip"}).status_code, 409)
                self.assertEqual(client.post("/skill-deployments", content=b"not JSON").status_code, 400)
                self.assertEqual(client.post("/skill-deployment-tasks/x/remove", json={}).status_code, 404)
                self.assertEqual(client.get("/skill-deployments/missing").status_code, 404)

    def test_upload_and_batch_survive_app_recreation(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"skill_deployment": {"package_root": str(Path(root)/"packages")}}
            with patch("workflow_service_config._load", return_value=config):
                app = create_app(db_path=Path(root)/"state.db")
                with TestClient(app) as client:
                    group = client.post("/agent-groups", json={"name":"测试组"}).json()["id"]
                    other = client.post("/agent-groups", json={"name":"其他组"}).json()["id"]
                    self.assertEqual(client.post("/skills", content=bundle(), headers={"Content-Type":"application/zip"}).status_code, 400)
                    response = client.post("/skills?groupId=" + group, content=bundle(), headers={"Content-Type":"application/zip"})
                    self.assertEqual(response.status_code, 201, response.text)
                    key = response.json()["id"]
                    self.assertEqual(client.get("/skills?groupId=" + other).json()["skills"], [])
                    self.assertEqual(client.post("/skills/groups/assign", json={"groupId":other,"packageIds":[key]}).status_code, 200)
                    self.assertEqual(client.get("/skills?groupId=" + other).json()["skills"][0]["id"], key)
                    self.assertEqual(client.get("/skills/machines?groupId=bad").status_code, 400)
                    self.assertEqual(client.get("/skill-deployments?groupId=bad").status_code, 400)
                    self.assertEqual(client.get("/skills/inventory?groupId=bad").status_code, 400)
                    self.assertEqual(client.post("/skills/groups/assign", json={"groupId":other,"packageIds":[{}]}).status_code, 400)
                    self.assertEqual(client.get("/skills/" + key).json()["name"], "demo")
                    batch = client.post("/skill-deployments", json={"groupId":group,"requestId":str(uuid.uuid4()), "packageId":key,"agentIds":["unknown"]})
                    self.assertEqual(batch.status_code, 409)
                with TestClient(create_app(db_path=Path(root)/"state.db")) as client:
                    self.assertEqual(client.get("/skills").json()["skills"][0]["id"], key)
                    self.assertEqual(len(client.get("/skill-deployments").json()["deployments"]),0)
                    inventory = client.get("/skills/inventory")
                    self.assertEqual(inventory.status_code, 200)
                    self.assertEqual(inventory.json()["items"], [])


if __name__ == "__main__": unittest.main()
