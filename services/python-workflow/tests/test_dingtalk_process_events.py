import tempfile
import unittest
import uuid
from pathlib import Path

from codex_orchestrator_mcp import AgentConfig, Orchestrator
from workflow_gateway import WorkflowGateway
from workflow_store import WorkflowStore
from tests.mock_app_server import MockAppServer
from tests.test_workflow_store import serial_workflow


class DingTalkProcessEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_tool_output_keeps_lifecycle_and_readable_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "store.db"))
            store.create_workflow(serial_workflow())
            for item in (
                {"type": "imageGeneration", "status": "completed", "result": "x" * 300_000},
                {"type": "mcpToolCall", "tool": "check", "status": "failed", "error": {"message": "x" * 300_000}},
                {"type": "reasoning", "summary": ["核对结果"], "content": ["x" * 300_000]},
            ):
                store.add_event("serial-demo", node_id="a", source="assistant",
                    event_type="appserver.item/completed",
                    payload={"messageId": "question", "message": {"params": {"item": item}}})
            events = store.event_page("serial-demo", view="bot")["events"]
            self.assertEqual(len(events), 3)
            items = [event["payload"]["message"]["params"]["item"] for event in events]
            self.assertEqual(items[0]["status"], "completed")
            self.assertNotIn("result", items[0])
            self.assertEqual(items[1]["tool"], "check")
            self.assertIsNotNone(items[1]["error"])
            self.assertEqual(items[2]["summary"], ["核对结果"])
            self.assertNotIn("content", items[2])
            self.assertTrue(all(event["payload"]["messageId"] == "question" for event in events))

    async def test_assistant_process_precedes_answer_and_carries_question_identity(self):
        notifications = [
            {"method": "item/started", "params": {"item": {"type": "webSearch", "id": "tool-1"}}},
            {"method": "item/completed", "params": {"item": {"type": "webSearch", "id": "tool-1"}}},
            {"method": "item/reasoning/textDelta", "params": {"delta": "private reasoning"}},
            {"method": "item/completed", "params": {"item": {"type": "reasoning", "id": "r-1", "summary": ["核对资料"]}}},
        ]
        async with MockAppServer(process_notifications=notifications) as server:
            with tempfile.TemporaryDirectory() as directory:
                store = WorkflowStore(Path(directory, "store.db"))
                spec = serial_workflow()
                spec["supervisorAgentId"] = "local"
                store.create_workflow(spec)
                agent = AgentConfig.from_dict("local", {"url": server.url, "cwd": "/tasks"})
                orchestrator = Orchestrator(Path("unused-test-config"))
                orchestrator.load_agents = lambda: {"local": agent}
                gateway = WorkflowGateway(store, orchestrator, orchestrator)
                try:
                    for _ in range(2):
                        message = store.accept_chat_message("serial-demo", str(uuid.uuid4()), "检查进度")
                        await gateway._process_chat_message("serial-demo", message)
                    events = store.event_page("serial-demo", view="bot")["events"]
                    answers = [e for e in events if e["type"] == "chat.assistant.completed"]
                    self.assertEqual(len(answers), 2)
                    for answer in answers:
                        process = [e for e in events if e["source"] == "assistant"
                                   and e["payload"]["messageId"] == answer["payload"]["messageId"]]
                        self.assertEqual(len(process), 4)
                        self.assertTrue(all(e["sequence"] < answer["sequence"] for e in process))
                    self.assertNotIn("private reasoning", str(events))
                    starts = [r for r in server.requests if r.get("method") == "turn/start"]
                    self.assertTrue(starts)
                    self.assertTrue(all(r["params"]["summary"] == "auto" for r in starts))
                finally:
                    await gateway.stop()
                    await gateway.event_batcher.close()

    async def test_remote_batch_events_preserve_identity_and_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "store.db"))
            store.create_workflow(serial_workflow())
            event = {"workflow_id": "serial-demo", "node_id": "a", "source": "worker",
                     "event_type": "appserver.item/started", "payload": {"message": {"params": {"item": {"type": "webSearch"}}}},
                     "external_event_id": "remote-event"}
            first = store.add_events([event])
            self.assertEqual(store.add_events([event]), first)
            page = store.event_page("serial-demo", view="bot")
            self.assertEqual(len(page["events"]), 1)
            self.assertEqual(page["events"][0]["nodeId"], "a")
            self.assertEqual(store.event_page("serial-demo", view="bot", after=page["nextCursor"])["events"], [])
