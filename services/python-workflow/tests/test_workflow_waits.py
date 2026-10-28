import json
import tempfile
import unittest
import uuid
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

from workflow_store import WorkflowStore, utc_now
from workflow_outcomes import instruction
from tests.registry_fixtures import seed_agents
from tests.test_workflow_store import serial_workflow
from tests.test_workflow_outcomes import result


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkflowStore(Path(self.temp.name) / "state.db")
        seed_agents(self.store, {"local": {"capabilities": ["supervisor", "executor"]}})

    def start(self, single=False, manual=False):
        spec = serial_workflow()
        spec["advanceMode"] = "semi_automatic"
        spec["waitPolicy"] = {"version": 2} if manual else {"version": 1, "confirmationMinutes": 2, "blockedMinutes": 30}
        if single:
            spec["nodes"] = spec["nodes"][:1]
        self.store.create_workflow(spec)
        self.store.claim_next_workflow("local")
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.attach_node_job("serial-demo", "a", {"job_id": "job-a", "thread_id": "thread-a", "status": "running"})

    def sync(self, value):
        self.store.sync_node_job("serial-demo", "a", {"job_id": "job-a", "thread_id": "thread-a", "status": "completed", "finished_at": utc_now(), "businessResult": value})
        return self.store.get_workflow("serial-demo")["pendingAdvance"]

    def expire(self, gate):
        with self.store._connect() as connection:
            connection.execute("UPDATE workflow_advance_gates SET expires_at='2000-01-01T00:00:00+00:00' WHERE gate_id=?", (gate["gateId"],))

    def test_notification_context_is_excluded_from_execution_views(self):
        from workflow_waits import execution_wait_view
        from workflow_gateway import _sidecar_workflow_view
        gate = {"gateId": "gate", "notificationContextId": str(uuid.uuid4()), "state": "held"}
        self.assertEqual(execution_wait_view(gate), {"gateId": "gate", "state": "held"})
        self.assertIsNone(execution_wait_view(None))
        self.assertNotIn("notificationContextId", _sidecar_workflow_view({"pendingAdvance": gate})["pendingAdvance"])
        self.assertIn("notificationContextId", gate)

    def test_concurrent_clicks_keep_one_winning_context(self):
        from concurrent.futures import ThreadPoolExecutor
        self.start(manual=True)
        gate = self.sync(result("success"))
        contexts = [str(uuid.uuid4()), str(uuid.uuid4())]
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(lambda context: self.store.confirm_advance("serial-demo", gate["gateId"], context), contexts))
        self.assertEqual(replies[0]["notificationContextId"], replies[1]["notificationContextId"])
        self.assertEqual(sum(bool(reply.get("duplicate")) for reply in replies), 1)

    def test_existing_database_adds_nullable_notification_columns(self):
        self.start(manual=True)
        gate = self.sync(result("success"))
        with self.store._connect() as connection:
            connection.execute("ALTER TABLE workflow_nodes DROP COLUMN notification_context_id")
            connection.execute("ALTER TABLE workflow_advance_gates DROP COLUMN notification_context_id")
            connection.execute("ALTER TABLE workflow_advance_gates DROP COLUMN result_notification_context_id")
        self.store = WorkflowStore(Path(self.temp.name) / "state.db")
        restored = self.store.get_workflow("serial-demo")["pendingAdvance"]
        self.assertEqual(restored["gateId"], gate["gateId"])
        self.assertIsNone(restored["notificationContextId"])
        self.store.confirm_advance("serial-demo", gate["gateId"])

    def test_notification_context_tracks_winning_confirm_and_survives_restart(self):
        self.start(manual=True)
        gate = self.sync(result("success"))
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        self.assertEqual(self.store.confirm_advance("serial-demo", gate["gateId"], first)["notificationContextId"], first)
        self.assertEqual(self.store.confirm_advance("serial-demo", gate["gateId"], second)["notificationContextId"], first)
        self.store = WorkflowStore(Path(self.temp.name) / "state.db")
        self.store.prepare_node_dispatch("serial-demo", "b")
        self.store.attach_node_job("serial-demo", "b", {"job_id": "job-b", "thread_id": "thread-b", "status": "running"})
        self.store.sync_node_job("serial-demo", "b", {"job_id": "job-b", "thread_id": "thread-b", "status": "completed", "businessResult": result("success")})
        next_gate = self.store.get_workflow("serial-demo")["pendingAdvance"]
        self.assertEqual(next_gate["notificationContextId"], first)
        self.assertNotEqual(next_gate["gateId"], gate["gateId"])

    def test_web_confirm_cannot_be_claimed_by_late_dingtalk_click(self):
        self.start(manual=True)
        gate = self.sync(result("success"))
        self.store.confirm_advance("serial-demo", gate["gateId"])
        duplicate = self.store.confirm_advance("serial-demo", gate["gateId"], str(uuid.uuid4()))
        self.assertTrue(duplicate["duplicate"])
        self.assertIsNone(duplicate["notificationContextId"])

    def test_retry_context_changes_only_for_next_real_execution(self):
        self.start(manual=True)
        gate = self.sync(result("blocked"))
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry", notification_context_id=first)
        duplicate = self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry", notification_context_id=second)
        self.assertEqual(duplicate["notificationContextId"], first)
        self.store.prepare_wait_execution("serial-demo", gate["gateId"])
        self.store.attach_node_job("serial-demo", "a", {"job_id": "job-a", "thread_id": "thread-a", "status": "running"})
        new_gate = self.sync(result("blocked"))
        self.assertEqual(new_gate["notificationContextId"], first)
        self.store.claim_policy_wait("serial-demo", new_gate["gateId"], "retry", notification_context_id=second)
        self.store.prepare_wait_execution("serial-demo", new_gate["gateId"])
        self.store.attach_node_job("serial-demo", "a", {"job_id": "job-a", "thread_id": "thread-a", "status": "running"})
        self.assertEqual(self.sync(result("success"))["notificationContextId"], second)

    def test_success_uses_central_timer_and_never_dispatches_early(self):
        self.start()
        gate = self.sync(result("success"))
        self.assertEqual(gate["kind"], "advance")
        self.expire(gate)
        self.assertIsNotNone(self.store.get_workflow("serial-demo")["pendingAdvance"])
        self.assertFalse(self.store.release_timed_out_advance("serial-demo", gate["gateId"]))
        with self.assertRaises(RuntimeError):
            self.store.prepare_node_dispatch("serial-demo", "b")
        self.store.claim_policy_wait("serial-demo", gate["gateId"], "confirm", timed_out=True)
        self.assertFalse(self.store.prepare_node_dispatch("serial-demo", "b")["alreadyDispatched"])

    def test_last_step_waits_and_finishes_with_latest_summary(self):
        self.start(single=True)
        gate = self.sync(result("success"))
        self.assertEqual(gate["kind"], "finish")
        self.assertIsNone(gate["nextNodeId"])
        self.store.finish_workflow("serial-demo", supervisor_status="completed", response="old", error=None)
        self.assertEqual(self.store.get_workflow("serial-demo")["status"], "running")
        self.assertTrue(self.store.has_supervisor_lease("serial-demo"))
        self.store.confirm_advance("serial-demo", gate["gateId"])
        self.store.complete_final_wait("serial-demo", gate["gateId"])
        state = self.store.get_workflow("serial-demo")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["response"], result("success")["summary"])
        self.assertFalse(self.store.has_supervisor_lease("serial-demo"))
        self.assertTrue(self.store.confirm_advance("serial-demo", gate["gateId"])["duplicate"])

    def test_blocked_wait_does_not_terminate_or_skip_and_can_discuss(self):
        self.start()
        gate = self.sync(result())
        state = self.store.get_workflow("serial-demo")
        self.assertEqual(gate["kind"], "blocked")
        self.assertIsNone(state["termination"])
        self.assertEqual([n["status"] for n in state["nodes"]], ["waiting", "pending", "pending"])
        with self.assertRaises(RuntimeError):
            self.store.confirm_advance("serial-demo", gate["gateId"])
        mid = str(uuid.uuid4())
        self.store.observe_input("serial-demo", mid)
        self.assertEqual(self.store.discussion_target("serial-demo", mid)["threadId"], "thread-a")
        self.expire(gate)
        self.assertEqual(self.store.due_policy_waits(), [])

    def test_retry_claim_is_idempotent_and_archives_only_current_step(self):
        self.start()
        gate = self.sync(result())
        with self.store._connect() as c:
            c.execute("INSERT INTO workflow_node_runtime VALUES ('serial-demo','a',0,'/original/work','original-model')")
        self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")
        self.assertTrue(self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")["duplicate"])
        target = self.store.prepare_wait_execution("serial-demo", gate["gateId"])
        self.assertEqual(target["threadId"], "thread-a")
        self.assertEqual(target["cwd"], "/original/work")
        self.assertEqual(target["model"], "original-model")
        self.assertIsNone(self.store.prepare_wait_execution("serial-demo", gate["gateId"]))
        self.sync(result("success"))  # 上轮结果迟到，不能完成尚未绑定的新执行。
        with self.store._connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM workflow_node_attempts").fetchone()[0], 1)
        state = self.store.get_workflow("serial-demo")
        self.assertEqual([n["status"] for n in state["nodes"]], ["queued", "pending", "pending"])

    def test_legacy_attempt_table_upgrade_preserves_history_and_allows_retry(self):
        self.start(manual=True)
        gate = self.sync(result("blocked"))
        with self.store._connect() as c:
            c.execute("ALTER TABLE workflow_node_attempts DROP COLUMN dispatch_token")
            c.execute(
                "INSERT INTO workflow_node_attempts(workflow_id,node_id,attempt_number,status,response,archived_at) "
                "VALUES ('serial-demo','a',0,'waiting','历史总结',?)", (utc_now(),))
            c.execute("UPDATE workflow_nodes SET attempt_count=1 WHERE workflow_id='serial-demo' AND node_id='a'")
        self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")
        with self.assertRaisesRegex(sqlite3.OperationalError, "dispatch_token"):
            self.store.prepare_wait_execution("serial-demo", gate["gateId"])
        self.assertEqual(self.store.policy_wait("serial-demo", gate["gateId"])["execution_started"], 0)
        # 重复初始化不改写历史，也不自动执行工作流。
        self.store = WorkflowStore(self.store.path)
        self.store = WorkflowStore(self.store.path)
        self.assertTrue(self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")["duplicate"])
        self.assertIsNotNone(self.store.prepare_wait_execution("serial-demo", gate["gateId"]))
        self.assertIsNone(self.store.prepare_wait_execution("serial-demo", gate["gateId"]))
        with self.store._connect() as c:
            rows = c.execute("SELECT attempt_number,response,dispatch_token FROM workflow_node_attempts ORDER BY attempt_number").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(tuple(rows[0]), (0, "历史总结", None))
        self.assertEqual(rows[1]["attempt_number"], 1)
        state = self.store.get_workflow("serial-demo")
        self.assertEqual(state["nodes"][0]["attemptCount"], 2)
        self.assertEqual([n["status"] for n in state["nodes"]], ["queued", "pending", "pending"])

    def test_timeout_only_finalizes_after_cleanup_and_preserves_original_reason(self):
        self.start()
        gate = self.sync(result())
        self.expire(gate)
        self.store.claim_policy_wait("serial-demo", gate["gateId"], "timeout", timed_out=True)
        self.assertIsNone(self.store.get_workflow("serial-demo")["termination"])
        cleanup = result()
        cleanup["reason"] = "收尾中的其他说明"
        self.store.finish_wait_result("serial-demo", gate["gateId"], cleanup)
        self.store.finish_business_termination("serial-demo")
        state = self.store.get_workflow("serial-demo")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["termination"]["reason"], "缺少产品确认")

    def test_receipt_does_not_extend_and_stop_is_not_business_termination(self):
        self.start()
        gate = self.sync(result())
        self.assertFalse(self.store.mark_advance_notified("serial-demo", gate["gateId"], utc_now())["updated"])
        self.store.cancel_workflow("serial-demo", gate_id=gate["gateId"])
        state = self.store.get_workflow("serial-demo")
        self.assertEqual(state["status"], "cancelled")
        self.assertIsNone(state["termination"])

    def test_old_card_cannot_cancel_new_step(self):
        self.start()
        gate = self.sync(result("success"))
        self.store.confirm_advance("serial-demo", gate["gateId"])
        with self.assertRaises(RuntimeError):
            self.store.cancel_workflow("serial-demo", gate_id=gate["gateId"])

    def test_policy_validation_and_legacy_snapshot(self):
        spec = serial_workflow()
        self.assertNotIn("waitPolicy", self.store.normalize_spec(spec))
        for minutes in (True, 0, 721, "2"):
            spec["waitPolicy"] = {"version": 1, "confirmationMinutes": minutes, "blockedMinutes": 30}
            with self.assertRaises(ValueError):
                self.store.normalize_spec(spec)

    def test_deferred_instruction_does_not_authorize_stop_comment(self):
        prompt = instruction("workflow", "a", False, defer_blocked=True)
        self.assertIn("禁止执行停止评论", prompt)
        self.assertNotIn("允许将该 Jira 的处理角色字段改为", prompt)

    def test_schema_reopens_without_losing_wait(self):
        self.start()
        gate = self.sync(result())
        reopened = WorkflowStore(self.store.path)
        self.assertEqual(reopened.get_workflow("serial-demo")["pendingAdvance"], gate)

    def test_queued_input_blocks_retry_and_stop_cannot_race_claimed_cleanup(self):
        self.start()
        gate = self.sync(result())
        self.store.observe_input("serial-demo", str(uuid.uuid4()), receiving=True)
        with self.assertRaises(RuntimeError):
            self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")

    def test_cancel_during_claimed_cleanup_is_rejected(self):
        self.start()
        gate = self.sync(result())
        self.expire(gate)
        self.store.claim_policy_wait("serial-demo", gate["gateId"], "timeout", timed_out=True)
        with self.assertRaises(RuntimeError):
            self.store.cancel_workflow("serial-demo")

    def test_prestart_failure_holds_but_unknown_write_blocks_repetition(self):
        self.start()
        gate = self.sync(result())
        self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")
        self.store.prepare_wait_execution("serial-demo", gate["gateId"])
        self.store.fail_wait_execution("serial-demo", gate["gateId"], before_start=True)
        restored = self.store.get_workflow("serial-demo")["pendingAdvance"]
        self.assertEqual(restored["state"], "held")
        self.assertNotEqual(restored["gateId"], gate["gateId"])
        gate = restored
        self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")
        self.store.prepare_wait_execution("serial-demo", gate["gateId"])
        self.store.fail_wait_execution("serial-demo", gate["gateId"], uncertain=True)
        state = self.store.get_workflow("serial-demo")
        self.assertEqual(state["pendingAdvance"]["state"], "busy")
        self.assertEqual(self.store.due_policy_waits(), [])
        self.assertIsNone(self.store.prepare_wait_execution("serial-demo", gate["gateId"]))
        with self.assertRaises(RuntimeError):
            self.store.cancel_workflow("serial-demo")


class ManualWaitTests(unittest.TestCase):
    setUp = WaitTests.setUp
    start = WaitTests.start
    sync = WaitTests.sync
    expire = WaitTests.expire

    def manual_wait(self, outcome="success", single=False):
        self.start(single=single, manual=True)
        gate = self.sync(result(outcome))
        self.assertEqual(gate["state"], "held")
        self.expire(gate)
        self.assertEqual(self.store.due_policy_waits(), [])
        action = "timeout" if outcome == "blocked" else "confirm"
        with self.assertRaises(RuntimeError):
            self.store.claim_policy_wait("serial-demo", gate["gateId"], action, timed_out=True)
        self.assertFalse(self.store.release_timed_out_advance("serial-demo", gate["gateId"]))
        self.assertEqual(WorkflowStore(self.store.path).get_workflow("serial-demo")["pendingAdvance"]["state"], "held")
        return gate

    def test_intermediate_requires_explicit_confirmation(self):
        gate = self.manual_wait()
        with self.assertRaises(RuntimeError):
            self.store.prepare_node_dispatch("serial-demo", "b")
        self.assertFalse(self.store.mark_advance_notified("serial-demo", gate["gateId"], utc_now())["updated"])
        self.store.confirm_advance("serial-demo", gate["gateId"])
        self.assertTrue(self.store.confirm_advance("serial-demo", gate["gateId"])["duplicate"])
        self.assertFalse(self.store.prepare_node_dispatch("serial-demo", "b")["alreadyDispatched"])

    def test_final_requires_explicit_confirmation(self):
        gate = self.manual_wait(single=True)
        self.store.finish_workflow("serial-demo", supervisor_status="completed", response="old", error=None)
        self.assertEqual(self.store.get_workflow("serial-demo")["status"], "running")
        self.assertTrue(self.store.has_supervisor_lease("serial-demo"))
        self.store.confirm_advance("serial-demo", gate["gateId"])
        self.store.complete_final_wait("serial-demo", gate["gateId"])
        self.assertEqual(self.store.get_workflow("serial-demo")["status"], "completed")
        self.assertFalse(self.store.has_supervisor_lease("serial-demo"))

    def test_blocked_requires_retry_and_repeated_block_stays_held(self):
        gate = self.manual_wait("blocked")
        self.assertIsNone(self.store.get_workflow("serial-demo")["termination"])
        with self.assertRaises(RuntimeError):
            self.store.confirm_advance("serial-demo", gate["gateId"])
        self.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")
        self.store.prepare_wait_execution("serial-demo", gate["gateId"])
        self.store.attach_node_job("serial-demo", "a", {"job_id": "retry-a", "thread_id": "thread-a", "status": "running"})
        self.store.sync_node_job("serial-demo", "a", {"job_id": "retry-a", "thread_id": "thread-a", "status": "completed", "businessResult": result("blocked")})
        current = self.store.get_workflow("serial-demo")["pendingAdvance"]
        self.assertEqual(current["state"], "held")
        self.assertNotEqual(current["gateId"], gate["gateId"])
        self.assertEqual(self.store.due_policy_waits(), [])
        with self.assertRaises(RuntimeError):
            self.store.cancel_workflow("serial-demo", gate_id=gate["gateId"])
        self.store.cancel_workflow("serial-demo", gate_id=current["gateId"])
        self.assertEqual(self.store.get_workflow("serial-demo")["status"], "cancelled")

    def test_input_in_progress_still_blocks_confirmation(self):
        gate = self.manual_wait()
        self.store.observe_input("serial-demo", str(uuid.uuid4()), receiving=True)
        with self.assertRaises(RuntimeError):
            self.store.confirm_advance("serial-demo", gate["gateId"])


    def test_policy_rejects_timer_fields_and_invalid_versions(self):
        spec = serial_workflow()
        spec["advanceMode"] = "semi_automatic"
        for policy in ({"version": 2, "confirmationMinutes": 2}, {"version": True}, {"version": 3}):
            spec["waitPolicy"] = policy
            with self.assertRaises(ValueError):
                self.store.normalize_spec(spec)


class WaitExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_preparation_failure_returns_to_manual_wait_without_dispatch(self):
        from workflow_wait_service import WaitService
        fixture = WaitTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start(manual=True)
        gate = fixture.sync(result("blocked"))
        fixture.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")
        with fixture.store._connect() as c:
            c.execute("ALTER TABLE workflow_node_attempts DROP COLUMN dispatch_token")
        gateway = SimpleNamespace(store=fixture.store, _control_locks={},
            _pause_supervisor=AsyncMock(), _resume_supervisor_if_needed=AsyncMock())
        service = WaitService(gateway)
        with patch.object(service, "execute", new_callable=AsyncMock) as execute:
            with self.assertLogs("workflow_wait_service", level="ERROR"):
                await service.process(fixture.store.due_policy_waits()[0])
            execute.assert_not_awaited()
        state = fixture.store.get_workflow("serial-demo")
        self.assertEqual(state["pendingAdvance"]["state"], "held")
        self.assertNotEqual(state["pendingAdvance"]["gateId"], gate["gateId"])
        self.assertEqual(state["nodes"][0]["attemptCount"], 0)
        self.assertIn("未能启动", state["nodes"][0]["error"])
        self.assertEqual(fixture.store.due_policy_waits(), [])

    async def test_manual_retry_uses_original_workspace_and_returns_to_wait(self):
        from codex_orchestrator_mcp import Orchestrator, AgentConfig
        from workflow_wait_service import WaitService
        from workflow_event_batcher import AsyncEventBatcher
        from tests.mock_app_server import MockAppServer
        fixture = ManualWaitTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start(manual=True)
        first = fixture.sync(result("success"))
        fixture.store.confirm_advance("serial-demo", first["gateId"])
        fixture.store.prepare_node_dispatch("serial-demo", "b")
        fixture.store.attach_node_job("serial-demo", "b", {"job_id": "job-b", "thread_id": "thread-b", "status": "running"})
        fixture.store.sync_node_job("serial-demo", "b", {"job_id": "job-b", "thread_id": "thread-b", "status": "completed", "businessResult": result("blocked")})
        gate = fixture.store.get_workflow("serial-demo")["pendingAdvance"]
        fixture.store.claim_policy_wait("serial-demo", gate["gateId"], "retry")
        target = fixture.store.prepare_wait_execution("serial-demo", gate["gateId"])
        async with MockAppServer(structured_reply=json.dumps(result("success")), resume_thread_id="thread-b") as server:
            orchestrator = Orchestrator()
            orchestrator.agent_provider = lambda: {"local": AgentConfig.from_dict("local", {
                "url": server.url, "cwd": str(Path.cwd()), "allow_write": True,
                "artifact_root": str(Path(fixture.temp.name) / "artifacts")})}
            batcher = AsyncEventBatcher(fixture.store)
            gateway = SimpleNamespace(store=fixture.store, assistant_orchestrator=orchestrator,
                event_batcher=batcher, flush_events=batcher.flush, _schedule_pending=AsyncMock())
            await WaitService(gateway).execute("serial-demo", gate["gateId"], target)
            state = fixture.store.get_workflow("serial-demo")
            self.assertEqual(state["pendingAdvance"]["state"], "held")
            self.assertEqual(state["pendingAdvance"]["completedNodeId"], "b")
            resume = next(r["params"] for r in server.requests if r["method"] == "thread/resume")
            self.assertEqual(resume["cwd"], str(Path.cwd()))
            self.assertFalse(Path(fixture.temp.name, "artifacts").exists())

    async def test_scheduler_does_not_advance_manual_final_wait(self):
        from workflow_wait_service import WaitService
        fixture = ManualWaitTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        gate = fixture.manual_wait(single=True)
        gateway = SimpleNamespace(store=fixture.store, _control_locks={},
            _pause_supervisor=AsyncMock(), _schedule_pending=AsyncMock(), _resume_supervisor_if_needed=AsyncMock())
        service = WaitService(gateway)
        await service.tick()
        self.assertEqual(service.tasks, {})
        self.assertEqual(fixture.store.get_workflow("serial-demo")["status"], "running")
        gateway._pause_supervisor.assert_awaited_once_with("serial-demo")
        fixture.store.confirm_advance("serial-demo", gate["gateId"])
        await service.tick()
        await asyncio.gather(*list(service.tasks.values()))
        self.assertEqual(fixture.store.get_workflow("serial-demo")["status"], "completed")

    async def test_scheduler_pauses_and_finalizes_expired_final_wait_once(self):
        from workflow_wait_service import WaitService
        fixture = WaitTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start(single=True)
        gate = fixture.sync(result("success"))
        fixture.expire(gate)
        gateway = SimpleNamespace(store=fixture.store, _control_locks={},
            _pause_supervisor=AsyncMock(), _schedule_pending=AsyncMock(), _resume_supervisor_if_needed=AsyncMock())
        service = WaitService(gateway)
        await service.tick()
        await asyncio.gather(*list(service.tasks.values()))
        await service.tick()
        self.assertEqual(fixture.store.get_workflow("serial-demo")["status"], "completed")
        gateway._schedule_pending.assert_awaited_once_with()

    async def execute(self, outcome, *, closing=False, malformed=False):
        from codex_orchestrator_mcp import Orchestrator, AgentConfig
        from workflow_wait_service import WaitService
        from workflow_event_batcher import AsyncEventBatcher
        from tests.mock_app_server import MockAppServer
        fixture = WaitTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start(single=True)
        gate = fixture.sync(result())
        if closing:
            fixture.expire(gate)
        fixture.store.claim_policy_wait("serial-demo", gate["gateId"], "timeout" if closing else "retry", timed_out=closing)
        reply = "invalid" if malformed else json.dumps(result(outcome))
        async with MockAppServer(structured_reply=reply, resume_thread_id="thread-a") as server:
            orchestrator = Orchestrator()
            orchestrator.agent_provider = lambda: {"local": AgentConfig.from_dict("local", {"url": server.url, "cwd": str(Path.cwd())})}
            batcher = AsyncEventBatcher(fixture.store)
            gateway = SimpleNamespace(store=fixture.store, assistant_orchestrator=orchestrator,
                event_batcher=batcher, flush_events=batcher.flush, _schedule_pending=AsyncMock())
            service = WaitService(gateway)
            target = fixture.store.prepare_wait_execution("serial-demo", gate["gateId"])
            await asyncio.wait_for(service.execute("serial-demo", gate["gateId"], target), 10)
            self.assertEqual(sum(r["method"] == "turn/start" for r in server.requests), 1)
            self.assertEqual(sum(r["method"] == "thread/start" for r in server.requests), 0)
            return fixture.store.get_workflow("serial-demo"), gate

    async def test_retry_success_creates_final_wait(self):
        state, old = await self.execute("success")
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["nodes"][0]["status"], "completed")
        self.assertEqual(state["nodes"][0]["attemptCount"], 1)
        self.assertEqual(state["pendingAdvance"]["kind"], "finish")
        self.assertNotEqual(state["pendingAdvance"]["gateId"], old["gateId"])

    async def test_retry_blocked_creates_fresh_countdown(self):
        state, old = await self.execute("blocked")
        self.assertEqual(state["pendingAdvance"]["kind"], "blocked")
        self.assertEqual(state["pendingAdvance"]["state"], "countdown")
        self.assertNotEqual(state["pendingAdvance"]["gateId"], old["gateId"])
        self.assertIsNone(state["termination"])

    async def test_closing_finishes_and_releases(self):
        state, _ = await self.execute("blocked", closing=True)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["termination"]["jiraComment"]["status"], "succeeded")

    async def test_closing_invalid_result_still_finishes_without_resend(self):
        state, _ = await self.execute("blocked", closing=True, malformed=True)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["termination"]["jiraComment"]["status"], "unknown")

    async def test_retry_invalid_result_is_technical_failure(self):
        state, _ = await self.execute("blocked", malformed=True)
        self.assertEqual(state["status"], "failed")
        self.assertIsNone(state["termination"])
