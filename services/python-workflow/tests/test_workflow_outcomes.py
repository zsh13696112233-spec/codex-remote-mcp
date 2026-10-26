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
            "jiraDeveloper": {"status": "resolved", "accountType": "key", "accountId": "dev-1", "displayName": "开发人", "detail": "实际读取"},
            "reason": "缺少产品确认" if outcome != "success" else "", "document": None,
            "jiraComment": {"status": jira, "issueKey": "TEST-1",
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

    def test_success_jira_context_survives_restart_and_late_results(self):
        self.start("automatic")
        self.sync(value=result("success", "not_applicable"))
        expected = {"issueKey": "TEST-1", "developerStatus": "resolved", "developerName": "开发人"}
        self.assertEqual(WorkflowStore(self.store.path).get_workflow("serial-demo")["jiraContext"], expected)
        late = result("success", "not_applicable")
        late["jiraComment"]["issueKey"] = "WRONG-2"
        self.sync(value=late)
        self.assertEqual(self.store.get_workflow("serial-demo")["jiraContext"], expected)
        self.store.prepare_node_dispatch("serial-demo", "b")
        followup = result("success", "not_applicable")
        followup["jiraDeveloper"].update(status="not_applicable", accountType="", accountId="", displayName="")
        self.sync("b", followup)
        self.assertEqual(self.store.get_workflow("serial-demo")["jiraContext"], expected)
        self.store.prepare_node_dispatch("serial-demo", "c")
        followup["jiraDeveloper"].update(status="resolved", accountType="key", accountId="dev-2", displayName="新开发人")
        self.sync("c", followup)
        self.store.finish_workflow("serial-demo", supervisor_status="completed", response="已结束", error=None)
        final = self.store.get_workflow("serial-demo")
        self.assertEqual(final["jiraContext"]["developerName"], "新开发人")
        self.assertEqual(final["status"], "completed")
        self.assertIsNone(final["termination"])

    def test_changed_issue_does_not_inherit_developer(self):
        self.start("automatic")
        self.sync(value=result("success", "not_applicable"))
        self.store.prepare_node_dispatch("serial-demo", "b")
        value = result("success", "not_applicable")
        value["jiraComment"]["issueKey"] = "TEST-2"
        value["jiraDeveloper"].update(status="unavailable", accountType="", accountId="", displayName="")
        self.sync("b", value)
        self.assertEqual(self.store.get_workflow("serial-demo")["jiraContext"],
                         {"issueKey": "TEST-2", "developerStatus": "unavailable", "developerName": ""})

    def test_existing_database_migrates_without_inventing_historical_jira(self):
        self.start("automatic")
        with self.store._connect() as db:
            db.execute("ALTER TABLE workflows DROP COLUMN jira_context_json")
        reopened = WorkflowStore(self.store.path)
        self.assertIsNone(reopened.get_workflow("serial-demo")["jiraContext"])
        self.assertEqual(reopened.get_workflow("serial-demo")["status"], "running")
        self.sync(value=result("success", "not_applicable"))
        self.assertEqual(reopened.get_workflow("serial-demo")["jiraContext"]["issueKey"], "TEST-1")

    def test_no_jira_result_does_not_invent_context(self):
        self.start("automatic")
        value = result("success", "not_applicable")
        value["jiraComment"]["issueKey"] = ""
        value["jiraDeveloper"].update(status="not_applicable", accountType="", accountId="", displayName="")
        self.sync(value=value)
        self.assertIsNone(self.store.get_workflow("serial-demo")["jiraContext"])
        self.assertIsNone(self.store.get_workflow("serial-demo")["termination"])

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

    def test_display_name_instructions_preserve_result_protocol(self):
        from workflow_outcomes import validate
        for version in (2, 3, 4, 5):
            for waiting in (False, True):
                prompt = instruction("w", "n", False, version, defer_blocked=waiting)
                self.assertIn("displayName 必须填写开发人对象实际返回的显示名", prompt)
                self.assertIn("平台按显示名精确匹配钉钉人员", prompt)
                self.assertNotIn("accountType 固定为 name", prompt)
        for identity_type in ("name", "key", "accountId"):
            value = result()
            value["jiraDeveloper"].update(accountType=identity_type, accountId="Developer1")
            self.assertEqual(validate(value, 2)["jiraDeveloper"]["accountId"], "Developer1")

    def test_protocol_and_marker_are_stable(self):
        self.start()
        self.assertEqual(self.store.get_spec("serial-demo")["resultProtocolVersion"], 2)
        self.assertEqual(instruction("w", "n", False), instruction("w", "n", False))
        self.assertNotEqual(instruction("w", "n", False), instruction("w2", "n", False))

    def test_required_summary_documents_preserve_stop_and_wait_boundaries(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                prompt = instruction("w", "n", False, version)
                self.assertIn("原工作目录和写入权限内", prompt)
                self.assertIn("不改变阻断结论", prompt)
                self.assertIn("不重发结果不明的写入", prompt)
                self.assertIn("不再执行下一步", prompt)
                waiting = instruction("w", "n", False, version, defer_blocked=True)
                self.assertNotIn("允许的收尾交付", waiting)
                self.assertIn("禁止执行停止评论", waiting)
                self.assertIn("等待期间不允许进入后续步骤", waiting)
        prompt = instruction("w", "n", False, 3)
        self.assertIn("回读并列入 documents", prompt)
        self.assertIn("未要求生成总结文件的步骤不额外生成", prompt)
        self.assertIn("summary 都须保留完整文字总结", prompt)

    def test_blocked_assignment_requires_confirmed_comment_and_preserves_role(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                prompt = instruction("w", "n", False, version)
                self.assertIn("评论未确认成功不得转交", prompt)
                self.assertIn("读取失败先重试读取一次", prompt)
                self.assertIn("为空或多人时使用 Public.Jira.Manager", prompt)
                self.assertIn("转交后回查负责人、状态和处理角色", prompt)
                self.assertIn("仍不明不得盲目重复操作", prompt)
                self.assertIn("Jira 状态和处理角色保持不变，不得改为 Natural Person", prompt)
                self.assertIn("无论转交成功与否最终仍返回 blocked", prompt)
                self.assertNotIn("不改变状态、负责人", prompt)
                waiting = instruction("w", "n", False, version, defer_blocked=True)
                self.assertIn("禁止执行停止评论和负责人转交", waiting)
                self.assertNotIn("之后必须在原授权内尝试转交负责人", waiting)
                self.assertNotIn("读取失败先重试读取一次", waiting)

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

    async def test_success_with_empty_developer_and_detail_completes(self):
        value = result("success", "not_applicable")
        value["jiraDeveloper"].update(status="not_applicable", accountType="", accountId="", displayName="", detail="")
        async with MockAppServer(structured_reply=json.dumps(value)) as server:
            orchestrator = Orchestrator()
            orchestrator.agent_provider = lambda: {"local": AgentConfig.from_dict("local", {"url": server.url, "cwd": str(Path.cwd())})}
            job = await orchestrator.dispatch(agent_id="local", prompt="测试", timeout_sec=10, output_schema=SCHEMA, thread_id=None, cwd=None, write=False, model=None)
            await asyncio.wait_for(job.completed.wait(), 5)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.business_result, value)
            self.assertEqual(job.response, value["summary"])
            self.assertIsNone(job.error)

    async def test_invalid_model_output_is_failure(self):
        async with MockAppServer(structured_reply='{"summary":"阻断"}') as server:
            orchestrator = Orchestrator()
            orchestrator.agent_provider = lambda: {"local": AgentConfig.from_dict("local", {"url": server.url, "cwd": str(Path.cwd())})}
            job = await orchestrator.dispatch(agent_id="local", prompt="测试", timeout_sec=10, output_schema=SCHEMA, thread_id=None, cwd=None, write=False, model=None)
            await asyncio.wait_for(job.completed.wait(), 5)
            self.assertEqual(job.status, "failed")
            self.assertIsNone(job.business_result)
            self.assertEqual(job.error_kind, "invalid_business_result")
            self.assertEqual(job.error_stage, "result/validate")
            self.assertEqual(job.error_details["validation_error"], "步骤业务结果格式无效。")

    async def test_actual_blocked_unavailable_model_result_completes_as_blocked(self):
        value = result()
        value["jiraComment"].update(status="not_applicable", issueKey="", reference="", detail="未锁定工单")
        value["jiraDeveloper"].update(status="not_applicable", accountType="", accountId="", displayName="", detail="未锁定工单，无法读取")
        async with MockAppServer(structured_reply=json.dumps(value)) as server:
            orchestrator = Orchestrator()
            orchestrator.agent_provider = lambda: {"local": AgentConfig.from_dict("local", {"url": server.url, "cwd": str(Path.cwd())})}
            job = await orchestrator.dispatch(agent_id="local", prompt="测试", timeout_sec=10, output_schema=SCHEMA, thread_id=None, cwd=None, write=False, model=None)
            await asyncio.wait_for(job.completed.wait(), 5)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.business_result["outcome"], "blocked")
            self.assertEqual(job.business_result["jiraDeveloper"]["status"], "unavailable")
            self.assertEqual(job.response, value["summary"])
