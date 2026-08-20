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


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def _write_config(
        self,
        directory: str,
        url: str,
        *,
        cwd: str = "/srv/codex",
        token_env: str | None = None,
    ) -> Path:
        agent: dict[str, object] = {
            "url": url,
            "cwd": cwd,
            "allow_cwd_override": True,
        }
        if token_env:
            agent["token_env"] = token_env
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


if __name__ == "__main__":
    unittest.main()
