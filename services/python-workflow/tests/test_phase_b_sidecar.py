import asyncio
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from codex_orchestrator_mcp import AgentConfig, Orchestrator
from starlette.testclient import TestClient
from workflow_gateway import WorkflowGateway, create_app
from workflow_runtime_client import InternalApiClient, RemoteEventBatcher, resolve_token
from workflow_store import WorkflowStore, utc_now


def remote_workflow(workflow_id: str, supervisor_id: str = "supervisor-a") -> dict:
    return {
        "workflowId": workflow_id,
        "name": workflow_id,
        "supervisorAgentId": supervisor_id,
        "handoffMode": "legacy_text",
        "nodes": [
            {
                "id": "a",
                "agentId": supervisor_id,
                "prompt": "只输出 a",
                "timeoutSec": 10,
            }
        ],
    }


class SidecarAgentConfigTests(unittest.TestCase):
    def test_remote_sidecar_requires_supervisor_and_one_private_token_source(self) -> None:
        base = {
            "url": "ws://127.0.0.1:4500",
            "cwd": "/srv/work",
            "capabilities": ["supervisor", "executor"],
            "capacity": 1,
            "orchestration_mode": "remote_sidecar",
        }
        with self.assertRaisesRegex(ValueError, "令牌来源"):
            AgentConfig.from_dict("supervisor-a", base)
        with self.assertRaisesRegex(ValueError, "只能配置一个"):
            AgentConfig.from_dict(
                "supervisor-a",
                {
                    **base,
                    "sidecar_token_env": "TOKEN_A",
                    "sidecar_token_file": str(Path.cwd().joinpath("a.token").resolve()),
                },
            )
        with self.assertRaisesRegex(ValueError, "supervisor 能力"):
            AgentConfig.from_dict(
                "executor-a",
                {
                    **base,
                    "capabilities": ["executor"],
                    "capacity": 0,
                    "sidecar_token_env": "TOKEN_A",
                },
            )

        config = AgentConfig.from_dict(
            "supervisor-a", {**base, "sidecar_token_env": "TOKEN_A"}
        )
        public = config.public_dict()
        self.assertEqual(public["orchestration_mode"], "remote_sidecar")
        self.assertTrue(public["sidecar_authenticated"])
        self.assertNotIn("sidecar_token_env", public)

    def test_sidecar_token_file_is_bounded_and_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory, "sidecar.token").resolve()
            token_path.write_text("machine-secret\n", encoding="utf-8")
            config = AgentConfig.from_dict(
                "supervisor-a",
                {
                    "url": "ws://127.0.0.1:4500",
                    "cwd": "/srv/work",
                    "capabilities": ["supervisor"],
                    "capacity": 1,
                    "orchestration_mode": "remote_sidecar",
                    "sidecar_token_file": str(token_path),
                },
            )
            self.assertEqual(
                Orchestrator.resolve_sidecar_token(config), "machine-secret"
            )
            self.assertNotIn("machine-secret", json.dumps(config.public_dict()))


class SidecarStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = WorkflowStore(Path(self.directory.name, "workflows.db"))
        self.started_a = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        self.started_b = datetime.now(UTC).isoformat()

    def _online_and_claim(self, workflow_id: str) -> str:
        self.store.record_sidecar_heartbeat(
            "supervisor-a", "instance-a", self.started_a, lease_timeout_sec=20
        )
        self.store.create_workflow(remote_workflow(workflow_id))
        claimed = self.store.claim_next_workflow(
            "supervisor-a", sidecar_instance_id="instance-a", lease_timeout_sec=20
        )
        self.assertIsNotNone(claimed)
        heartbeat = self.store.record_sidecar_heartbeat(
            "supervisor-a", "instance-a", self.started_a, lease_timeout_sec=20
        )
        return str(heartbeat["lease"]["leaseToken"])

    def test_instance_replacement_fails_workflow_and_fences_old_instance(self) -> None:
        lease_token = self._online_and_claim("replace-demo")
        prepared = self.store.prepare_node_dispatch(
            "replace-demo",
            "a",
            sidecar_supervisor_id="supervisor-a",
            lease_token=lease_token,
            sidecar_dispatch_id="dispatch-a",
        )
        self.assertFalse(prepared["alreadyDispatched"])
        retried = self.store.prepare_node_dispatch(
            "replace-demo",
            "a",
            sidecar_supervisor_id="supervisor-a",
            lease_token=lease_token,
            sidecar_dispatch_id="dispatch-a",
        )
        competing = self.store.prepare_node_dispatch(
            "replace-demo",
            "a",
            sidecar_supervisor_id="supervisor-a",
            lease_token=lease_token,
            sidecar_dispatch_id="dispatch-b",
        )
        self.assertFalse(retried["alreadyDispatched"])
        self.assertTrue(competing["alreadyDispatched"])

        replaced = self.store.record_sidecar_heartbeat(
            "supervisor-a", "instance-b", self.started_b, lease_timeout_sec=20
        )
        self.assertEqual(replaced["failedWorkflowId"], "replace-demo")
        self.assertEqual(self.store.get_workflow("replace-demo")["status"], "failed")
        with self.assertRaisesRegex(RuntimeError, "旧 Sidecar"):
            self.store.record_sidecar_heartbeat(
                "supervisor-a", "instance-a", self.started_a, lease_timeout_sec=20
            )
        with self.assertRaisesRegex(RuntimeError, "租约"):
            self.store.sync_node_job(
                "replace-demo",
                "a",
                {"status": "completed", "response": "late"},
                sidecar_supervisor_id="supervisor-a",
                lease_token=lease_token,
            )

        self.store.create_workflow(remote_workflow("replacement-next"))
        next_claim = self.store.claim_next_workflow(
            "supervisor-a",
            sidecar_instance_id="instance-b",
            lease_timeout_sec=20,
        )
        self.assertIsNotNone(next_claim)
        self.assertEqual(
            self.store.get_workflow("replacement-next")["status"], "running"
        )

    def test_expiry_fails_active_workflow_and_releases_slot(self) -> None:
        self._online_and_claim("expiry-demo")
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE supervisor_leases SET expires_at = ? WHERE workflow_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "expiry-demo"),
            )

        expired = self.store.expire_sidecar_leases()

        self.assertEqual(expired[0]["workflowId"], "expiry-demo")
        self.assertEqual(self.store.get_workflow("expiry-demo")["status"], "failed")
        self.assertFalse(self.store.has_supervisor_lease("expiry-demo"))

    def test_event_retry_is_idempotent_per_workflow(self) -> None:
        self.store.create_workflow(remote_workflow("events-a"))
        self.store.create_workflow(remote_workflow("events-b"))
        event_a = {
            "workflow_id": "events-a",
            "node_id": "a",
            "source": "worker",
            "event_type": "appserver.item/completed",
            "payload": {"ok": True},
            "external_event_id": "same-retry-key",
        }
        first = self.store.add_events([event_a])[0]
        retried = self.store.add_events([event_a])[0]
        second_workflow = self.store.add_events(
            [{**event_a, "workflow_id": "events-b"}]
        )[0]

        self.assertEqual(first, retried)
        self.assertNotEqual(first, second_workflow)
        self.assertEqual(len(self.store.list_events("events-a")), 2)
        self.assertEqual(len(self.store.list_events("events-b")), 2)

    def test_schema_upgrade_contains_sidecar_and_fencing_state(self) -> None:
        with self.store._connect() as connection:
            lease_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(supervisor_leases)"
                ).fetchall()
            }
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            node_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(workflow_nodes)"
                ).fetchall()
            }
        self.assertTrue(
            {"lease_token", "sidecar_instance_id", "renewed_at", "expires_at"}
            <= lease_columns
        )
        self.assertIn("supervisor_sidecars", tables)
        self.assertIn("supervisor_sidecar_instances", tables)
        self.assertIn("dispatch_token", node_columns)


class SidecarInternalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = Path(self.directory.name, "agents.json")
        agents = {}
        for index, suffix in enumerate(("a", "b")):
            agents[f"supervisor-{suffix}"] = {
                "url": f"ws://127.0.0.1:{4500 + index}",
                "cwd": "/srv/work",
                "enabled": True,
                "capabilities": ["supervisor", "executor"],
                "capacity": 1,
                "orchestration_mode": "remote_sidecar",
                "sidecar_token_env": f"SIDECAR_TOKEN_{suffix.upper()}",
            }
        config.write_text(json.dumps({"agents": agents}), encoding="utf-8")
        self.tokens = {"SIDECAR_TOKEN_A": "token-a", "SIDECAR_TOKEN_B": "token-b"}
        self.environment = patch.dict(os.environ, self.tokens)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app(
            db_path=Path(self.directory.name, "workflows.db"), config_path=config
        )
        self.client = TestClient(self.app)
        self.store = self.app.state.gateway.store
        self.started = datetime.now(UTC).isoformat()

    @staticmethod
    def _auth(token: str, lease: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}"}
        if lease:
            headers["X-Workflow-Lease"] = lease
        return headers

    def _claim(self, workflow_id: str, supervisor_id: str, token: str) -> str:
        heartbeat = self.client.post(
            "/internal/v1/sidecars/heartbeat",
            headers=self._auth(token),
            json={"instanceId": f"instance-{supervisor_id}", "startedAt": self.started},
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.store.create_workflow(remote_workflow(workflow_id, supervisor_id))
        self.store.claim_next_workflow(
            supervisor_id,
            sidecar_instance_id=f"instance-{supervisor_id}",
            lease_timeout_sec=20,
        )
        renewed = self.client.post(
            "/internal/v1/sidecars/heartbeat",
            headers=self._auth(token),
            json={"instanceId": f"instance-{supervisor_id}", "startedAt": self.started},
        )
        self.assertEqual(renewed.status_code, 200)
        return str(renewed.json()["lease"]["leaseToken"])

    def test_auth_isolation_status_codes_and_sanitized_context(self) -> None:
        lease_a = self._claim("api-a", "supervisor-a", "token-a")
        self._claim("api-b", "supervisor-b", "token-b")

        self.assertEqual(
            self.client.get("/internal/v1/workflows/api-a").status_code, 401
        )
        self.assertEqual(
            self.client.get(
                "/internal/v1/workflows/api-b", headers=self._auth("token-a")
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/internal/v1/workflows/missing", headers=self._auth("token-a")
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/internal/v1/workflows/api-a/nodes/a/prepare",
                headers=self._auth("token-a"),
                json={"dispatchId": "prepare-a"},
            ).status_code,
            409,
        )

        prepared = self.client.post(
            "/internal/v1/workflows/api-a/nodes/a/prepare",
            headers=self._auth("token-a", lease_a),
            json={"dispatchId": "prepare-a"},
        )
        self.assertEqual(prepared.status_code, 200)
        attached = self.client.post(
            "/internal/v1/workflows/api-a/nodes/a/state",
            headers=self._auth("token-a", lease_a),
            json={
                "operation": "attach",
                "snapshot": {
                    "job_id": "job-secret",
                    "thread_id": "thread-secret",
                    "turn_id": "turn-secret",
                    "status": "running",
                },
            },
        )
        self.assertEqual(attached.status_code, 200)
        self.assertNotIn("threadId", attached.json())
        self.assertNotIn("turnId", attached.json())
        workflow = self.client.get(
            "/internal/v1/workflows/api-a", headers=self._auth("token-a")
        ).json()
        self.assertNotIn("supervisor", workflow)
        self.assertNotIn("assistant", workflow)
        self.assertNotIn("threadId", workflow["nodes"][0])

    def test_event_retry_and_stale_lease_conflict(self) -> None:
        lease = self._claim("api-events", "supervisor-a", "token-a")
        payload = {
            "events": [
                {
                    "eventId": "retry-1",
                    "nodeId": "a",
                    "source": "worker",
                    "type": "appserver.item/completed",
                    "payload": {
                        "ok": True,
                        "authorization": "Bearer secret",
                        "turn": {"id": "turn-secret", "status": "completed"},
                    },
                    "createdAt": utc_now(),
                }
            ]
        }
        path = "/internal/v1/workflows/api-events/events:batch"
        first = self.client.post(path, headers=self._auth("token-a", lease), json=payload)
        retried = self.client.post(path, headers=self._auth("token-a", lease), json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), retried.json())
        stored_payload = self.store.list_events("api-events")[-1]["payload"]
        self.assertNotIn("authorization", stored_payload)
        self.assertNotIn("id", stored_payload["turn"])

        with self.store._connect() as connection:
            connection.execute(
                "UPDATE supervisor_leases SET expires_at = ? WHERE workflow_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "api-events"),
            )
        stale = self.client.post(path, headers=self._auth("token-a", lease), json=payload)
        self.assertEqual(stale.status_code, 409)


