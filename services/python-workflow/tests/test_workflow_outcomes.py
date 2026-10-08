import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_orchestrator_mcp import AgentConfig, Orchestrator
from workflow_gateway import WorkflowGateway
from workflow_outcomes import SCHEMA, instruction, parse
from workflow_store import WorkflowStore, utc_now
from tests.registry_fixtures import seed_agents
from tests.test_workflow_store import serial_workflow
from tests.mock_app_server import MockAppServer


def result(outcome="blocked", jira="succeeded"):
    return {"summary": "已核验；未修改业务代码。", "outcome": outcome,
            "reason": "缺少产品确认" if outcome != "success" else "", "document": None,
            "jiraComment": {"status": jira, "issueKey": "TEST-1" if jira == "succeeded" else "",
                            "reference": "123" if jira == "succeeded" else "", "detail": "测试备注结果"}}


class OutcomeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkflowStore(Path(self.temp.name) / "state.db")
        seed_agents(self.store, {"local": {"capabilities": ["supervisor", "executor"]}})

    def start(self, mode="semi_automatic"):
        spec = serial_workflow()
        spec["advanceMode"] = mode
        self.store.create_workflow(spec)
        self.store.claim_next_workflow("local")
        self.store.prepare_node_dispatch("serial-demo", "a")

    def sync(self, node="a", value=None):
        self.store.sync_node_job("serial-demo", node, {
            "status": "completed", "finished_at": utc_now(), "businessResult": value or result()})

    def test_blocked_stops_before_wait_and_preserves_lease_until_finalized(self):
        self.start()
        self.sync()
        state = self.store.get_workflow("serial-demo")
        self.assertIsNone(state["pendingAdvance"])
        self.assertEqual([n["status"] for n in state["nodes"]], ["failed", "skipped", "skipped"])
        self.assertTrue(self.store.has_supervisor_lease("serial-demo"))
        with self.assertRaises(ValueError):
            self.store.prepare_node_dispatch("serial-demo", "b")
        self.assertFalse(WorkflowGateway._workflow_can_continue(self.store.get_spec("serial-demo"), state))
        self.assertTrue(self.store.finish_business_termination("serial-demo"))
        self.assertFalse(self.store.has_supervisor_lease("serial-demo"))
        self.assertEqual(self.store.get_workflow("serial-demo")["status"], "failed")

    def test_no_task_finishes_normally_without_fabricating_completed_steps(self):
        self.start("automatic")
        self.sync(value=result("no_task", "not_applicable"))
        self.store.finish_business_termination("serial-demo")
        state = self.store.get_workflow("serial-demo")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["progress"]["completed"], 1)
        self.assertIn("无待处理任务", state["response"])

    def test_second_step_blocked_and_old_button_cannot_continue(self):
        self.start()
        self.sync(value=result("success"))
        gate = self.store.get_workflow("serial-demo")["pendingAdvance"]["gateId"]
        self.store.confirm_advance("serial-demo", gate)
        self.store.prepare_node_dispatch("serial-demo", "b")
        self.sync("b")
        self.store.finish_business_termination("serial-demo")
        with self.assertRaises((ValueError, RuntimeError)):
            self.store.confirm_advance("serial-demo", gate)
        self.assertIsNone(self.store.get_workflow("serial-demo")["nodes"][2]["startedAt"])

    def test_jira_failures_do_not_prevent_termination(self):
        for status in ("failed", "unknown", "not_applicable"):
            with self.subTest(status=status):
                value = result(jira=status)
                self.assertEqual(parse(json.dumps(value))["outcome"], "blocked")
        self.start()
        self.sync(value=result(jira="unknown"))
        self.store.finish_business_termination("serial-demo")
        self.assertIn("未确认", self.store.get_workflow("serial-demo")["response"])

    def test_missing_or_invalid_result_fails_closed(self):
        self.start()
        self.store.sync_node_job("serial-demo", "a", {"status": "completed"})
        state = self.store.get_workflow("serial-demo")
        self.assertEqual(state["nodes"][0]["status"], "failed")
        self.assertIsNone(state["pendingAdvance"])
        with self.assertRaises(ValueError):
            self.store.prepare_node_dispatch("serial-demo", "b")

    def test_duplicate_and_late_results_preserve_decision(self):
        self.start()
        self.sync()
        self.sync(value=result("success"))
        self.store.finish_business_termination("serial-demo")
        before = self.store.get_workflow("serial-demo")
        self.store.finish_business_termination("serial-demo")
        self.store.update_supervisor("serial-demo", {"status": "completed", "response": "任务全部完成"})
        self.sync(value=result("success"))
        after = self.store.get_workflow("serial-demo")
        self.assertEqual(before, after)
        self.assertEqual(WorkflowStore(self.store.path).workflow_terminations(["serial-demo"])["serial-demo"]["outcome"], "blocked")

    def test_plain_text_does_not_control_success(self):
        self.start()
        value = result("success")
        value["summary"] = "无阻断；说明文档中引用了阻断。"
        self.sync(value=value)
        self.assertIsNone(self.store.get_workflow("serial-demo")["termination"])
        self.assertIsNotNone(self.store.get_workflow("serial-demo")["pendingAdvance"])

    def test_legacy_results_remain_compatible(self):
        self.start()
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET result_protocol_version=0")
        self.store.sync_node_job("serial-demo", "a", {"status": "completed", "response": "阻断"})
        self.assertIsNone(self.store.get_workflow("serial-demo")["termination"])
        self.assertIsNotNone(self.store.get_workflow("serial-demo")["pendingAdvance"])

    def test_protocol_and_marker_are_stable(self):
        self.start()
        self.assertEqual(self.store.get_spec("serial-demo")["resultProtocolVersion"], 1)
        self.assertEqual(instruction("w", "n", False), instruction("w", "n", False))
        self.assertNotEqual(instruction("w", "n", False), instruction("w2", "n", False))

    def test_restart_completes_saved_no_task_decision(self):
        self.start()
        self.sync(value=result("no_task", "not_applicable"))
        store = WorkflowStore(self.store.path)
        store.recover_active_workflows_after_restart()
        self.assertEqual(store.get_workflow("serial-demo")["status"], "completed")
        self.assertFalse(store.has_supervisor_lease("serial-demo"))

    def test_oversized_summary_is_rejected(self):
        value = result()
        value["summary"] = "长" * 20001
        with self.assertRaises(ValueError):
            parse(json.dumps(value))

    def test_regular_finish_cannot_overwrite_concurrent_stop_decision(self):
        self.start()
        original = self.store.get_workflow
        def read_then_stop(workflow_id):
            stale = original(workflow_id)
            self.sync()
            return stale
        with patch.object(self.store, "get_workflow", side_effect=read_then_stop):
            self.store.finish_workflow("serial-demo", supervisor_status="completed", response="全部完成", error=None)
        state = original("serial-demo")
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["termination"]["outcome"], "blocked")
        self.assertTrue(self.store.has_supervisor_lease("serial-demo"))

    def test_missing_or_stale_execution_identity_cannot_stop_workflow(self):
        self.start()
        self.store.attach_node_job("serial-demo", "a", {"job_id": "current", "status": "running", "turn_id": "current-turn"})
        for fields in ({}, {"job_id": "old"}, {"job_id": "current", "turn_id": "old-turn"}):
            with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                self.store.sync_node_job("serial-demo", "a", {"status": "completed", "businessResult": result(), **fields})
        self.assertIsNone(self.store.get_workflow("serial-demo")["termination"])


class OutcomeExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_stops_supervisor_before_releasing_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory) / "state.db")
            seed_agents(store, {"local": {"capabilities": ["supervisor", "executor"]}})
            store.create_workflow(serial_workflow())
            store.claim_next_workflow("local")
            job = SimpleNamespace(job_id="supervisor-job", agent_id="local", status="running", thread_id="thread", completed=asyncio.Event())
            job.snapshot = lambda: {"job_id": job.job_id, "status": job.status}
            async def dispatch(**kwargs):
                store.prepare_node_dispatch("serial-demo", "a")
                store.sync_node_job("serial-demo", "a", {"status": "completed", "businessResult": result(), "finished_at": utc_now()})
                return job
            async def cancel(job_id):
                self.assertTrue(store.has_supervisor_lease("serial-demo"))
                self.assertEqual(store.get_workflow("serial-demo")["status"], "running")
                job.status = "interrupted"
                job.completed.set()
            orchestrator = SimpleNamespace(dispatch=AsyncMock(side_effect=dispatch), cancel=AsyncMock(side_effect=cancel))
            gateway = WorkflowGateway(store, orchestrator)
            await asyncio.wait_for(gateway._run_supervisor(store.get_spec("serial-demo")), 5)
            self.assertEqual(store.get_workflow("serial-demo")["status"], "failed")
            self.assertFalse(store.has_supervisor_lease("serial-demo"))
            self.assertEqual(orchestrator.dispatch.await_count, 1)
            self.assertEqual(orchestrator.cancel.await_count, 1)

    async def test_actual_appserver_result_is_parsed_before_completion(self):
        async with MockAppServer(structured_reply=json.dumps(result())) as server:
            orchestrator = Orchestrator()
            orchestrator.agent_provider = lambda: {"local": AgentConfig.from_dict("local", {"url": server.url, "cwd": str(Path.cwd())})}
            job = await orchestrator.dispatch(agent_id="local", prompt="测试", timeout_sec=10, output_schema=SCHEMA, thread_id=None, cwd=None, write=False, model=None)
            await asyncio.wait_for(job.completed.wait(), 5)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.business_result["outcome"], "blocked")
            self.assertEqual(job.response, result()["summary"])

    async def test_invalid_model_output_is_failure(self):
        async with MockAppServer(structured_reply='{"summary":"阻断"}') as server:
            orchestrator = Orchestrator()
            orchestrator.agent_provider = lambda: {"local": AgentConfig.from_dict("local", {"url": server.url, "cwd": str(Path.cwd())})}
            job = await orchestrator.dispatch(agent_id="local", prompt="测试", timeout_sec=10, output_schema=SCHEMA, thread_id=None, cwd=None, write=False, model=None)
            await asyncio.wait_for(job.completed.wait(), 5)
            self.assertEqual(job.status, "failed")
            self.assertIsNone(job.business_result)
