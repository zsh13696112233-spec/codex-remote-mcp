import asyncio
import base64
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from workflow_store import AsyncEventBatcher, WorkflowStore, utc_now


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

    def test_compressed_spec_preserves_full_prompt_without_legacy_duplicate(self) -> None:
        value = serial_workflow()
        value["nodes"][0]["prompt"] = "长提示" * 10_000
        self.store.create_workflow(value)

        with self.store._connect() as connection:
            workflow = connection.execute(
                "SELECT spec_json, spec_zlib FROM workflows WHERE workflow_id = ?",
                ("serial-demo",),
            ).fetchone()
            node = connection.execute(
                "SELECT prompt, original_prompt FROM workflow_nodes "
                "WHERE workflow_id = ? AND node_id = ?",
                ("serial-demo", "a"),
            ).fetchone()

        self.assertIsNotNone(workflow["spec_zlib"])
        self.assertNotIn("长提示", workflow["spec_json"])
        self.assertEqual(node["prompt"], "")
        self.assertEqual(node["original_prompt"], value["nodes"][0]["prompt"])
        self.assertEqual(
            self.store.get_spec("serial-demo")["nodes"][0]["prompt"],
            value["nodes"][0]["prompt"],
        )

    def test_old_uncompressed_spec_remains_readable(self) -> None:
        value = serial_workflow()
        self.store.create_workflow(value)
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE workflows SET spec_json = ?, spec_zlib = NULL "
                "WHERE workflow_id = ?",
                (json.dumps(value, ensure_ascii=False), "serial-demo"),
            )
        self.assertEqual(
            self.store.get_spec("serial-demo")["nodes"][0]["prompt"],
            "只写一个 a",
        )

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

    def test_generated_image_is_persisted_and_exposed_as_node_artifact(self) -> None:
        self.store.create_workflow(serial_workflow())
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        first = self.store.save_image_artifact(
            "serial-demo", "a", "image-item-1", base64.b64encode(png).decode("ascii")
        )
        repeated = self.store.save_image_artifact(
            "serial-demo", "a", "image-item-1", base64.b64encode(png).decode("ascii")
        )

        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(first["mediaType"], "image/png")
        node = self.store.get_workflow("serial-demo")["nodes"][0]
        self.assertEqual(node["artifacts"], [first])
        artifact = self.store.get_artifact("serial-demo", first["id"])
        self.assertEqual(artifact["content"], png)
        self.assertEqual(artifact["nodeId"], "a")
        with self.assertRaisesRegex(ValueError, "找不到工作流图片"):
            self.store.get_artifact("another-workflow", first["id"])

    def test_historical_generated_image_link_is_imported_only_from_trusted_root(self) -> None:
        self.store.create_workflow(serial_workflow())
        trusted_root = Path(self.directory.name, "generated_images")
        image_path = trusted_root / "thread-a" / "image.png"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {
                "status": "completed",
                "response": f"完成。\n\n[查看生成图片]({image_path.as_posix()})",
                "finished_at": utc_now(),
            },
        )

        self.assertEqual(self.store.import_legacy_generated_images(trusted_root), 1)
        self.assertEqual(self.store.import_legacy_generated_images(trusted_root), 0)
        artifacts = self.store.get_workflow("serial-demo")["nodes"][0]["artifacts"]
        self.assertEqual(len(artifacts), 1)

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


class AsyncEventBatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_are_flushed_in_one_transaction_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            store.create_workflow(serial_workflow())
            batcher = AsyncEventBatcher(store, batch_size=3, flush_interval=10)
            for index in range(3):
                await batcher.add(
                    "serial-demo",
                    node_id=None,
                    source="test",
                    event_type="test.delta",
                    payload={"index": index},
                )
            await batcher.close()

            events = [
                event for event in store.list_events("serial-demo")
                if event["type"] == "test.delta"
            ]
            self.assertEqual(
                [event["payload"]["index"] for event in events], [0, 1, 2]
            )

    async def test_timer_flushes_without_duplicate_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            store.create_workflow(serial_workflow())
            batcher = AsyncEventBatcher(store, batch_size=64, flush_interval=0.01)
            await batcher.add(
                "serial-demo",
                node_id=None,
                source="test",
                event_type="test.timer",
                payload={"index": 1},
            )
            await asyncio.sleep(0.03)
            await batcher.add(
                "serial-demo",
                node_id=None,
                source="test",
                event_type="test.timer",
                payload={"index": 2},
            )
            await batcher.flush()
            await batcher.close()

            events = [
                event for event in store.list_events("serial-demo")
                if event["type"] == "test.timer"
            ]
            self.assertEqual(
                [event["payload"]["index"] for event in events], [1, 2]
            )


if __name__ == "__main__":
    unittest.main()
