import tempfile
import unittest
import uuid
from pathlib import Path

from workflow_store import WorkflowStore, utc_now


def serial_workflow() -> dict:
    return {
        "workflowId": "serial-demo",
        "name": "a-b-c",
        "supervisorAgentId": "local",
        "nodes": [
            {"id": "a", "prompt": "只写一个 a", "timeoutSec": 10},
            {
                "id": "b",
                "prompt": "只写一个 b",
                "dependsOn": ["a"],
                "timeoutSec": 10,
            },
            {
                "id": "c",
                "prompt": "只写一个 c",
                "dependsOn": ["b"],
                "timeoutSec": 10,
            },
        ],
    }


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = WorkflowStore(Path(self.directory.name, "workflows.db"))

    def test_create_returns_monitorable_snapshot(self) -> None:
        snapshot = self.store.create_workflow(serial_workflow())

        self.assertEqual(snapshot["workflowId"], "serial-demo")
        self.assertEqual(snapshot["status"], "queued")
        self.assertEqual(snapshot["currentNodes"], [])
        self.assertEqual(snapshot["progress"], {"completed": 0, "total": 3})
        self.assertEqual([node["status"] for node in snapshot["nodes"]], ["pending"] * 3)
        events = self.store.list_events("serial-demo")
        self.assertEqual(events[0]["type"], "workflow.created")

    def test_cycle_is_rejected(self) -> None:
        value = serial_workflow()
        value["nodes"][0]["dependsOn"] = ["c"]
        with self.assertRaisesRegex(ValueError, "存在环"):
            WorkflowStore.normalize_spec(value)

    def test_dependency_gate_and_idempotent_dispatch(self) -> None:
        self.store.create_workflow(serial_workflow())
        with self.assertRaisesRegex(ValueError, "依赖尚未完成"):
            self.store.prepare_node_dispatch("serial-demo", "b")

        dispatch = self.store.prepare_node_dispatch("serial-demo", "a")
        self.assertFalse(dispatch["alreadyDispatched"])
        self.assertEqual(dispatch["status"], "queued")
        repeated = self.store.prepare_node_dispatch("serial-demo", "a")
        self.assertTrue(repeated["alreadyDispatched"])

        job = {
            "job_id": "job-a",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "status": "running",
            "response": None,
            "error": None,
        }
        self.store.attach_node_job("serial-demo", "a", job)
        job.update(
            status="completed",
            response="a",
            finished_at="2026-08-20T00:00:00+00:00",
        )
        self.store.sync_node_job("serial-demo", "a", job)

        second = self.store.prepare_node_dispatch("serial-demo", "b")
        self.assertFalse(second["alreadyDispatched"])
        snapshot = self.store.get_workflow("serial-demo")
        self.assertEqual(snapshot["currentNodes"], ["b"])
        self.assertEqual(snapshot["progress"], {"completed": 1, "total": 3})
        event_types = [event["type"] for event in self.store.list_events("serial-demo")]
        self.assertIn("node.completed", event_types)

    def test_event_cursor_only_returns_newer_events(self) -> None:
        self.store.create_workflow(serial_workflow())
        first = self.store.list_events("serial-demo")[-1]["sequence"]
        self.store.add_event(
            "serial-demo",
            node_id=None,
            source="test",
            event_type="custom.progress",
            payload={"message": "正在执行 a"},
        )
        events = self.store.list_events("serial-demo", after=first)
        self.assertEqual([event["type"] for event in events], ["custom.progress"])

    def test_metadata_and_dependency_result_are_added_to_actual_prompt(self) -> None:
        value = serial_workflow()
        value["nodes"][1].update(displayName="复核结果", roleName="质量审查员")
        self.store.create_workflow(value)
        first = self.store.prepare_node_dispatch("serial-demo", "a")
        self.assertEqual(first["prompt"], "只写一个 a")
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {"status": "completed", "response": "前序成果", "finished_at": utc_now()},
        )
        second = self.store.prepare_node_dispatch("serial-demo", "b")
        self.assertIn("【第1步结果】\n前序成果", second["prompt"])
        node = self.store.get_workflow("serial-demo")["nodes"][1]
        self.assertEqual(node["displayName"], "复核结果")
        self.assertEqual(node["roleName"], "质量审查员")

    def test_dependency_result_and_final_prompt_are_truncated(self) -> None:
        self.store.create_workflow(serial_workflow())
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {"status": "completed", "response": "甲" * 25_000, "finished_at": utc_now()},
        )
        prompt = self.store.prepare_node_dispatch("serial-demo", "b")["prompt"]
        self.assertLessEqual(len(prompt), 100_000)
        self.assertIn("内容过长，已在此处省略", prompt)

    def test_chat_message_is_persisted_and_idempotent(self) -> None:
        self.store.create_workflow(serial_workflow())
        message_id = str(uuid.uuid4())
        first = self.store.accept_chat_message("serial-demo", message_id, "  现在到哪了？  ")
        repeated = self.store.accept_chat_message("serial-demo", message_id, "现在到哪了？")
        self.assertEqual(first["messageId"], message_id)
        self.assertEqual(repeated["status"], "accepted")
        self.assertEqual(self.store.pending_chat_count("serial-demo"), 1)
        accepted = [event for event in self.store.list_events("serial-demo")
                    if event["type"] == "chat.user.accepted"]
        self.assertEqual(len(accepted), 1)
        with self.assertRaisesRegex(RuntimeError, "不同内容"):
            self.store.accept_chat_message("serial-demo", message_id, "另一个问题")

    def test_failed_chat_retry_reuses_message_id(self) -> None:
        self.store.create_workflow(serial_workflow())
        message_id = str(uuid.uuid4())
        self.store.accept_chat_message("serial-demo", message_id, "状态？")
        self.store.claim_next_chat_message("serial-demo")
        self.store.fail_chat_message("serial-demo", message_id, "断线")
        retried = self.store.accept_chat_message("serial-demo", message_id, "状态？")
        self.assertEqual(retried["status"], "accepted")
        self.assertEqual(self.store.pending_chat_count("serial-demo"), 1)

    def test_control_requires_separate_exact_confirmation(self) -> None:
        self.store.create_workflow(serial_workflow())
        proposed_message = str(uuid.uuid4())
        confirmation_message = str(uuid.uuid4())
        self.store.accept_chat_message("serial-demo", proposed_message, "跳过第1步")
        action = self.store.propose_control("serial-demo", "skip", "a", proposed_message)
        with self.assertRaisesRegex(ValueError, "单独回复"):
            self.store.confirm_control("serial-demo", action["actionId"], proposed_message)
        self.store.accept_chat_message("serial-demo", confirmation_message, "确认执行")
        confirmed = self.store.confirm_control(
            "serial-demo", action["actionId"], confirmation_message
        )
        self.assertEqual(confirmed["actionType"], "skip")

    def test_skipped_dependency_allows_next_step(self) -> None:
        self.store.create_workflow(serial_workflow())
        self.store.skip_node("serial-demo", "a")
        next_step = self.store.prepare_node_dispatch("serial-demo", "b")
        self.assertIn("已跳过", next_step["prompt"])
        self.assertEqual(self.store.get_workflow("serial-demo")["nodes"][0]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
