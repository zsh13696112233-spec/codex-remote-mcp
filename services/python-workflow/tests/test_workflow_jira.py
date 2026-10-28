import base64
import json
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jira_runtime import account_from_config, claim_next, issue_keys, excluding_query, LAUNCHER
from tests.registry_fixtures import seed_agents
from workflow_jira import run_directory, server_key
from workflow_outcomes import parse, schema
from workflow_store import WorkflowStore


def outcome(key="TEST-1", status="completed", reference=""):
    return {"summary": "本步骤结束", "outcome": "success", "reason": "", "document": None,
        "jiraComment": {"status": "not_applicable", "issueKey": key, "reference": "", "detail": "无需评论"},
        "jiraDeveloper": {"status": "not_applicable", "accountType": "", "accountId": "", "displayName": "", "detail": ""},
        "jiraDelivery": {"status": status, "reference": reference, "summary": "已核对结果"}}


class JiraStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = WorkflowStore(Path(directory.name) / "runtime.db")
        self.groups = [str(uuid.uuid4()), str(uuid.uuid4())]
        seed_agents(self.store, {key: {"cwd": r"C:\work", "allow_write": True, "allow_full_access": True,
            "capabilities": [role]} for key, role in (("s1", "supervisor"), ("s2", "supervisor"), ("e1", "executor"), ("e2", "executor"))})
        with self.store._connect() as db:
            for index, group in enumerate(self.groups, 1):
                db.execute("INSERT INTO agent_groups(id,name) VALUES(?,?)", (group, "Group " + str(index)))
                db.execute("UPDATE registered_agents SET group_id=? WHERE id=?", (group, "e" + str(index)))
        self.specs = {}

    def create(self, workflow_id, index=1, **extra):
        spec = {"workflowId": workflow_id, "dispatchMode": "group", "groupId": self.groups[index-1],
            "taskDefinitionId": "same-task", "jira": {"jql": "project = TEST", "account": "bot"},
            "nodes": [{"id": "one", "prompt": "处理任务"}], **extra}
        self.specs[workflow_id] = spec
        self.store.create_workflow(spec)
        self.assertIsNotNone(self.store.claim_next_workflow("s" + str(index)))

    def claim(self, workflow_id, keys=None):
        return self.store.jira_update(workflow_id, "claim", {"server": "http://jira.test:80/",
            "account": "bot", "fingerprint": "a" * 64, "candidates": ["TEST-1"] if keys is None else keys})

    def ready(self, workflow_id):
        self.claim(workflow_id)
        return self.store.jira_update(workflow_id, "ready", {
            "launcherPath": "C:\\Users\\worker\\AppData\\Local\\CodexFlowJira\\" + run_directory(workflow_id) + "\\jira.ps1"})

    def test_claim_is_atomic_across_groups_and_idempotent(self):
        self.create("a")
        self.create("b", 2)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(self.claim, ["a", "b"]))
        self.assertEqual(sum(row["issueKey"] == "TEST-1" for row in results), 1)
        winner = "a" if results[0]["issueKey"] else "b"
        self.assertEqual(self.claim(winner)["issueKey"], "TEST-1")
        self.assertEqual(len(self.store.jira_runs("")["items"]), 2)
        self.assertEqual(len(self.store.jira_runs(self.groups[0])["items"]), 1)

    def test_dispatch_requires_claim_and_credentials_and_freezes_issue(self):
        self.create("a")
        with self.assertRaisesRegex(RuntimeError, "尚未领取"):
            self.store.prepare_node_dispatch("a", "one")
        self.ready("a")
        node = self.store.prepare_node_dispatch("a", "one")
        self.assertIn("TEST-1", node["prompt"])
        self.assertIn("jira.ps1", node["prompt"])
        self.assertTrue(node["jiraEnabled"])
        self.assertEqual(self.store.get_spec("a")["jira"], self.specs["a"]["jira"])
        self.store.sync_node_job("a", "one", {"status": "completed", "businessResult": outcome("TEST-2")})
        self.assertEqual(self.store.get_node("a", "one")["status"], "failed")
        self.assertEqual(self.store.jira_runs("")["items"][0]["deliveries"], [])

    def test_failed_issue_reclaimed_only_after_confirmed_stop(self):
        self.create("a")
        self.ready("a")
        self.store.prepare_node_dispatch("a", "one")
        self.store.finish_workflow("a", supervisor_status="failed", response=None, error="中断")
        self.create("b", 2)
        self.assertIsNone(self.claim("b")["issueKey"])
        self.claim("b", [])
        self.store.finish_business_termination("b")
        self.store.sync_node_job("a", "one", {"status": "failed", "execution_stopped": True})
        self.create("c", 2)
        self.assertEqual(self.claim("c")["issueKey"], "TEST-1")

    def test_delivery_requires_reference_and_does_not_infer_submission(self):
        self.create("a")
        self.ready("a")
        self.store.prepare_node_dispatch("a", "one")
        self.store.sync_node_job("a", "one", {"status": "completed", "businessResult": outcome()})
        self.store.finish_workflow("a", supervisor_status="completed", response="完成", error=None)
        row = self.store.jira_runs("")["items"][0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["deliveries"][0]["status"], "completed")
        self.create("b", 2)
        self.assertIsNone(self.claim("b")["issueKey"], "已完成工单不会被定时重复领取")
        with self.assertRaisesRegex(ValueError, "提交凭证"):
            parse(json.dumps(outcome(status="submitted")), 2, jira=True)
        self.assertEqual(parse(json.dumps(outcome(status="submitted", reference="r123")), 2, jira=True)["jiraDelivery"]["reference"], "r123")

    def test_no_task_finishes_without_business_step(self):
        self.create("a")
        self.assertEqual(self.claim("a", [])["state"], "no_task")
        self.assertTrue(self.store.finish_business_termination("a"))
        self.assertEqual(self.store.get_node("a", "one")["status"], "skipped")
        self.assertEqual(self.store.machine_claims(), {})

    def test_upgrade_existing_jira_table_preserves_runs_and_cleanup_progress(self):
        self.create("a")
        self.ready("a")
        self.store.cancel_workflow("a")
        original_spec = self.store.get_spec("a")
        with self.store._connect() as db:
            db.execute("ALTER TABLE workflow_jira_runs DROP COLUMN cleanup_attempted_at")
            original_run = dict(db.execute("SELECT * FROM workflow_jira_runs").fetchone())
            original_claim = dict(db.execute("SELECT * FROM jira_issue_claims").fetchone())

        upgraded = WorkflowStore(self.store.path)
        with upgraded._connect() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM workflow_jira_runs").fetchone()),
                {**original_run, "cleanup_attempted_at": None})
            self.assertEqual(dict(db.execute("SELECT * FROM jira_issue_claims").fetchone()), original_claim)
        self.assertEqual(upgraded.get_spec("a"), original_spec)
        self.assertEqual(upgraded.jira_cleanup_candidates(), [{"workflow_id": "a", "executor_id": "e1",
            "launcher_path": original_run["launcher_path"]}])
        upgraded.jira_cleanup_attempted("a")
        with upgraded._connect() as db:
            attempted_at = db.execute("SELECT cleanup_attempted_at FROM workflow_jira_runs").fetchone()[0]
        self.assertIsNotNone(attempted_at)
        upgraded.jira_credentials_cleared("a")

        restarted = WorkflowStore(self.store.path)
        self.assertEqual(restarted.jira_cleanup_candidates(), [])
        with restarted._connect() as db:
            self.assertEqual(db.execute("SELECT cleanup_attempted_at FROM workflow_jira_runs").fetchone()[0], attempted_at)

    def test_old_protocol_remains_strict(self):
        with self.assertRaises(ValueError):
            parse(json.dumps(outcome()), 2)
        self.assertNotIn("jiraDelivery", schema(2)["properties"])
        self.assertIn("jiraDelivery", schema(2, jira=True)["properties"])

    def test_credential_metadata_is_not_returned_in_ledger(self):
        self.create("a")
        self.ready("a")
        value = json.dumps(self.store.jira_runs(""))
        for text in ["fingerprint", "launcherPath", '"account"', '"token"', "a" * 64]:
            self.assertNotIn(text, value)

    def test_manual_retry_only_claims_original_issue(self):
        self.create("a")
        self.ready("a")
        self.store.cancel_workflow("a")
        self.create("b", jiraRetryOf="a")
        self.assertEqual(self.claim("b", ["TEST-2", "TEST-1"])["issueKey"], "TEST-1")

    def test_credentials_clean_only_after_stop_and_block_endpoint_removal(self):
        from agent_registry import AgentRegistry
        self.create("a")
        self.ready("a")
        self.store.prepare_node_dispatch("a", "one")
        self.store.finish_workflow("a", supervisor_status="failed", response=None, error="中断")
        self.assertEqual(self.store.jira_cleanup_candidates(), [])
        self.store.sync_node_job("a", "one", {"status": "failed", "execution_stopped": True})
        self.assertEqual(self.store.jira_cleanup_candidates(), [{"workflow_id": "a", "executor_id": "e1", "launcher_path": self.store.jira_context("a")["launcherPath"]}])
        with self.assertRaisesRegex(ValueError, "尚未确认清理"):
            AgentRegistry(self.store).delete_agent("e1")
        self.store.jira_credentials_cleared("a")
        self.assertEqual(self.store.jira_cleanup_candidates(), [])

    def test_jira_write_rechecks_sidecar_lease(self):
        self.create("a")
        with self.assertRaises(PermissionError):
            self.store.jira_update("a", "claim", {}, sidecar_supervisor_id="s2", lease_token="invalid")


class JiraRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_coalesces_same_task_without_blocking_other_groups(self):
        from jira_routes import jira_capacity
        pending = []
        agents = {key: SimpleNamespace(enabled=True, cwd=r"C:\work", allow_full_access=True,
            capabilities=[role], orchestration_mode="local_db") for key, role in (("s", "supervisor"), ("e", "executor"))}
        gateway = SimpleNamespace(
            store=SimpleNamespace(normalize_spec=lambda spec: spec, machine_claims=lambda: {}, pending_allocation_specs=lambda: pending),
            registry=SimpleNamespace(rows=lambda: {key: {"test_status": "passed"} for key in agents}, config_from_row=lambda key, _: agents[key]),
            _eligible_group_executors=AsyncMock(return_value={"probe": {"e"}}),
            _supervisor_runtime={"s": {"connectionStatus": "online"}})
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway=gateway)),
            json=AsyncMock(return_value={"workflowId": "probe", "jira": {"jql": "project = TEST", "account": "bot"}, "taskDefinitionId": "task"}))
        pending.append({"taskDefinitionId": "other-group-task"})
        self.assertEqual(json.loads((await jira_capacity(request)).body)["capacity"], 1)
        pending.append({"taskDefinitionId": "task"})
        self.assertEqual(json.loads((await jira_capacity(request)).body)["capacity"], 0)

    async def test_executor_parses_extended_schema_and_returns_delivery(self):
        from codex_orchestrator_mcp import AgentConfig, Orchestrator
        from tests.mock_app_server import MockAppServer
        async with MockAppServer(structured_reply=json.dumps(outcome(status="submitted", reference="r123"))) as server:
            orchestrator = Orchestrator()
            orchestrator.agent_provider = lambda: {"worker": AgentConfig.from_dict("worker", {"url": server.url, "cwd": "/work"})}
            job = await orchestrator.dispatch(agent_id="worker", prompt="处理指定工单", output_schema=schema(2, jira=True),
                thread_id=None, cwd=None, write=False, model=None, timeout_sec=30)
            job.result_protocol_version = 2
            job.jira_enabled = True
            await job.completed.wait()
            self.assertEqual(job.status, "completed", job.error)
            self.assertEqual(job.business_result["jiraDelivery"]["reference"], "r123")

    def test_existing_config_and_search_json_contract(self):
        raw = json.dumps({"accounts": {"bot": {"baseUrl": "http://JIRA.test:80/", "token": "fixture-secret", "readOnly": False}}}).encode()
        alias, account = account_from_config(raw, "")
        self.assertEqual(alias, "bot")
        self.assertEqual(account["baseUrl"], "http://jira.test")
        self.assertEqual(issue_keys('{"issues":[{"key":"TEST-1","fields":{"summary":"测试"}}]}'), ["TEST-1"])
        self.assertEqual(issue_keys('[{"key":"TEST-2"}]'), ["TEST-2"])
        for value in ['{"data":[]}', '{"issues":[{"key":"not a key"}]}']:
            with self.assertRaises(ValueError): issue_keys(value)
        with self.assertRaises(ValueError): account_from_config(b'{"accounts":{}}', "")
        self.assertIn("auth override", LAUNCHER)
        self.assertIn("read only", LAUNCHER)
        self.assertEqual(excluding_query('project = TEST OR summary ~ "ORDER BY" ORDER BY created ASC', ["TEST-1"]),
            '(project = TEST OR summary ~ "ORDER BY") AND key NOT IN (TEST-1) ORDER BY created ASC')

    async def test_claim_transfers_credentials_only_by_file_rpc(self):
        secret = "fixture-secret-never-in-prompts"
        files = {str(PureWindowsPath(r"C:\Users\admin\.jira_cli\config.json")): json.dumps({"accounts": {
            "bot": {"baseUrl": "http://jira.test", "token": secret, "readOnly": False}}}).encode()}
        directory = r"C:\Users\worker\AppData\Local\CodexFlowJira" + "\\" + run_directory("run")
        calls = []
        async def request(method, params):
            calls.append((method, params))
            if method == "fs/getMetadata":
                return {"isSymlink": False, "isFile": params["path"] in files, "isDirectory": params["path"] not in files}
            if method == "fs/readFile": return {"dataBase64": base64.b64encode(files[params["path"]]).decode()}
            if method == "fs/writeFile": files[params["path"]] = base64.b64decode(params["dataBase64"]); return {}
            if method == "command/exec":
                script = base64.b64decode(params["command"][-1]).decode("utf-16-le")
                return {"exitCode": 0, "stdout": directory if "Check-Path" in script else '[{"key":"TEST-1"}]'}
            self.fail(method)
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.request.side_effect = request
        store = SimpleNamespace(jira_context=lambda _: {"state": "waiting_claim", "supervisorId": "s", "executorId": "e", "issueKey": None,
                "config": {"jql": "project = TEST", "account": ""}},
            jira_update=lambda _, operation, payload: {"issueKey": "TEST-1", "state": "ready" if operation == "ready" else "claimed"})
        agent = SimpleNamespace(enabled=True, allow_full_access=True, allow_write=True, cwd=r"C:\work", url="ws://fixture")
        orchestrator = SimpleNamespace(get_agent=lambda _: agent, _client_factory=lambda *a, **kw: client, _resolve_agent_token=lambda _: None)
        value = await claim_next(store, orchestrator, "run")
        self.assertEqual(value, {"issueKey": "TEST-1", "state": "ready"})
        self.assertNotIn(secret, json.dumps(value))
        for method, params in calls:
            if method == "command/exec":
                self.assertNotIn(secret, base64.b64decode(params["command"][-1]).decode("utf-16-le"))
        self.assertEqual(json.loads(files[str(PureWindowsPath(directory) / "account.json")])["token"], secret)


if __name__ == "__main__":
    unittest.main()
