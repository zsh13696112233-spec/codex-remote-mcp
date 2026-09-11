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
        self.env = patch.dict(os.environ, {
            "CODEX_AGENT_SOURCE": "registry", "CODEX_MACHINE_CWD": str(self.root),
            "CODEX_MACHINE_PROTOCOL": "ws", "CODEX_MACHINE_TOKEN_ENV": "REGISTRY_TEST_APP_TOKEN",
            "CODEX_MACHINE_SIDECAR_TOKEN_TEMPLATE": str(self.root / "{ip}.token"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = create_app(db_path=self.root / "runtime.db", config_path=self.root / "legacy.json")
        self.gateway = self.app.state.gateway
        self.registry = self.gateway.registry
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.group = self.registry.save_group({"name": "第一组"})["id"]

    def machine(self, ip="127.0.0.1", capabilities=None, group=None):
        return self.registry.save_agent({"ip": ip, "port": 4500, "groupId": group or self.group,
                                         "capabilities": capabilities or ["supervisor", "executor"]})

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
            self.registry.validate(key, [key], require_test=True)
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

    def test_group_crud_and_scope(self):
        other = self.registry.save_group({"name": "第二组"})["id"]
        self.registry.save_group({"name": "新名称"}, other)
        supervisor = self.machine()
        executor = self.machine("127.0.0.2", ["executor"], other)
        with self.assertRaisesRegex(ValueError, "同一分组"):
            self.registry.validate(supervisor["agentId"], [executor["agentId"]])
        with self.assertRaisesRegex(ValueError, "能力"):
            self.registry.validate(executor["agentId"], [])
        self.registry.validate(supervisor["agentId"], [supervisor["agentId"]])
        with self.assertRaisesRegex(ValueError, "检测"):
            self.registry.validate(supervisor["agentId"], [], require_test=True)

    def test_detection_and_public_response(self):
        machine = self.machine()
        key = machine["agentId"]
        with patch.object(self.gateway.orchestrator, "probe_agent", new=AsyncMock()):
            response = self.client.post(f"/agents/{key}/test", json={})
            self.assertFalse(response.json()["passed"])
            self.gateway.store.record_sidecar_heartbeat(key, "instance", "2026-09-11T00:00:00+00:00", lease_timeout_sec=20)
            self.assertTrue(self.client.post(f"/agents/{key}/test", json={}).json()["passed"])
        with patch.object(self.gateway.orchestrator, "probe_agent", new=AsyncMock(side_effect=RuntimeError("private error"))):
            response = self.client.post(f"/agents/{key}/test", json={})
            self.assertFalse(response.json()["passed"])
            self.assertNotIn("private error", response.text)
        listing = self.client.get("/agents").json()
        self.assertEqual(listing["source"], "registry")
        self.assertNotIn("token", json.dumps(listing))

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

    def test_import_once_preserves_ids_and_settings(self):
        path = self.root / "legacy.json"
        path.write_text(json.dumps({"agents": {"local": {"url": "ws://127.0.0.1:4500", "cwd": str(self.root), "allow_write": True}}}), encoding="utf-8")
        self.assertEqual(self.registry.import_file(path), {"imported": 1})
        config = self.registry.configs()["local"]
        self.assertTrue(config.allow_write)
        self.assertEqual(config.orchestration_mode, "local_db")
        self.assertEqual(self.registry.rows()["local"]["test_status"], "untested")
        machine = next(row for row in self.registry.public() if row["agentId"] == "local")
        self.registry.save_agent({**machine, "enabled": False}, "local")
        self.assertEqual(self.registry.configs()["local"].orchestration_mode, "local_db")
        with self.assertRaisesRegex(ValueError, "已经导入"):
            self.registry.import_file(path)
        self.assertTrue(path.exists())

    def test_imported_domain_can_be_disabled_without_rewriting_url(self):
        path = self.root / "legacy.json"
        path.write_text(json.dumps({"agents": {"old": {"url": "wss://worker.internal/codex", "cwd": str(self.root)}}}), encoding="utf-8")
        self.registry.import_file(path)
        machine = next(row for row in self.registry.public() if row["agentId"] == "old")
        self.registry.save_agent({**machine, "enabled": False}, "old")
        self.assertEqual(self.registry.configs()["old"].url, "wss://worker.internal/codex")
        self.assertFalse(self.registry.configs()["old"].enabled)

    def test_distinct_supervisors_require_distinct_credential_references(self):
        machine = self.machine()
        with self.assertRaisesRegex(ValueError, "凭据引用重复"):
            self.registry.save_agent({**machine, "port": 4501})
        with patch.dict(os.environ, {"CODEX_MACHINE_SIDECAR_TOKEN_TEMPLATE": str(self.root / "{ip}-{port}.token")}):
            second = self.registry.save_agent({**machine, "port": 4501})
        self.assertNotEqual(machine["agentId"], second["agentId"])

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
        self.registry.save_agent({**worker, "groupId": other}, worker["agentId"])
        with self.assertRaisesRegex(ValueError, "同一分组"):
            self.gateway.store.prepare_node_dispatch("registered-run", "step")

    def test_import_conflict_rolls_back_whole_batch(self):
        self.machine()
        path = self.root / "legacy.json"
        path.write_text(json.dumps({"agents": {
            "new": {"url": "ws://127.0.0.3:4500", "cwd": str(self.root)},
            "collision": {"url": "ws://127.0.0.1:4500", "cwd": str(self.root)},
        }}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.registry.import_file(path)
        self.assertNotIn("new", self.registry.rows())
        self.assertFalse(any(group["id"] == "default" for group in self.registry.groups()))

    def test_executor_can_become_supervisor_and_ipv6_is_bracketed(self):
        worker = self.machine("::1", ["executor"])
        self.assertEqual(self.registry.configs()[worker["agentId"]].url, "ws://[::1]:4500")
        self.registry.save_agent({**worker, "capabilities": ["supervisor", "executor"]}, worker["agentId"])
        self.assertEqual(self.registry.configs()[worker["agentId"]].orchestration_mode, "remote_sidecar")

    def test_default_changes_do_not_rewrite_existing_credentials(self):
        machine = self.machine()
        key = machine["agentId"]
        before = self.registry.configs()[key].sidecar_token_file
        self.registry.record_test(key, True)
        with patch.dict(os.environ, {"CODEX_MACHINE_SIDECAR_TOKEN_TEMPLATE": str(self.root / "changed-{ip}-{port}.token")}):
            self.registry.save_agent({**machine, "enabled": False}, key)
        self.assertEqual(self.registry.configs()[key].sidecar_token_file, before)
        self.assertEqual(self.registry.rows()[key]["test_status"], "passed")

    def test_remote_client_parses_fetched_group_and_does_not_read_sqlite(self):
        from workflow_runtime_client import InternalApiClient
        supervisor = self.machine()
        self.machine("127.0.0.2", ["executor"])
        (self.root / "127.0.0.1.token").write_text("test-only-machine-secret", encoding="utf-8")
        runtime = InternalApiClient("http://central.test", "registered-machine", token_env="FIXTURE", started_at="2026-09-11T00:00:00+00:00")
        def request(method, path, payload=None):
            response = self.client.request(method, path, headers={"Authorization": "Bearer test-only-machine-secret"}, json=payload)
            self.assertEqual(response.status_code, 200)
            return response.json()
        with patch.object(runtime, "_request", side_effect=request):
            agents = runtime.group_agents()
        self.assertEqual(len(agents), 2)
        self.assertIn(supervisor["agentId"], agents)
        self.assertTrue(all(agent.sidecar_token_file is None for agent in agents.values()))

    def test_file_mode_management_is_disabled(self):
        with patch.dict(os.environ, {"CODEX_AGENT_SOURCE": "file"}):
            app = create_app(db_path=self.root / "file.db", config_path=self.root / "legacy.json")
            client = TestClient(app, raise_server_exceptions=False)
            self.addCleanup(client.close)
            self.assertEqual(client.post("/agents", json={}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
