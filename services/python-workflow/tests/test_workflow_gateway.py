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
from workflow_gateway import WorkflowGateway, build_argument_parser, create_app
from workflow_store import SINGLE_OUTPUT_CONSTRAINT, WorkflowStore, utc_now


class GatewayCommandLineTests(unittest.TestCase):
    def test_default_host_listens_on_all_interfaces(self) -> None:
        args = build_argument_parser().parse_args([])

        self.assertEqual(args.host, "0.0.0.0")


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
        self.assertIn("用户也可以选择暂停", prompt)

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
        self.assertIn("revisionInstruction", prompt)
        self.assertIn("不得虚构品牌、颜色", prompt)
        self.assertIn('"number": 1', prompt)

    def test_assistant_decision_validates_revision_instruction_semantics(self) -> None:
        snapshot = {
            "nodes": [{"id": "a", "displayName": "图片生成", "status": "completed"}]
        }
        decision = WorkflowGateway._validate_assistant_decision(
            {
                "kind": "propose_control",
                "text": "准备返工",
                "actionType": "restart_from",
                "nodeId": "a",
                "revisionInstruction": "增加清晰的机身涂装和标识。",
            },
            snapshot,
        )
        self.assertEqual(decision["revisionInstruction"], "增加清晰的机身涂装和标识。")
        with self.assertRaisesRegex(RuntimeError, "格式无效"):
            WorkflowGateway._validate_assistant_decision(
                {
                    "kind": "answer",
                    "text": "普通回答",
                    "actionType": None,
                    "nodeId": None,
                    "revisionInstruction": "不应出现",
                },
                snapshot,
            )
        with self.assertRaisesRegex(RuntimeError, "格式无效"):
            WorkflowGateway._validate_assistant_decision(
                {
                    "kind": "propose_control",
                    "text": "准备返工",
                    "actionType": "restart_from",
                    "nodeId": "a",
                },
                snapshot,
            )

    def test_restart_confirmation_displays_summarized_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            store.create_workflow(
                {
                    "workflowId": "revision-demo",
                    "supervisorAgentId": "local",
                    "nodes": [{"id": "a", "prompt": "生成飞机", "timeoutSec": 10}],
                }
            )
            message_id = str(uuid.uuid4())
            store.accept_chat_message(
                "revision-demo", message_id, "没有 logo、没有涂装，重新生成"
            )
            gateway = WorkflowGateway(store, object())
            answer = gateway._apply_assistant_decision(
                "revision-demo",
                message_id,
                {
                    "kind": "propose_control",
                    "text": "准备返工",
                    "actionType": "restart_from",
                    "nodeId": "a",
                    "revisionInstruction": "增加清晰、完整的机身涂装和标识。",
                },
            )
            self.assertIn("本次返工要求", answer)
            self.assertIn("增加清晰、完整的机身涂装和标识", answer)

    def test_public_agents_excludes_connection_and_authentication_details(self) -> None:
        class FakeOrchestrator:
            def list_agents(self):
                return [{
                    "agent_id": "local", "url": "ws://secret", "cwd": "/work",
                    "authenticated": True, "token_env": "SECRET_TOKEN",
                    "allow_write": True, "allow_cwd_override": False, "model": "gpt-5.6-sol",
                    "permission_profiles": [
                        "read_only", "workspace_write", "auto_review", "full_access",
                    ],
                }]

        gateway = WorkflowGateway(None, FakeOrchestrator())
        value = gateway.public_agents()[0]
        self.assertEqual(value["agentId"], "local")
        self.assertEqual(value["permissionProfiles"], [
            "read_only", "workspace_write", "auto_review", "full_access",
        ])
        self.assertTrue(value["allowFullAccess"])
        self.assertTrue(value["enabled"])
        self.assertEqual(value["capabilities"], ["supervisor", "executor"])
        self.assertEqual(value["supervisorCapacity"], 1)
        self.assertEqual(value["connectionStatus"], "unknown")
        self.assertEqual(value["availability"], "idle")
        self.assertNotIn("url", value)
        self.assertNotIn("authenticated", value)
        self.assertNotIn("token_env", value)


class SupervisorReachabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_uses_two_failures_and_recovers_online(self) -> None:
        class FakeOrchestrator:
            should_fail = True

            def list_agents(self):
                return [{
                    "agent_id": "supervisor-a",
                    "cwd": "/work",
                    "enabled": True,
                    "capabilities": ["supervisor"],
                    "supervisor_capacity": 1,
                    "permission_profiles": ["read_only"],
                }]

            async def probe_agent(self, agent_id, *, timeout_sec):
                self.last_probe = (agent_id, timeout_sec)
                if self.should_fail:
                    raise ConnectionError("secret address must not be exposed")

        orchestrator = FakeOrchestrator()
        gateway = WorkflowGateway(None, orchestrator)

        await gateway._probe_supervisors_once()
        first = gateway.public_agents()[0]
        self.assertEqual(first["connectionStatus"], "unknown")

        await gateway._probe_supervisors_once()
        second = gateway.public_agents()[0]
        self.assertEqual(second["connectionStatus"], "offline")
        self.assertNotIn("error", second)

        orchestrator.should_fail = False
        await gateway._probe_supervisors_once()
        recovered = gateway.public_agents()[0]
        self.assertEqual(recovered["connectionStatus"], "online")
        self.assertEqual(recovered["checkedAt"], recovered["lastOnlineAt"])

    def test_public_agents_reports_busy_from_persisted_lease(self) -> None:
        class FakeStore:
            def leased_supervisor_ids(self):
                return {"supervisor-a"}

        class FakeOrchestrator:
            def list_agents(self):
                return [{
                    "agent_id": "supervisor-a",
                    "cwd": "/work",
                    "enabled": True,
                    "capabilities": ["supervisor"],
                    "supervisor_capacity": 1,
                    "permission_profiles": ["read_only"],
                }]

        gateway = WorkflowGateway(FakeStore(), FakeOrchestrator())
        self.assertEqual(gateway.public_agents()[0]["availability"], "busy")


class WorkflowSubmissionPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_rejects_profile_above_agent_cap_before_persisting(self) -> None:
        class ReadOnlyOrchestrator:
            def list_agents(self):
                return [
                    {
                        "agent_id": "local",
                        "permission_profiles": ["read_only"],
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            gateway = WorkflowGateway(store, ReadOnlyOrchestrator())
            with self.assertRaisesRegex(PermissionError, "不允许节点"):
                await gateway.submit(
                    {
                        "workflowId": "write-denied",
                        "supervisorAgentId": "local",
                        "nodes": [
                            {
                                "id": "a",
                                "prompt": "修改文件",
                                "write": True,
                                "permissionProfile": "workspace_write",
                                "timeoutSec": 10,
                            }
                        ],
                    }
                )
            with self.assertRaisesRegex(ValueError, "找不到工作流"):
                store.get_workflow("write-denied")

    async def test_submit_rejects_disabled_or_capability_mismatched_agents(self) -> None:
        class CapabilityOrchestrator:
            def list_agents(self):
                return [
                    {
                        "agent_id": "supervisor-ok",
                        "enabled": True,
                        "capabilities": ["supervisor"],
                        "permission_profiles": ["read_only"],
                    },
                    {
                        "agent_id": "supervisor-disabled",
                        "enabled": False,
                        "capabilities": ["supervisor"],
                        "permission_profiles": ["read_only"],
                    },
                    {
                        "agent_id": "executor-ok",
                        "enabled": True,
                        "capabilities": ["executor"],
                        "permission_profiles": ["read_only"],
                    },
                    {
                        "agent_id": "executor-disabled",
                        "enabled": False,
                        "capabilities": ["executor"],
                        "permission_profiles": ["read_only"],
                    },
                ]

        cases = [
            ("unknown", "missing", "executor-ok", ValueError, "未知执行机"),
            (
                "disabled-supervisor",
                "supervisor-disabled",
                "executor-ok",
                PermissionError,
                "已停用",
            ),
            (
                "wrong-supervisor-capability",
                "executor-ok",
                "executor-ok",
                PermissionError,
                "主监督能力",
            ),
            (
                "disabled-executor",
                "supervisor-ok",
                "executor-disabled",
                PermissionError,
                "已停用",
            ),
            (
                "wrong-executor-capability",
                "supervisor-ok",
                "supervisor-ok",
                PermissionError,
                "步骤执行能力",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            gateway = WorkflowGateway(store, CapabilityOrchestrator())
            gateway._schedule_pending = AsyncMock()
            for workflow_id, supervisor_id, executor_id, error_type, message in cases:
                with self.subTest(workflow_id=workflow_id):
                    with self.assertRaisesRegex(error_type, message):
                        await gateway.submit(
                            {
                                "workflowId": workflow_id,
                                "supervisorAgentId": supervisor_id,
                                "nodes": [
                                    {
                                        "id": "a",
                                        "agentId": executor_id,
                                        "prompt": "测试",
                                        "timeoutSec": 10,
                                    }
                                ],
                            }
                        )
                    with self.assertRaisesRegex(ValueError, "找不到工作流"):
                        store.get_workflow(workflow_id)

    async def test_submit_allows_different_executors_and_working_directories(self) -> None:
        class CapabilityOrchestrator:
            def list_agents(self):
                return [
                    {
                        "agent_id": "supervisor-a",
                        "enabled": True,
                        "capabilities": ["supervisor"],
                        "permission_profiles": ["read_only"],
                    },
                    {
                        "agent_id": "executor-a",
                        "enabled": True,
                        "capabilities": ["executor"],
                        "permission_profiles": ["read_only"],
                    },
                    {
                        "agent_id": "executor-b",
                        "enabled": True,
                        "capabilities": ["executor"],
                        "permission_profiles": ["read_only"],
                    },
                ]

        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            gateway = WorkflowGateway(store, CapabilityOrchestrator())
            gateway._schedule_pending = AsyncMock()

            snapshot = await gateway.submit(
                {
                    "workflowId": "mixed-executors",
                    "supervisorAgentId": "supervisor-a",
                    "nodes": [
                        {
                            "id": "a",
                            "agentId": "executor-a",
                            "cwd": "/work/a",
                            "prompt": "步骤 A",
                            "timeoutSec": 10,
                        },
                        {
                            "id": "b",
                            "agentId": "executor-b",
                            "cwd": "",
                            "dependsOn": ["a"],
                            "prompt": "步骤 B",
                            "timeoutSec": 10,
                        },
                    ],
                }
            )

            self.assertEqual(snapshot["status"], "queued")
            spec = store.get_spec("mixed-executors")
            self.assertEqual(
                [node["agentId"] for node in spec["nodes"]],
                ["executor-a", "executor-b"],
            )
            self.assertEqual(spec["nodes"][0]["cwd"], "/work/a")
            self.assertEqual(spec["nodes"][1]["cwd"], "")


class MultiSupervisorSchedulingTests(unittest.IsolatedAsyncioTestCase):
    class FakeJob:
        def __init__(self, job_id: str, agent_id: str) -> None:
            self.job_id = job_id
            self.agent_id = agent_id
            self.thread_id = f"thread-{job_id}"
            self.turn_id = f"turn-{job_id}"
            self.status = "running"
            self.response = None
            self.error = None
            self.completed = asyncio.Event()

        def snapshot(self):
            return {
                "job_id": self.job_id,
                "agent_id": self.agent_id,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "status": self.status,
                "response": self.response,
                "error": self.error,
                "started_at": utc_now(),
                "finished_at": utc_now() if self.completed.is_set() else None,
            }

    class FakeOrchestrator:
        def __init__(self, *, fail_first: bool = False) -> None:
            self.fail_first = fail_first
            self.dispatch_count = 0
            self.jobs = {}
            self.dispatched = []

        def list_agents(self):
            return [
                {
                    "agent_id": "supervisor-a",
                    "enabled": True,
                    "capabilities": ["supervisor"],
                    "permission_profiles": ["read_only"],
                },
                {
                    "agent_id": "supervisor-b",
                    "enabled": True,
                    "capabilities": ["supervisor"],
                    "permission_profiles": ["read_only"],
                },
                {
                    "agent_id": "executor",
                    "enabled": True,
                    "capabilities": ["executor"],
                    "permission_profiles": ["read_only"],
                },
            ]

        async def dispatch(self, *, agent_id, prompt, **_):
            self.dispatch_count += 1
            if self.fail_first and self.dispatch_count == 1:
                raise ConnectionError("主监督连接失败")
            job = MultiSupervisorSchedulingTests.FakeJob(
                f"job-{self.dispatch_count}", agent_id
            )
            self.jobs[job.job_id] = job
            self.dispatched.append((agent_id, prompt, job))
            return job

        def get_job(self, job_id):
            return self.jobs[job_id]

        async def cancel(self, job_id):
            job = self.jobs[job_id]
            job.status = "cancelled"
            job.error = "cancelled"
            job.completed.set()
            return job

    @staticmethod
    def spec(workflow_id: str, supervisor_id: str) -> dict:
        return {
            "workflowId": workflow_id,
            "supervisorAgentId": supervisor_id,
            "nodes": [
                {
                    "id": "a",
                    "agentId": "executor",
                    "prompt": "测试",
                    "timeoutSec": 10,
                }
            ],
        }

    async def test_same_supervisor_is_fifo_and_different_supervisors_run_in_parallel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            orchestrator = self.FakeOrchestrator()
            gateway = WorkflowGateway(store, orchestrator)
            try:
                await gateway.submit(self.spec("first", "supervisor-a"))
                await gateway.submit(self.spec("second", "supervisor-a"))
                await gateway.submit(self.spec("parallel", "supervisor-b"))
                for _ in range(100):
                    if len(orchestrator.dispatched) == 2:
                        break
                    await asyncio.sleep(0.01)

                self.assertEqual(store.get_workflow("first")["status"], "running")
                self.assertEqual(store.get_workflow("second")["status"], "queued")
                self.assertEqual(store.get_workflow("parallel")["status"], "running")
                self.assertEqual(
                    {item[0] for item in orchestrator.dispatched},
                    {"supervisor-a", "supervisor-b"},
                )

                first_job = next(
                    item[2]
                    for item in orchestrator.dispatched
                    if item[0] == "supervisor-a"
                )
                first_job.status = "failed"
                first_job.error = "测试结束"
                first_job.completed.set()
                for _ in range(100):
                    if store.get_workflow("second")["status"] == "running":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(store.get_workflow("second")["status"], "running")
            finally:
                await gateway.stop()
                await gateway.event_batcher.close()

    async def test_connection_failure_releases_slot_for_next_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            orchestrator = self.FakeOrchestrator(fail_first=True)
            gateway = WorkflowGateway(store, orchestrator)
            try:
                await gateway.submit(self.spec("offline", "supervisor-a"))
                await gateway.submit(self.spec("next", "supervisor-a"))
                for _ in range(100):
                    if store.get_workflow("next")["status"] == "running":
                        break
                    await asyncio.sleep(0.01)

                self.assertEqual(store.get_workflow("offline")["status"], "failed")
                self.assertEqual(store.get_workflow("next")["status"], "running")
            finally:
                await gateway.stop()


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
            document = store.save_artifact_bytes(
                "artifact-demo", "a", "document-item", "report.html", b"<html>", "text/html"
            )

            with TestClient(app) as client:
                response = client.get(
                    f"/workflows/artifact-demo/artifacts/{artifact['id']}"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
                self.assertEqual(response.content, png)
                download = client.get(
                    f"/workflows/artifact-demo/artifacts/{document['id']}"
                )
                self.assertEqual(download.headers["content-type"], "text/html; charset=utf-8")
                self.assertTrue(
                    download.headers["content-disposition"].startswith("attachment;")
                )
                self.assertEqual(download.headers["x-content-type-options"], "nosniff")
                missing = client.get(
                    f"/workflows/another-workflow/artifacts/{artifact['id']}"
                )
                self.assertEqual(missing.status_code, 404)

    def test_semi_automatic_advance_can_be_held_and_resumed_over_http(self) -> None:
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
            app.state.gateway._pause_supervisor = AsyncMock()
            app.state.gateway._resume_supervisor_if_needed = AsyncMock()

            with TestClient(app) as client:
                store.create_workflow(
                    {
                        "workflowId": "advance-demo",
                        "supervisorAgentId": "local",
                        "advanceMode": "semi_automatic",
                        "nodes": [
                            {"id": "a", "prompt": "a", "timeoutSec": 10},
                            {
                                "id": "b",
                                "prompt": "b",
                                "dependsOn": ["a"],
                                "timeoutSec": 10,
                            },
                        ],
                    }
                )
                store.prepare_node_dispatch("advance-demo", "a")
                store.sync_node_job(
                    "advance-demo",
                    "a",
                    {
                        "status": "completed",
                        "response": "A",
                        "finished_at": utc_now(),
                    },
                )
                gate = store.get_workflow("advance-demo")["pendingAdvance"]
                held = client.post(
                    f"/workflows/advance-demo/advance/{gate['gateId']}/hold"
                )
                self.assertEqual(held.status_code, 200)
                self.assertEqual(held.json()["status"], "held")
                repeated_hold = client.post(
                    f"/workflows/advance-demo/advance/{gate['gateId']}/hold"
                )
                self.assertEqual(repeated_hold.status_code, 200)
                self.assertEqual(
                    store.get_workflow("advance-demo")["pendingAdvance"]["state"],
                    "held",
                )
                response = client.post(
                    f"/workflows/advance-demo/advance/{gate['gateId']}/confirm"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "confirmed")
                self.assertTrue(response.json()["resumedFromHold"])
                repeated = client.post(
                    f"/workflows/advance-demo/advance/{gate['gateId']}/confirm"
                )
                self.assertEqual(repeated.status_code, 200)
            self.assertGreaterEqual(
                app.state.gateway._pause_supervisor.await_count, 1
            )
            self.assertGreaterEqual(
                app.state.gateway._resume_supervisor_if_needed.await_count, 1
            )


class AdvanceControlConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_waits_until_hold_has_finished_pausing(self) -> None:
        class FakeStore:
            confirm_calls = 0

            @staticmethod
            def hold_advance(_workflow_id: str, _gate_id: str) -> dict:
                return {"gateId": "gate-1", "status": "held"}

            def confirm_advance(self, _workflow_id: str, _gate_id: str) -> dict:
                self.confirm_calls += 1
                return {
                    "gateId": "gate-1",
                    "status": "confirmed",
                    "resumedFromHold": True,
                }

        store = FakeStore()
        gateway = WorkflowGateway(store, object())
        pause_started = asyncio.Event()
        allow_pause_to_finish = asyncio.Event()

        async def pause(_workflow_id: str) -> None:
            pause_started.set()
            await allow_pause_to_finish.wait()

        gateway._pause_supervisor = AsyncMock(side_effect=pause)
        gateway._resume_supervisor_if_needed = AsyncMock()

        hold_task = asyncio.create_task(gateway.hold_advance("demo", "gate-1"))
        await pause_started.wait()
        confirm_task = asyncio.create_task(
            gateway.confirm_advance("demo", "gate-1")
        )
        await asyncio.sleep(0)
        self.assertEqual(store.confirm_calls, 0)

        allow_pause_to_finish.set()
        await hold_task
        await confirm_task
        self.assertEqual(store.confirm_calls, 1)
        gateway._resume_supervisor_if_needed.assert_awaited_once_with("demo")


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

                await gateway.stop()
                await gateway.event_batcher.close()

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
                    await gateway.stop()
                    await gateway.event_batcher.close()

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

                await gateway.stop()
                await gateway.event_batcher.close()


class WorkflowControlIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _confirmed_restart(
        store: WorkflowStore,
        instruction: str | None = "增加清晰、完整的机身涂装和标识。",
    ) -> dict:
        proposed_id = str(uuid.uuid4())
        confirmed_id = str(uuid.uuid4())
        store.accept_chat_message("control-demo", proposed_id, "从第2步重跑")
        proposal = store.propose_control(
            "control-demo", "restart_from", "b", proposed_id, instruction
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
            gateway._resume_supervisor_if_needed = AsyncMock()
            confirmed = self._confirmed_restart(store)
            answer = await gateway._execute_control("control-demo", confirmed)

            snapshot = store.get_workflow("control-demo")
            self.assertIn("重新打开", answer)
            self.assertIn("返工要求加入", answer)
            self.assertEqual(snapshot["retryPolicy"]["usedRetries"], 1)
            self.assertEqual(
                [node["status"] for node in snapshot["nodes"]],
                ["completed", "pending", "pending"],
            )
            prompt = store.prepare_node_dispatch("control-demo", "b")["prompt"]
            self.assertIn("增加清晰、完整的机身涂装和标识。", prompt)
            self.assertTrue(prompt.endswith(SINGLE_OUTPUT_CONSTRAINT))
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
                revision_count = connection.execute(
                    "SELECT COUNT(*) FROM workflow_node_revision_instructions "
                    "WHERE workflow_id = ?",
                    ("control-demo",),
                ).fetchone()[0]
            self.assertEqual(action["status"], "failed")
            self.assertIsNone(action["retry_ordinal"])
            self.assertEqual(revision_count, 0)


if __name__ == "__main__":
    unittest.main()
