import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_orchestrator_mcp import (
    AgentConfig,
    AppServerClient,
    AppServerDisconnected,
    Orchestrator,
    TurnNotActiveError,
    is_absolute_remote_path,
    remote_path_join,
)
from tests.mock_app_server import MockAppServer


class RemotePathTests(unittest.TestCase):
    def test_accepts_windows_unc_and_unix_absolute_paths(self) -> None:
        for path in (r"D:\codex", r"\\server\share\codex", "/srv/codex"):
            with self.subTest(path=path):
                self.assertTrue(is_absolute_remote_path(path))
                config = AgentConfig.from_dict(
                    "remote", {"url": "ws://127.0.0.1:4500", "cwd": path}
                )
                self.assertEqual(config.cwd, path)

    def test_rejects_relative_remote_path(self) -> None:
        self.assertFalse(is_absolute_remote_path("codex/worktree"))
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            AgentConfig.from_dict(
                "remote", {"url": "ws://127.0.0.1:4500", "cwd": "codex/worktree"}
            )

    def test_rejects_inline_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "token_env"):
            AgentConfig.from_dict(
                "remote",
                {
                    "url": "ws://127.0.0.1:4500",
                    "cwd": "/srv/codex",
                    "token": "must-not-be-stored-here",
                },
            )

    def test_artifact_root_must_be_absolute_and_is_not_exposed(self) -> None:
        with self.assertRaisesRegex(ValueError, "artifact_root.*绝对路径"):
            AgentConfig.from_dict(
                "remote",
                {"url": "ws://127.0.0.1:4500", "cwd": "/srv/work", "artifact_root": "tmp"},
            )
        config = AgentConfig.from_dict(
            "remote",
            {
                "url": "ws://127.0.0.1:4500",
                "cwd": "/srv/work",
                "artifact_root": "/srv/artifacts",
            },
        )
        self.assertTrue(config.public_dict()["artifact_transfer_enabled"])
        self.assertNotIn("artifact_root", config.public_dict())

    def test_public_permission_profiles_respect_agent_write_cap(self) -> None:
        read_only = AgentConfig.from_dict(
            "remote", {"url": "ws://127.0.0.1:4500", "cwd": "/srv/work"}
        )
        writable = AgentConfig.from_dict(
            "remote",
            {
                "url": "ws://127.0.0.1:4500",
                "cwd": "/srv/work",
                "allow_write": True,
            },
        )
        self.assertEqual(read_only.public_dict()["permission_profiles"], ["read_only"])
        self.assertEqual(
            writable.public_dict()["permission_profiles"],
            ["read_only", "workspace_write", "auto_review"],
        )

    def test_agent_capability_defaults_and_fixed_supervisor_capacity(self) -> None:
        local = AgentConfig.from_dict(
            "local", {"url": "ws://127.0.0.1:4500", "cwd": "/srv/work"}
        )
        remote = AgentConfig.from_dict(
            "remote", {"url": "ws://127.0.0.1:4501", "cwd": "/srv/work"}
        )

        self.assertEqual(local.capabilities, ("supervisor", "executor"))
        self.assertEqual(local.capacity, 1)
        self.assertEqual(remote.capabilities, ("executor",))
        self.assertEqual(remote.capacity, 0)
        self.assertTrue(local.enabled)
        self.assertEqual(local.public_dict()["supervisor_capacity"], 1)

    def test_agent_capabilities_enabled_and_capacity_are_validated(self) -> None:
        disabled = AgentConfig.from_dict(
            "supervisor-b",
            {
                "url": "ws://127.0.0.1:4501",
                "cwd": "/srv/work",
                "enabled": False,
                "capabilities": ["supervisor"],
                "capacity": 1,
            },
        )
        self.assertFalse(disabled.enabled)
        with self.assertRaisesRegex(ValueError, "capabilities"):
            AgentConfig.from_dict(
                "bad",
                {
                    "url": "ws://127.0.0.1:4502",
                    "cwd": "/srv/work",
                    "capabilities": ["scheduler"],
                },
            )
        with self.assertRaisesRegex(ValueError, "只能是 1"):
            AgentConfig.from_dict(
                "bad-capacity",
                {
                    "url": "ws://127.0.0.1:4502",
                    "cwd": "/srv/work",
                    "capabilities": ["supervisor"],
                    "capacity": 2,
                },
            )
        with self.assertRaisesRegex(ValueError, "不能配置 capacity"):
            AgentConfig.from_dict(
                "executor-only",
                {
                    "url": "ws://127.0.0.1:4502",
                    "cwd": "/srv/work",
                    "capabilities": ["executor"],
                    "capacity": 1,
                },
            )

    def test_token_file_must_be_absolute_and_is_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "token_file.*绝对路径"):
            AgentConfig.from_dict(
                "remote",
                {
                    "url": "ws://127.0.0.1:4500",
                    "cwd": "/srv/codex",
                    "token_file": "secrets/token.txt",
                },
            )
        with self.assertRaisesRegex(ValueError, "只能配置一个"):
            AgentConfig.from_dict(
                "remote",
                {
                    "url": "ws://127.0.0.1:4500",
                    "cwd": "/srv/codex",
                    "token_env": "REMOTE_TOKEN",
                    "token_file": str(Path.cwd().joinpath("token.txt").resolve()),
                },
            )

    def test_token_file_content_is_bounded_utf8_and_single_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory, "remote.token").resolve()
            config = AgentConfig.from_dict(
                "remote",
                {
                    "url": "ws://127.0.0.1:4500",
                    "cwd": "/srv/codex",
                    "token_file": str(token_path),
                },
            )
            cases = (
                (b"", "为空"),
                (b"first\nsecond", "一行令牌"),
                (b"x" * 8193, "不能超过"),
                (b"\xff", "UTF-8"),
            )
            for content, message in cases:
                with self.subTest(message=message):
                    token_path.write_bytes(content)
                    with self.assertRaisesRegex(RuntimeError, message):
                        Orchestrator._resolve_agent_token(config)

    def test_managed_remote_paths_cannot_accept_escape_components(self) -> None:
        self.assertEqual(
            remote_path_join(r"D:\artifacts", "workflows", "abc"),
            r"D:\artifacts\workflows\abc",
        )
        self.assertEqual(
            remote_path_join("/srv/artifacts", "workflows", "abc"),
            "/srv/artifacts/workflows/abc",
        )
        with self.assertRaisesRegex(ValueError, "目录名称无效"):
            remote_path_join("/srv/artifacts", "..")

