import concurrent.futures
import asyncio
import tempfile
import threading
import unittest
import json
import subprocess
import sys
import uuid
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from starlette.testclient import TestClient
from workflow_gateway import WorkflowGateway, create_app, _database_call

from workflow_store import WorkflowStore


def spec(workflow_id, task_id=None):
    return {"workflowId": workflow_id, "taskDefinitionId": task_id,
            "nodes": [{"id": "a", "prompt": "test", "timeoutSec": 10}]}


class DatabaseThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_arriving_during_empty_queue_read_is_processed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "runtime.db"))
            store.create_workflow(spec("one"))
            gateway = WorkflowGateway(store, SimpleNamespace(jobs={}))
            entered, release = threading.Event(), threading.Event()
            original = store.claim_next_chat_message

            def claim(workflow_id):
                message = original(workflow_id)
                if message is None and not entered.is_set():
                    entered.set()
                    release.wait(5)
                return message

            async def process(workflow_id, message):
                await _database_call(store.complete_chat_message, workflow_id,
                                     message["messageId"], str(uuid.uuid4()), "已回复")

            gateway._process_chat_message = AsyncMock(side_effect=process)
            with patch.object(store, "claim_next_chat_message", side_effect=claim):
                gateway._ensure_chat_worker("one")
                worker = gateway._chat_tasks["one"]
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    message_id = str(uuid.uuid4())
                    await gateway.accept_message("one", message_id, "查询进度")
                finally:
                    release.set()
                    await asyncio.wait_for(worker, 5)
                gateway._process_chat_message.assert_awaited_once()
                self.assertEqual(store.get_workflow("one")["pendingChatCount"], 0)
                self.assertFalse(gateway._chat_tasks)

    async def test_concurrent_resumes_start_only_one_supervisor(self):
        store = SimpleNamespace(
            get_workflow=lambda _: {"status": "running", "supervisor": {}},
            has_supervisor_lease=lambda _: True,
            get_workflow_spec=lambda _: {"workflowId": "one"},
        )
        gateway = WorkflowGateway(store, SimpleNamespace(jobs={}, list_agents=lambda: []))
        entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finish = asyncio.Event()
        running = []

        async def database_call(function, *args, **kwargs):
            if function is store.get_workflow_spec:
                entered.set()
                await release.wait()
            return function(*args, **kwargs)

        async def run(*args, **kwargs):
            running.append(asyncio.current_task())
            await finish.wait()

        async def second_resume():
            second_started.set()
            await gateway._resume_supervisor_if_needed("one")

        gateway._run_supervisor = AsyncMock(side_effect=run)
        with patch("workflow_gateway._database_call", side_effect=database_call):
            first = asyncio.create_task(gateway._resume_supervisor_if_needed("one"))
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                second = asyncio.create_task(second_resume())
                await asyncio.wait_for(second_started.wait(), 2)
                release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 2)
                gateway._run_supervisor.assert_awaited_once()
                self.assertEqual(len(gateway._tasks), 1)
            finally:
                gateway._closing = True
                release.set()
                await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
                finish.set()
                await asyncio.gather(*running)

    async def test_cancel_waits_for_inflight_database_write_without_blocking_loop(self):
        entered = threading.Event()
        release = threading.Event()
        written = []
        def write():
            entered.set()
            release.wait(2)
            written.append(True)
        task = asyncio.create_task(_database_call(write))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(written, [True])


class RuntimePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name, "runtime.db")
        self.store = WorkflowStore(self.path)

    def test_identical_supervisor_and_assistant_snapshots_do_not_invalidate_poll(self):
        self.store.create_workflow(spec("one"))
        snapshot = {"status": "running", "job_id": "job", "thread_id": "thread"}
        self.store.update_supervisor("one", snapshot)
        self.store.update_assistant("one", snapshot)
        first = self.store.poll_workflow("one")
        self.store.update_supervisor("one", snapshot)
        self.store.update_assistant("one", snapshot)
        self.assertTrue(self.store.poll_workflow("one", first["revision"])["unchanged"])
        self.store.update_supervisor("one", {**snapshot, "response": "new result"})
        changed = self.store.poll_workflow("one", first["revision"])
        self.assertEqual(changed["response"], "new result")
        self.store.update_assistant("one", {**snapshot, "turn_id": "next"})
        self.assertNotEqual(self.store.poll_workflow("one")["revision"], changed["revision"])

    def test_task_scope_blocks_create_and_restart_across_supervisors(self):
        self.store.create_workflow(spec("old", "task"))
        self.store.stop_workflow("old")
        other = spec("new", "task")
        other["supervisorAgentId"] = "another-supervisor"
        self.store.create_workflow(other)
        reopened_store = WorkflowStore(self.path)
        with self.assertRaisesRegex(ValueError, "其他运行"):
            reopened_store.restart_from_node("old", "a")
        self.assertEqual(reopened_store.get_workflow("old")["retryPolicy"]["usedRetries"], 0)
        self.assertEqual(reopened_store.get_workflow("old")["status"], "cancelled")
        self.store.stop_workflow("new")
        self.store.restart_from_node("old", "a")
        with self.assertRaisesRegex(ValueError, "其他运行"):
            self.store.create_workflow(spec("third", "task"))

    def test_historical_binding_is_immutable_and_does_not_change_spec(self):
        self.store.create_workflow(spec("old"))
        original = self.store.get_spec("old")
        self.store.register_task_bindings("task", ["old", "absent"])
        self.store.register_task_bindings("task", ["old"])
        self.assertEqual(original, self.store.get_spec("old"))
        with self.assertRaisesRegex(ValueError, "归属不能修改"):
            self.store.register_task_bindings("another", ["old"])
        with self.assertRaisesRegex(ValueError, "其他运行"):
            self.store.create_workflow(spec("new", "task"))

    def test_legacy_restart_fails_closed_until_task_scope_is_registered(self):
        self.store.create_workflow(spec("legacy"))
        self.store.stop_workflow("legacy")
        original = self.store.get_spec("legacy")
        original.pop("taskDefinitionId")
        with self.store._connect() as connection:
            connection.execute("UPDATE workflows SET spec_zlib = ? WHERE workflow_id = 'legacy'",
                               (zlib.compress(json.dumps(original).encode()),))
        with self.assertRaisesRegex(ValueError, "升级登记"):
            self.store.restart_from_node("legacy", "a")
        self.store.register_task_bindings("task", ["legacy"])
        self.assertEqual(self.store.restart_from_node("legacy", "a")["status"], "queued")

    def test_concurrent_submissions_acquire_only_one_task_scope(self):
        barrier = threading.Barrier(2)
        def submit(workflow_id):
            barrier.wait()
            try:
                self.store.create_workflow(spec(workflow_id, "task"))
                return True
            except ValueError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, name) for name in ("one", "two")]
            self.assertEqual(sum(future.result() for future in futures), 1)

    def test_unchanged_poll_does_not_read_full_snapshot_and_detects_changes(self):
        self.store.create_workflow(spec("one"))
        first = self.store.poll_workflow("one")
        with patch.object(self.store, "get_workflow", side_effect=AssertionError("full read")):
            self.assertTrue(self.store.poll_workflow("one", first["revision"])["unchanged"])
        self.store.set_supervisor_message("one", "progress")
        changed = self.store.poll_workflow("one", first["revision"])
        self.assertEqual(changed["supervisor"]["lastMessage"], "progress")
        self.assertNotEqual(changed["revision"], first["revision"])

    def test_filtered_pages_preserve_cursors_and_backwards_history(self):
        self.store.create_workflow(spec("one"))
        raw = {"workflow_id": "one", "node_id": "a", "source": "worker",
               "event_type": "appserver.raw", "payload": {"noise": "x"}}
        visible = {**raw, "source": "chat", "event_type": "chat.assistant.completed"}
        events = [dict(raw) for _ in range(10000)]
        for index in (5, 300, 9990):
            events[index] = dict(visible)
        sequences = self.store.add_events(events)
        latest = self.store.event_page("one", view="monitor", tail=True, limit=2)
        self.assertEqual([event["sequence"] for event in latest["events"]],
                         [sequences[300], sequences[9990]])
        self.assertTrue(latest["hasOlder"])
        older = self.store.event_page("one", view="monitor", before=latest["oldestCursor"], limit=2)
        self.assertEqual([event["sequence"] for event in older["events"]], [sequences[5]])
        self.assertFalse(older["hasOlder"])
        first = self.store.event_page("one", view="bot", limit=2)
        second = self.store.event_page("one", view="bot", after=first["nextCursor"], limit=2)
        self.assertTrue(first["hasMore"])
        self.assertEqual(second["nextCursor"], sequences[-1])
        empty = self.store.event_page("one", view="bot", after=second["nextCursor"])
        self.assertEqual(empty["events"], [])
        extra = self.store.add_events([visible])[0]
        self.assertEqual(self.store.event_page("one", view="bot", after=empty["nextCursor"])
                         ["events"][0]["sequence"], extra)

    def test_poll_omits_unchanged_result_bodies_and_detects_late_result_update(self):
        self.store.create_workflow(spec("one"))
        with self.store._connect() as connection:
            connection.execute("UPDATE workflow_nodes SET response = ? WHERE workflow_id = ?",
                               ("长结果" * 5000, "one"))
        first = self.store.poll_workflow("one")
        known = [node["resultRevision"] for node in first["nodes"]]
        self.store.set_supervisor_message("one", "新进度")
        second = self.store.poll_workflow("one", first["revision"], known)
        self.assertTrue(second["nodes"][0]["resultUnchanged"])
        self.assertNotIn("response", second["nodes"][0])
        with self.store._connect() as connection:
            connection.execute("UPDATE workflow_nodes SET response = '更新结果' WHERE workflow_id = 'one'")
        third = self.store.poll_workflow("one", second["revision"], known)
        self.assertEqual(third["nodes"][0]["response"], "更新结果")

    def test_raw_events_change_cursor_without_reloading_results(self):
        self.store.create_workflow(spec("one"))
        first = self.store.poll_workflow("one")
        sequence = self.store.add_event("one", node_id="a", source="worker", event_type="raw", payload={})
        with patch.object(self.store, "get_workflow", side_effect=AssertionError("full read")):
            current = self.store.poll_workflow("one", first["revision"])
        self.assertTrue(current["unchanged"])
        self.assertEqual(current["lastEventSequence"], sequence)

    def test_invalid_paging_and_batch_inputs_are_rejected(self):
        self.store.create_workflow(spec("one"))
        for kwargs in ({"view": "invalid"}, {"after": -1}, {"limit": 0},
                       {"before": 0}, {"tail": True, "after": 1}):
            with self.assertRaises(ValueError):
                self.store.event_page("one", **kwargs)
        with self.assertRaises(ValueError):
            self.store.workflow_statuses([None])
        self.assertEqual(self.store.workflow_statuses(["one", "missing"]), {"one": "queued"})

    def test_event_compression_preserves_payload_and_old_history(self):
        self.store.create_workflow(spec("one"))
        payload = {"text": "中文完整消息" * 1000}
        sequence = self.store.add_event("one", node_id=None, source="chat",
                                       event_type="chat.assistant.completed", payload=payload)
        self.assertEqual(self.store.list_events("one")[-1]["payload"], payload)
        # 模拟升级前的未压缩事件。
        with self.store._connect() as connection:
            connection.execute("UPDATE workflow_events SET payload_json = ?, payload_zlib = NULL WHERE sequence = ?",
                               (json.dumps(payload), sequence))
        self.assertEqual(self.store.compact_terminal_events("2100-01-01T00:00:00+00:00")["scanned"], 0)
        self.store.stop_workflow("one")
        script = Path(__file__).resolve().parents[3] / "scripts/compact_workflow_events.py"
        result = subprocess.run([sys.executable, str(script), "--db", str(self.path),
                                 "--before", "2100-01-01T00:00:00+00:00"], capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)["dryRun"])
        with self.store._connect() as connection:
            self.assertIsNone(connection.execute("SELECT payload_zlib FROM workflow_events WHERE sequence = ?", (sequence,)).fetchone()[0])
        compressed = self.store.compact_terminal_events("2100-01-01T00:00:00+00:00")
        self.assertEqual(compressed["compacted"], 1)
        self.assertGreater(compressed["savedBytes"], 1000)
        event = self.store.event_page("one", view="monitor")["events"][0]
        self.assertEqual(event["sequence"], sequence)
        self.assertEqual(event["payload"], payload)

    def test_http_poll_events_scope_registration_and_status_batches(self):
        self.store.create_workflow(spec("one"))
        app = create_app(db_path=self.path, orchestrator=SimpleNamespace())
        client = TestClient(app)
        self.addCleanup(client.close)
        first = client.get("/workflows/one").json()
        same = client.get("/workflows/one", params={"knownRevision": first["revision"]})
        self.assertTrue(same.json()["unchanged"])
        self.assertEqual(client.get("/workflows/one?knownResults=bad-json").status_code, 400)
        events = client.get("/workflows/one/events/history?view=monitor&tail=true").json()
        self.assertEqual(events["events"], [])
        self.assertGreater(events["nextCursor"], 0)
        response = client.post("/workflow-task-bindings", json={"taskDefinitionId": "task", "workflowIds": ["one"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.post("/workflow-statuses", json={"workflowIds": ["one"]}).json(),
                         {"statuses": {"one": "queued"}})
        self.assertEqual(client.post("/workflow-statuses", json={"workflowIds": []}).status_code, 400)
