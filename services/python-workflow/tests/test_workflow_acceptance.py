import json
import tempfile
import unittest
import uuid
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

from tests.registry_fixtures import FixtureWorkflowStore
from tests.test_workflow_store import serial_workflow
from workflow_acceptance import normalize_acceptance, parse_result


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FixtureWorkflowStore(Path(self.directory.name) / "workflow.db")

    def start(self, limit=2, mode="automatic"):
        spec = serial_workflow()
        spec["advanceMode"] = mode
        spec["nodes"][0]["acceptance"] = {"name": "检查", "criteria": "测试通过", "maxRepairs": limit}
        self.store.create_workflow(spec)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job("serial-demo", "a", {"status": "completed", "thread_id": "original", "response": "产物"})

    def finish(self, response, status="completed"):
        claim = self.store.acceptance_operation("serial-demo", "a", "claim", {})
        self.assertEqual("original", claim["threadId"])
        payload = {"token": claim["token"], "status": status, "response": response}
        self.store.acceptance_operation("serial-demo", "a", "finish", payload)
        return payload

    def decision(self, decision):
        return json.dumps({"decision": decision, "reason": "检查原因", "issues": []})

    def test_gate_blocks_downstream_and_preserves_business_result(self):
        self.start()
        with self.assertRaises(ValueError):
            self.store.prepare_node_dispatch("serial-demo", "b")
        self.finish(self.decision("passed"))
        self.assertEqual("产物", self.store.get_node("serial-demo", "a")["response"])
        self.store.prepare_node_dispatch("serial-demo", "b")

    def test_two_repairs_then_held_and_duplicate_result_is_ignored(self):
        self.start()
        for i in range(2):
            payload = self.finish(self.decision("failed"))
            self.store.acceptance_operation("serial-demo", "a", "finish", payload)
            self.assertEqual(i + 1, self.store.acceptance_snapshot("serial-demo", "a")["repairs"])
            self.finish("修复后的产物")
        self.finish(self.decision("failed"))
        self.assertEqual("acceptance_held", self.store.get_node("serial-demo", "a")["status"])
        self.assertEqual("修复后的产物", self.store.get_node("serial-demo", "a")["response"])
        with self.assertRaises(ValueError):
            self.store.skip_node("serial-demo", "a")
        with self.assertRaises(ValueError):
            self.store.prepare_node_dispatch("serial-demo", "b")

    def test_invalid_result_holds_without_repair(self):
        self.start()
        self.finish("通过")
        self.assertEqual("held", self.store.acceptance_snapshot("serial-demo", "a")["state"])
        self.assertEqual(0, self.store.acceptance_snapshot("serial-demo", "a")["repairs"])

    def test_zero_limit_and_needs_input(self):
        self.start(0)
        self.finish(self.decision("failed"))
        self.assertEqual("held", self.store.acceptance_snapshot("serial-demo", "a")["state"])

    def test_semi_automatic_countdown_only_starts_after_pass(self):
        self.start(mode="semi_automatic")
        self.assertIsNone(self.store.get_workflow("serial-demo")["pendingAdvance"])
        self.finish(self.decision("passed"))
        self.assertEqual("b", self.store.get_workflow("serial-demo")["pendingAdvance"]["nextNodeId"])

    def test_needs_input_never_starts_automatic_repair(self):
        self.start()
        self.finish(self.decision("needs_input"))
        gate = self.store.acceptance_snapshot("serial-demo", "a")
        self.assertEqual(("held", 0), (gate["state"], gate["repairs"]))

    def test_sidecar_without_valid_lease_cannot_claim(self):
        self.start()
        with self.assertRaises((PermissionError, RuntimeError)):
            self.store.acceptance_operation("serial-demo", "a", "claim", {},
                                            sidecar_supervisor_id="local", lease_token="invalid")
        self.assertEqual("check_pending", self.store.acceptance_snapshot("serial-demo", "a")["state"])

    def test_strict_configuration(self):
        for limit in (True, -1, 11, 2.5):
            with self.assertRaises(ValueError):
                normalize_acceptance({"name": "检查", "criteria": "通过", "maxRepairs": limit})
        with self.assertRaises(ValueError):
            parse_result('{"decision":"passed"}')

    def test_only_one_claim_and_late_business_completion_cannot_bypass(self):
        self.start()
        claim = self.store.acceptance_operation("serial-demo", "a", "claim", {})
        self.assertIsNotNone(claim)
        self.assertIsNone(self.store.acceptance_operation("serial-demo", "a", "claim", {}))
        self.store.sync_node_job("serial-demo", "a", {"status": "completed", "response": "旧结果"})
        self.assertEqual("running", self.store.get_node("serial-demo", "a")["status"])
        self.assertEqual("产物", self.store.get_node("serial-demo", "a")["response"])

    def test_manual_repair_requires_confirmation_and_charges_once(self):
        self.start(0)
        self.finish(self.decision("failed"))
        message, confirmation = str(uuid.uuid4()), str(uuid.uuid4())
        self.store.accept_chat_message("serial-demo", message, "修复缺失项")
        action = self.store.propose_control("serial-demo", "repair_acceptance", "a", message, "补齐缺失项")
        with self.assertRaises(ValueError):
            self.store.resume_acceptance("serial-demo", "a", action["actionId"])
        self.store.accept_chat_message("serial-demo", confirmation, "确认执行")
        self.store.confirm_control("serial-demo", action["actionId"], confirmation)
        self.store.start_control_execution(action["actionId"])
        self.store.resume_acceptance("serial-demo", "a", action["actionId"])
        self.store.resume_acceptance("serial-demo", "a", action["actionId"])
        self.assertEqual(1, self.store.get_workflow("serial-demo")["retryPolicy"]["usedRetries"])
        self.finish("人工确认后修复的产物")
        self.finish(self.decision("failed"))
        self.assertEqual("held", self.store.acceptance_snapshot("serial-demo", "a")["state"])

    def test_restart_holds_gate_without_releasing_task(self):
        self.start()
        self.store.acceptance_operation("serial-demo", "a", "claim", {})
        self.store.recover_active_workflows_after_restart()
        snapshot = self.store.get_workflow("serial-demo")
        self.assertEqual("running", snapshot["status"])
        self.assertEqual("held", snapshot["nodes"][0]["acceptance"]["state"])
        with self.assertRaises(ValueError):
            self.store.prepare_node_dispatch("serial-demo", "b")

    def test_repair_replaces_current_file_and_archives_old_version(self):
        self.start()
        self.store.save_artifact_bytes("serial-demo", "a", "old", "result.txt", b"old")
        self.finish(self.decision("failed"))
        claim = self.store.acceptance_operation("serial-demo", "a", "claim", {})
        self.store.acceptance_operation("serial-demo", "a", "finish", {
            "token": claim["token"], "status": "completed", "response": "新文件",
            "artifacts": [{"filename": "result.txt", "content": b"new"}],
        })
        inputs = self.store.get_cumulative_artifact_inputs("serial-demo", "a", include_current=True)
        self.assertEqual([b"new"], [a["content"] for a in inputs[0]["artifacts"]])
        with self.store._connect() as c:
            self.assertEqual(b"old", c.execute("SELECT content FROM workflow_attempt_artifacts").fetchone()[0])


class AcceptanceRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_cleanup_waits_for_concurrently_claimed_check(self):
        import codex_orchestrator_mcp as runtime
        with tempfile.TemporaryDirectory() as directory:
            store = FixtureWorkflowStore(Path(directory) / "workflow.db")
            spec = serial_workflow()
            spec["nodes"][0]["acceptance"] = {"name": "检查", "criteria": "文件完整", "maxRepairs": 0}
            store.create_workflow(spec)
            store.prepare_node_dispatch("serial-demo", "a")
            store.sync_node_job("serial-demo", "a", {"status": "completed", "thread_id": "original", "response": "文件"})
            claim = store.acceptance_operation("serial-demo", "a", "claim", {})
            completed = asyncio.Event()
            completed.set()
            job = SimpleNamespace(completed=completed, artifact_contract=True, managed_attempt_dir="unused")
            cleaned = []

            async def finish_check():
                await asyncio.sleep(0.05)
                store.acceptance_operation("serial-demo", "a", "finish", {
                    "token": claim["token"], "status": "completed",
                    "response": json.dumps({"decision": "passed", "reason": "文件完整", "issues": []}),
                })

            async def cleanup(value):
                self.assertEqual("passed", store.acceptance_snapshot("serial-demo", "a")["state"])
                cleaned.append(value)

            task = asyncio.create_task(finish_check())
            with patch.object(runtime, "get_workflow_store", return_value=store), \
                    patch.object(runtime, "_sync_workflow_job"), \
                    patch.object(runtime, "flush_workflow_events", new=AsyncMock()), \
                    patch.object(runtime, "_run_acceptance", new=AsyncMock()), \
                    patch.object(runtime, "orchestrator", SimpleNamespace(cleanup_managed_artifacts=cleanup)):
                await runtime._monitor_workflow_node("serial-demo", "a", job)
            await task
            self.assertEqual([job], cleaned)

    async def test_original_session_is_used_for_check_repair_and_recheck(self):
        import codex_orchestrator_mcp as runtime
        with tempfile.TemporaryDirectory() as directory:
            store = FixtureWorkflowStore(Path(directory) / "workflow.db")
            spec = serial_workflow()
            spec["nodes"][0]["acceptance"] = {"name": "检查", "criteria": "测试通过", "maxRepairs": 2}
            store.create_workflow(spec)
            store.prepare_node_dispatch("serial-demo", "a")
            store.sync_node_job("serial-demo", "a", {"status": "completed", "thread_id": "original", "response": "初稿"})

            class Client:
                async def open(self): pass
                async def close(self): pass
                async def request(self, method, params):
                    return {"thread": {"turns": [{"status": "completed"}]}}

            calls = []
            responses = [json.dumps({"decision": "failed", "reason": "缺测试", "issues": ["补测试"]}),
                         "已修复", json.dumps({"decision": "passed", "reason": "测试通过", "issues": []})]

            async def dispatch(**kwargs):
                calls.append(kwargs)
                event = asyncio.Event()
                event.set()
                return SimpleNamespace(job_id=str(len(calls)), completed=event, status="completed",
                                       response=responses.pop(0), turn_id=str(len(calls)))

            fake = SimpleNamespace(load_agents=lambda: {"local": SimpleNamespace(url="ws://unused")},
                                   _client_factory=lambda *args, **kwargs: Client(),
                                   _resolve_agent_token=lambda agent: None, dispatch=dispatch)
            with patch.object(runtime, "get_workflow_store", return_value=store), patch.object(runtime, "orchestrator", fake):
                await runtime._run_acceptance("serial-demo", "a")
            self.assertEqual(["original"] * 3, [call["thread_id"] for call in calls])
            self.assertIsNotNone(calls[0]["output_schema"])
            self.assertIsNone(calls[1]["output_schema"])
            self.assertEqual("completed", store.get_node("serial-demo", "a")["status"])
            self.assertEqual("已修复", store.get_node("serial-demo", "a")["response"])