class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def _write_config(
        self,
        directory: str,
        url: str,
        *,
        cwd: str = "/srv/codex",
        token_env: str | None = None,
        token_file: str | None = None,
        artifact_root: str | None = None,
        allow_write: bool = False,
    ) -> Path:
        agent: dict[str, object] = {
            "url": url,
            "cwd": cwd,
            "allow_cwd_override": True,
            "allow_write": allow_write,
        }
        if token_env:
            agent["token_env"] = token_env
        if token_file:
            agent["token_file"] = token_file
        if artifact_root:
            agent["artifact_root"] = artifact_root
        path = Path(directory, "agents.json")
        path.write_text(json.dumps({"agents": {"remote": agent}}), encoding="utf-8")
        return path

    async def _dispatch(self, orchestrator: Orchestrator, **kwargs: object):
        values = {
            "agent_id": "remote",
            "prompt": "do the work",
            "thread_id": None,
            "cwd": None,
            "write": False,
            "model": None,
            "timeout_sec": 10,
        }
        values.update(kwargs)
        return await orchestrator.dispatch(**values)

    async def test_probe_agent_only_initializes_connection(self) -> None:
        async with MockAppServer() as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))

                await orchestrator.probe_agent("remote", timeout_sec=0.5)

                self.assertEqual(
                    [request["method"] for request in server.requests], ["initialize"]
                )

    async def _publish_outputs(
        self,
        job: object,
        files: dict[str, bytes] | None = None,
        directories: list[str] | None = None,
    ) -> None:
        output_dir = Path(str(getattr(job, "managed_output_dir")))
        for _ in range(200):
            if output_dir.is_dir():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(output_dir.is_dir(), "托管输出目录未及时创建")
        for filename, content in (files or {}).items():
            output_dir.joinpath(filename).write_bytes(content)
        for name in directories or []:
            output_dir.joinpath(name).mkdir()

    async def test_local_close_wakes_notification_waiter(self) -> None:
        client = AppServerClient("ws://127.0.0.1:1")
        waiter = asyncio.create_task(client.next_notification(5))
        await asyncio.sleep(0)
        await client.close()
        with self.assertRaises(AppServerDisconnected):
            await asyncio.wait_for(waiter, 1)

    async def test_repeated_waits_do_not_cancel_long_job(self) -> None:
        async with MockAppServer(delay_sec=2.5) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                job = await self._dispatch(orchestrator)

                first = await orchestrator.wait(job.job_id, 1)
                self.assertEqual(first.status, "running")
                self.assertEqual(orchestrator.get_job(job.job_id).status, "running")
                second = await orchestrator.wait(job.job_id, 1)
                self.assertEqual(second.status, "running")
                final = await orchestrator.wait(job.job_id, 1)

                self.assertEqual(final.status, "completed")
                self.assertEqual(final.response, "mock final reply")
                self.assertEqual(final.last_event_method, "turn/completed")
                self.assertIsNotNone(final.last_event_at)
                self.assertEqual(final.events_seen, 2)

    async def test_cumulative_files_are_staged_and_one_output_is_captured(self) -> None:
        async with MockAppServer() as server:
            with tempfile.TemporaryDirectory() as directory:
                artifact_root = Path(directory, "workflow-artifacts")
                orchestrator = Orchestrator(
                    self._write_config(
                        directory, server.url, artifact_root=str(artifact_root)
                    )
                )
                job = await self._dispatch(
                    orchestrator,
                    artifact_handoff={
                        "workflowId": "workflow-1",
                        "nodeId": "step-2",
                        "stepNumber": 2,
                        "steps": [
                            {
                                "stepNumber": 1,
                                "displayName": "生成飞机",
                                "artifacts": [{
                                    "id": "artifact-1",
                                    "filename": "plane.txt",
                                    "byteSize": 5,
                                    "mediaType": "text/plain",
                                    "content": b"plane",
                                }],
                            }
                        ],
                    },
                )
                await self._publish_outputs(job, {"merged.txt": b"merged"})
                await orchestrator.wait(job.job_id, 2)

                self.assertEqual(job.status, "completed")
                self.assertEqual(job.captured_files[0]["content"], b"merged")
                input_file = Path(str(job.managed_attempt_dir)).joinpath(
                    "inputs", "step-01", "plane.txt"
                )
                self.assertEqual(input_file.read_bytes(), b"plane")
                self.assertFalse(any(
                    str(request.get("method", "")).startswith("fs/")
                    for request in server.requests
                ))
                turn = next(
                    request for request in server.requests
                    if request.get("method") == "turn/start"
                )
                roots = turn["params"]["sandboxPolicy"]["writableRoots"]
                self.assertEqual(roots, [job.managed_output_dir])
                self.assertIn("第1步 生成飞机", job.prompt)
                self.assertIn("否则不得打开、引用或合并", job.prompt)

    async def test_auto_review_file_handoff_keeps_scoped_roots_and_network_off(self) -> None:
        async with MockAppServer() as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(
                    self._write_config(
                        directory,
                        server.url,
                        artifact_root=str(Path(directory, "artifacts")),
                        allow_write=True,
                    )
                )
                job = await self._dispatch(
                    orchestrator,
                    write=True,
                    permission_profile="auto_review",
                    artifact_handoff={
                        "workflowId": "workflow-auto-review",
                        "nodeId": "step-1",
                        "stepNumber": 1,
                        "steps": [],
                    },
                )
                await orchestrator.wait(job.job_id, 2)

                turn = next(
                    request
                    for request in server.requests
                    if request.get("method") == "turn/start"
                )["params"]
                self.assertEqual(
                    turn["sandboxPolicy"]["writableRoots"],
                    [job.cwd, job.managed_output_dir],
                )
                self.assertFalse(turn["sandboxPolicy"]["networkAccess"])
                self.assertEqual(turn["approvalsReviewer"], "auto_review")

    async def test_multiple_output_files_fail_validation(self) -> None:
        async with MockAppServer() as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(
                    self._write_config(
                        directory,
                        server.url,
                        artifact_root=str(Path(directory, "artifacts")),
                    )
                )
                job = await self._dispatch(
                    orchestrator,
                    artifact_handoff={
                        "workflowId": "workflow-1",
                        "nodeId": "step-1",
                        "stepNumber": 1,
                        "steps": [],
                    },
                )
                await self._publish_outputs(job, {"a.txt": b"a", "b.txt": b"b"})
                await orchestrator.wait(job.job_id, 2)
                self.assertEqual(job.status, "failed")
                self.assertIn("多个不同文件", job.error)

    async def test_output_directory_fails_validation(self) -> None:
        async with MockAppServer() as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(
                    self._write_config(
                        directory,
                        server.url,
                        artifact_root=str(Path(directory, "artifacts")),
                    )
                )
                job = await self._dispatch(
                    orchestrator,
                    artifact_handoff={
                        "workflowId": "workflow-1",
                        "nodeId": "step-1",
                        "stepNumber": 1,
                        "steps": [],
                    },
                )
                await self._publish_outputs(job, directories=["nested"])
                await orchestrator.wait(job.job_id, 2)
                self.assertEqual(job.status, "failed")
                self.assertIn("不允许目录", job.error)

    async def test_oversized_output_fails_validation(self) -> None:
        async with MockAppServer(delay_sec=0.2) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(
                    self._write_config(
                        directory,
                        server.url,
                        artifact_root=str(Path(directory, "artifacts")),
                    )
                )
                job = await self._dispatch(
                    orchestrator,
                    artifact_handoff={
                        "workflowId": "workflow-1",
                        "nodeId": "step-1",
                        "stepNumber": 1,
                        "steps": [],
                    },
                )
                await self._publish_outputs(job, {"large.bin": b"x" * 20_000_001})
                await orchestrator.wait(job.job_id, 5)
                self.assertEqual(job.status, "failed")
                self.assertIn("20 MB", job.error)

    async def test_cumulative_mode_requires_artifact_root(self) -> None:
        async with MockAppServer() as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                with self.assertRaisesRegex(ValueError, "artifact_root"):
                    await self._dispatch(
                        orchestrator,
                        artifact_handoff={
                            "workflowId": "workflow-1",
                            "nodeId": "step-1",
                            "stepNumber": 1,
                            "steps": [],
                        },
                    )

    async def test_same_agent_jobs_are_strictly_serial(self) -> None:
        async with MockAppServer(delay_sec=0.3) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                first = await self._dispatch(orchestrator)
                second = await self._dispatch(orchestrator)
                for _ in range(100):
                    if first.status == "running":
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)

                self.assertEqual(first.status, "running")
                self.assertEqual(second.status, "queued")
                await orchestrator.wait(first.job_id, 1)
                await orchestrator.wait(second.job_id, 1)
                self.assertEqual(second.status, "completed")

    async def test_assistant_jobs_can_run_concurrently_and_forward_output_schema(self) -> None:
        schema = {"type": "object", "properties": {"text": {"type": "string"}}}
        async with MockAppServer(delay_sec=0.3) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(
                    self._write_config(directory, server.url),
                    serialize_agent_jobs=False,
                )
                first = await self._dispatch(orchestrator, output_schema=schema)
                second = await self._dispatch(orchestrator, output_schema=schema)
                for _ in range(100):
                    if first.status == second.status == "running":
                        break
                    await asyncio.sleep(0.01)

                self.assertEqual(first.status, "running")
                self.assertEqual(second.status, "running")
                await asyncio.gather(
                    orchestrator.wait(first.job_id, 1),
                    orchestrator.wait(second.job_id, 1),
                )
                turn_starts = [
                    request for request in server.requests
                    if request.get("method") == "turn/start"
                ]
                self.assertEqual(len(turn_starts), 2)
                self.assertTrue(all(
                    request["params"]["outputSchema"] == schema
                    for request in turn_starts
                ))

    async def test_completed_jobs_are_bounded(self) -> None:
        async with MockAppServer(delay_sec=0.01) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(
                    self._write_config(directory, server.url),
                    max_retained_jobs=2,
                )
                ids = []
                for _ in range(3):
                    job = await self._dispatch(orchestrator)
                    ids.append(job.job_id)
                    await orchestrator.wait(job.job_id, 1)

                self.assertNotIn(ids[0], orchestrator.jobs)
                self.assertEqual(set(orchestrator.jobs), set(ids[1:]))

    async def test_raw_app_server_events_reach_monitor_callback(self) -> None:
        observed: list[dict[str, object]] = []
        async with MockAppServer(delay_sec=0.01, send_message_delta=True) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                job = await self._dispatch(
                    orchestrator,
                    event_callback=lambda message, _: observed.append(message),
                )
                final = await orchestrator.wait(job.job_id, 1)

                self.assertEqual(final.status, "completed")
                self.assertEqual(
                    [message.get("method") for message in observed],
                    [
                        "item/agentMessage/delta",
                        "item/completed",
                        "turn/completed",
                    ],
                )
                initialize = next(
                    request
                    for request in server.requests
                    if request.get("method") == "initialize"
                )
                capabilities = initialize["params"]["capabilities"]
                self.assertNotIn("optOutNotificationMethods", capabilities)

    async def test_steer_uses_current_turn_and_client_message_id(self) -> None:
        async with MockAppServer(delay_sec=1) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                job = await self._dispatch(orchestrator)
                for _ in range(100):
                    if job.turn_id:
                        break
                    await asyncio.sleep(0.01)
                await orchestrator.steer(job.job_id, "现在到哪了", "message-1")
                request = next(item for item in server.requests if item["method"] == "turn/steer")
                self.assertEqual(request["params"], {
                    "threadId": "thread-1",
                    "input": [{"type": "text", "text": "现在到哪了"}],
                    "expectedTurnId": "turn-1",
                    "clientUserMessageId": "message-1",
                })
                await orchestrator.wait(job.job_id, 2)

    async def test_steer_completed_turn_has_typed_error(self) -> None:
        async with MockAppServer(delay_sec=0.01) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                job = await self._dispatch(orchestrator)
                await orchestrator.wait(job.job_id, 1)
                with self.assertRaises(TurnNotActiveError):
                    await orchestrator.steer(job.job_id, "hello", "message-2")

    async def test_scoped_approval_settings_are_forwarded(self) -> None:
        async with MockAppServer(delay_sec=0.01) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                job = await self._dispatch(
                    orchestrator,
                    approval_policy="on-request",
                    approvals_reviewer="auto_review",
                )
                final = await orchestrator.wait(job.job_id, 1)

                self.assertEqual(final.status, "completed")
                for method in ("thread/start", "turn/start"):
                    request = next(
                        item for item in server.requests if item.get("method") == method
                    )
                    self.assertEqual(request["params"]["approvalPolicy"], "on-request")
                    self.assertEqual(
                        request["params"]["approvalsReviewer"], "auto_review"
                    )

    async def test_permission_profiles_map_to_thread_and_turn_parameters(self) -> None:
        cases = (
            ("read_only", False, "read-only", "never", None),
            ("workspace_write", True, "workspace-write", "never", None),
            ("auto_review", True, "workspace-write", "on-request", "auto_review"),
        )
        for profile, write, sandbox, approval, reviewer in cases:
            with self.subTest(profile=profile):
                async with MockAppServer(delay_sec=0.01) as server:
                    with tempfile.TemporaryDirectory() as directory:
                        orchestrator = Orchestrator(
                            self._write_config(
                                directory, server.url, allow_write=True
                            )
                        )
                        job = await self._dispatch(
                            orchestrator,
                            write=write,
                            permission_profile=profile,
                        )
                        final = await orchestrator.wait(job.job_id, 1)
                        self.assertEqual(final.status, "completed", final.snapshot())
                        thread = next(
                            request
                            for request in server.requests
                            if request["method"] == "thread/start"
                        )["params"]
                        turn = next(
                            request
                            for request in server.requests
                            if request["method"] == "turn/start"
                        )["params"]
                        self.assertEqual(thread["sandbox"], sandbox)
                        self.assertEqual(thread["approvalPolicy"], approval)
                        self.assertEqual(turn["approvalPolicy"], approval)
                        self.assertEqual(thread.get("approvalsReviewer"), reviewer)
                        self.assertEqual(turn.get("approvalsReviewer"), reviewer)

    async def test_permission_profile_rejects_write_cap_and_field_conflict(self) -> None:
        async with MockAppServer() as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                with self.assertRaisesRegex(PermissionError, "未启用写权限"):
                    await self._dispatch(
                        orchestrator,
                        write=True,
                        permission_profile="workspace_write",
                    )
                with self.assertRaisesRegex(ValueError, "矛盾"):
                    await self._dispatch(
                        orchestrator,
                        write=False,
                        permission_profile="auto_review",
                    )

    async def test_managed_config_requirements_can_reject_profile_before_thread(self) -> None:
        async with MockAppServer(
            config_requirements={
                "allowedApprovalPolicies": ["never"],
                "allowedSandboxModes": ["readOnly", "workspaceWrite"],
            }
        ) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(
                    self._write_config(directory, server.url, allow_write=True)
                )
                job = await self._dispatch(
                    orchestrator,
                    write=True,
                    permission_profile="auto_review",
                )
                final = await orchestrator.wait(job.job_id, 1)
                self.assertEqual(final.status, "failed")
                self.assertEqual(final.error_stage, "permission/check")
                self.assertIn("执行机管理策略不允许", final.error or "")
                self.assertFalse(
                    any(request["method"] == "thread/start" for request in server.requests)
                )

    async def test_auto_review_remote_rejection_and_timeout_are_terminal(self) -> None:
        async with MockAppServer(
            turn_status="failed",
            turn_error={"code": "approval_rejected", "message": "auto review rejected"},
        ) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(
                    self._write_config(directory, server.url, allow_write=True)
                )
                job = await self._dispatch(
                    orchestrator, write=True, permission_profile="auto_review"
                )
                rejected = await orchestrator.wait(job.job_id, 1)
                self.assertEqual(rejected.status, "failed")
                self.assertEqual(rejected.error_kind, "turn_failed")
                self.assertEqual(
                    rejected.error_details["turn_error"]["code"], "approval_rejected"
                )

        async with MockAppServer(delay_sec=10) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(
                    self._write_config(directory, server.url, allow_write=True)
                )
                job = await self._dispatch(
                    orchestrator, write=True, permission_profile="auto_review"
                )
                job.timeout_sec = 0.15
                timed_out = await orchestrator.wait(job.job_id, 1)
                self.assertEqual(timed_out.status, "failed")
                self.assertEqual(timed_out.error_kind, "job_timeout")

    async def test_disconnect_wakes_waiter_and_marks_job_failed(self) -> None:
        async with MockAppServer(close_after_turn_start=(4101, "test disconnect")) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                started = time.monotonic()
                job = await self._dispatch(orchestrator)
                final = await orchestrator.wait(job.job_id, 1)

                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(final.status, "failed")
                self.assertEqual(final.error_kind, "network_disconnect")
                self.assertEqual(final.error_stage, "turn/completed")
                self.assertEqual(final.ws_close_code, 4101)
                self.assertEqual(final.ws_close_reason, "test disconnect")

    async def test_rpc_timeouts_report_exact_method(self) -> None:
        cases = (
            ("thread/start", None),
            ("thread/resume", "existing-thread"),
            ("turn/start", None),
        )
        for method, thread_id in cases:
            with self.subTest(method=method):
                async with MockAppServer(ignore_methods={method}) as server:
                    with tempfile.TemporaryDirectory() as directory:
                        def fast_client(url: str, **kwargs: object) -> AppServerClient:
                            return AppServerClient(
                                url, request_timeout_sec=0.25, **kwargs
                            )

                        orchestrator = Orchestrator(
                            self._write_config(directory, server.url),
                            client_factory=fast_client,
                        )
                        job = await self._dispatch(orchestrator, thread_id=thread_id)
                        final = await orchestrator.wait(job.job_id, 1)
                        self.assertEqual(final.status, "failed")
                        self.assertEqual(final.error_kind, "rpc_timeout", final.snapshot())
                        self.assertEqual(final.error_stage, method)
                        self.assertEqual(final.error_details["method"], method)

    async def test_failed_turn_preserves_remote_error(self) -> None:
        remote_error = {"code": "sandbox", "message": "remote turn failed"}
        async with MockAppServer(
            turn_status="failed", turn_error=remote_error
        ) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                job = await self._dispatch(orchestrator)
                final = await orchestrator.wait(job.job_id, 1)

                self.assertEqual(final.status, "failed")
                self.assertEqual(final.error_kind, "turn_failed")
                self.assertEqual(final.error_stage, "turn/completed")
                self.assertEqual(final.error_details["turn_status"], "failed")
                self.assertEqual(final.error_details["turn_error"], remote_error)

    async def test_total_timeout_attempts_interrupt_without_losing_primary_error(self) -> None:
        async with MockAppServer(
            delay_sec=10, interrupt_error="interrupt rejected"
        ) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                job = await self._dispatch(orchestrator)
                job.timeout_sec = 0.15
                final = await orchestrator.wait(job.job_id, 1)

                self.assertEqual(final.status, "failed")
                self.assertEqual(final.error_kind, "job_timeout")
                self.assertEqual(final.thread_id, "thread-1")
                self.assertEqual(final.turn_id, "turn-1")
                interrupt = final.error_details["interrupt"]
                self.assertTrue(interrupt["attempted"])
                self.assertFalse(interrupt["succeeded"])
                self.assertIn("interrupt rejected", interrupt["error"])
                self.assertEqual(server.interrupt_requests, 1)

    async def test_total_timeout_also_bounds_connect_and_initialize(self) -> None:
        async with MockAppServer(ignore_methods={"initialize"}) as server:
            with tempfile.TemporaryDirectory() as directory:
                orchestrator = Orchestrator(self._write_config(directory, server.url))
                job = await self._dispatch(orchestrator)
                job.timeout_sec = 0.15
                started = time.monotonic()
                final = await orchestrator.wait(job.job_id, 1)

                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(final.status, "failed")
                self.assertEqual(final.error_kind, "job_timeout")
                self.assertEqual(final.error_stage, "connect")
                self.assertFalse(final.error_details["interrupt"]["attempted"])

    async def test_token_comes_from_environment_and_is_not_exposed(self) -> None:
        secret = "test-secret-that-must-not-leak"
        async with MockAppServer(delay_sec=0.01) as server:
            with tempfile.TemporaryDirectory() as directory:
                config_path = self._write_config(
                    directory,
                    server.url,
                    cwd=r"D:\codex",
                    token_env="TEST_REMOTE_CODEX_TOKEN",
                )
                with patch.dict(os.environ, {"TEST_REMOTE_CODEX_TOKEN": secret}):
                    orchestrator = Orchestrator(config_path)
                    job = await self._dispatch(orchestrator)
                    final = await orchestrator.wait(job.job_id, 1)

                self.assertEqual(final.status, "completed")
                self.assertEqual(server.authorization, f"Bearer {secret}")
                self.assertNotIn(secret, config_path.read_text(encoding="utf-8"))
                self.assertNotIn(secret, json.dumps(final.snapshot()))
                thread_start = next(
                    request for request in server.requests if request.get("method") == "thread/start"
                )
                self.assertEqual(thread_start["params"]["cwd"], r"D:\codex")

    async def test_token_file_is_read_for_each_connection_and_is_not_exposed(self) -> None:
        first_secret = "first-secret-that-must-not-leak"
        second_secret = "second-secret-that-must-not-leak"
        async with MockAppServer(delay_sec=0.01) as server:
            with tempfile.TemporaryDirectory() as directory:
                token_path = Path(directory, "remote.token")
                token_path.write_text(first_secret + "\n", encoding="utf-8")
                config_path = self._write_config(
                    directory,
                    server.url,
                    token_file=str(token_path.resolve()),
                )
                orchestrator = Orchestrator(config_path)
                first = await orchestrator.wait(
                    (await self._dispatch(orchestrator)).job_id, 1
                )
                self.assertEqual(first.status, "completed")
                self.assertEqual(server.authorization, f"Bearer {first_secret}")

                token_path.write_text(second_secret, encoding="utf-8")
                second = await orchestrator.wait(
                    (await self._dispatch(orchestrator)).job_id, 1
                )
                self.assertEqual(second.status, "completed")
                self.assertEqual(server.authorization, f"Bearer {second_secret}")
                snapshots = json.dumps([first.snapshot(), second.snapshot()])
                self.assertNotIn(first_secret, snapshots)
                self.assertNotIn(second_secret, snapshots)
                public_agent = orchestrator.list_agents()[0]
                self.assertTrue(public_agent["authenticated"])
                self.assertNotIn("token_file", public_agent)

    async def test_invalid_token_file_fails_without_exposing_its_path(self) -> None:
        async with MockAppServer(delay_sec=0.01) as server:
            with tempfile.TemporaryDirectory() as directory:
                token_path = Path(directory, "missing-secret.token").resolve()
                orchestrator = Orchestrator(
                    self._write_config(
                        directory,
                        server.url,
                        token_file=str(token_path),
                    )
                )
                final = await orchestrator.wait(
                    (await self._dispatch(orchestrator)).job_id, 1
                )

                self.assertEqual(final.status, "failed")
                self.assertIn("令牌文件不存在", final.error)
                self.assertNotIn(str(token_path), final.error)


if __name__ == "__main__":
    unittest.main()
