"""分组收录、下发边界和旧数据库兼容回归。"""

import asyncio
import json
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_registry import AgentRegistry
from skill_deployment import SkillDeployment
from skill_packages import SkillError
from skill_store import SkillStore
from workflow_store import WorkflowStore
from tests.test_skills import bundle, MemoryRemote


class SkillGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = patch("workflow_service_config._load", return_value={
            "machine_defaults": {"cwd": "/work", "allow_write": True}})
        self.config.start()
        self.addCleanup(self.config.stop)
        self.db = WorkflowStore(Path(self.temp.name) / "state.db")
        self.registry = AgentRegistry(self.db)
        self.a = self.registry.save_group({"name": "甲组"})["id"]
        self.b = self.registry.save_group({"name": "乙组"})["id"]
        self.machine = self.registry.save_agent({"groupId": self.a, "ip": "192.0.2.1", "port": 4500,
            "capabilities": ["executor"], "skillInstallation": {"enabled": True, "root": "/work/skills"}})
        self.agent = self.machine["agentId"]
        self.registry.record_test(self.agent, True)
        self.remote = MemoryRemote()
        self.manager = SkillDeployment(SimpleNamespace(store=self.db, registry=self.registry),
            {"package_root": str(Path(self.temp.name) / "packages")}, self.remote)
        self.store = self.manager.store
        self.package = self.manager.upload(bundle(), self.a)

    def request(self, group=None):
        return {"requestId": str(uuid.uuid4()), "groupId": group or self.a,
                "packageId": self.package["id"], "agentIds": [self.agent]}

    def assign(self, group, *keys):
        return self.store.assign({"groupId": group, "packageIds": list(keys or [self.package["id"]])})

    def test_multi_group_dedup_and_batch_rollback(self):
        self.assign(self.b)
        self.assign(self.b)
        self.assertEqual(len(self.store.packages()), 1)
        self.assertEqual({g["id"] for g in self.store.package(self.package["id"])["groups"]}, {self.a, self.b})
        other = self.registry.save_group({"name": "丙组"})["id"]
        with self.assertRaises(SkillError):
            self.assign(other, self.package["id"], "missing")
        self.assertEqual(self.store.packages(other), [])
        self.assertEqual([g["skillCount"] for g in self.registry.groups() if g["id"] in {self.a, self.b}], [1, 1])

    def test_upload_requires_group_and_same_bytes_join_another_group(self):
        with self.assertRaises(SkillError):
            self.manager.upload(bundle(), None)
        duplicate = self.manager.upload(bundle(), self.b)
        self.assertEqual(duplicate["id"], self.package["id"])
        self.assertEqual(len(list(self.manager.root.iterdir())), 1)

    def test_invalid_and_cross_group_requests_leave_no_batch(self):
        for value in (None, "bad", str(uuid.uuid4())):
            with self.assertRaises(SkillError):
                self.store.validate_group(value)
        with self.assertRaisesRegex(SkillError, "加入"):
            self.manager.create(self.request(self.b))
        self.assign(self.b)
        with self.assertRaisesRegex(SkillError, "执行机"):
            self.manager.create(self.request(self.b))
        self.assertEqual(self.store.batches(), [])

    def test_frozen_history_idempotency_and_move_protection(self):
        request = self.request()
        batch = self.manager.create(request)
        self.assertEqual(self.manager.create(request)["id"], batch["id"])
        with self.assertRaises(SkillError):
            self.manager.create({**request, "groupId": self.b})
        with self.assertRaisesRegex(ValueError, "待处理"):
            self.registry.save_agent({**self.machine, "groupId": self.b}, self.agent)
        task = self.store.claim()
        with self.assertRaisesRegex(ValueError, "待处理"):
            self.registry.save_agent({**self.machine, "groupId": self.b}, self.agent)
        self.store.finish(task, "测试失败")
        self.registry.save_group({"name": "甲组改名"}, self.a)
        self.registry.save_agent({**self.machine, "groupId": self.b}, self.agent)
        self.assertEqual(self.store.batch(batch["id"])["groupName"], "甲组")
        self.assertEqual(len(self.store.batches(self.a)), 1)
        self.assertEqual(self.store.batches(self.b), [])
        self.assertEqual(self.store.inventory(self.a), [])
        self.assertEqual(len(self.store.inventory(self.b)), 1)
        self.assertFalse(self.store.inventory(self.b)[0]["canRetry"])
        with self.assertRaises(SkillError):
            self.store.action(task["id"], str(uuid.uuid4()), "retry")
        self.registry.record_test(self.agent, True)
        self.store.action(task["id"], str(uuid.uuid4()), "check")
        with patch("skill_deployment.Orchestrator._resolve_agent_token", return_value=None):
            asyncio.run(self.manager.run(self.store.claim()))
        self.assertFalse(self.remote.writes)

    def test_execution_rechecks_membership_before_remote_writes(self):
        self.manager.create(self.request())
        task = self.store.claim()
        # 模拟外部修改，验证后台还有独立边界，正常改组接口不允许这样操作。
        with self.db._connect() as db:
            db.execute("UPDATE registered_agents SET group_id=? WHERE id=?", (self.b, self.agent))
        asyncio.run(self.manager.run(task))
        self.assertFalse(self.remote.writes)
        self.assertEqual(self.store.batch(task["batch_id"])["tasks"][0]["state"], "failed")

    def test_limits_apply_after_filtering_and_deletion_protects_history(self):
        old = self.manager.create(self.request())
        task = self.store.claim()
        self.store.finish(task, "测试失败")
        self.registry.save_agent({**self.machine, "groupId": self.b}, self.agent)
        self.assign(self.b)
        with self.db._connect() as db:
            for n in range(205):
                key = str(uuid.uuid4())
                meta = {**self.package, "id": key}
                db.execute("INSERT INTO skill_packages VALUES(?,?,?)", (key, json.dumps(meta), "9999"))
                db.execute("INSERT INTO skill_group_packages VALUES(?,?)", (self.b, key))
                db.execute("INSERT INTO skill_batches(id,package_id,request_json,created_at,group_id,group_name) VALUES(?,?,?,?,?,?)",
                           (key, key, "{}", "9999", self.b, "乙组"))
        self.assertEqual(len(self.store.packages(self.a)), 1)
        self.assertEqual(self.store.batches(self.a)[0]["id"], old["id"])
        with self.assertRaisesRegex(ValueError, "Skill"):
            self.registry.delete_group(self.a)
        with self.db._connect() as db:
            db.execute("DELETE FROM skill_group_packages WHERE group_id=?", (self.a,))
        with self.assertRaisesRegex(ValueError, "历史"):
            self.registry.delete_group(self.a)

    def test_concurrent_move_and_deployment_never_cross_groups(self):
        def move():
            try:
                self.registry.save_agent({**self.machine, "groupId": self.b}, self.agent)
                return True
            except ValueError:
                return False
        def deploy():
            try:
                self.manager.create(self.request())
                return True
            except SkillError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(move), pool.submit(deploy)
            self.assertNotEqual(first.result(), second.result())

    def test_old_database_migration_preserves_pending_and_unassigned(self):
        batch = self.manager.create(self.request())
        # 还原旧版四列批次表，不改已有任务及安装记录。
        with self.db._connect() as db:
            db.execute("DELETE FROM skill_group_packages")
            db.execute("DROP INDEX skill_batch_group")
            db.execute("ALTER TABLE skill_batches DROP COLUMN group_id")
            db.execute("ALTER TABLE skill_batches DROP COLUMN group_name")
        restored = SkillStore(self.db)
        SkillStore(self.db)
        self.assertEqual(restored.package(self.package["id"])["groups"], [])
        self.assertIsNone(restored.batch(batch["id"])["groupId"])
        task = restored.claim()
        with patch("skill_deployment.Orchestrator._resolve_agent_token", return_value=None):
            asyncio.run(self.manager.run(task))
        self.assertEqual(restored.batch(batch["id"])["tasks"][0]["state"], "completed")
        restored.action(task["id"], str(uuid.uuid4()), "check")
        restored.finish(restored.claim(), "测试失败")
        with self.assertRaisesRegex(SkillError, "旧下发"):
            restored.action(task["id"], str(uuid.uuid4()), "retry")
        self.assign(self.a)
        self.assertTrue(self.manager.create(self.request())["groupId"])
