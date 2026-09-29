import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_orchestrator_mcp import Orchestrator
from tests.registry_fixtures import FixtureWorkflowStore, fixture_gateway, seed_agents
from tests.mock_app_server import MockAppServer
from tests.test_workflow_store import serial_workflow
from workflow_discussion import DiscussionService, validate
from workflow_store import utc_now


class DiscussionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FixtureWorkflowStore(Path(self.directory.name, "state.db"))
        spec = serial_workflow()
        spec["advanceMode"] = "semi_automatic"
        self.store.create_workflow(spec)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job("serial-demo", "a", {
            "status": "completed", "thread_id": "original-thread", "turn_id": "original-turn",
            "response": "原计划：登录、支付", "cwd": "/work", "model": "model-original", "finished_at": utc_now()})
        with self.store._connect() as connection:
            connection.execute("UPDATE workflows SET status='running' WHERE workflow_id='serial-demo'")
        self.gate = self.store.get_workflow("serial-demo")["pendingAdvance"]["gateId"]

    def accept(self, text="取消支付，只保留登录"):
        value = self.store.accept_chat_message("serial-demo", str(uuid.uuid4()), text)
        return self.store.claim_next_chat_message("serial-demo")

    def finish(self, message, summary):
        target = self.store.discussion_target("serial-demo", message["messageId"])
        self.store.start_discussion("serial-demo", message["messageId"], target)
        self.store.update_discussion("serial-demo", message["messageId"], state="finished")
        self.store.complete_discussion("serial-demo", message["messageId"], {"text": "已处理", "summary": summary})

    def test_latest_summary_is_handed_to_next_step_without_new_attempt(self):
        before = self.store.get_workflow("serial-demo")["nodes"][0]
        first = self.accept()
        self.finish(first, "计划：只实现登录")
        second = self.accept("采用手机号登录，把它整理进计划")
        self.finish(second, "计划：只实现手机号登录，不做支付")
        after = self.store.get_workflow("serial-demo")["nodes"][0]
        self.assertEqual(after["attemptCount"], before["attemptCount"])
        self.assertEqual(after["finishedAt"], before["finishedAt"])
        self.assertGreater(after["resultRevision"], before["resultRevision"])
        self.store.confirm_advance("serial-demo", self.gate)
        prompt = self.store.prepare_node_dispatch("serial-demo", "b")["prompt"]
        self.assertIn("计划：只实现手机号登录，不做支付", prompt)
        self.assertNotIn("原计划：登录、支付", prompt)

    def test_question_does_not_replace_summary(self):
        self.finish(self.accept("为什么要支付？"), None)
        self.assertEqual(self.store.get_node("serial-demo", "a")["response"], "原计划：登录、支付")

    def test_late_business_poll_cannot_restore_old_summary(self):
        self.finish(self.accept(), "已确认只做登录")
        self.store.sync_node_job("serial-demo", "a", {"status": "completed", "response": "原计划：登录、支付"})
        self.assertEqual(self.store.get_node("serial-demo", "a")["response"], "已确认只做登录")

    def test_removed_control_types_are_rejected(self):
        from workflow_gateway import WorkflowGateway
        for kind in ("propose_control", "stop", "skip", "restart_from"):
            with self.assertRaises(RuntimeError):
                WorkflowGateway._validate_assistant_decision({"kind": kind, "text": "停止",
                    "actionType": "stop", "nodeId": None, "revisionInstruction": None}, {})

    def test_summary_failure_rolls_back_answer_and_node(self):
        message = self.accept()
        self.store.start_discussion("serial-demo", message["messageId"], self.store.discussion_target("serial-demo", message["messageId"]))
        self.store.update_discussion("serial-demo", message["messageId"], state="finished")
        with self.assertRaises(ValueError):
            self.store.complete_discussion("serial-demo", message["messageId"], {"text": "完成", "summary": "x" * 20001})
        self.assertEqual(self.store.get_node("serial-demo", "a")["response"], "原计划：登录、支付")
        self.assertEqual(self.store.pending_chat_count("serial-demo"), 1)

    async def test_missing_original_session_never_starts_replacement(self):
        with self.store._connect() as connection:
            connection.execute("UPDATE workflow_nodes SET thread_id=NULL WHERE node_id='a'")
        gateway = fixture_gateway(self.store, Orchestrator())
        with self.assertRaisesRegex(RuntimeError, "原步骤会话"):
            await DiscussionService(gateway).run("serial-demo", self.accept())
        self.assertFalse(gateway.assistant_orchestrator.jobs)
        await gateway.event_batcher.close()

    async def test_text_continue_keeps_wait_and_never_updates_summary(self):
        async with MockAppServer(structured_reply=json.dumps({"text": "请点击按钮继续", "summary": None})) as server:
            seed_agents(self.store, {"local": {"url": server.url, "cwd": "/work", "capabilities": ["supervisor", "executor"]}})
            gateway = fixture_gateway(self.store, Orchestrator())
            try:
                await gateway._process_chat_message("serial-demo", self.accept("确认继续"))
                self.assertEqual(self.store.get_workflow("serial-demo")["pendingAdvance"]["state"], "held")
                self.assertEqual(self.store.get_node("serial-demo", "b")["status"], "pending")
                self.assertEqual(self.store.get_node("serial-demo", "a")["response"], "原计划：登录、支付")
            finally:
                await gateway.event_batcher.close()

    async def test_cancel_interrupts_discussion_before_reporting_cancelled(self):
        async with MockAppServer(delay_sec=1, structured_reply=json.dumps({"text": "已修改", "summary": "不应交接"})) as server:
            seed_agents(self.store, {"local": {"url": server.url, "cwd": "/work", "capabilities": ["supervisor", "executor"]}})
            gateway = fixture_gateway(self.store, Orchestrator())
            await gateway.accept_message("serial-demo", str(uuid.uuid4()), "修改计划")
            try:
                for _ in range(200):
                    job = gateway._discussion_jobs.get("serial-demo")
                    if job and job.turn_id:
                        break
                    await asyncio.sleep(.01)
                result = await gateway.cancel("serial-demo")
                self.assertEqual(result["status"], "cancelled")
                self.assertGreater(server.interrupt_requests, 0)
                self.assertEqual(self.store.get_node("serial-demo", "a")["response"], "原计划：登录、支付")
            finally:
                await gateway.event_batcher.close()

    def test_pending_and_unknown_discussion_block_advance_and_dispatch(self):
        message = self.accept()
        with self.assertRaises(RuntimeError):
            self.store.confirm_advance("serial-demo", self.gate)
        target = self.store.discussion_target("serial-demo", message["messageId"])
        self.store.start_discussion("serial-demo", message["messageId"], target)
        self.store.fail_chat_message("serial-demo", message["messageId"], "连接断开")
        with self.assertRaises(RuntimeError):
            self.store.confirm_advance("serial-demo", self.gate)
        with self.assertRaises(RuntimeError):
            self.store.prepare_node_dispatch("serial-demo", "b")
        self.assertTrue(self.store.get_workflow("serial-demo")["discussionBusy"])

    def test_image_receiving_blocks_advance_until_queued_or_released(self):
        message_id = str(uuid.uuid4())
        self.store.observe_input("serial-demo", message_id, receiving=True)
        self.assertTrue(self.store.get_workflow("serial-demo")["discussionBusy"])
        revision = self.store.poll_workflow("serial-demo")["revision"]
        with self.assertRaises(RuntimeError):
            self.store.confirm_advance("serial-demo", self.gate)
        self.store.observe_input("serial-demo", message_id, hold=False, receiving=False)
        self.assertFalse(self.store.poll_workflow("serial-demo", revision).get("unchanged", False))
        self.assertFalse(self.store.get_workflow("serial-demo")["discussionBusy"])
        self.assertEqual(self.store.get_workflow("serial-demo")["pendingAdvance"]["state"], "held")
        self.store.observe_input("serial-demo", message_id, receiving=True)
        self.store.accept_chat_message("serial-demo", message_id, "按图修改")
        self.store.observe_input("serial-demo", message_id, hold=False, receiving=False)
        with self.assertRaises(RuntimeError):
            self.store.confirm_advance("serial-demo", self.gate)

    def test_cancelled_wait_rejects_late_summary(self):
        message = self.accept()
        target = self.store.discussion_target("serial-demo", message["messageId"])
        self.store.start_discussion("serial-demo", message["messageId"], target)
        self.store.update_discussion("serial-demo", message["messageId"], state="finished")
        self.store.cancel_workflow("serial-demo")
        with self.assertRaises(RuntimeError):
            self.store.complete_discussion("serial-demo", message["messageId"], {"text": "完成", "summary": "迟到结果"})
        self.assertEqual(self.store.get_node("serial-demo", "a")["response"], "原计划：登录、支付")

    def test_invalid_summary_is_not_committed(self):
        for raw in ('{}', '{"text":"x","summary":""}', json.dumps({"text": "x", "summary": "x" * 20001})):
            with self.assertRaises(RuntimeError):
                validate(raw)

    async def test_original_executor_resumed_and_duplicate_not_dispatched(self):
        async with MockAppServer(structured_reply=json.dumps({"text": "已更新计划", "summary": "只实现登录"})) as server:
            seed_agents(self.store, {"local": {"url": server.url, "cwd": "/work", "allow_write": True,
                "capabilities": ["supervisor", "executor"]}})
            gateway = fixture_gateway(self.store, Orchestrator())
            message = self.accept()
            try:
                await gateway._process_chat_message("serial-demo", message)
                await gateway._process_chat_message("serial-demo", message)
                resumes = [r for r in server.requests if r["method"] == "thread/resume"]
                starts = [r for r in server.requests if r["method"] == "turn/start"]
                self.assertEqual(len(resumes), 1)
                self.assertEqual(resumes[0]["params"]["threadId"], "original-thread")
                self.assertEqual(resumes[0]["params"]["model"], "model-original")
                self.assertEqual(len(starts), 1)
                self.assertIn("原计划：登录、支付", starts[0]["params"]["input"][0]["text"])
                self.assertEqual(self.store.get_node("serial-demo", "a")["response"], "只实现登录")
                self.assertEqual(self.store.get_workflow("serial-demo")["pendingAdvance"]["state"], "held")
            finally:
                await gateway.event_batcher.close()

    async def test_unknown_start_is_not_repeated(self):
        message = self.accept()
        self.store.start_discussion("serial-demo", message["messageId"], self.store.discussion_target("serial-demo", message["messageId"]))
        gateway = fixture_gateway(self.store, Orchestrator())
        with self.assertRaisesRegex(RuntimeError, "尚未确认"):
            await DiscussionService(gateway).run("serial-demo", message)
        await gateway.event_batcher.close()

    async def test_resume_mismatch_does_not_start_replacement_turn(self):
        async with MockAppServer(resume_thread_id="replacement") as server:
            seed_agents(self.store, {"local": {"url": server.url, "cwd": "/work", "capabilities": ["supervisor", "executor"]}})
            gateway = fixture_gateway(self.store, Orchestrator())
            try:
                with self.assertRaises(RuntimeError):
                    await gateway._process_chat_message("serial-demo", self.accept())
                self.assertFalse(any(r["method"] == "turn/start" for r in server.requests))
                self.assertEqual(self.store.get_workflow("serial-demo")["pendingAdvance"]["state"], "held")
            finally:
                await gateway.event_batcher.close()

    async def test_oversized_original_summary_is_not_silently_truncated(self):
        with self.store._connect() as connection:
            connection.execute("UPDATE workflow_nodes SET response=? WHERE node_id='a'", ("x" * 20001,))
        gateway = fixture_gateway(self.store, Orchestrator())
        try:
            with self.assertRaisesRegex(RuntimeError, "容量限制"):
                await gateway._process_chat_message("serial-demo", self.accept())
            self.assertFalse(gateway._discussion_jobs)
        finally:
            await gateway.event_batcher.close()

    async def test_saved_turn_replay_retries_commit_without_model_dispatch(self):
        message = self.accept()
        self.store.start_discussion("serial-demo", message["messageId"], self.store.discussion_target("serial-demo", message["messageId"]))
        self.store.update_discussion("serial-demo", message["messageId"], state="finished",
            response=json.dumps({"text": "已修改", "summary": "新计划"}))
        gateway = fixture_gateway(self.store, Orchestrator())
        try:
            with patch.object(self.store, "complete_discussion", side_effect=RuntimeError("保存失败")):
                with self.assertRaisesRegex(RuntimeError, "保存失败"):
                    await DiscussionService(gateway).run("serial-demo", message)
            self.assertEqual(self.store.get_node("serial-demo", "a")["response"], "原计划：登录、支付")
            with self.assertRaises(RuntimeError):
                self.store.confirm_advance("serial-demo", self.gate)
            await DiscussionService(gateway).run("serial-demo", message)
            self.assertEqual(self.store.get_node("serial-demo", "a")["response"], "新计划")
            self.assertFalse(gateway.assistant_orchestrator.jobs)
        finally:
            await gateway.event_batcher.close()

    async def test_cancel_reconciliation_reads_already_finished_turn_without_interrupt(self):
        message = self.accept()
        self.store.start_discussion("serial-demo", message["messageId"], self.store.discussion_target("serial-demo", message["messageId"]))
        self.store.update_discussion("serial-demo", message["messageId"], state="running", turn_id="known-turn")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.request.return_value = {"thread": {"turns": [{"id": "known-turn", "status": "completed",
            "items": [{"type": "agentMessage", "text": '{"text":"完成","summary":null}'}]}]}}
        gateway = fixture_gateway(self.store, Orchestrator())
        try:
            with patch.object(gateway.assistant_orchestrator, "_client_factory", return_value=client):
                await DiscussionService(gateway).reconcile(self.store.get_discussion("serial-demo", message["messageId"]), interrupt=True)
            self.assertEqual(client.request.await_count, 1)
            self.assertEqual(client.request.call_args.args[0], "thread/read")
        finally:
            await gateway.event_batcher.close()


if __name__ == "__main__":
    unittest.main()