class InternalApiClientTests(unittest.TestCase):
    def test_token_resolution_and_constructor_validation(self) -> None:
        with patch.dict(os.environ, {"CENTRAL_TOKEN": "secret-value"}):
            self.assertEqual(
                resolve_token(token_env="CENTRAL_TOKEN", token_file=None),
                "secret-value",
            )
        with self.assertRaisesRegex(ValueError, "必须且只能"):
            InternalApiClient(
                "http://127.0.0.1:8080",
                "supervisor-a",
                started_at=datetime.now(UTC).isoformat(),
            )

    def test_remote_workflow_rejects_file_handoff_before_persistence(self) -> None:
        class RemoteOrchestrator:
            def list_agents(self):
                return [
                    {
                        "agent_id": "supervisor-a",
                        "enabled": True,
                        "capabilities": ["supervisor", "executor"],
                        "supervisor_capacity": 1,
                        "orchestration_mode": "remote_sidecar",
                        "permission_profiles": ["read_only"],
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            gateway = WorkflowGateway(store, RemoteOrchestrator())
            value = remote_workflow("file-handoff")
            value["handoffMode"] = "cumulative_files"
            with self.assertRaisesRegex(ValueError, "legacy_text"):
                asyncio.run(gateway.submit(value))
            with self.assertRaisesRegex(ValueError, "找不到工作流"):
                store.get_workflow("file-handoff")


class RemoteSidecarSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_remote_supervisor_fails_before_dispatch(self) -> None:
        class OfflineRemoteOrchestrator:
            def __init__(self) -> None:
                self.dispatched = False

            def list_agents(self):
                return [
                    {
                        "agent_id": "supervisor-a",
                        "cwd": "/srv/work",
                        "enabled": True,
                        "capabilities": ["supervisor", "executor"],
                        "supervisor_capacity": 1,
                        "orchestration_mode": "remote_sidecar",
                        "permission_profiles": ["read_only"],
                    }
                ]

            async def dispatch(self, **kwargs):
                self.dispatched = True
                raise AssertionError("离线远程主监督不应启动 app-server 会话。")

        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory, "workflows.db"))
            orchestrator = OfflineRemoteOrchestrator()
            gateway = WorkflowGateway(store, orchestrator)

            snapshot = await gateway.submit(remote_workflow("offline-submit"))

            self.assertEqual(snapshot["status"], "failed")
            self.assertIn("Sidecar", snapshot["error"])
            self.assertFalse(orchestrator.dispatched)
            self.assertFalse(store.has_supervisor_lease("offline-submit"))
            await gateway.event_batcher.close()


class RemoteEventBatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_batch_retries_with_the_same_event_id(self) -> None:
        class FlakyStore:
            def __init__(self) -> None:
                self.calls = []

            def add_events(self, events):
                self.calls.append([dict(event) for event in events])
                if len(self.calls) == 1:
                    raise RuntimeError("temporary failure")
                return [1]

        store = FlakyStore()
        batcher = RemoteEventBatcher(store, flush_interval=60)
        await batcher.add(
            "retry-events",
            node_id="a",
            source="worker",
            event_type="appserver.turn/completed",
            payload={"ok": True},
        )
        with self.assertRaisesRegex(RuntimeError, "temporary"):
            await batcher.flush()
        await batcher.flush()
        await batcher.close()

        self.assertEqual(len(store.calls), 2)
        self.assertEqual(
            store.calls[0][0]["external_event_id"],
            store.calls[1][0]["external_event_id"],
        )


if __name__ == "__main__":
    unittest.main()
