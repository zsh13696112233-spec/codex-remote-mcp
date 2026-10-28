import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock, AsyncMock

import codex_orchestrator_mcp as service
from tests.mock_app_server import MockAppServer
from workflow_store import WorkflowStore


from tests.registry_fixtures import (fixture_orchestrator, fixture_gateway, fixture_app, FixtureWorkflowStore)

class WorkflowMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_finished_during_start_cancels_unattached_job(self):
        from tests.test_workflow_store import serial_workflow
        with tempfile.TemporaryDirectory() as directory:
            store = FixtureWorkflowStore(Path(directory, "workflows.db"))
            store.create_workflow(serial_workflow())
            job = Mock(job_id="late-job")
            job.snapshot.return_value = {"job_id": "late-job", "status": "queued"}
            async def dispatch(**kwargs):
                store.finish_workflow("serial-demo", supervisor_status="failed", response=None, error="429")
                return job
            orchestrator = Mock()
            orchestrator.dispatch = AsyncMock(side_effect=dispatch)
            orchestrator.cancel = AsyncMock()
            with patch.object(service, "_workflow_store", store), patch.object(service, "orchestrator", orchestrator):
                with self.assertRaisesRegex(RuntimeError, "已结束"):
                    await service.dispatch_node("serial-demo", "a")
            orchestrator.cancel.assert_awaited_once_with("late-job")
            self.assertEqual(store.get_node("serial-demo", "a")["status"], "interrupted")

    async def test_dispatch_rechecks_deadline_when_release_loses_to_delivery(self) -> None:
        store = Mock()
        store.pending_advance_for_node.side_effect = [
            {"gateId": "gate", "state": "countdown", "expiresAt": "2000-01-01T00:00:00+00:00"},
            {"gateId": "gate", "state": "held", "expiresAt": "2000-01-01T00:00:00+00:00"},
            None,
        ]
        store.release_timed_out_advance.return_value = False
        store.prepare_node_dispatch.return_value = {"alreadyDispatched": True, "jobId": "job"}
        orchestrator = Mock()
        orchestrator.jobs = {"job": object()}
        orchestrator.get_job.return_value.snapshot.return_value = {"status": "running"}
        with patch.object(service, "_workflow_store", store), patch.object(service, "orchestrator", orchestrator), patch.object(service.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            await service.dispatch_node("workflow", "node")
        self.assertEqual(store.pending_advance_for_node.call_count, 3)
        sleep.assert_awaited_once_with(0.25)
        store.prepare_node_dispatch.assert_called_once()

    async def test_node_tool_dispatches_new_thread_and_persists_events(self) -> None:
        async with MockAppServer(delay_sec=0.01, send_message_delta=True) as server:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory, "agents.json")
                config.write_text(
                    json.dumps(
                        {
                            "agents": {
                                "local": {
                                    "url": server.url,
                                    "cwd": "/srv/work",
                                    "allow_cwd_override": True,
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                store = FixtureWorkflowStore(Path(directory, "workflows.db"))
                store.create_workflow(
                    {
                        "workflowId": "mcp-demo",
                        "supervisorAgentId": "local",
                        "nodes": [
                            {
                                "id": "a",
                                "prompt": "只输出 a",
                                "timeoutSec": 10,
                            },
                            {
                                "id": "b",
                                "prompt": "只输出 b",
                                "dependsOn": ["a"],
                                "timeoutSec": 10,
                            },
                        ],
                    }
                )
                orchestrator = fixture_orchestrator(config)
                with (
                    patch.object(service, "orchestrator", orchestrator),
                    patch.object(service, "_workflow_store", store),
                ):
                    with self.assertRaisesRegex(ValueError, "依赖尚未完成"):
                        await service.dispatch_node("mcp-demo", "b")

                    started = await service.dispatch_node("mcp-demo", "a")
                    self.assertIsNotNone(started["job_id"])
                    final = await service.wait_node("mcp-demo", "a", timeout_sec=1)
                    self.assertEqual(final["status"], "completed")
                    # 等待后台落库完成后才能撤销临时存储替身。
                    await asyncio.gather(*list(service._workflow_monitors))


                snapshot = store.get_workflow("mcp-demo")
                self.assertEqual(snapshot["nodes"][0]["status"], "completed")
                self.assertEqual(snapshot["nodes"][0]["threadId"], "thread-1")
                event_types = [event["type"] for event in store.list_events("mcp-demo")]
                self.assertIn("appserver.item/agentMessage/delta", event_types)
                self.assertIn("appserver.turn/completed", event_types)

    async def test_shared_workspace_needs_no_managed_output(self) -> None:
        async with MockAppServer(delay_sec=0.01) as server:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory, "agents.json")
                config.write_text(
                    json.dumps({
                        "agents": {
                            "local": {
                                "url": server.url,
                                "cwd": directory,
                                "artifact_root": str(Path(directory, "artifacts")),
                                "allow_write": True,
                            }
                        }
                    }),
                    encoding="utf-8",
                )
                store = FixtureWorkflowStore(Path(directory, "workflows.db"))
                for workflow_id, write in (("write-demo", True), ("read-demo", False)):
                    nodes = [
                        {
                            "id": "a",
                            "prompt": "完成步骤",
                            "write": write,
                            "timeoutSec": 10,
                        }
                    ]
                    if workflow_id == "write-demo":
                        nodes.append(
                            {
                                "id": "b",
                                "prompt": "检查前一步需要的业务文件",
                                "dependsOn": ["a"],
                                "timeoutSec": 10,
                            }
                        )
                    store.create_workflow(
                        {
                            "workflowId": workflow_id,
                            "supervisorAgentId": "local",
                            "nodes": nodes,
                        }
                    )
                orchestrator = fixture_orchestrator(config)
                with (
                    patch.object(service, "orchestrator", orchestrator),
                    patch.object(service, "_workflow_store", store),
                ):
                    write_started = await service.dispatch_node("write-demo", "a")
                    write_result = await service.wait_node(
                        "write-demo", "a", timeout_sec=1
                    )
                    self.assertEqual(write_result["status"], "completed")
                    write_job = orchestrator.get_job(write_started["job_id"])
                    self.assertNotIn("系统托管文件清单", write_job.prompt)
                    Path(directory, "plan.txt").write_text("shared plan", encoding="utf-8")
                    self.assertNotIn("必须将恰好一个交付文件写入", write_job.prompt)
                    next_started = await service.dispatch_node("write-demo", "b")
                    next_result = await service.wait_node(
                        "write-demo", "b", timeout_sec=1
                    )
                    self.assertEqual(next_result["status"], "completed")
                    next_job = orchestrator.get_job(next_started["job_id"])
                    self.assertEqual(write_job.cwd, directory)
                    self.assertEqual(next_job.cwd, directory)
                    self.assertEqual(Path(next_job.cwd, "plan.txt").read_text(encoding="utf-8"), "shared plan")
                    self.assertIn("第1步结果", next_job.prompt)
                    self.assertFalse(Path(directory, "artifacts").exists())

                    store.finish_workflow("write-demo", supervisor_status="completed", response="完成", error=None)
                    await service.dispatch_node("read-demo", "a")
                    read_result = await service.wait_node(
                        "read-demo", "a", timeout_sec=1
                    )
                    self.assertEqual(read_result["status"], "completed")
                    await asyncio.gather(*tuple(service._workflow_monitors))


if __name__ == "__main__":
    unittest.main()
