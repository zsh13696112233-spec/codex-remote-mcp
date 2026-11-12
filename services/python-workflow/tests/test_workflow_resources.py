import json
import asyncio
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.registry_fixtures import FixtureWorkflowStore, seed_agents
from workflow_store import WorkflowStore
from workflow_gateway import WorkflowGateway


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FixtureWorkflowStore(Path(self.temp.name) / "runtime.db")
        seed_agents(self.store, {key: {"capabilities": [role]} for key, role in (
            ("s1", "supervisor"), ("s2", "supervisor"), ("e1", "executor"), ("e2", "executor"))})

    def create(self, key, supervisor="s1", executor="e1", **extra):
        self.store.create_workflow({"workflowId": key, "supervisorAgentId": supervisor,
            "nodes": [{"id": "one", "agentId": executor, "prompt": "工作"},
                      {"id": "two", "agentId": executor, "prompt": "检查", "dependsOn": ["one"]}], **extra})

    def test_connection_detection_preserves_busy_and_unconfirmed_claims(self):
        from codex_orchestrator_mcp import Orchestrator
        gateway = WorkflowGateway(self.store, Orchestrator())
        self.create("detection")
        self.store.claim_next_workflow("s1")
        self.store.prepare_node_dispatch("detection", "one")
        async def check():
            for review in (False, True):
                if review:
                    self.store.finish_workflow("detection", supervisor_status="failed", response=None, error="中断")
                claims = self.store.machine_claims()
                for failure in (ConnectionError("private"), None):
                    gateway.orchestrator.probe_agent = AsyncMock(side_effect=failure)
                    await gateway.test_machine("e1")
                    self.assertEqual(self.store.machine_claims(), claims)
                    machine = next(a for a in gateway.public_agents() if a["agentId"] == "e1")
                    self.assertEqual(machine["availability"], "busy")
                    self.assertEqual(machine["occupation"]["state"], "review" if review else "occupied")
                    self.assertEqual(machine["testStatus"], "failed" if failure else "passed")
        asyncio.run(check())

    def test_two_supervisors_cannot_share_executor_and_wait_keeps_claim(self):
        self.create("a")
        self.create("b", "s2")
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(self.store.claim_next_workflow, ("s1", "s2")))
        self.assertEqual(sum(value is not None for value in results), 1)
        winner = next(value for value in results if value)
        loser_supervisor = "s2" if winner["workflowId"] == "a" else "s1"
        self.store.prepare_node_dispatch(winner["workflowId"], "one")
        self.store.sync_node_job(winner["workflowId"], "one", {"status": "completed"})
        self.assertIsNone(self.store.claim_next_workflow(loser_supervisor))
        self.assertEqual(self.store.machine_claims()["e1"]["workflowId"], winner["workflowId"])
        self.store.cancel_workflow(winner["workflowId"])
        self.assertIsNotNone(self.store.claim_next_workflow(loser_supervisor))

    def test_unknown_execution_survives_restart_and_only_exact_confirmation_releases(self):
        self.create("a")
        self.create("b", "s2")
        self.store.claim_next_workflow("s1")
        self.store.prepare_node_dispatch("a", "one")
        self.store.attach_node_job("a", "one", {"job_id": "job", "thread_id": "thread", "turn_id": "turn", "status": "running"})
        self.store.recover_active_workflows_after_restart()
        reloaded = FixtureWorkflowStore(self.store.path)
        self.assertEqual(reloaded.machine_claims()["e1"]["state"], "review")
        self.assertIsNone(reloaded.claim_next_workflow("s2"))
        record, = reloaded.unresolved_resource_executions()
        reloaded.confirm_resource_stopped({**record, "turn_id": "other"})
        self.assertIn("e1", reloaded.machine_claims())
        reloaded.confirm_resource_stopped(record)
        self.assertIsNotNone(reloaded.claim_next_workflow("s2"))

    def test_early_supervisor_failure_does_not_release_running_executor(self):
        self.create("a")
        self.store.claim_next_workflow("s1")
        self.store.prepare_node_dispatch("a", "one")
        self.store.finish_workflow("a", supervisor_status="failed", response=None, error="中断")
        self.assertEqual(self.store.machine_claims()["e1"]["state"], "review")
        self.assertEqual(self.store.get_workflow("a")["resources"]["state"], "review")
        self.store.sync_node_job("a", "one", {"status": "failed", "execution_stopped": True})
        self.assertEqual(self.store.get_workflow("a")["resources"]["state"], "released")

    def test_same_task_cannot_restart_on_another_worker_until_execution_has_stopped(self):
        self.create("a", taskDefinitionId="task")
        self.store.claim_next_workflow("s1")
        self.store.prepare_node_dispatch("a", "one")
        self.store.finish_workflow("a", supervisor_status="failed", response=None, error="中断")
        with self.assertRaisesRegex(ValueError, "当前任务已有"):
            self.create("b", "s2", "e2", taskDefinitionId="task")
        self.store.sync_node_job("a", "one", {"status": "failed", "execution_stopped": True})
        self.create("b", "s2", "e2", taskDefinitionId="task")

    def test_upgrade_protects_old_executions_and_rejects_existing_overlap(self):
        self.create("a")
        self.create("b", "s2")
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET status='running' WHERE workflow_id='a'")
            db.execute("UPDATE workflow_nodes SET status='running' WHERE workflow_id='a' AND node_id='one'")
        upgraded = WorkflowStore(self.store.path)
        self.assertEqual(upgraded.machine_claims()["e1"]["workflowId"], "a")
        with upgraded._connect() as db:
            db.execute("UPDATE workflows SET status='running' WHERE workflow_id='b'")
        with self.assertRaisesRegex(RuntimeError, "共享执行机"):
            WorkflowStore(self.store.path)

    def test_failed_before_turn_can_release_but_lost_turn_start_cannot(self):
        for stopped in (True, False):
            with self.subTest(stopped=stopped):
                key = str(stopped)
                self.create(key)
                self.store.claim_next_workflow("s1")
                self.store.prepare_supervisor_execution(key)
                self.store.update_supervisor(key, {"job_id": key, "status": "failed", "execution_stopped": stopped})
                self.store.finish_workflow(key, supervisor_status="failed", response=None, error="启动失败")
                self.assertEqual("s1" in self.store.machine_claims(), not stopped)

    def pending_restart(self, supervisor_pending=False):
        self.create("restart")
        self.store.claim_next_workflow("s1")
        if supervisor_pending:
            self.store.prepare_supervisor_execution("restart")
        self.store.prepare_node_dispatch("restart", "one")
        self.store.attach_node_job("restart", "one", {
            "job_id": "job", "thread_id": "thread", "turn_id": None, "status": "running"})
        self.store.finish_workflow("restart", supervisor_status="failed", response=None, error="启动请求超时")
        claim = self.store.machine_claims()["e1"]
        return {"workflowId": "restart", "acquiredAt": claim["acquiredAt"], "confirmation": "machine_restarted"}

    def test_restart_confirmation_releases_missing_turn_and_is_idempotent(self):
        body = self.pending_restart()
        self.assertTrue(self.store.machine_claims()["e1"]["stopConfirmationRequired"])
        self.assertFalse(self.store.machine_claims()["s1"]["stopConfirmationRequired"])
        # This is the incident: the automatic turn checker has no execution ID to query.
        self.assertEqual(self.store.unresolved_resource_executions(), [])
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.store.confirm_machine_restarted("e1", body), range(2)))
        self.assertEqual(sum(not r["alreadyConfirmed"] for r in results), 1)
        self.assertTrue(all(r["resources"]["state"] == "released" for r in results))
        self.assertEqual(self.store.get_workflow("restart")["status"], "failed")
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM workflow_events WHERE event_type='workflow.machine_restart_confirmed'").fetchone()[0], 1)
            guard = db.execute("SELECT turn_id,stopped FROM workflow_execution_guards WHERE execution_key='node:one'").fetchone()
            self.assertEqual(tuple(guard), (None, 1))

    def test_restart_confirmation_does_not_confirm_other_machine(self):
        body = self.pending_restart(supervisor_pending=True)
        result = self.store.confirm_machine_restarted("e1", body)
        self.assertEqual(result["resources"]["state"], "review")
        claims = self.store.machine_claims()
        self.assertFalse(claims["e1"]["stopConfirmationRequired"])
        self.assertTrue(claims["s1"]["stopConfirmationRequired"])
        self.assertEqual(self.store.confirm_machine_restarted("s1", body)["resources"]["state"], "released")

    def test_restart_confirmation_rejects_invalid_or_stale_requests(self):
        body = self.pending_restart()
        claims = self.store.machine_claims()
        for invalid in ({}, {**body, "confirmation": False}, {**body, "confirmation": "service_online"},
                        {**body, "extra": True}, {**body, "acquiredAt": "old"},
                        {**body, "workflowId": []}, {**body, "workflowId": "unknown"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.confirm_machine_restarted("e1", invalid)
            self.assertEqual(self.store.machine_claims(), claims)
        with self.assertRaises(ValueError):
            self.store.confirm_machine_restarted("e2", body)
        self.store.confirm_machine_restarted("e1", body)
        self.create("new")
        self.store.claim_next_workflow("s1")
        self.store.prepare_node_dispatch("new", "one")
        # Even an already-confirmed historical request cannot release a newer claim.
        with self.assertRaisesRegex(ValueError, "占用已变化"):
            self.store.confirm_machine_restarted("e1", body)
        current = self.store.machine_claims()["e1"]
        with self.assertRaisesRegex(ValueError, "运行中的任务请先取消"):
            self.store.confirm_machine_restarted("e1", {**body, "workflowId": "new", "acquiredAt": current["acquiredAt"]})
        self.assertEqual(self.store.machine_claims()["e1"], current)

    def test_restart_confirmation_http_contract_and_queue_wakeup(self):
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient
        from workflow_gateway import manage_machines
        body = self.pending_restart()
        app = Starlette(routes=[Route("/agents/{agent_id}/confirm-restarted", manage_machines, methods=["POST"])])
        scheduler = AsyncMock()
        app.state.gateway = SimpleNamespace(store=self.store, registry=None, _schedule_pending=scheduler)
        with TestClient(app) as client:
            self.assertEqual(client.post("/agents/e1/confirm-restarted", json=[]).status_code, 400)
            self.assertEqual(client.post("/agents/e1/confirm-restarted", json={}).status_code, 400)
            scheduler.assert_not_awaited()
            response = client.post("/agents/e1/confirm-restarted", json=body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["resources"]["state"], "released")
            scheduler.assert_awaited_once()

    def test_group_assignment_uses_global_supervisor_and_freezes_all_steps_directory(self):
        group = str(uuid.uuid4())
        with self.store._connect() as db:
            db.execute("INSERT INTO agent_groups(id,name) VALUES(?,?)", (group, "A"))
            db.execute("UPDATE registered_agents SET group_id=? WHERE id IN ('e1','e2')", (group,))
        template = {"workflowId": "pooled", "groupId": group, "dispatchMode": "group",
                    "nodes": [{"id": "a", "prompt": "工作"}, {"id": "b", "prompt": "检查", "dependsOn": ["a"]}]}
        self.store.create_workflow(template)
        assigned = self.store.claim_next_workflow("s1")
        self.assertEqual(assigned["supervisorAgentId"], "s1")
        self.assertEqual({n["agentId"] for n in assigned["nodes"]}, {"e1"})
        self.assertEqual({n["cwd"] for n in assigned["nodes"]}, {"/work"})
        # Simulate later registry edits, retaining the frozen execution specification.
        with self.store._connect() as db:
            config = json.loads(db.execute("SELECT config FROM registered_agents WHERE id='e1'").fetchone()[0])
            config["cwd"] = "/new"
            db.execute("UPDATE registered_agents SET config=? WHERE id='e1'", (json.dumps(config),))
        self.assertEqual(self.store.get_spec("pooled")["nodes"][1]["cwd"], "/work")
        self.store.prepare_node_dispatch("pooled", "a")

    def test_group_assignment_does_not_accept_caller_machine_or_directory(self):
        for node in ({"agentId": "e1"}, {"cwd": "/other"}, {"agent_id": "e1"}, {"executor": {"agent_id": "e1"}}):
            with self.assertRaisesRegex(ValueError, "不能指定"):
                self.store.create_workflow({"dispatchMode": "group", "groupId": str(uuid.uuid4()),
                    "nodes": [{"id": "a", "prompt": "工作", **node}]})

    def test_group_waits_if_no_executor_without_occupying_supervisor(self):
        group = str(uuid.uuid4())
        with self.store._connect() as db:
            db.execute("INSERT INTO agent_groups(id,name) VALUES(?,?)", (group, "Empty"))
        self.store.create_workflow({"workflowId": "pooled", "dispatchMode": "group", "groupId": group,
                                   "nodes": [{"id": "a", "prompt": "工作"}]})
        self.assertIsNone(self.store.claim_next_workflow("s1"))
        self.assertEqual(self.store.machine_claims(), {})
        self.assertIn("组内没有", self.store.get_workflow("pooled")["allocation"]["waitingReason"])

    def test_legacy_check_claim_still_blocks_workflow(self):
        self.create("a")
        with self.store._connect() as db:
            db.execute("INSERT INTO machine_check_claims VALUES('e1','check','now','occupied')")
        self.assertIsNone(self.store.claim_next_workflow("s1"))
        with self.store._connect() as db:
            db.execute("UPDATE machine_check_claims SET state='review' WHERE operation_id='check'")
        self.assertIsNone(self.store.claim_next_workflow("s1"))
        self.assertEqual(self.store.machine_claims()['e1']['state'], 'review')

    def test_sidecar_can_only_read_current_pair_even_across_groups(self):
        from workflow_store import utc_now
        from agent_registry import AgentRegistry
        group = AgentRegistry(self.store).save_group({"name": "目标组"})["id"]
        with self.store._connect() as db:
            db.execute("UPDATE registered_agents SET group_id=? WHERE id='e1'", (group,))
        self.assertEqual(self.store.sidecar_agent_ids("s1", None), {"s1"})
        self.store.create_workflow({"workflowId": "scoped", "groupId": group, "dispatchMode": "group",
            "nodes": [{"id": "a", "prompt": "工作"}]})
        self.store.record_sidecar_heartbeat("s1", "instance", utc_now(), lease_timeout_sec=20)
        self.store.claim_next_workflow("s1", sidecar_instance_id="instance", lease_timeout_sec=20)
        with self.store._connect() as db:
            token = db.execute("SELECT lease_token FROM supervisor_leases WHERE supervisor_id='s1'").fetchone()[0]
        self.assertEqual(self.store.sidecar_agent_ids("s1", token), {"s1", "e1"})
        with self.assertRaisesRegex(RuntimeError, "租约"):
            self.store.sidecar_agent_ids("s1", "old-token")

    def test_dependency_order_and_single_chain_are_enforced(self):
        from agent_registry import AgentRegistry
        group = AgentRegistry(self.store).save_group({"name": "链路组"})["id"]
        with self.store._connect() as db:
            db.execute("UPDATE registered_agents SET group_id=? WHERE id='e1'", (group,))
        for nodes in ([{"id": "a", "prompt": "a"}, {"id": "b", "prompt": "b"}],
                      [{"id": "a", "prompt": "a"}, {"id": "b", "prompt": "b", "dependsOn": ["a"]},
                       {"id": "c", "prompt": "c", "dependsOn": ["a"]}]):
            with self.assertRaisesRegex(ValueError, "严格串行"):
                self.store.create_workflow({"groupId": group, "dispatchMode": "group", "nodes": nodes})
        self.store.create_workflow({"workflowId": "reverse", "groupId": group, "dispatchMode": "group",
            "nodes": [{"id": "b", "prompt": "b", "dependsOn": ["a"]}, {"id": "a", "prompt": "a"}]})
        self.store.claim_next_workflow("s1")
        with self.assertRaisesRegex(ValueError, "依赖尚未完成"):
            self.store.prepare_node_dispatch("reverse", "b")
        self.store.prepare_node_dispatch("reverse", "a")


class GroupGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_lookup_does_not_block_submission_and_one_executor_must_cover_all_steps(self):
        with tempfile.TemporaryDirectory() as folder:
            store = WorkflowStore(Path(folder) / "runtime.db")
            registry = seed_agents(store, {"s": {"capabilities": ["supervisor"]},
                                           "e1": {"capabilities": ["executor"]},
                                           "e2": {"capabilities": ["executor"]}})
            group = registry.save_group({"name": "A"})["id"]
            with store._connect() as db:
                db.execute("UPDATE registered_agents SET group_id=? WHERE id IN ('e1','e2')", (group,))
            ready = asyncio.Event()
            async def models(agent_id):
                await ready.wait()
                return [{"model": model, "supportedReasoningEfforts": ["high"]}
                        for model in (["model-a"] if agent_id == "e1" else ["model-a", "model-b"])]
            orchestrator = SimpleNamespace(
                list_agents=lambda: [agent.public_dict() for agent in registry.configs().values()],
                agent_models=AsyncMock(side_effect=models))
            gateway = WorkflowGateway(store, orchestrator)
            gateway._supervisor_runtime["s"] = {"connectionStatus": "online"}
            async def supervise(_):
                await asyncio.Event().wait()
            gateway._run_supervisor = AsyncMock(side_effect=supervise)
            try:
                snapshot = await asyncio.wait_for(gateway.submit({"workflowId": "pooled", "dispatchMode": "group", "groupId": group,
                    "nodes": [{"id": "a", "prompt": "工作", "model": "model-a", "reasoningEffort": "high"},
                              {"id": "b", "prompt": "检查", "model": "model-b", "dependsOn": ["a"]}]}), 1)
                self.assertEqual(snapshot["status"], "queued")
                self.assertEqual(store.machine_claims(), {})
                ready.set()
                await gateway._group_check_task
                allocated = store.get_workflow("pooled")["allocation"]
                self.assertEqual(allocated["executorAgentId"], "e2")
                self.assertEqual(allocated["supervisorAgentId"], "s")
            finally:
                await gateway.stop()


if __name__ == "__main__":
    unittest.main()
