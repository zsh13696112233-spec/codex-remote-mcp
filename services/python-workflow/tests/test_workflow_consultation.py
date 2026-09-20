import asyncio
import json
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_orchestrator_mcp import AppServerRpcError, AgentConfig
from workflow_consultation import ConsultationService, ConsultationStore, bounded_result, safe_text, visible_item
from tests.registry_fixtures import FixtureWorkflowStore, fixture_gateway
from tests.test_workflow_store import serial_workflow
from workflow_store import utc_now
from workflow_gateway import WorkflowGateway, _sidecar_job_snapshot


class Client:
    def __init__(self):
        self.calls = []
        self.fork_supported = True
        self.network = False
        self.tools = []
        self.policy = "readOnly"
        self.recovery_status = "completed"
        self.lost_start = False

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"cwd": "/work", "turns": [
                {"id": "original-turn", "status": "completed", "items": [
                    {"type": "agentMessage", "text": "最初方案"},
                    {"type": "reasoning", "text": "不公开推理"},
                ]},
                {"id": "later-turn", "status": "completed", "items": [{"type": "agentMessage", "text": "后来返工"}]},
                {"id": "consult-turn", "status": self.recovery_status, "items": [{"type": "agentMessage", "text": "恢复的核查结果"}]},
            ]}}
        if method == "config/read":
            return {"config": {"mcp_servers": {"business": {}}, "plugins": {"plugin": {}}}}
        if method in {"thread/fork", "thread/start", "thread/resume"}:
            if method == "thread/fork" and not self.fork_supported:
                raise AppServerRpcError("unsupported", code=-32601)
            return {"thread": {"id": "consult-thread"}, "cwd": "/work", "approvalPolicy": "never",
                    "sandbox": {"type": self.policy, "networkAccess": self.network}}
        if method == "mcpServerStatus/list":
            return {"data": self.tools}
        if method == "turn/start":
            if self.lost_start:
                raise ConnectionError("uncertain")
            return {"turn": {"id": "consult-turn"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(method)


class ConsultationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FixtureWorkflowStore(Path(self.directory.name, "workflows.db"))
        self.store.create_workflow(serial_workflow())
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job("serial-demo", "a", {"status": "completed", "thread_id": "original-thread",
            "turn_id": "original-turn", "response": "已完成", "cwd": "/work", "model": "test-model", "finished_at": utc_now()})
        self.gateway = fixture_gateway(self.store, object())
        self.service = ConsultationService(self.gateway)
        self.client = Client()
        self.agent = AgentConfig.from_dict("local", {"url": "ws://127.0.0.1:4500", "cwd": "/work", "allow_cwd_override": True})

        @asynccontextmanager
        async def connection(attempt):
            yield self.client, self.agent
        self.service.connection = connection

        async def consume(job, client, deadline):
            job.status = "completed"
            job.response = "结论：缺少去重。证据：当前代码。建议：唯一约束。尚未运行测试。"
        self.gateway.assistant_orchestrator._consume_turn = AsyncMock(side_effect=consume)
        self.message = {"messageId": str(uuid.uuid4()), "text": "第二步有没有问题", "actorId": "alice"}

    async def call(self, name="consult_step", **kwargs):
        request = {"name": name, "nodeId": "a", "question": "有没有去重？", **kwargs}
        return await self.service.execute("serial-demo", self.message, 0, request)

    async def test_records_select_exact_turn_and_exclude_reasoning(self):
        result = await self.call("read_step_records")
        body = json.dumps(result, ensure_ascii=False)
        self.assertIn("最初方案", body)
        self.assertNotIn("后来返工", body)
        self.assertNotIn("不公开推理", body)
        self.assertFalse(any(method == "turn/start" for method, _ in self.client.calls))

    async def test_consult_forks_read_only_and_keeps_business_unchanged(self):
        before = self.store.get_workflow("serial-demo")
        result = await self.call()
        self.assertIn("缺少去重", result["answer"])
        fork = next(p for m, p in self.client.calls if m == "thread/fork")
        self.assertEqual(fork["lastTurnId"], "original-turn")
        self.assertEqual(fork["sandbox"], "read-only")
        self.assertFalse(fork["config"]["mcp_servers"]["business"]["enabled"])
        self.assertFalse(fork["config"]["plugins"]["plugin"]["enabled"])
        self.assertEqual(fork["model"], "test-model")
        turn = next(p for m, p in self.client.calls if m == "turn/start")
        self.assertEqual(turn["threadId"], "consult-thread")
        self.assertEqual(turn["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        after = self.store.get_workflow("serial-demo")
        self.assertEqual(before["nodes"], after["nodes"])
        self.assertEqual(before["retryPolicy"], after["retryPolicy"])

    async def test_followup_reuses_consultation_not_original(self):
        await self.call()
        self.message["messageId"] = str(uuid.uuid4())
        await self.call()
        resumes = [p for m, p in self.client.calls if m == "thread/resume"]
        self.assertEqual([p["threadId"] for p in resumes], ["consult-thread"])
        self.assertEqual(sum(m == "thread/fork" for m, _ in self.client.calls), 1)

    async def test_actor_has_separate_consultation(self):
        await self.call()
        self.message = {**self.message, "messageId": str(uuid.uuid4()), "actorId": "bob"}
        await self.call()
        self.assertEqual(sum(m == "thread/fork" for m, _ in self.client.calls), 2)

    async def test_duplicate_message_reuses_result(self):
        first = await self.call()
        again = await self.call()
        self.assertEqual(first, again)
        self.assertEqual(sum(m == "turn/start" for m, _ in self.client.calls), 1)

    async def test_repeated_same_question_in_model_loop_is_not_dispatched_twice(self):
        first = await self.call()
        second = await self.service.execute("serial-demo", self.message, 1,
            {"name": "consult_step", "nodeId": "a", "question": "有没有去重？"})
        self.assertEqual(first, second)
        self.assertEqual(sum(m == "turn/start" for m, _ in self.client.calls), 1)

    async def test_pagination_freezes_source_and_content(self):
        self.client.request = AsyncMock(return_value={"thread": {"turns": [{"id": "original-turn", "status": "completed",
            "items": [{"type": "agentMessage", "text": f"记录{i}"} for i in range(30)]}]}})
        attempt = self.service.store.attempt("serial-demo", "a")
        first = await self.service.records("serial-demo", attempt)
        self.assertIsNotNone(first["nextCursor"])
        self.client.request = AsyncMock(side_effect=ConnectionError("offline"))
        second = await self.service.records("serial-demo", attempt, cursor=first["nextCursor"])
        self.assertEqual(second["records"][0]["text"], "记录20")
        self.assertEqual(second["observedAt"], first["observedAt"])
        self.client.request.assert_not_awaited()

    async def test_delayed_event_retains_old_attempt_and_truncation_marker(self):
        self.store.restart_from_node("serial-demo", "a", revision_instruction="修改")
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job("serial-demo", "a", {"status": "completed", "turn_id": "later-turn"})
        self.store.add_event("serial-demo", node_id="a", source="worker", event_type="appserver.item/completed",
            payload={"message": {"params": {"turnId": "original-turn", "item": {"type": "agentMessage", "text": "x" * 300_000}}}})
        event = self.store.list_events("serial-demo", limit=100)[-1]
        self.assertEqual(event["payload"]["attemptNumber"], 0)
        self.assertTrue(event["payload"]["truncated"])
        self.assertEqual(self.store.event_attempt("serial-demo", "a", "original-turn"), 0)

    async def test_unsupported_fork_uses_limited_context(self):
        self.client.fork_supported = False
        result = await self.call()
        self.assertEqual(result["contextMode"], "records")
        start = next(p for m, p in self.client.calls if m == "turn/start")
        self.assertIn("历史上下文不完整", start["input"][0]["text"])
        self.assertFalse(any(m == "thread/resume" for m, _ in self.client.calls))

    async def test_running_step_never_starts_consultation(self):
        self.store.sync_node_job("serial-demo", "a", {"status": "running"})
        result = await self.call()
        self.assertIn("只查询记录", result["notice"])
        self.assertFalse(any(m == "turn/start" for m, _ in self.client.calls))

    async def test_failed_isolated_policy_refuses_start(self):
        for field, value in (("network", True), ("policy", "dangerFullAccess"), ("tools", [{"tools": {"write": {}}}])):
            with self.subTest(field=field):
                self.client = Client()
                setattr(self.client, field, value)
                self.message["messageId"] = str(uuid.uuid4())
                result = await self.call()
                self.assertIn("error", result)
                self.assertFalse(any(m == "turn/start" for m, _ in self.client.calls))

    async def test_lost_turn_start_never_replayed(self):
        self.client.lost_start = True
        await self.call()
        self.message["messageId"] = str(uuid.uuid4())
        result = await self.call()
        self.assertIn("尚未确认", result["error"])
        self.assertEqual(sum(m == "turn/start" for m, _ in self.client.calls), 1)

    async def test_recover_known_turn_reads_without_start(self):
        request = {"name": "consult_step", "nodeId": "a", "attempt": 0, "question": "问题"}
        key = ("serial-demo", self.message["messageId"], 0)
        self.service.store.claim(*key, request)
        self.service.store.update_call(key, status="running", thread_id="consult-thread", turn_id="consult-turn")
        self.service.store.save_session(("serial-demo", "a", 0, "alice"), "consult-thread", "fork", 1)
        result = await self.call()
        self.assertIn("恢复的核查结果", result["answer"])
        self.assertFalse(any(m == "turn/start" for m, _ in self.client.calls))
        self.assertEqual(self.service.store.session(("serial-demo", "a", 0, "alice"))["blocked"], 0)

    async def test_offline_uses_local_records_with_missing_marker(self):
        @asynccontextmanager
        async def offline(attempt):
            raise ConnectionError("private-address")
            yield
        self.service.connection = offline
        result = await self.call("read_step_records")
        self.assertFalse(result["complete"])
        self.assertEqual(result["source"], "中央记录")
        self.assertNotIn("private-address", json.dumps(result))

    async def test_offline_consultation_returns_saved_evidence(self):
        @asynccontextmanager
        async def offline(attempt):
            raise ConnectionError("private-address")
            yield
        self.service.connection = offline
        result = await self.call()
        self.assertEqual(result["savedRecords"]["source"], "中央记录")
        self.assertFalse(result["savedRecords"]["complete"])
        self.assertNotIn("private-address", json.dumps(result))

    async def test_empty_remote_history_does_not_claim_completeness(self):
        self.client.request = AsyncMock(return_value={"thread": {"turns": [
            {"id": "original-turn", "status": "completed", "items": []}]}})
        result = await self.call("read_step_records")
        self.assertEqual(result["source"], "中央记录")
        self.assertFalse(result["complete"])

    async def test_attempts_preserve_old_runtime(self):
        self.store.restart_from_node("serial-demo", "a", revision_instruction="修改")
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job("serial-demo", "a", {"status": "completed", "thread_id": "original-thread", "turn_id": "later-turn", "cwd": "/new-work", "model": "new-model"})
        old = self.service.store.attempt("serial-demo", "a", 0)
        new = self.service.store.attempt("serial-demo", "a", 1)
        self.assertEqual(old["cwd"], "/work")
        self.assertEqual(new["cwd"], "/new-work")
        result = await self.call("read_step_records", attempt=0)
        body = json.dumps(result, ensure_ascii=False)
        self.assertIn("最初方案", body)
        self.assertNotIn("后来返工", body)

    async def test_bad_target_and_cursor_are_rejected(self):
        with self.assertRaises(ValueError):
            await self.call(nodeId="outside")
        result = await self.call("read_step_records", cursor="other:0")
        self.assertIn("error", result)

    async def test_progress_in_monitor_and_bot_views(self):
        await self.call("read_step_records")
        for view in ("monitor", "bot"):
            events = self.store.event_page("serial-demo", view=view)["events"]
            progress = [e for e in events if e["type"] == "chat.assistant.progress"]
            self.assertEqual(progress[0]["payload"]["messageId"], self.message["messageId"])
            self.assertNotIn("original-thread", json.dumps(progress))


class AssistantLoopTests(unittest.IsolatedAsyncioTestCase):
    setUp = ConsultationTests.setUp
    async def test_routes_tool_result_back_to_model(self):
        request = {"kind": "tool_request", "toolRequest": {"name": "read_step_records", "nodeId": "a"}}
        answer = {"kind": "answer", "text": "第一步采用最初方案", "actionType": None, "nodeId": None, "revisionInstruction": None}
        self.gateway._run_assistant_model_turn = AsyncMock(side_effect=[request, answer])
        with patch("workflow_consultation.ConsultationService", return_value=self.service):
            result = await self.gateway._run_assistant_turn("serial-demo", self.message["messageId"], self.store.get_workflow("serial-demo"), self.message)
        self.assertEqual(result, answer)
        prompt = self.gateway._run_assistant_model_turn.call_args.kwargs["prompt_override"]
        self.assertIn("最初方案", prompt)
        self.assertEqual(self.gateway._run_assistant_model_turn.await_count, 2)

    async def test_tool_limit(self):
        self.gateway._run_assistant_model_turn = AsyncMock(return_value={"kind": "tool_request", "toolRequest": {"name": "read_step_records", "nodeId": "a"}})
        with patch("workflow_consultation.ConsultationService", return_value=self.service):
            answer = await self.gateway._run_assistant_turn("serial-demo", self.message["messageId"], self.store.get_workflow("serial-demo"), self.message)
        self.assertIn("上限", answer["text"])
        self.assertEqual(len(self.service.store.replay("serial-demo", self.message["messageId"])), 8)


class ValidationTests(unittest.TestCase):
    def test_remote_runtime_fields_are_validated_and_preserved(self):
        self.assertEqual(_sidecar_job_snapshot({"status": "completed", "cwd": "/work", "model": "demo"}),
                         {"status": "completed", "cwd": "/work", "model": "demo"})
        for cwd in ("relative", [], "x" * 5000):
            with self.assertRaises(ValueError):
                _sidecar_job_snapshot({"cwd": cwd})

    def test_no_arbitrary_execution_arguments(self):
        for field in ("workflowId", "threadId", "cwd", "agentId", "url"):
            with self.assertRaises(ValueError):
                ConsultationService.validate({"name": "read_step_records", "nodeId": "a", field: "other"})

    def test_tool_cannot_include_control(self):
        value = {"kind": "tool_request", "text": "", "actionType": "stop", "nodeId": None,
                 "revisionInstruction": None, "toolRequest": {"name": "read_step_records", "nodeId": "a"}}
        with self.assertRaises(RuntimeError):
            WorkflowGateway._validate_assistant_decision(value, {"nodes": [{"id": "a"}]})

    def test_payload_capacity_and_sensitive_fields(self):
        text = safe_text('token=abc password="def othersecret" Bearer private-token http://10.0.0.1/a sk-secret')
        for secret in ("abc", "def", "othersecret", "private-token", "10.0.0.1", "sk-secret"):
            self.assertNotIn(secret, text)
        self.assertEqual(visible_item({"type": "reasoning", "text": "hidden"}), "")
        self.assertEqual(visible_item({"type": "imageGeneration", "result": "base64"}), "")
        for value in ("x" * 100_000, '"\\' * 100_000):
            result = bounded_result({"text": value})
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 20_000)


if __name__ == "__main__":
    unittest.main()
