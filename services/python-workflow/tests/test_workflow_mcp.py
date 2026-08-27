import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_orchestrator_mcp as service
from tests.mock_app_server import MockAppServer
from workflow_store import WorkflowStore


class WorkflowMcpTests(unittest.IsolatedAsyncioTestCase):
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
                store = WorkflowStore(Path(directory, "workflows.db"))
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
                orchestrator = service.Orchestrator(config)
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

                snapshot = store.get_workflow("mcp-demo")
                self.assertEqual(snapshot["nodes"][0]["status"], "completed")
                self.assertEqual(snapshot["nodes"][0]["threadId"], "thread-1")
                event_types = [event["type"] for event in store.list_events("mcp-demo")]
                self.assertIn("appserver.item/agentMessage/delta", event_types)
                self.assertIn("appserver.turn/completed", event_types)

    async def test_file_contract_requires_output_only_for_write_steps(self) -> None:
        async with MockAppServer(delay_sec=0.01) as server:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory, "agents.json")
                config.write_text(
                    json.dumps({
                        "agents": {
                            "local": {
                                "url": server.url,
                                "cwd": "/srv/work",
                                "artifact_root": str(Path(directory, "artifacts")),
                                "allow_write": True,
                            }
                        }
                    }),
                    encoding="utf-8",
                )
                store = WorkflowStore(Path(directory, "workflows.db"))
                for workflow_id, write in (("write-demo", True), ("read-demo", False)):
                    store.create_workflow({
                        "workflowId": workflow_id,
                        "handoffMode": "cumulative_files",
                        "supervisorAgentId": "local",
                        "nodes": [{
                            "id": "a",
                            "prompt": "完成步骤",
                            "write": write,
                            "timeoutSec": 10,
                        }],
                    })
                orchestrator = service.Orchestrator(config)
                with (
                    patch.object(service, "orchestrator", orchestrator),
                    patch.object(service, "_workflow_store", store),
                ):
                    await service.dispatch_node("write-demo", "a")
                    write_result = await service.wait_node(
                        "write-demo", "a", timeout_sec=1
                    )
                    self.assertEqual(write_result["status"], "failed")
                    self.assertIn("恰好一个文件", write_result["error"])

                    await service.dispatch_node("read-demo", "a")
                    read_result = await service.wait_node(
                        "read-demo", "a", timeout_sec=1
                    )
                    self.assertEqual(read_result["status"], "completed")
                    await asyncio.gather(*tuple(service._workflow_monitors))


if __name__ == "__main__":
    unittest.main()
