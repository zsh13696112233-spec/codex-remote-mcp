import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from workflow_store import (
    SINGLE_OUTPUT_CONSTRAINT,
    AsyncEventBatcher,
    WorkflowStore,
    utc_now,
)


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
        self.assertEqual(snapshot["advanceMode"], "automatic")
        self.assertEqual(snapshot["handoffMode"], "legacy_text")
        self.assertIsNone(snapshot["pendingAdvance"])
        self.assertEqual(snapshot["currentNodes"], [])
        self.assertEqual(snapshot["progress"], {"completed": 0, "total": 3})
        self.assertEqual([node["status"] for node in snapshot["nodes"]], ["pending"] * 3)
        self.assertEqual(snapshot["retryPolicy"], {
            "maxRetries": 10, "usedRetries": 0, "remainingRetries": 10,
        })
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

    def test_legacy_database_is_upgraded_with_default_retry_policy(self) -> None:
        legacy_path = Path(self.directory.name, "legacy.db")
        spec = serial_workflow()
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE workflows (
                    workflow_id TEXT PRIMARY KEY, name TEXT, status TEXT NOT NULL,
                    failure_policy TEXT NOT NULL, supervisor_agent_id TEXT NOT NULL,
                    supervisor_job_id TEXT, supervisor_thread_id TEXT,
                    supervisor_turn_id TEXT, supervisor_status TEXT NOT NULL,
                    supervisor_last_message TEXT, response TEXT, error TEXT,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    spec_json TEXT NOT NULL
                );
                CREATE TABLE workflow_nodes (
                    workflow_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    position INTEGER NOT NULL, agent_id TEXT NOT NULL,
                    executor_type TEXT NOT NULL, prompt TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL, cwd TEXT,
                    write_enabled INTEGER NOT NULL, model TEXT,
                    timeout_sec INTEGER NOT NULL, status TEXT NOT NULL,
                    job_id TEXT, thread_id TEXT, turn_id TEXT, response TEXT,
                    error TEXT, created_at TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, PRIMARY KEY (workflow_id, node_id),
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
                );
                """
            )
            connection.execute(
                "INSERT INTO workflows (workflow_id, name, status, failure_policy, "
                "supervisor_agent_id, supervisor_status, created_at, spec_json) "
                "VALUES (?, ?, 'queued', 'stop', 'local', 'queued', ?, ?)",
                ("legacy", "旧任务", utc_now(), json.dumps(spec, ensure_ascii=False)),
            )
            connection.execute(
                "INSERT INTO workflow_nodes (workflow_id, node_id, position, agent_id, "
                "executor_type, prompt, depends_on_json, write_enabled, timeout_sec, "
                "status, created_at) VALUES "
                "('legacy', 'a', 0, 'local', 'local', '旧提示', '[]', 1, 10, "
                "'pending', ?)",
                (utc_now(),),
            )
            connection.commit()

        upgraded = WorkflowStore(legacy_path).get_workflow("legacy")

        self.assertEqual(upgraded["retryPolicy"], {
            "maxRetries": 10, "usedRetries": 0, "remainingRetries": 10,
        })
        self.assertEqual(upgraded["assistant"]["status"], "idle")
        with WorkflowStore(legacy_path)._connect() as connection:
            control_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(workflow_control_actions)"
                ).fetchall()
            }
            revision_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'workflow_node_revision_instructions'"
            ).fetchone()
        self.assertIn("revision_instruction", control_columns)
        self.assertIsNotNone(revision_table)
        dispatch = WorkflowStore(legacy_path).prepare_node_dispatch("legacy", "a")
        self.assertEqual(dispatch["permissionProfile"], "workspace_write")
        self.assertTrue(dispatch["write"])

    def test_permission_profile_defaults_and_legacy_write_are_compatible(self) -> None:
        default = WorkflowStore.normalize_spec(serial_workflow())
        self.assertEqual(default["nodes"][0]["permissionProfile"], "read_only")
        self.assertFalse(default["nodes"][0]["write"])

        legacy = serial_workflow()
        legacy["nodes"][0]["write"] = True
        normalized = WorkflowStore.normalize_spec(legacy)
        self.assertEqual(
            normalized["nodes"][0]["permissionProfile"], "workspace_write"
        )
        self.assertTrue(normalized["nodes"][0]["write"])

    def test_permission_profile_rejects_unknown_and_conflicting_legacy_field(self) -> None:
        unknown = serial_workflow()
        unknown["nodes"][0]["permissionProfile"] = "danger_full_access"
        with self.assertRaisesRegex(ValueError, "permissionProfile"):
            WorkflowStore.normalize_spec(unknown)

        conflicting = serial_workflow()
        conflicting["nodes"][0].update(
            permissionProfile="auto_review", write=False
        )
        with self.assertRaisesRegex(ValueError, "矛盾"):
            WorkflowStore.normalize_spec(conflicting)

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

    def test_semi_automatic_wait_can_be_confirmed_idempotently(self) -> None:
        value = serial_workflow()
        value["advanceMode"] = "semi_automatic"
        self.store.create_workflow(value)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {"status": "completed", "response": "A", "finished_at": utc_now()},
        )

        pending = self.store.get_workflow("serial-demo")["pendingAdvance"]
        self.assertEqual(pending["completedNodeId"], "a")
        self.assertEqual(pending["nextNodeId"], "b")
        self.assertEqual(pending["state"], "countdown")
        self.assertIsNone(pending["heldAt"])
        with self.assertRaisesRegex(RuntimeError, "等待用户确认"):
            self.store.prepare_node_dispatch("serial-demo", "b")

        first = self.store.confirm_advance("serial-demo", pending["gateId"])
        repeated = self.store.confirm_advance("serial-demo", pending["gateId"])
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(repeated, first)
        self.assertIsNone(self.store.get_workflow("serial-demo")["pendingAdvance"])

    def test_semi_automatic_wait_can_be_held_until_manually_resumed(self) -> None:
        value = serial_workflow()
        value["advanceMode"] = "semi_automatic"
        self.store.create_workflow(value)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo", "a", {"status": "completed", "finished_at": utc_now()}
        )
        gate_id = self.store.get_workflow("serial-demo")["pendingAdvance"]["gateId"]

        held = self.store.hold_advance("serial-demo", gate_id)
        repeated = self.store.hold_advance("serial-demo", gate_id)
        self.assertEqual(held, repeated)
        reloaded = WorkflowStore(self.store.path)
        pending = reloaded.get_workflow("serial-demo")["pendingAdvance"]
        self.assertEqual(pending["state"], "held")
        self.assertEqual(pending["heldAt"], held["heldAt"])
        with reloaded._connect() as connection:
            connection.execute(
                "UPDATE workflow_advance_gates SET expires_at = ? WHERE gate_id = ?",
                ("2000-01-01T00:00:00+00:00", gate_id),
            )
        self.assertFalse(reloaded.release_timed_out_advance("serial-demo", gate_id))
        with self.assertRaisesRegex(RuntimeError, "已暂停"):
            reloaded.prepare_node_dispatch("serial-demo", "b")

        reloaded.confirm_advance("serial-demo", gate_id)
        self.assertIsNone(reloaded.get_workflow("serial-demo")["pendingAdvance"])
        self.assertFalse(
            reloaded.prepare_node_dispatch("serial-demo", "b")["alreadyDispatched"]
        )
        event_types = [event["type"] for event in reloaded.list_events("serial-demo")]
        self.assertIn("step.advance.held", event_types)
        self.assertIn("step.advance.resumed", event_types)

    def test_semi_automatic_wait_times_out_and_last_step_has_no_gate(self) -> None:
        value = serial_workflow()
        value["advanceMode"] = "semi_automatic"
        self.store.create_workflow(value)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo", "a", {"status": "completed", "finished_at": utc_now()}
        )
        gate_id = self.store.get_workflow("serial-demo")["pendingAdvance"]["gateId"]
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE workflow_advance_gates SET expires_at = ? WHERE gate_id = ?",
                ("2000-01-01T00:00:00+00:00", gate_id),
            )
        self.assertTrue(self.store.release_timed_out_advance("serial-demo", gate_id))
        self.assertFalse(
            self.store.prepare_node_dispatch("serial-demo", "b")["alreadyDispatched"]
        )

        self.store.sync_node_job(
            "serial-demo", "b", {"status": "completed", "finished_at": utc_now()}
        )
        second = self.store.get_workflow("serial-demo")["pendingAdvance"]
        self.store.confirm_advance("serial-demo", second["gateId"])
        self.store.prepare_node_dispatch("serial-demo", "c")
        self.store.sync_node_job(
            "serial-demo", "c", {"status": "completed", "finished_at": utc_now()}
        )
        self.assertIsNone(self.store.get_workflow("serial-demo")["pendingAdvance"])

    def test_semi_automatic_rejects_non_serial_dependency_shape(self) -> None:
        value = serial_workflow()
        value["advanceMode"] = "semi_automatic"
        value["nodes"][2]["dependsOn"] = ["a", "b"]
        with self.assertRaisesRegex(ValueError, "严格串行"):
            WorkflowStore.normalize_spec(value)

    def test_semi_automatic_restart_and_stop_supersede_old_waits(self) -> None:
        value = serial_workflow()
        value["advanceMode"] = "semi_automatic"
        self.store.create_workflow(value)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo", "a", {"status": "completed", "finished_at": utc_now()}
        )
        first_gate = self.store.get_workflow("serial-demo")["pendingAdvance"]["gateId"]
        self.store.hold_advance("serial-demo", first_gate)

        restarted = self.store.restart_from_node("serial-demo", "b")
        self.assertIsNone(restarted["pendingAdvance"])
        with self.assertRaisesRegex(RuntimeError, "失效"):
            self.store.confirm_advance("serial-demo", first_gate)

        self.store.prepare_node_dispatch("serial-demo", "b")
        self.store.sync_node_job(
            "serial-demo", "b", {"status": "completed", "finished_at": utc_now()}
        )
        second_gate = self.store.get_workflow("serial-demo")["pendingAdvance"]["gateId"]
        self.store.hold_advance("serial-demo", second_gate)
        stopped = self.store.stop_workflow("serial-demo")
        self.assertIsNone(stopped["pendingAdvance"])
        with self.assertRaisesRegex(RuntimeError, "失效"):
            self.store.confirm_advance("serial-demo", second_gate)

    def test_cancelling_held_workflow_supersedes_the_wait(self) -> None:
        value = serial_workflow()
        value["advanceMode"] = "semi_automatic"
        self.store.create_workflow(value)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo", "a", {"status": "completed", "finished_at": utc_now()}
        )
        gate_id = self.store.get_workflow("serial-demo")["pendingAdvance"]["gateId"]
        self.store.hold_advance("serial-demo", gate_id)

        self.store.finish_workflow(
            "serial-demo",
            supervisor_status="cancelled",
            response=None,
            error="用户已取消任务。",
        )

        snapshot = self.store.get_workflow("serial-demo")
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertIsNone(snapshot["pendingAdvance"])
        with self.assertRaisesRegex(RuntimeError, "失效"):
            self.store.confirm_advance("serial-demo", gate_id)

    def test_semi_automatic_skip_and_failure_do_not_create_waits(self) -> None:
        skipped = serial_workflow()
        skipped["advanceMode"] = "semi_automatic"
        self.store.create_workflow(skipped)
        self.store.skip_node("serial-demo", "a")
        self.assertIsNone(self.store.get_workflow("serial-demo")["pendingAdvance"])
        self.assertFalse(
            self.store.prepare_node_dispatch("serial-demo", "b")["alreadyDispatched"]
        )

        failed = serial_workflow()
        failed["workflowId"] = "failed-demo"
        failed["advanceMode"] = "semi_automatic"
        self.store.create_workflow(failed)
        self.store.prepare_node_dispatch("failed-demo", "a")
        self.store.sync_node_job(
            "failed-demo",
            "a",
            {"status": "failed", "error": "测试失败", "finished_at": utc_now()},
        )
        self.assertIsNone(self.store.get_workflow("failed-demo")["pendingAdvance"])

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
        self.assertTrue(first["prompt"].startswith("只写一个 a"))
        self.assertTrue(first["prompt"].endswith(SINGLE_OUTPUT_CONSTRAINT))
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {"status": "completed", "response": "前序成果", "finished_at": utc_now()},
        )
        second = self.store.prepare_node_dispatch("serial-demo", "b")
        self.assertIn("【第1步结果】\n前序成果", second["prompt"])
        self.assertTrue(second["prompt"].endswith(SINGLE_OUTPUT_CONSTRAINT))
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
        self.assertTrue(prompt.endswith(SINGLE_OUTPUT_CONSTRAINT))

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

    def test_completed_workflow_still_accepts_chat(self) -> None:
        self.store.create_workflow(serial_workflow())
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE workflows SET status = 'completed' WHERE workflow_id = ?",
                ("serial-demo",),
            )
        accepted = self.store.accept_chat_message(
            "serial-demo", str(uuid.uuid4()), "请总结一下"
        )
        self.assertEqual(accepted["workflowStatusAtAcceptance"], "completed")

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
        snapshot = self.store.get_workflow("serial-demo")
        self.assertEqual(snapshot["nodes"][0]["status"], "skipped")
        self.assertEqual(snapshot["retryPolicy"]["usedRetries"], 0)
        stopped = self.store.stop_workflow("serial-demo")
        self.assertEqual(stopped["retryPolicy"]["usedRetries"], 0)

    def test_restart_from_archives_tail_and_consumes_one_shared_retry(self) -> None:
        value = serial_workflow()
        value["maxRetryCount"] = 2
        self.store.create_workflow(value)
        for node_id, response in (("a", "结果A"), ("b", "结果B"), ("c", "结果C")):
            self.store.prepare_node_dispatch("serial-demo", node_id)
            self.store.sync_node_job(
                "serial-demo",
                node_id,
                {"status": "completed", "response": response, "finished_at": utc_now()},
            )
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        old_artifact = self.store.save_image_bytes(
            "serial-demo", "b", "same-source", png
        )
        self.store.finish_workflow(
            "serial-demo", supervisor_status="completed", response="完成", error=None
        )

        restarted = self.store.restart_from_node("serial-demo", "b")

        self.assertEqual(restarted["status"], "running")
        self.assertEqual(restarted["retryPolicy"], {
            "maxRetries": 2, "usedRetries": 1, "remainingRetries": 1,
        })
        self.assertEqual(restarted["nodes"][0]["response"], "结果A")
        self.assertEqual(
            [node["status"] for node in restarted["nodes"]],
            ["completed", "pending", "pending"],
        )
        self.assertEqual(
            [node["attemptCount"] for node in restarted["nodes"]], [0, 1, 1]
        )
        self.assertEqual(restarted["nodes"][1]["artifacts"], [])
        with self.assertRaisesRegex(ValueError, "找不到工作流图片"):
            self.store.get_artifact("serial-demo", old_artifact["id"])
        new_artifact = self.store.save_image_bytes(
            "serial-demo", "b", "same-source", png
        )
        self.assertNotEqual(new_artifact["id"], old_artifact["id"])
        prompt = self.store.prepare_node_dispatch("serial-demo", "b")["prompt"]
        self.assertIn("结果A", prompt)
        with self.store._connect() as connection:
            archived_nodes = connection.execute(
                "SELECT node_id, attempt_number FROM workflow_node_attempts "
                "WHERE workflow_id = ? ORDER BY node_id",
                ("serial-demo",),
            ).fetchall()
            archived_images = connection.execute(
                "SELECT node_id, attempt_number FROM workflow_attempt_artifacts "
                "WHERE workflow_id = ?",
                ("serial-demo",),
            ).fetchall()
        self.assertEqual(
            [(row["node_id"], row["attempt_number"]) for row in archived_nodes],
            [("b", 0), ("c", 0)],
        )
        self.assertEqual(
            [(row["node_id"], row["attempt_number"]) for row in archived_images],
            [("b", 0)],
        )

    def test_confirmed_revision_instruction_is_audited_before_hidden_constraint(self) -> None:
        value = serial_workflow()
        value["maxRetryCount"] = 3
        self.store.create_workflow(value)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {"status": "completed", "response": "结果A", "finished_at": utc_now()},
        )
        first_prompt = self.store.prepare_node_dispatch("serial-demo", "b")["prompt"]
        self.store.sync_node_job(
            "serial-demo",
            "b",
            {"status": "completed", "response": "结果B", "finished_at": utc_now()},
        )

        proposed_message = str(uuid.uuid4())
        confirmed_message = str(uuid.uuid4())
        instruction = "重新生成空客 A380，并增加清晰、完整的机身涂装和标识。"
        self.store.accept_chat_message(
            "serial-demo", proposed_message, "没有 logo、没有涂装，重新生成"
        )
        proposal = self.store.propose_control(
            "serial-demo",
            "restart_from",
            "b",
            proposed_message,
            instruction,
        )
        self.store.accept_chat_message("serial-demo", confirmed_message, "确认执行")
        confirmed = self.store.confirm_control(
            "serial-demo", proposal["actionId"], confirmed_message
        )
        action = self.store.start_control_execution(confirmed["actionId"])
        self.store.restart_from_node(
            "serial-demo",
            "b",
            action_id=action["actionId"],
            revision_instruction=action["revisionInstruction"],
            source_message_id=action["proposedByMessageId"],
        )
        self.store.finish_control_execution(action["actionId"], result={})

        prompt = self.store.prepare_node_dispatch("serial-demo", "b")["prompt"]
        self.assertIn("结果A", prompt)
        self.assertIn("【本次及历史返工要求】", prompt)
        self.assertTrue(prompt.endswith(SINGLE_OUTPUT_CONSTRAINT))
        self.assertLess(prompt.index("结果A"), prompt.index("【本次及历史返工要求】"))
        self.assertLess(prompt.index(instruction), prompt.index("【系统单次产物约束】"))
        with self.store._connect() as connection:
            node = connection.execute(
                "SELECT original_prompt FROM workflow_nodes "
                "WHERE workflow_id = ? AND node_id = ?",
                ("serial-demo", "b"),
            ).fetchone()
            archived = connection.execute(
                "SELECT actual_prompt FROM workflow_node_attempts "
                "WHERE workflow_id = ? AND node_id = ? AND attempt_number = 0",
                ("serial-demo", "b"),
            ).fetchone()
            revision = connection.execute(
                "SELECT action_id, source_message_id, instruction "
                "FROM workflow_node_revision_instructions WHERE workflow_id = ?",
                ("serial-demo",),
            ).fetchone()
            stored_action = connection.execute(
                "SELECT revision_instruction FROM workflow_control_actions "
                "WHERE action_id = ?",
                (action["actionId"],),
            ).fetchone()
        self.assertEqual(node["original_prompt"], "只写一个 b")
        self.assertEqual(archived["actual_prompt"], first_prompt)
        self.assertEqual(revision["action_id"], action["actionId"])
        self.assertEqual(revision["source_message_id"], proposed_message)
        self.assertEqual(revision["instruction"], instruction)
        self.assertEqual(stored_action["revision_instruction"], instruction)

    def test_revision_history_keeps_latest_and_drops_oldest_when_too_long(self) -> None:
        value = serial_workflow()
        value["maxRetryCount"] = 10
        self.store.create_workflow(value)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {"status": "completed", "response": "A", "finished_at": utc_now()},
        )
        for index in range(1, 7):
            self.store.restart_from_node(
                "serial-demo",
                "b",
                revision_instruction=f"要求{index}-" + str(index) * 3_980,
            )

        prompt = self.store.prepare_node_dispatch("serial-demo", "b")["prompt"]
        self.assertLessEqual(len(prompt), 100_000)
        self.assertIn("【较早返工要求因内容过长已省略】", prompt)
        self.assertNotIn("要求1-", prompt)
        self.assertIn("要求6-", prompt)
        self.assertLess(prompt.index("要求5-"), prompt.index("要求6-"))

    def test_restart_without_revision_instruction_keeps_existing_prompt_behavior(self) -> None:
        self.store.create_workflow(serial_workflow())
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {"status": "completed", "response": "A", "finished_at": utc_now()},
        )
        self.store.restart_from_node("serial-demo", "b")

        prompt = self.store.prepare_node_dispatch("serial-demo", "b")["prompt"]
        self.assertNotIn("本次及历史返工要求", prompt)
        self.assertTrue(prompt.endswith(SINGLE_OUTPUT_CONSTRAINT))

    def test_retry_budget_rejects_next_restart_and_proposals_do_not_consume(self) -> None:
        value = serial_workflow()
        value["maxRetryCount"] = 10
        self.store.create_workflow(value)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo", "a",
            {"status": "completed", "response": "A", "finished_at": utc_now()},
        )
        proposal_message = str(uuid.uuid4())
        self.store.accept_chat_message("serial-demo", proposal_message, "从第2步重跑")
        proposal = self.store.propose_control(
            "serial-demo", "restart_from", "b", proposal_message
        )
        self.assertEqual(proposal["retryCost"], 1)
        self.assertEqual(
            self.store.get_workflow("serial-demo")["retryPolicy"]["usedRetries"], 0
        )
        for _ in range(10):
            self.store.restart_from_node("serial-demo", "b")
        self.assertEqual(
            self.store.get_workflow("serial-demo")["retryPolicy"]["usedRetries"], 10
        )
        with self.assertRaisesRegex(ValueError, "次数已经用完"):
            self.store.restart_from_node("serial-demo", "b")
        with self.assertRaisesRegex(ValueError, "次数已经用完"):
            self.store.propose_control(
                "serial-demo", "restart_from", "b", str(uuid.uuid4())
            )

    def test_cumulative_files_omits_predecessor_text_and_keeps_revision_local(self) -> None:
        value = serial_workflow()
        value["handoffMode"] = "cumulative_files"
        self.store.create_workflow(value)
        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {"status": "completed", "response": "机密的第一步文字", "finished_at": utc_now()},
        )
        self.store.restart_from_node(
            "serial-demo", "b", revision_instruction="只属于第二步的返工要求"
        )

        second = self.store.prepare_node_dispatch("serial-demo", "b")
        self.assertEqual(second["handoffMode"], "cumulative_files")
        self.assertNotIn("机密的第一步文字", second["prompt"])
        self.assertIn("只属于第二步的返工要求", second["prompt"])
        self.store.sync_node_job(
            "serial-demo",
            "b",
            {"status": "completed", "response": "第二步文字", "finished_at": utc_now()},
        )
        third = self.store.prepare_node_dispatch("serial-demo", "c")
        self.assertNotIn("第二步文字", third["prompt"])
        self.assertNotIn("只属于第二步的返工要求", third["prompt"])

    def test_generic_artifacts_are_cumulative_and_restart_uses_only_current_files(self) -> None:
        value = serial_workflow()
        value["handoffMode"] = "cumulative_files"
        self.store.create_workflow(value)
        first = self.store.save_artifact_bytes(
            "serial-demo", "a", "first", "plane.svg", b"<svg>plane</svg>", "image/svg+xml"
        )
        second = self.store.save_artifact_bytes(
            "serial-demo", "b", "second", "airport.txt", b"airport", "text/plain"
        )
        duplicate = self.store.save_artifact_bytes(
            "serial-demo", "b", "other-source", "copy.txt", b"airport", "text/plain"
        )
        self.assertEqual(second["id"], duplicate["id"])
        inputs = self.store.get_cumulative_artifact_inputs("serial-demo", "c")
        self.assertEqual([step["stepNumber"] for step in inputs], [1, 2])
        self.assertEqual(inputs[0]["artifacts"][0]["id"], first["id"])
        self.assertEqual(inputs[1]["artifacts"][0]["content"], b"airport")

        self.store.prepare_node_dispatch("serial-demo", "a")
        self.store.sync_node_job(
            "serial-demo",
            "a",
            {"status": "completed", "response": "done", "finished_at": utc_now()},
        )
        self.store.restart_from_node("serial-demo", "b")
        restarted = self.store.get_cumulative_artifact_inputs("serial-demo", "c")
        self.assertEqual(len(restarted[0]["artifacts"]), 1)
        self.assertEqual(restarted[1]["artifacts"], [])

    def test_generic_artifact_rejects_oversized_content(self) -> None:
        self.store.create_workflow(serial_workflow())
        with self.assertRaisesRegex(ValueError, "20 MB"):
            self.store.save_artifact_bytes(
                "serial-demo", "a", "large", "large.bin", b"x" * 20_000_001
            )


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
