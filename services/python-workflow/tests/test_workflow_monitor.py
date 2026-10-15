import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient
from workflow_gateway import create_app

from workflow_store import WorkflowStore
from workflow_monitor import monitor_event, public_text


def event(item, sequence=2, source="worker", completed=True):
    return {"sequence": sequence, "nodeId": "a", "source": source,
            "type": "appserver.item/" + ("completed" if completed else "started"),
            "payload": {"message": {"params": {"turnId": "private-turn", "item": item}}}}


class MonitorTests(unittest.TestCase):
    def test_large_completed_tool_retains_identity_after_storage_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "monitor.db"))
            store.create_workflow({"workflowId": "one", "nodes": [{"id": "a", "prompt": "测试"}]})
            item = {"id": "tool-1", "type": "commandExecution"}
            for raw in (event(item, completed=False), event({**item, "aggregatedOutput": "x" * 300000})):
                store.add_event("one", node_id="a", source="worker", event_type=raw["type"], payload=raw["payload"])
            details = [e for e in store.event_page("one", view="monitor_details")["events"]
                       if e["type"] == "monitor.execution.detail"]
            self.assertEqual(details[0]["payload"]["key"], details[1]["payload"]["key"])
            self.assertTrue(details[1]["payload"]["completed"])
            self.assertTrue(details[1]["payload"]["truncated"])

    def test_invalid_detail_fields_do_not_break_page_or_change_bot_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "monitor.db"))
            store.create_workflow({"workflowId": "one", "nodes": [{"id": "a", "prompt": "测试"}]})
            payloads = [{"message": "旧记录"}, {"message": {"params": None}}]
            payloads += [event(item)["payload"] for item in (
                {"type": []}, {"type": "reasoning", "summary": 42},
                {"type": "reasoning", "summary": None},
                {"type": "mcpToolCall", "status": [], "result": {"content": [{"type": []}]}},
                {"type": "reasoning", "summary": ["保留的摘要"]})]
            for payload in payloads:
                store.add_event("one", node_id="a", source="worker",
                                event_type="appserver.item/completed", payload=payload)
            bot_before = store.event_page("one", view="bot")
            audit_before = store.event_page("one", view="all")
            page = store.event_page("one", view="monitor_details")
            self.assertEqual(page["events"][-1]["payload"]["text"], "保留的摘要")
            self.assertEqual(page["nextCursor"], audit_before["nextCursor"])
            self.assertEqual(store.event_page("one", view="bot"), bot_before)
            self.assertEqual(store.event_page("one", view="all"), audit_before)

    def test_gateway_details_projection_and_tool_text_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "monitor.db")
            store = WorkflowStore(path)
            store.create_workflow({"workflowId": "one", "nodes": [{"id": "a", "prompt": "测试"}]})
            raw = event({"id": "tool", "type": "dynamicToolCall", "success": True,
                         "contentItems": [{"type": "inputText", "text": "找到记录\npasswd=hidden-value"},
                                          {"type": "image", "data": "private-image"}]})
            store.add_event("one", node_id="a", source="worker", event_type=raw["type"], payload=raw["payload"])
            client = TestClient(create_app(db_path=path, orchestrator=SimpleNamespace()))
            self.addCleanup(client.close)
            response = client.get("/workflows/one/events/history?view=monitor_details&tail=true")
            self.assertEqual(response.status_code, 200)
            self.assertIn("找到记录", response.json()["events"][-1]["payload"]["text"])
            for hidden in ("hidden-value", "private-image", "private-turn", "contentItems"):
                self.assertNotIn(hidden, response.text)
            after = response.json()["nextCursor"]
            late = store.add_event("one", node_id="a", source="worker", event_type=raw["type"], payload=raw["payload"])
            incremental = client.get(f"/workflows/one/events/history?view=monitor_details&after={after}").json()
            self.assertEqual([e["sequence"] for e in incremental["events"]], [late])

    def test_only_public_summary_not_raw_reasoning_or_internal_metadata(self):
        result = monitor_event(event({"id": "private-item", "type": "reasoning",
                                      "summary": [{"text": "核对步骤依赖"}],
                                      "content": [{"text": "raw-private-reasoning"}]}))
        self.assertEqual(result["payload"]["text"], "核对步骤依赖")
        self.assertNotIn("private", str(result))
        self.assertIsNone(monitor_event(event({"type": "reasoning", "summary": []})))

    def test_tool_updates_share_key_and_filter_secrets_without_mutating_audit(self):
        item = {"id": "tool", "type": "commandExecution", "command": "private-command",
                "aggregatedOutput": "测试通过\nTOKEN=secret-value\nhttp://10.0.0.1:8080/private\nBearer secret-bearer"}
        start = monitor_event(event(item, completed=False))
        done = monitor_event(event(item, sequence=3))
        self.assertEqual(start["payload"]["key"], done["payload"]["key"])
        self.assertIn("测试通过", done["payload"]["text"])
        for value in ("secret-value", "secret-bearer", "10.0.0.1", "private-command", "private-turn"):
            self.assertNotIn(value, str(done))
        self.assertIn("secret-value", item["aggregatedOutput"])
        failed = monitor_event(event({**item, "exitCode": 1, "error": "private-error"}))
        self.assertEqual(failed["payload"]["text"], "未成功")

    def test_supervisor_noise_final_answer_and_unknown_items_not_duplicated(self):
        for item in ({"type": "agentMessage", "phase": "commentary", "text": "等待"},
                     {"type": "mcpToolCall", "tool": "wait_node"}):
            self.assertIsNone(monitor_event(event(item, source="supervisor")))
        self.assertIsNone(monitor_event(event({"type": "agentMessage", "phase": "final_answer", "text": "结果"})))
        self.assertIsNone(monitor_event(event({"type": "unknown"})))
        self.assertIn("已省略", public_text("长" * 9000))

    def test_pagination_filtered_empty_pages_reopen_and_bot_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "monitor.db")
            store = WorkflowStore(path)
            store.create_workflow({"workflowId": "one", "nodes": [{"id": "a", "prompt": "测试"}]})
            for source, item in (("supervisor", {"type": "agentMessage", "text": "等待"}),
                                 ("worker", {"type": "reasoning", "summary": ["公开摘要"]})):
                raw = event(item, source=source)
                store.add_event("one", node_id="a", source=source,
                                event_type=raw["type"], payload=raw["payload"])
            store = WorkflowStore(path)
            first = store.event_page("one", view="monitor_details", limit=1)
            empty = store.event_page("one", view="monitor_details", limit=1, after=first["nextCursor"])
            self.assertEqual(empty["events"], [])
            self.assertTrue(empty["hasMore"])
            last = store.event_page("one", view="monitor_details", limit=1, after=empty["nextCursor"])
            self.assertEqual(last["events"][0]["payload"]["text"], "公开摘要")
            tail = store.event_page("one", view="monitor_details", limit=1, tail=True)
            older = store.event_page("one", view="monitor_details", limit=1, before=tail["oldestCursor"])
            self.assertTrue(older["hasOlder"])
            self.assertEqual(older["events"], [])
            self.assertEqual(len(store.event_page("one", view="monitor")["events"]), 1)
            self.assertEqual(len(store.event_page("one", view="bot")["events"]), 2)
