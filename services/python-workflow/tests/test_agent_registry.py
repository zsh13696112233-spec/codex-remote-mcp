import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient
from agent_registry import AgentRegistry
from workflow_gateway import create_app
from workflow_store import WorkflowStore


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        import workflow_service_config as config
        self.config_values = {'machine_defaults': {
            'cwd': str(self.root), 'protocol': 'ws', 'token_env': 'REGISTRY_TEST_APP_TOKEN',
            'sidecar_token_template': str(self.root / '{ip}.token')}}
        self.config_patch = patch.object(config, '_load', return_value=self.config_values)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.app = create_app(db_path=self.root / 'runtime.db')
        self.gateway = self.app.state.gateway
        self.registry = self.gateway.registry
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.group = self.registry.save_group({"name": "第一组"})["id"]

    def machine(self, ip="127.0.0.1", capabilities=None, group=None):
        return self.registry.save_agent({"ip": ip, "port": 4500, "groupId": group or self.group,
                                         "capabilities": capabilities or ["supervisor"]})

    def test_single_role_and_deleted_endpoint_reuse(self):
        for roles in (["supervisor", "executor"], ["executor", "executor"], []):
            with self.assertRaisesRegex(ValueError, "一种职责"):
                self.registry.save_agent({"ip": "127.0.0.1", "port": 4500, "capabilities": roles})
        old = self.machine()
        self.registry.record_test(old["agentId"], True)
        with self.assertRaisesRegex(ValueError, "已登记|有效机器"):
            self.machine()
        self.registry.delete_agent(old["agentId"])
        new = self.machine()
        self.assertNotEqual(old["agentId"], new["agentId"])
        self.assertEqual(new["testStatus"], "untested")
        other = self.registry.save_agent({"ip": new["ip"], "port": 4501,
            "groupId": self.group, "capabilities": ["executor"]})
        self.assertNotEqual(new["agentId"], other["agentId"])
        AgentRegistry(self.gateway.store)
        self.assertEqual(len(self.registry.rows()), 2)

    def test_endpoint_index_upgrade_preserves_machine_references(self):
        import sqlite3
        machine = self.machine()
        key = machine["agentId"]
        with self.gateway.store._connect() as db:
            db.execute("DROP INDEX registered_agents_active_endpoint")
            db.execute("CREATE UNIQUE INDEX legacy_endpoint ON registered_agents(ip,port)")
        restored = AgentRegistry(self.gateway.store)
        self.assertEqual(restored.rows()[key]["ip"], machine["ip"])
        self.assertIsNotNone(restored.skill_settings(key))
        restored.delete_agent(key)
        replacement = self.machine()
        with self.gateway.store._connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE registered_agents SET deleted_at=NULL WHERE id=?", (key,))
        self.assertNotEqual(replacement["agentId"], key)

    def test_deleted_allocation_keeps_original_machine_identity(self):
        from workflow_allocation import snapshot
        supervisor = self.machine()
        executor = self.machine("127.0.0.2", ["executor"])
        self.gateway.store.create_workflow({"workflowId": "allocation-history",
            "supervisorAgentId": supervisor["agentId"],
            "nodes": [{"id": "one", "prompt": "测试", "agentId": executor["agentId"]}]})
        with self.gateway.store._connect() as db:
            db.execute("UPDATE workflows SET status='completed' WHERE workflow_id='allocation-history'")
            db.execute("INSERT INTO workflow_allocations(workflow_id,group_id,supervisor_id,executor_id,cwd) VALUES(?,?,?,?,?)",
                ("allocation-history", self.group, supervisor["agentId"], executor["agentId"], str(self.root)))
        self.registry.delete_agent(executor["agentId"])
        replacement = self.machine("127.0.0.2", ["executor"])
        with self.gateway.store._connect() as db:
            allocation = snapshot(db, "allocation-history")
        self.assertTrue(allocation["executorDeleted"])
        self.assertFalse(allocation["supervisorDeleted"])
        self.assertEqual(allocation["executorAgentId"], executor["agentId"])
        self.assertNotEqual(allocation["executorAgentId"], replacement["agentId"])

    def test_supervisor_can_be_global_and_executor_requires_group(self):
        body = {"ip": "127.0.0.8", "port": 4500, "capabilities": ["supervisor"]}
        supervisor = self.registry.save_agent(body)
        self.assertIsNone(supervisor["groupId"])
        with self.assertRaisesRegex(ValueError, "分组"):
            self.registry.save_agent({**body, "ip": "127.0.0.9", "capabilities": ["executor"]})

    def test_working_directory_is_machine_configuration_and_cannot_change_while_occupied(self):
        machine = self.machine()
        key = machine["agentId"]
        for directory in ("relative", "", "C:/bad\npath", "x" * 1001):
            with self.assertRaisesRegex(ValueError, "工作目录"):
                self.registry.save_agent({**machine, "workingDirectory": directory}, key)
        machine = self.registry.save_agent({**machine, "workingDirectory": "C:/work"}, key)
        self.assertEqual(machine["defaultCwd"], "C:/work")
        self.registry.record_test(key, True)
        self.gateway.store.create_workflow({"workflowId": "occupied", "supervisorAgentId": key,
            "nodes": [{"id": "step", "agentId": key, "prompt": "工作"}]})
        self.gateway.store.claim_next_workflow(key)
        with self.assertRaisesRegex(ValueError, "占用"):
            self.registry.save_agent({**machine, "workingDirectory": "C:/changed"}, key)
        self.assertEqual(self.registry.save_remark({"remark": "仍可写备注"}, key)["remark"], "仍可写备注")

    def test_upgrade_to_nullable_group_preserves_machine_foreign_keys(self):
        legacy = WorkflowStore(self.root / "legacy.db")
        with legacy._connect() as db:
            db.execute("CREATE TABLE agent_groups(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE)")
            db.execute("""CREATE TABLE registered_agents(id TEXT PRIMARY KEY,group_id TEXT NOT NULL REFERENCES agent_groups(id),
                ip TEXT NOT NULL,port INTEGER NOT NULL,config TEXT NOT NULL,test_status TEXT NOT NULL DEFAULT 'untested',tested_at TEXT,
                UNIQUE(ip,port))""")
            db.execute("INSERT INTO agent_groups VALUES('old','旧组')")
            db.execute("INSERT INTO registered_agents VALUES('old','old','127.0.0.1',4500,?,'passed',NULL)",
                       (json.dumps({"url": "ws://127.0.0.1:4500", "cwd": "/work", "capabilities": ["supervisor"]}),))
            db.execute("CREATE TABLE existing_child(agent_id TEXT REFERENCES registered_agents(id))")
            db.execute("INSERT INTO existing_child VALUES('old')")
        upgraded = AgentRegistry(legacy)
        self.assertEqual(upgraded.rows()["old"]["test_status"], "passed")
        with legacy._connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            db.execute("UPDATE registered_agents SET group_id=NULL WHERE id='old'")
            self.assertEqual(db.execute("SELECT agent_id FROM existing_child").fetchone()[0], "old")

    def test_remark_roundtrip_preserves_execution_and_test_state(self):
        body = {"ip": "127.0.0.1", "port": 4500, "groupId": self.group,
                "capabilities": ["executor"], "remark": "  开发机  "}
        response = self.client.post("/agents", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        machine = response.json()
        key = machine["agentId"]
        self.assertEqual(machine["remark"], "开发机")
        self.registry.record_test(key, True)
        before = self.registry.rows()[key]
        self.config_values["machine_defaults"].update(allow_write=True, token_env="CHANGED_REFERENCE")
        for remark, expected in (("  测试机  ", "测试机"), ("   ", "")):
            response = self.client.put("/agents/" + key, json={**machine, "remark": remark})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["remark"], expected)
            row = self.registry.rows()[key]
            for field in ("config", "tested_at", "test_status"):
                self.assertEqual(row[field], before[field])
        reopened = AgentRegistry(WorkflowStore(self.root / "runtime.db"))
        self.assertEqual(reopened.rows()[key]["remark"], "")
        self.assertEqual(self.client.get("/agents").json()["agents"][0]["remark"], "")

    def test_remark_validation_and_omitted_field_compatibility(self):
        machine = self.machine(capabilities=["executor"])
        key = machine["agentId"]
        self.assertEqual(machine["remark"], "")
        saved = self.registry.save_agent({**machine, "remark": "😀" * 100}, key)
        for value in (None, 1, False, [], {}, "字" * 101):
            response = self.client.put("/agents/" + key, json={**machine, "remark": value})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("备注", response.json()["error"])
        body = {k: v for k, v in machine.items() if k != "remark"}
        self.assertEqual(self.registry.save_agent(body, key)["remark"], saved["remark"])
        self.assertEqual(AgentRegistry(self.gateway.store).rows()[key]["remark"], saved["remark"])

    def test_remark_edit_keeps_machine_identity(self):
        machine = self.machine(capabilities=["executor"])
        key = machine["agentId"]
        self.registry.record_test(key, True)
        before = self.registry.rows()[key]
        self.config_values["machine_defaults"].update(allow_write=True, token_env="CHANGED_REFERENCE")
        updated = self.registry.save_agent({**machine, "capabilities": ["executor"],
                                            "remark": "执行机"}, key)
        self.assertEqual(updated["agentId"], key)
        for field in ("config", "tested_at", "test_status"):
            self.assertEqual(self.registry.rows()[key][field], before[field])

    def test_inline_remark_endpoint_only_updates_remark_and_is_repeatable(self):
        machine = self.machine(capabilities=["executor"])
        key = machine["agentId"]
        self.registry.record_test(key, True)
        before = self.registry.rows()[key]
        self.config_values["machine_defaults"].update(allow_write=True, token_env="CHANGED_REFERENCE")
        for value in ("  卡片备注  ", "卡片备注", ""):
            response = self.client.put("/agents/" + key + "/remark", json={"remark": value})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"agentId": key, "remark": value.strip()})
            self.assertEqual(self.registry.rows()[key], {**before, "remark": value.strip()})
        for body in ({}, {"remark": None}, {"remark": "字" * 101}, {"remark": "备注", "enabled": False}):
            response = self.client.put("/agents/" + key + "/remark", json=body)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.put("/agents/missing/remark", json={"remark": "备注"}).status_code, 400)

    def test_business_delete_hides_machine_and_preserves_history(self):
        machine = self.machine()
        key = machine["agentId"]
        spec = {"workflowId": "deleted-history", "supervisorAgentId": key,
                "nodes": [{"id": "one", "prompt": "测试", "executor": {"agentId": key}}]}
        self.gateway.store.create_workflow(spec)
        for status in ("queued", "running", "cancelling"):
            with self.gateway.store._connect() as db:
                db.execute("UPDATE workflows SET status=? WHERE workflow_id='deleted-history'", (status,))
            response = self.client.delete("/agents/" + key)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn(key, self.registry.rows())
        with self.gateway.store._connect() as db:
            db.execute("UPDATE workflows SET status='completed' WHERE workflow_id='deleted-history'")
        before = self.registry.rows()[key]
        for _ in range(2):
            response = self.client.delete("/agents/" + key)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["deleted"])
        self.assertNotIn(key, self.registry.rows())
        self.assertNotIn(key, AgentRegistry(self.gateway.store).configs())
        self.assertEqual(self.client.get("/agents").json()["agents"], [])
        self.assertEqual(self.gateway.store.get_spec("deleted-history")["supervisorAgentId"], key)
        with self.gateway.store._connect() as db:
            archived = dict(db.execute("SELECT * FROM registered_agents WHERE id=?", (key,)).fetchone())
        self.assertIsNotNone(archived.pop("deleted_at"))
        self.assertEqual(archived, {k: v for k, v in before.items() if k != "deleted_at"})
        self.assertEqual(self.client.put("/agents/" + key, json=machine).status_code, 400)
        self.assertEqual(self.client.put("/agents/" + key + "/remark", json={"remark": "改名"}).status_code, 400)
        self.assertEqual(self.client.post("/agents/" + key + "/test", json={}).status_code, 400)
        with self.assertRaisesRegex(ValueError, "已删除"):
            self.gateway.store.create_workflow({**spec, "workflowId": "deleted-race"})
        replacement = self.machine()
        self.assertNotEqual(replacement["agentId"], key)
        self.assertEqual(self.gateway.store.get_spec("deleted-history")["supervisorAgentId"], key)
        self.assertEqual(self.gateway.store.get_workflow("deleted-history")["status"], "completed")
        self.assertEqual(self.client.delete("/agents/missing").status_code, 400)

    def test_business_delete_blocks_pending_installations(self):
        machine = self.machine(capabilities=["executor"])
        key = machine["agentId"]
        with self.gateway.store._connect() as db:
            db.execute("INSERT INTO skill_packages VALUES('delete-package','{}','2026-01-01')")
            db.execute("INSERT INTO skill_batches(id,package_id,request_json,created_at) VALUES('delete-batch','delete-package','{}','2026-01-01')")
            db.execute("INSERT INTO skill_tasks(id,batch_id,agent_id,name,state,created_at,updated_at) VALUES('delete-task','delete-batch',?,'test','queued','2026-01-01','2026-01-01')", (key,))
        with self.assertRaisesRegex(ValueError, "安装任务"):
            self.registry.delete_agent(key)
        self.assertIn(key, self.registry.rows())

    def test_old_machine_schema_upgrade_preserves_records(self):
        machine = self.machine(capabilities=["executor"])
        key = machine["agentId"]
        self.registry.record_test(key, True)
        before = self.registry.rows()[key]
        # Reconstruct the previous schema only in this temporary test database.
        with self.gateway.store._connect() as db:
            db.executescript("""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE old_agents (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
                    ip TEXT NOT NULL, port INTEGER NOT NULL, config TEXT NOT NULL,
                    test_status TEXT NOT NULL DEFAULT 'untested', tested_at TEXT, UNIQUE(ip, port));
                INSERT INTO old_agents SELECT id,group_id,ip,port,config,test_status,tested_at FROM registered_agents;
                DROP TABLE registered_agents;
                ALTER TABLE old_agents RENAME TO registered_agents;
            """)
        for _ in range(2):
            upgraded = AgentRegistry(self.gateway.store)
            self.assertEqual(upgraded.rows()[key], before)
        self.assertEqual(upgraded.save_agent({**machine, "remark": "升级后"}, key)["remark"], "升级后")

    def test_skill_settings_persist_and_do_not_elevate_permissions(self):
        machine = self.machine(capabilities=["executor"])
        key = machine["agentId"]
        rule = {"enabled": True, "root": "C:/Users/worker/.agents/skills"}
        response = self.client.put("/agents/" + key, json={**machine, "skillInstallation": rule})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["skillInstallation"]["enabled"])
        self.assertFalse(self.registry.configs()[key].allow_write)
        reopened = AgentRegistry(WorkflowStore(self.root / 'runtime.db'))
        self.assertEqual(reopened.skill_settings(key)["root"], "C:\\Users\\worker\\.agents\\skills")
        # Old clients/toggle actions omit the new field and must preserve it.
        self.registry.save_agent({k:v for k,v in machine.items() if k != "skillInstallation"}, key)
        self.assertTrue(reopened.skill_settings(key)["enabled"])
        public = self.client.get("/agents").json()["agents"][0]
        self.assertEqual(public["skillInstallation"], reopened.skill_settings(key))

    def test_skill_settings_reject_bad_paths_and_non_executor(self):
        machine = self.machine(capabilities=["executor"])
        for rule in ({"enabled": "true", "root": "/work/skills"},
                     {"enabled": True, "root": "../skills"},
                     {"enabled": True, "root": ""},
                     {"enabled": True, "root": "C:/work/../skills"}):
            response = self.client.put("/agents/" + machine["agentId"], json={**machine, "skillInstallation": rule})
            self.assertEqual(response.status_code, 400, response.text)
        response = self.client.put("/agents/" + machine["agentId"], json={**machine,
            "capabilities": ["supervisor"], "skillInstallation": {"enabled": True, "root": "/work/skills"}})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.registry.skill_settings(machine["agentId"])["enabled"])

    def test_legacy_skill_authorization_imports_once(self):
        machine = self.machine(capabilities=["executor"])
        key = machine["agentId"]
        # Simulate an old database before the additive settings table existed.
        with self.gateway.store._connect() as db:
            db.execute("DROP TABLE agent_skill_settings")
        self.config_values["skill_deployment"] = {"agents": {key: {"enabled": True, "root": "/work/skills"}}}
        registry = AgentRegistry(self.gateway.store)
        self.assertTrue(registry.skill_settings(key)["enabled"])
        registry.save_agent({**machine, "skillInstallation": {"enabled": False, "root": "/work/skills"}}, key)
        self.assertFalse(AgentRegistry(self.gateway.store).skill_settings(key)["enabled"])

    def test_web_skill_settings_detection_install_and_pending_protection(self):
        from skill_deployment import SkillDeployment
        from tests.test_skills import MemoryRemote, bundle
        import uuid
        self.config_values["machine_defaults"]["allow_write"] = True
        machine = self.machine(capabilities=["executor"])
        key = machine["agentId"]
        machine = self.registry.save_agent({**machine, "skillInstallation": {"enabled": True, "root": "/work/skills"}}, key)
        self.registry.record_test(key, True)
        remote = MemoryRemote()
        manager = SkillDeployment(self.gateway, {"package_root": str(self.root / "packages")}, remote)
        self.app.state.skills = manager
        with patch("skill_deployment.Orchestrator._resolve_agent_token", return_value=None):
            result = self.client.post("/skills/machines/" + key + "/check", json={})
            self.assertTrue(result.json()["passed"], result.text)
            self.assertEqual(remote.writes, [])
            self.assertIsNotNone(self.registry.skill_settings(key)["checkedAt"])
            self.assertEqual(self.client.post("/skills/machines/" + key + "/check", json={"root":"/outside"}).status_code, 400)
            package = manager.upload(bundle(), self.group)
            batch = manager.create({"groupId": self.group, "requestId":str(uuid.uuid4()), "packageId":package["id"], "agentIds":[key]})
            with self.assertRaisesRegex(ValueError, "待处理"):
                self.registry.save_agent({**machine, "skillInstallation":{"enabled":True,"root":"/work/other"}}, key)
            asyncio.run(manager.run(manager.store.claim()))
            self.assertEqual(manager.store.batch(batch["id"])["tasks"][0]["state"], "completed")
            self.registry.save_agent({**machine, "skillInstallation":{"enabled":False,"root":"/work/skills"}}, key)
            self.assertFalse(manager.eligibility(key)["eligible"])
            self.assertIn("/work/skills/demo/SKILL.md", remote.files)
            self.registry.save_agent({**machine, "skillInstallation":{"enabled":True,"root":"/work/other"}}, key)
            self.assertIsNone(self.registry.skill_settings(key)["checkedAt"])
            remote.unsupported = "fs/getMetadata"
            result = self.client.post("/skills/machines/" + key + "/check", json={})
            self.assertFalse(result.json()["passed"])
            self.assertNotIn("private", result.text)

    def test_active_runtime_blocks_machine_move_and_creation_rechecks_group(self):
        machine = self.machine()
        key = machine["agentId"]
        other = self.registry.save_group({"name": "运行归组"})["id"]
        spec = {"workflowId": "group-atomic", "name": "分组事务", "groupId": self.group,
                "supervisorAgentId": key,
                "nodes": [{"id": "one", "prompt": "测试", "executor": {"agentId": key}}]}
        self.gateway.store.create_workflow(spec)
        self.assertEqual(self.gateway.store.get_spec("group-atomic")["groupId"], self.group)
        with self.assertRaisesRegex(ValueError, "关联任务运行"):
            self.registry.save_agent({**machine, "groupId": other}, key)
        self.assertEqual(self.registry.rows()[key]["group_id"], self.group)
        with self.gateway.store._connect() as db:
            db.execute("UPDATE workflows SET status='completed' WHERE workflow_id='group-atomic'")
        self.registry.save_agent({**machine, "groupId": other}, key)
        with self.assertRaisesRegex(ValueError, "工作流的分组"):
            self.gateway.store.create_workflow({**spec, "workflowId": "group-race"})
        with self.gateway.store._connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM workflows WHERE workflow_id='group-race'").fetchone())

    def test_workflow_group_is_checked_at_registry_boundary(self):
        machine = self.machine()
        key = machine["agentId"]
        other = self.registry.save_group({"name": "另一业务组"})["id"]
        self.registry.validate(key, [], group_id=self.group)
        # Legacy independent callers remain compatible without groupId.
        self.registry.validate(key, [])
        for group in (other, "bad", "", 123):
            with self.subTest(group=group), self.assertRaises(ValueError):
                self.registry.validate(key, [], group_id=group)
        response = self.client.post("/agents/validate", json={
            "supervisorId": key, "executorIds": [key], "groupId": other})
        self.assertEqual(response.status_code, 400)

    def test_workflow_group_survives_normalization_and_invalid_values_rejected(self):
        spec = {"workflowId": "group-test", "name": "分组测试", "groupId": self.group,
                "nodes": [{"id": "one", "prompt": "测试", "executor": {"agentId": "local"}}]}
        normalized = WorkflowStore.normalize_spec(spec)
        self.assertEqual(normalized["groupId"], self.group)
        self.assertEqual(WorkflowStore.normalize_spec(normalized)["groupId"], self.group)
        for invalid in (None, "", "missing", 10, {}, []):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "groupId"):
                WorkflowStore.normalize_spec({**spec, "groupId": invalid})
        legacy = dict(spec)
        del legacy["groupId"]
        self.assertNotIn("groupId", WorkflowStore.normalize_spec(legacy))

    def test_persistence_and_test_invalidation(self):
        machine = self.machine()
        key = machine["agentId"]
        self.registry.record_test(key, True)
        restored = AgentRegistry(WorkflowStore(self.root / "runtime.db"))
        self.assertEqual(restored.rows()[key]["test_status"], "passed")
        self.assertEqual(restored.configs()[key].capacity, 1)
        body = {**machine, "enabled": False}
        self.registry.save_agent(body, key)
        self.assertEqual(self.registry.rows()[key]["test_status"], "passed")
        with self.assertRaisesRegex(ValueError, "启用"):
            self.registry.validate(key, [], require_test=True)
        self.registry.save_agent({**body, "port": 4501}, key)
        self.assertEqual(self.registry.rows()[key]["test_status"], "untested")

    def test_input_and_duplicate_validation(self):
        machine = self.machine()
        for change in ({"ip": "example.com"}, {"ip": 123}, {"port": True}, {"port": 65536},
                       {"capabilities": []}, {"capabilities": [{}]}, {"groupId": "missing"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.registry.save_agent({**machine, **change})
        with self.assertRaisesRegex(ValueError, "已登记"):
            self.machine()
        with self.assertRaisesRegex(ValueError, "仍有机器"):
            self.registry.delete_group(self.group)

    def test_group_soft_delete_preserves_history_and_rejects_new_use(self):
        machine = self.machine(capabilities=["executor"])
        self.registry.delete_agent(machine["agentId"])
        for _ in range(2):
            response = self.client.delete("/agent-groups/" + self.group)
            self.assertEqual(response.status_code, 200, response.text)
        reopened = AgentRegistry(WorkflowStore(self.root / "runtime.db"))
        self.assertEqual(reopened.groups(), [])
        with self.gateway.store._connect() as db:
            row = db.execute("SELECT * FROM agent_groups WHERE id=?", (self.group,)).fetchone()
            self.assertEqual(row["name"], "第一组")
            self.assertIsNotNone(row["deleted_at"])
            self.assertEqual(db.execute("SELECT group_id FROM registered_agents WHERE id=?",
                                       (machine["agentId"],)).fetchone()[0], self.group)
            from skill_packages import SkillError
            from skill_store import SkillStore
            from mcp_store import McpStore
            with self.assertRaises(SkillError):
                SkillStore(self.gateway.store).require_group(db, self.group)
            with self.assertRaises(SkillError):
                McpStore(self.gateway.store).group(db, self.group)
        with self.assertRaisesRegex(ValueError, "有效分组"):
            self.machine("127.0.0.2")
        with self.assertRaisesRegex(ValueError, "找不到分组"):
            reopened.save_group({"name": "不能恢复"}, self.group)

    def test_old_group_schema_upgrade_preserves_records(self):
        with self.gateway.store._connect() as db:
            db.execute("ALTER TABLE agent_groups DROP COLUMN deleted_at")
        for _ in range(2):
            reopened = AgentRegistry(self.gateway.store)
            self.assertEqual(reopened.groups()[0]["id"], self.group)
        reopened.delete_group(self.group)
        self.assertEqual(reopened.groups(), [])

    def test_group_crud_and_scope(self):
        other = self.registry.save_group({"name": "第二组"})["id"]
        self.registry.save_group({"name": "新名称"}, other)
        supervisor = self.machine()
        executor = self.machine("127.0.0.2", ["executor"], other)
        with self.assertRaisesRegex(ValueError, "同一分组"):
            self.registry.validate(supervisor["agentId"], [executor["agentId"]])
        with self.assertRaisesRegex(ValueError, "能力"):
            self.registry.validate(executor["agentId"], [])
        self.registry.validate(supervisor["agentId"], [])
        with self.assertRaisesRegex(ValueError, "检测"):
            self.registry.validate(supervisor["agentId"], [], require_test=True)

    def test_detection_and_public_response(self):
        machine = self.machine()
        key = machine["agentId"]
        with patch.object(self.gateway.orchestrator, "probe_agent", new=AsyncMock()):
            response = self.client.post(f"/agents/{key}/test", json={})
            self.assertFalse(response.json()["passed"])
            self.assertIn("尚未上报有效心跳", response.json()["message"])
            self.gateway.store.record_sidecar_heartbeat(key, "instance", "2026-09-11T00:00:00+00:00", lease_timeout_sec=20)
            self.assertTrue(self.client.post(f"/agents/{key}/test", json={}).json()["passed"])
        with patch.object(self.gateway.orchestrator, "probe_agent", new=AsyncMock(side_effect=RuntimeError("private error"))):
            response = self.client.post(f"/agents/{key}/test", json={})
            self.assertFalse(response.json()["passed"])
            self.assertNotIn("private error", response.text)
        listing = self.client.get("/agents").json()
        self.assertEqual(listing["source"], "registry")
        self.assertNotIn("token", json.dumps(listing))

    def test_detection_reports_safe_credential_failure_without_connecting(self):
        token_path = self.root / "private-credential.token"
        defaults = self.config_values["machine_defaults"]
        defaults.pop("token_env")
        defaults["token_file"] = str(token_path)
        key = self.machine()["agentId"]
        for content, expected in ((None, "不存在"), (b"", "为空"),
                                  (b"\xff\xfe", "UTF-8"),
                                  (b"private-secret\nsecond-line", "一行令牌"),
                                  (b"x" * 8193, "大小限制")):
            with self.subTest(expected=expected):
                if content is not None:
                    token_path.write_bytes(content)
                with patch.object(self.gateway.orchestrator, "_client_factory") as factory:
                    with self.assertLogs("workflow_gateway", level="INFO") as captured:
                        result = self.client.post(f"/agents/{key}/test", json={}).json()
                    factory.assert_not_called()
                self.assertFalse(result["passed"])
                self.assertIn(expected, result["message"])
                output = json.dumps(result, ensure_ascii=False) + str(captured.output)
                for private in (str(token_path), key, "private-secret"):
                    self.assertNotIn(private, output)
                self.assertEqual(self.registry.rows()[key]["test_status"], "failed")

    def test_detection_reports_network_failure_without_raw_exception(self):
        key = self.machine()["agentId"]
        for error, expected in ((TimeoutError("private"), "超时"),
                                (ConnectionError("private"), "连接失败")):
            with self.subTest(expected=expected), patch.object(
                self.gateway.orchestrator, "probe_agent", new=AsyncMock(side_effect=error)
            ):
                result = self.client.post(f"/agents/{key}/test", json={}).json()
                self.assertFalse(result["passed"])
                self.assertIn(expected, result["message"])
                self.assertNotIn("private", result["message"])

    def test_remote_group_fetch_is_authenticated_and_filtered(self):
        supervisor = self.machine()
        other = self.registry.save_group({"name": "其他组"})["id"]
        excluded = self.machine("127.0.0.2", ["executor"], other)
        (self.root / "127.0.0.1.token").write_text("test-only-machine-secret", encoding="utf-8")
        self.assertEqual(self.client.get("/internal/v1/agents").status_code, 401)
        response = self.client.get("/internal/v1/agents", headers={"Authorization": "Bearer test-only-machine-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(supervisor["agentId"], response.json()["agents"])
        self.assertNotIn(excluded["agentId"], response.json()["agents"])
        self.assertNotIn("test-only-machine-secret", response.text)
        self.assertNotIn("sidecar_token", response.text)

    def test_distinct_supervisors_require_distinct_credential_references(self):
        machine = self.machine()
        with self.assertRaisesRegex(ValueError, "凭据引用重复"):
            self.registry.save_agent({**machine, "port": 4501})
        with patch.dict(self.config_values["machine_defaults"], {"sidecar_token_template": str(self.root / "{ip}-{port}.token")}):
            second = self.registry.save_agent({**machine, "port": 4501})
        self.assertNotEqual(machine["agentId"], second["agentId"])

    def test_full_access_defaults_apply_on_save_and_reach_remote(self):
        machine = self.machine()
        key = machine['agentId']
        self.assertFalse(self.registry.configs()[key].allow_full_access)
        self.config_values['machine_defaults'].update(allow_write=True, allow_full_access=True)
        self.assertFalse(self.registry.configs()[key].allow_full_access)
        self.registry.save_agent(machine, key)
        restored = AgentRegistry(WorkflowStore(self.root / 'runtime.db'))
        self.assertTrue(restored.configs()[key].allow_full_access)
        public = next(item for item in self.client.get('/agents').json()['agents'] if item['agentId'] == key)
        self.assertIn('full_access', public['permissionProfiles'])
        (self.root / '127.0.0.1.token').write_text('test-only-machine-secret', encoding='utf-8')
        response = self.client.get('/internal/v1/agents', headers={'Authorization': 'Bearer test-only-machine-secret'})
        self.assertEqual(response.status_code, 200)
        remote = response.json()['agents'][key]
        self.assertTrue(remote['allow_full_access'])
        self.assertTrue(remote['allow_write'])
        self.config_values['machine_defaults']['allow_full_access'] = False
        self.registry.save_agent(machine, key)
        self.assertNotIn('full_access', self.registry.configs()[key].public_dict()['permission_profiles'])

    def test_artifact_root_sync_on_save_is_persistent_and_reaches_remote(self):
        machine = self.machine()
        key = machine["agentId"]
        before = self.registry.configs()[key]
        self.registry.record_test(key, True)
        defaults = self.config_values["machine_defaults"]
        root = str(self.root / "conversation-artifacts")
        defaults.update(artifact_root=root, allow_write=True)
        self.assertIsNone(self.registry.configs()[key].artifact_root)
        response = self.client.put(f"/agents/{key}", json={
            **machine, "artifact_root": "ignored-request-path"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["agentId"], key)
        restored = AgentRegistry(WorkflowStore(self.root / "runtime.db"))
        config = restored.configs()[key]
        self.assertEqual(config.artifact_root, root)
        self.assertTrue(config.allow_write)
        self.assertEqual(config.cwd, before.cwd)
        self.assertEqual(config.sidecar_token_file, before.sidecar_token_file)
        self.assertEqual(restored.rows()[key]["test_status"], "passed")
        (self.root / "127.0.0.1.token").write_text("test-only-machine-secret", encoding="utf-8")
        remote = self.client.get("/internal/v1/agents", headers={
            "Authorization": "Bearer test-only-machine-secret"})
        self.assertEqual(remote.status_code, 200)
        self.assertEqual(remote.json()["agents"][key]["artifact_root"], root)
        defaults.pop("artifact_root")
        self.registry.save_agent(machine, key)
        self.assertEqual(restored.configs()[key].artifact_root, root)
        defaults["artifact_root"] = "relative/path"
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            self.registry.save_agent(machine, key)
        self.assertEqual(restored.configs()[key].artifact_root, root)
        defaults["artifact_root"] = None
        self.registry.save_agent(machine, key)
        self.assertEqual(restored.configs()[key].artifact_root, root)

    def test_submission_rejects_unchecked_and_cross_group_before_persistence(self):
        supervisor = self.machine()
        spec = {"workflowId": "registry-run", "supervisorAgentId": supervisor["agentId"],
                "nodes": [{"id": "step", "agentId": supervisor["agentId"], "prompt": "test"}]}
        with self.assertRaisesRegex(ValueError, "检测"):
            asyncio.run(self.gateway.submit(spec))
        with self.assertRaises(ValueError):
            self.gateway.store.get_spec("registry-run")
        self.registry.record_test(supervisor["agentId"], True)
        other = self.registry.save_group({"name": "另一组"})["id"]
        executor = self.machine("127.0.0.2", ["executor"], other)
        self.registry.record_test(executor["agentId"], True)
        spec["nodes"][0]["agentId"] = executor["agentId"]
        with self.assertRaisesRegex(ValueError, "同一分组"):
            asyncio.run(self.gateway.submit(spec))

    def test_same_group_submission_and_dispatch_recheck(self):
        supervisor = self.machine()
        worker = self.machine("127.0.0.2", ["executor"])
        for key in (supervisor["agentId"], worker["agentId"]):
            self.registry.record_test(key, True)
        spec = {"workflowId": "registered-run", "supervisorAgentId": supervisor["agentId"],
                "nodes": [{"id": "step", "agentId": worker["agentId"], "prompt": "test"}]}
        with patch.object(self.gateway, "_schedule_pending", new=AsyncMock()):
            snapshot = asyncio.run(self.gateway.submit(spec))
        self.assertEqual(snapshot["workflowId"], "registered-run")
        prepared = self.gateway.store.prepare_node_dispatch("registered-run", "step")
        self.assertEqual(prepared["agentId"], worker["agentId"])
        other = self.registry.save_group({"name": "移动目标"})["id"]
        with self.assertRaisesRegex(ValueError, "仍有关联任务运行"):
            self.registry.save_agent({**worker, "groupId": other}, worker["agentId"])
        # Simulate an out-of-band change to retain dispatch boundary coverage.
        with self.gateway.store._connect() as connection:
            connection.execute("UPDATE registered_agents SET group_id = ? WHERE id = ?",
                               (other, worker["agentId"]))
        with self.assertRaisesRegex(ValueError, "同一分组"):
            self.gateway.store.prepare_node_dispatch("registered-run", "step")

    def test_executor_can_become_supervisor_and_ipv6_is_bracketed(self):
        worker = self.machine("::1", ["executor"])
        self.assertEqual(self.registry.configs()[worker["agentId"]].url, "ws://[::1]:4500")
        self.registry.save_agent({**worker, "capabilities": ["supervisor"]}, worker["agentId"])
        self.assertEqual(self.registry.configs()[worker["agentId"]].orchestration_mode, "remote_sidecar")

    def test_save_syncs_execution_credentials_and_invalidates_only_when_changed(self):
        machine = self.machine()
        key = machine["agentId"]
        before = self.registry.configs()[key]
        defaults = self.config_values["machine_defaults"]
        for source in ({"token_file": str(self.root / "new.token")},
                       {"token_env": "NEW_TEST_TOKEN"}, {}):
            with self.subTest(source=source):
                self.registry.record_test(key, True)
                defaults.pop("token_env", None)
                defaults.pop("token_file", None)
                defaults.update(source)
                response = self.client.put(f"/agents/{key}", json={
                    **machine, "token_file": "ignored-request.token"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["agentId"], key)
                self.assertEqual(response.json()["testStatus"], "untested")
                self.assertIsNone(response.json()["testedAt"])
                restored = AgentRegistry(WorkflowStore(self.root / "runtime.db"))
                config = restored.configs()[key]
                self.assertEqual(config.token_file, source.get("token_file"))
                self.assertEqual(config.token_env, source.get("token_env"))
                self.assertEqual(config.sidecar_token_file, before.sidecar_token_file)
                self.assertEqual(config.cwd, before.cwd)
                self.assertEqual(config.url, before.url)
                self.registry.validate(key, [])
                with self.assertRaisesRegex(ValueError, "检测"):
                    self.registry.validate(key, [], require_test=True)
                self.registry.record_test(key, True)
                tested_at = self.registry.rows()[key]["tested_at"]
                self.client.put(f"/agents/{key}", json=machine)
                self.assertEqual(self.registry.rows()[key]["test_status"], "passed")
                self.assertEqual(self.registry.rows()[key]["tested_at"], tested_at)

    def test_default_changes_do_not_rewrite_existing_sidecar_credentials(self):
        machine = self.machine()
        key = machine["agentId"]
        before = self.registry.configs()[key].sidecar_token_file
        self.registry.record_test(key, True)
        with patch.dict(self.config_values["machine_defaults"], {"sidecar_token_template": str(self.root / "changed-{ip}-{port}.token")}):
            self.registry.save_agent({**machine, "enabled": False}, key)
        self.assertEqual(self.registry.configs()[key].sidecar_token_file, before)
        self.assertEqual(self.registry.rows()[key]["test_status"], "passed")

    def test_remote_client_parses_fetched_group_and_does_not_read_sqlite(self):
        from workflow_runtime_client import InternalApiClient
        supervisor = self.machine()
        with self.gateway.store._connect() as db:
            row = self.registry.rows()[supervisor['agentId']]
            old_config = json.loads(row['config'])
            old_config['allow_cwd_override'] = False
            db.execute('UPDATE registered_agents SET config=? WHERE id=?',
                       (json.dumps(old_config), supervisor['agentId']))
        self.machine("127.0.0.2", ["executor"])
        (self.root / "127.0.0.1.token").write_text("test-only-machine-secret", encoding="utf-8")
        runtime = InternalApiClient("http://central.test", "registered-machine", token_env="FIXTURE", started_at="2026-09-11T00:00:00+00:00")
        def request(method, path, payload=None):
            response = self.client.request(method, path, headers={"Authorization": "Bearer test-only-machine-secret"}, json=payload)
            self.assertEqual(response.status_code, 200)
            return response.json()
        with patch.object(runtime, "_request", side_effect=request):
            agents = runtime.group_agents()
        self.assertEqual(len(agents), 1)  # 无租约时不能获取其他机器的连接凭据。
        self.assertIn(supervisor["agentId"], agents)
        self.assertTrue(all(agent.sidecar_token_file is None for agent in agents.values()))
        self.assertTrue(all(agent.allow_cwd_override for agent in agents.values()))

    def test_registered_directory_override_reaches_execution_and_preserves_permissions(self):
        from tests.mock_app_server import MockAppServer
        from urllib.parse import urlparse
        from codex_orchestrator_mcp import Orchestrator

        async def run():
            async with MockAppServer(delay_sec=0.01) as server:
                with patch.dict(self.config_values['machine_defaults'], {'token_env': None}):
                    machine = self.registry.save_agent({
                        'ip': '127.0.0.1', 'port': urlparse(server.url).port,
                        'groupId': self.group, 'capabilities': ['executor']})
                key = machine['agentId']
                self.assertTrue(self.registry.configs()[key].allow_cwd_override)
                # 已登记记录中的旧标志不会继续阻止节点目录生效。
                with self.gateway.store._connect() as db:
                    stored = json.loads(self.registry.rows()[key]['config'])
                    stored['allow_cwd_override'] = False
                    db.execute('UPDATE registered_agents SET config=? WHERE id=?', (json.dumps(stored), key))
                restored = AgentRegistry(WorkflowStore(self.root / 'runtime.db'))
                runner = Orchestrator()
                runner.agent_provider = restored.configs
                options = dict(agent_id=key, prompt='test', thread_id=None, write=False,
                               model=None, timeout_sec=10)
                alternate = str(self.root / 'another-workspace')
                for cwd in (None, alternate):
                    job = await runner.dispatch(**options, cwd=cwd)
                    self.assertEqual((await runner.wait(job.job_id, 2)).status, 'completed')
                starts = [request['params'] for request in server.requests if request.get('method') == 'thread/start']
                self.assertEqual([item['cwd'] for item in starts], [str(self.root), alternate])
                with self.assertRaisesRegex(ValueError, '绝对路径'):
                    await runner.dispatch(**options, cwd='relative/path')
                with self.assertRaisesRegex(PermissionError, '写权限'):
                    await runner.dispatch(**{**options, 'write': True}, cwd=alternate)
                self.assertFalse(restored.configs()[key].allow_full_access)
                self.assertTrue(self.client.get('/agents').json()['agents'][0]['allowCwdOverride'])

        asyncio.run(run())


    def test_no_source_switch_or_import_route(self):
        with patch.dict(os.environ, {'CODEX_AGENT_SOURCE': 'file', 'CODEX_AGENTS_FILE': 'not-used.json'}):
            app = create_app(db_path=self.root / 'new.db')
            with TestClient(app) as client:
                self.assertEqual(client.get('/agents').json()['agents'], [])
                self.assertIn(client.post('/agents/import', json={}).status_code, (404, 405))
                response = client.post('/agent-groups', json={'name': '新部署'})
                self.assertEqual(response.status_code, 200)

    def test_local_supervisor_is_registered_and_dispatches_from_sqlite(self):
        with patch.dict(self.config_values['machine_defaults'], {'orchestration_mode': 'local_db'}):
            machine = self.machine()
        key = machine['agentId']
        worker = self.machine('127.0.0.2', ['executor'])['agentId']
        self.registry.record_test(worker, True)
        self.assertEqual(self.registry.configs()[key].orchestration_mode, 'local_db')
        self.assertIsNone(self.registry.configs()[key].sidecar_token_file)
        self.registry.record_test(key, True)
        from codex_orchestrator_mcp import Orchestrator
        with patch('codex_orchestrator_mcp.get_workflow_store', return_value=self.gateway.store):
            self.assertIn(key, Orchestrator().load_agents())
        with patch.object(self.gateway, '_schedule_pending', new=AsyncMock()):
            asyncio.run(self.gateway.submit({'workflowId': 'local-registered', 'supervisorAgentId': key,
                'nodes': [{'id': 'a', 'agentId': worker, 'prompt': 'test'}]}))
        self.assertEqual(self.gateway.store.prepare_node_dispatch('local-registered', 'a')['agentId'], worker)


if __name__ == "__main__":
    unittest.main()
