import asyncio
import base64
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from codex_orchestrator_mcp import Orchestrator
from starlette.testclient import TestClient
from tests.mock_app_server import MockAppServer
from workflow_gateway import WorkflowGateway, create_app
from workflow_store import WorkflowStore


class SupervisorPromptTests(unittest.TestCase):
    def test_visible_updates_are_written_for_nontechnical_users(self) -> None:
        prompt = WorkflowGateway._supervisor_prompt(
            {
                "workflowId": "demo",
                "failurePolicy": "stop",
                "nodes": [
                    {
                        "id": "node-a",
                        "agentId": "local",
                        "executorType": "local",
                        "dependsOn": [],
                        "prompt": "只输出 a",
                    }
                ],
            }
        )

        self.assertIn("完全不懂技术的普通用户", prompt)
        self.assertIn("对外把 node 称为“步骤”", prompt)
        self.assertIn("现在开始第1步", prompt)
        self.assertIn("不要直接抛出内部错误信息", prompt)
        self.assertIn("任务已全部完成", prompt)
        self.assertIn("timeout_sec 使用 10 秒", prompt)
        self.assertIn("节点派发后必须调用 wait_node", prompt)

    def test_chat_prompt_only_classifies_against_latest_snapshot(self) -> None:
        prompt = WorkflowGateway._chat_prompt(
            {
                "workflowId": "demo", "status": "running", "stateVersion": 3,
                "pendingControl": None,
                "nodes": [{"id": "a", "displayName": "收集", "status": "running"}],
            },
            {
                "messageId": "m1", "text": "跳过第1步",
                "workflowStatusAtAcceptance": "running", "stateVersionAtAcceptance": 2,
            },
        )
        self.assertIn("独立的任务助手", prompt)
        self.assertIn("不调用任何工具", prompt)
        self.assertIn("propose_control/restart_from", prompt)
        self.assertIn('"number": 1', prompt)

    def test_public_agents_excludes_connection_and_authentication_details(self) -> None:
        class FakeOrchestrator:
            def list_agents(self):
                return [{
                    "agent_id": "local", "url": "ws://secret", "cwd": "/work",
                    "authenticated": True, "token_env": "SECRET_TOKEN",
                    "allow_write": True, "allow_cwd_override": False, "model": "gpt-5.6-sol",
                }]

        gateway = WorkflowGateway(None, FakeOrchestrator())
        value = gateway.public_agents()[0]
        self.assertEqual(value["agentId"], "local")
        self.assertNotIn("url", value)
        self.assertNotIn("authenticated", value)
        self.assertNotIn("token_env", value)


class WorkflowArtifactHttpTests(unittest.TestCase):
    def test_artifact_endpoint_returns_only_workflow_owned_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory, "agents.json")
            config.write_text(
                json.dumps({"agents": {"local": {"url": "ws://127.0.0.1:1", "cwd": "/work"}}}),
                encoding="utf-8",
            )
            app = create_app(
                db_path=Path(directory, "workflows.db"), config_path=config
            )
            store = app.state.gateway.store
            store.create_workflow(
                {
                    "workflowId": "artifact-demo",
                    "supervisorAgentId": "local",
                    "nodes": [{"id": "a", "prompt": "demo", "timeoutSec": 10}],
                }
            )
            png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            artifact = store.save_image_bytes(
                "artifact-demo", "a", "image-item", png
            )

            with TestClient(app) as client:
                response = client.get(
                    f"/workflows/artifact-demo/artifacts/{artifact['id']}"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.content, png)
                missing = client.get(
                    f"/workflows/another-workflow/artifacts/{artifact['id']}"
                )
                self.assertEqual(missing.status_code, 404)


class WorkflowChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_reply_uses_separate_structured_turn(self) -> None:
        async with MockAppServer(delay_sec=2) as server:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory, "agents.json")
                config.write_text(json.dumps({"agents": {"local": {
                    "url": server.url, "cwd": "/work", "allow_cwd_override": True,
                }}}), encoding="utf-8")
                store = WorkflowStore(Path(directory, "workflows.db"))
                gateway = WorkflowGateway(store, Orchestrator(config))
                await gateway.submit({
                    "workflowId": "commentary-demo", "supervisorAgentId": "local",
                    "nodes": [{"id": "a", "prompt": "demo", "timeoutSec": 10}],
                })
                for _ in range(200):
                    snapshot = store.get_workflow("commentary-demo")
                    job_id = snapshot["supervisor"]["jobId"]
                    if job_id and gateway.orchestrator.get_job(job_id).turn_id:
                        break
                    await asyncio.sleep(0.01)

                message_id = str(uuid.uuid4())
                await gateway.accept_message("commentary-demo", message_id, "现在到哪里了？")
                for _ in range(200):
                    if store.pending_chat_count("commentary-demo") == 0:
                        break
                    await asyncio.sleep(0.01)
                completed = [
                    event for event in store.list_events("commentary-demo", limit=1000)
                    if event["type"] == "chat.assistant.completed"
                ]
                self.assertEqual(completed[-1]["payload"]["text"], "mock chat reply")
                self.assertFalse(any(
                    item["method"] == "turn/steer" for item in server.requests
                ))
                assistant_turns = [
                    item for item in server.requests
                    if item["method"] == "turn/start"
                    and "outputSchema" in item["params"]
                ]
                self.assertEqual(len(assistant_turns), 1)
                self.assertEqual(assistant_turns[0]["params"]["approvalPolicy"], "never")
                second_id = str(uuid.uuid4())
                await gateway.accept_message("commentary-demo", second_id, "再说明一次")
                for _ in range(300):
                    if store.pending_chat_count("commentary-demo") == 0:
                        break
                    await asyncio.sleep(0.01)
                assistant_resumes = [
                    item for item in server.requests
                    if item["method"] == "thread/resume"
                    and item["params"].get("approvalPolicy") == "never"
                ]
                self.assertEqual(len(assistant_resumes), 1)

                tasks = list(gateway._tasks.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_assistant_turn_does_not_end_or_resume_supervisor_turn(self) -> None:
        async with MockAppServer(delay_sec=2) as server:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory, "agents.json")
                config.write_text(json.dumps({"agents": {"local": {
                    "url": server.url, "cwd": "/work", "allow_cwd_override": True,
                }}}), encoding="utf-8")
                store = WorkflowStore(Path(directory, "workflows.db"))
                gateway = WorkflowGateway(store, Orchestrator(config))
                try:
                    await gateway.submit({
                        "workflowId": "resume-demo", "supervisorAgentId": "local",
                        "nodes": [{"id": "a", "prompt": "demo", "timeoutSec": 10}],
                    })
                    for _ in range(200):
                        snapshot = store.get_workflow("resume-demo")
                        job_id = snapshot["supervisor"]["jobId"]
                        if job_id and gateway.orchestrator.get_job(job_id).turn_id:
                            break
                        await asyncio.sleep(0.01)

                    await gateway.accept_message(
                        "resume-demo", str(uuid.uuid4()), "为什么等了这么久才开始？"
                    )
                    for _ in range(300):
                        if store.pending_chat_count("resume-demo") == 0:
                            break
                        await asyncio.sleep(0.01)

                    self.assertEqual(store.get_workflow("resume-demo")["status"], "running")
                    self.assertFalse(any(
                        item["method"] == "turn/steer" for item in server.requests
                    ))
                    self.assertEqual(len([
                        item for item in server.requests
                        if item["method"] == "turn/start"
                        and "outputSchema" in item["params"]
                    ]), 1)
                finally:
                    tasks = list(gateway._tasks.values())
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def test_running_message_streams_once_and_duplicate_is_idempotent(self) -> None:
        async with MockAppServer(delay_sec=2) as server:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory, "agents.json")
                config.write_text(json.dumps({"agents": {"local": {
                    "url": server.url, "cwd": "/work", "allow_cwd_override": True,
                }}}), encoding="utf-8")
                store = WorkflowStore(Path(directory, "workflows.db"))
                gateway = WorkflowGateway(store, Orchestrator(config))
                await gateway.submit({
                    "workflowId": "chat-demo", "supervisorAgentId": "local",
                    "nodes": [{"id": "a", "prompt": "demo", "timeoutSec": 10}],
                })
                for _ in range(200):
                    snapshot = store.get_workflow("chat-demo")
                    job_id = snapshot["supervisor"]["jobId"]
                    if job_id and gateway.orchestrator.get_job(job_id).turn_id:
                        break
                    await asyncio.sleep(0.01)

                message_id = str(uuid.uuid4())
                await gateway.accept_message("chat-demo", message_id, "现在到哪里了？")
                for _ in range(200):
                    if store.pending_chat_count("chat-demo") == 0:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(store.pending_chat_count("chat-demo"), 0)
                completed = [event for event in store.list_events("chat-demo", limit=1000)
                             if event["type"] == "chat.assistant.completed"]
                self.assertEqual(completed[-1]["payload"]["messageId"], message_id)

                before_duplicate = len([
                    item for item in server.requests if item["method"] == "turn/start"
                    and "outputSchema" in item["params"]
                ])
                await gateway.accept_message("chat-demo", message_id, "现在到哪里了？")
                await asyncio.sleep(0.05)
                assistant_requests = [
                    item for item in server.requests if item["method"] == "turn/start"
                    and "outputSchema" in item["params"]
                ]
                self.assertEqual(len(assistant_requests), before_duplicate)
                self.assertEqual(before_duplicate, 1)
                completed_after = [event for event in store.list_events("chat-demo", limit=1000)
                                   if event["type"] == "chat.assistant.completed"]
                self.assertEqual(len(completed_after), 1)

                running_tasks = list(gateway._tasks.values())
                for task in running_tasks:
                    task.cancel()
                await asyncio.gather(*running_tasks, return_exceptions=True)


class WorkflowControlIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _confirmed_restart(store: WorkflowStore) -> dict:
        proposed_id = str(uuid.uuid4())
        confirmed_id = str(uuid.uuid4())
        store.accept_chat_message("control-demo", proposed_id, "从第2步重跑")
        proposal = store.propose_control(
            "control-demo", "restart_from", "b", proposed_id
        )
        store.accept_chat_message("control-demo", confirmed_id, "确认执行")
        return store.confirm_control(
            "control-demo", proposal["actionId"], confirmed_id
        )

    @staticmethod
    def _running_store(path: Path) -> WorkflowStore:
        store = WorkflowStore(path)
        store.create_workflow({
            "workflowId": "control-demo",
            "supervisorAgentId": "local",
            "maxRetryCount": 2,
            "nodes": [
                {"id": "a", "prompt": "a", "timeoutSec": 10},
                {"id": "b", "prompt": "b", "dependsOn": ["a"], "timeoutSec": 10},
                {"id": "c", "prompt": "c", "dependsOn": ["b"], "timeoutSec": 10},
            ],
        })
        store.prepare_node_dispatch("control-demo", "a")
        store.sync_node_job(
            "control-demo", "a",
            {"status": "completed", "response": "A", "finished_at": "now"},
        )
        store.prepare_node_dispatch("control-demo", "b")
        store.attach_node_job("control-demo", "b", {
            "job_id": "remote-job", "thread_id": "remote-thread",
            "turn_id": "remote-turn", "status": "running",
        })
        return store

    async def test_cross_connection_interrupt_allows_atomic_tail_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._running_store(Path(directory, "workflows.db"))

            class InterruptingOrchestrator:
                jobs = {}

                async def interrupt_turn(self, **_: object) -> None:
                    store.sync_node_job(
                        "control-demo", "b",
                        {"status": "interrupted", "error": "已安全停止", "finished_at": "now"},
                    )

            gateway = WorkflowGateway(store, InterruptingOrchestrator())
            confirmed = self._confirmed_restart(store)
            answer = await gateway._execute_control("control-demo", confirmed)

            snapshot = store.get_workflow("control-demo")
            self.assertIn("重新打开", answer)
            self.assertEqual(snapshot["retryPolicy"]["usedRetries"], 1)
            self.assertEqual(
                [node["status"] for node in snapshot["nodes"]],
                ["completed", "pending", "pending"],
            )
            with self.assertRaisesRegex(ValueError, "已经处理"):
                await gateway._execute_control("control-demo", confirmed)
            self.assertEqual(
                store.get_workflow("control-demo")["retryPolicy"]["usedRetries"], 1
            )

    async def test_interrupt_failure_does_not_consume_or_partially_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._running_store(Path(directory, "workflows.db"))

            class FailingOrchestrator:
                jobs = {}

                async def interrupt_turn(self, **_: object) -> None:
                    raise RuntimeError("远端拒绝中止")

            gateway = WorkflowGateway(store, FailingOrchestrator())
            gateway._resume_supervisor_if_needed = AsyncMock()
            confirmed = self._confirmed_restart(store)
            with self.assertRaisesRegex(RuntimeError, "远端拒绝中止"):
                await gateway._execute_control("control-demo", confirmed)

            snapshot = store.get_workflow("control-demo")
            self.assertEqual(snapshot["retryPolicy"]["usedRetries"], 0)
            self.assertEqual(snapshot["nodes"][1]["status"], "running")
            with store._connect() as connection:
                action = connection.execute(
                    "SELECT status, retry_ordinal FROM workflow_control_actions "
                    "WHERE action_id = ?", (confirmed["actionId"],)
                ).fetchone()
            self.assertEqual(action["status"], "failed")
            self.assertIsNone(action["retry_ordinal"])


if __name__ == "__main__":
    unittest.main()
