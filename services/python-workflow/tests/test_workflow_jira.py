import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jira_runtime import account_from_config, claim_next, issue_keys, excluding_query, INITIALIZE_SCRIPT, LAUNCHER
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

    def test_machine_remarks_are_current_and_survive_logical_deletion(self):
        self.create("remarks")
        original_spec = self.store.get_spec("remarks")
        for remark, deleted_at in [("<监督>", None), ("更新备注", "2026-10-29")]:
            with self.store._connect() as db:
                db.execute("UPDATE registered_agents SET remark=?,deleted_at=? WHERE id='s1'",
                           (remark, deleted_at))
            ledger = self.store.jira_runs("")["items"][0]
            allocation = self.store.get_workflow("remarks")["allocation"]
            for value in (ledger, allocation):
                self.assertEqual(value["supervisorRemark"], remark)
                self.assertEqual(value["executorRemark"], "")
                self.assertEqual(value["supervisorAgentId"], "s1")
                self.assertEqual(value["executorAgentId"], "e1")
            self.assertEqual(allocation["supervisorDeleted"], deleted_at is not None)
        self.assertEqual(self.store.get_spec("remarks"), original_spec)

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
        self.assertIn("不要自行添加 --user", node["prompt"])
        self.assertIn("不绕过入口直接调用 jira_cli", node["prompt"])
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
        with self.assertRaisesRegex(ValueError, "提交凭证"):
            parse(json.dumps(outcome(status="submitted")), 2, jira=True)
        self.assertEqual(parse(json.dumps(outcome(status="submitted", reference="r123")), 2, jira=True)["jiraDelivery"]["reference"], "r123")

    def test_completed_issue_can_return_after_reassignment_and_keeps_history(self):
        self.create("a")
        self.ready("a")
        self.store.prepare_node_dispatch("a", "one")
        self.store.sync_node_job("a", "one", {"status": "completed", "businessResult": outcome()})
        self.store.finish_workflow("a", supervisor_status="completed", response="完成", error=None)
        original = self.store.jira_runs("")["items"][0]
        original_spec = self.store.get_spec("a")
        # 转交后查询不再返回此工单；返工转回后，新的查询再次返回它。
        self.create("b", 2)
        self.assertEqual(self.claim("b", [])["state"], "no_task")
        self.assertTrue(self.store.finish_business_termination("b"))
        self.create("c", 2)
        self.assertEqual(self.claim("c")["issueKey"], "TEST-1")
        self.assertEqual(self.claim("c")["issueKey"], "TEST-1")
        history = next(row for row in self.store.jira_runs("")["items"] if row["workflowId"] == "a")
        self.assertEqual(history, original)
        self.assertEqual(self.store.get_spec("a"), original_spec)
        self.create("d")
        self.assertEqual(self.claim("d")["state"], "contended")

    def test_completed_issue_with_unconfirmed_execution_cannot_be_reclaimed(self):
        self.create("a")
        self.ready("a")
        self.store.prepare_node_dispatch("a", "one")
        # 模拟历史终态但远端执行尚未核实，整项机器占用必须继续生效。
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET status='completed' WHERE workflow_id='a'")
        self.assertTrue(self.store.machine_claims())
        self.create("b", 2)
        self.assertEqual(self.claim("b")["state"], "contended")

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

    def test_password_accounts_and_invalid_credentials(self):
        def config(auth):
            return json.dumps({"accounts": {"bot": {"baseUrl": "http://jira.test", "readOnly": False, **auth}}}).encode()
        auth = {"username": "reviewer", "password": "密码含空格 !'\""}
        alias, account = account_from_config(config(auth), "bot")
        self.assertEqual(alias, "bot")
        self.assertEqual(account, {"baseUrl": "http://jira.test", "readOnly": False, **auth})
        self.assertNotIn("token", account)
        for invalid in ({}, {"username": "u"}, {"password": "p"},
                        {"token": "t", **auth}, {"token": "t", "username": "u"},
                        {"username": "u", "password": ""}, {"username": "u", "password": "p\n"},
                        {"token": False, **auth}, {"token": ["t"]}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError) as error:
                    account_from_config(config(invalid), "bot")
                self.assertNotIn(auth["password"], str(error.exception))
        with self.assertRaises(ValueError):
            account_from_config(config(auth), "missing")
        with self.assertRaises(ValueError):
            account_from_config(b'{"accounts":{"bot":[]}}', "bot")

    def test_items_pages_and_invalid_empty_pages(self):
        page = {"items": [{"key": "TEST-1"}, {"key": "TEST-1"}],
                "total": 50, "returned": 2, "start_at": 0, "max_results": 50}
        self.assertEqual(issue_keys(json.dumps(page)), ["TEST-1"])
        empty = {"items": [], "total": 0, "returned": 0, "start_at": 0, "max_results": 50}
        self.assertEqual(issue_keys(json.dumps(empty)), [])
        for change in ({"start_at": 50}, {"returned": 1}, {"total": 1},
                       {"items": None}, {"items": [{}], "returned": 1, "total": 1},
                       {"start_at": False}, {"total": "0"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                issue_keys(json.dumps({**empty, **change}))

    async def test_claim_transfers_credentials_only_by_file_rpc(self):
        await self._check_credential_transfer({"token": "fixture-secret-never-in-prompts"}, '[{"key":"TEST-1"}]')

    async def test_claim_transfers_password_credentials_only_by_file_rpc(self):
        await self._check_credential_transfer(
            {"username": "fixture-reviewer", "password": "fixture-password-not-in-prompts"},
            '{"items":[{"key":"TEST-1"}],"total":1,"returned":1,"start_at":0}')

    async def test_claim_preserves_explicit_alias_among_multiple_accounts(self):
        await self._check_credential_transfer({"token": "selected-account-secret"}, '[{"key":"TEST-1"}]', selected_alias="planner")

    async def _check_credential_transfer(self, credentials, search_output, selected_alias=""):
        alias = selected_alias or "bot"
        files = {str(PureWindowsPath(r"C:\Users\admin\.jira_cli\config.json")): json.dumps({"accounts": {
            alias: {"baseUrl": "http://jira.test", **credentials, "readOnly": False},
            **({"reviewer": {"baseUrl": "http://jira.test", "token": "unselected-secret", "readOnly": False}} if selected_alias else {})}}).encode()}
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
                return {"exitCode": 0, "stdout": directory if "Check-Path" in script else search_output}
            self.fail(method)
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.request.side_effect = request
        store = SimpleNamespace(jira_context=lambda _: {"state": "waiting_claim", "supervisorId": "s", "executorId": "e", "issueKey": None,
                "config": {"jql": "project = TEST", "account": selected_alias}},
            jira_update=lambda _, operation, payload: {"issueKey": "TEST-1", "state": "ready" if operation == "ready" else "claimed"})
        agent = SimpleNamespace(enabled=True, allow_full_access=True, allow_write=True, cwd=r"C:\work", url="ws://fixture")
        orchestrator = SimpleNamespace(get_agent=lambda _: agent, _client_factory=lambda *a, **kw: client, _resolve_agent_token=lambda _: None)
        value = await claim_next(store, orchestrator, "run")
        self.assertEqual(value, {"issueKey": "TEST-1", "state": "ready"})
        for secret in credentials.values():
            self.assertNotIn(secret, json.dumps(value))
        for method, params in calls:
            if method == "command/exec":
                script = base64.b64decode(params["command"][-1]).decode("utf-16-le")
                for secret in credentials.values():
                    self.assertNotIn(secret, script)
                self.assertIn("GetEnvironmentVariable('Path','User')", script)
                self.assertIn("$OutputEncoding", script)
        transferred = json.loads(files[str(PureWindowsPath(directory) / "account.json")])
        self.assertEqual(transferred, {"baseUrl": "http://jira.test", "readOnly": False, **credentials, "accountAlias": alias})
        self.assertEqual(files[str(PureWindowsPath(directory) / "jira.ps1")], b"\xef\xbb\xbf" + LAUNCHER.encode())


def launcher_probe_script(credentials, arguments, *, read_only=False, account_alias="fixed-planner"):
    """真实 PowerShell 执行入口；用内存函数代替账号文件和 Jira，不读写远端文件。"""
    def literal(value):
        return "'" + value.replace("'", "''") + "'"
    account = {"baseUrl": "https://jira.test", "readOnly": read_only, **credentials}
    expected = ["--base-url", account["baseUrl"]]
    if account_alias is not None:
        account["accountAlias"] = account_alias
        expected += ["--user", account_alias]
    if "token" in credentials:
        expected += ["--token", credentials["token"]]
        secrets = [credentials["token"]]
    else:
        expected += ["--username", credentials["username"], "--password", credentials["password"]]
        secrets = [credentials["password"], base64.b64encode(
            (credentials["username"] + ":" + credentials["password"]).encode()).decode()]
    checks = "($args.Count -eq " + str(len(expected + arguments)) + ") -and " + " -and ".join("($args[" + str(i) + "] -ceq " + literal(value) + ")"
                           for i, value in enumerate(expected + arguments))
    return ("function Get-Content { param($LiteralPath,[switch]$Raw,$Encoding) "
            + literal(json.dumps(account, ensure_ascii=False)) + " }\n"
            "function jira_cli { if (!(" + checks + ")) { throw 'unexpected arguments' }; "
            "Write-Output 'AUTH_OK'; " + "; ".join("Write-Output " + literal(value) for value in secrets)
            + "; $global:LASTEXITCODE=0 }\n"
            + "& {\n$PSScriptRoot='C:\\Windows'\n" + LAUNCHER + "\n} " + " ".join(literal(arg) for arg in arguments))


class JiraLauncherTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh") and os.name == "nt", "需要 Windows PowerShell 7 权限模块")
    def test_initialize_with_foreign_module_search_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = str(Path(temporary) / "jira").replace("'", "''")
            script = ("$ErrorActionPreference='Stop'\n"
                      "$env:PSModulePath='C:\\missing-module-directory'\n"
                      "Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Management')\n"
                      "Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Utility')\n"
                      "function Check-Path($path) { if (!(Test-Path -LiteralPath $path)) { throw 'missing' } }\n"
                      "function Get-Command { return @{Path='fixture-cli.exe'} }\n"
                      "$root='" + root + "'; $target=Join-Path $root 'run'\n"
                      + INITIALIZE_SCRIPT + "\n" + INITIALIZE_SCRIPT
                      + "\n$verified=Get-Acl -LiteralPath $target\n"
                      "if (!$verified.AreAccessRulesProtected) { throw 'inherited permissions' }\n"
                      "foreach ($rule in $verified.Access) { if ($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]) -ne $sid) { throw 'unexpected principal' } }\n")
            result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive",
                "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()],
                capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertTrue((Path(temporary) / "jira" / "run").is_dir())

    @unittest.skipUnless(shutil.which("powershell.exe") or shutil.which("pwsh"), "需要真实 PowerShell")
    def test_launcher_auth_redaction_and_restrictions(self):
        shell = shutil.which("pwsh") or shutil.which("powershell.exe")
        for credentials in ({"token": "fixture-token"}, {"username": "reviewer", "password": "p"}):
            for account_alias in ("fixed-planner", None):
                for arguments, read_only, success in (
                        (["issue", "get", "TEST-1"], False, True),
                        (["issue", "comment", "TEST-1", "text"], False, True),
                        (["--user=other-reviewer", "issue", "comment", "TEST-1", "text"], False, False),
                        (["--user", "fixed-planner", "issue", "get", "TEST-1"], False, False),
                        (["issue", "get", "TEST-1"], True, True),
                        (["--token", "override", "issue", "get", "TEST-1"], False, False),
                        (["issue", "search", "project = TEST"], False, False),
                        (["issue", "comment", "TEST-1", "text"], True, False)):
                    with self.subTest(credentials=list(credentials), arguments=arguments, read_only=read_only, alias=account_alias):
                        script = launcher_probe_script(credentials, arguments, read_only=read_only, account_alias=account_alias)
                        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                            base64.b64encode(script.encode("utf-16-le")).decode()], capture_output=True, timeout=30)
                        output = result.stdout.decode("utf-8-sig")
                        self.assertEqual(result.returncode, 0 if success else 1, output)
                        self.assertEqual("AUTH_OK" in output, success)
                        if success:
                            self.assertGreaterEqual(output.count("[已隐藏凭据]"), 1)
                            secret = credentials.get("token", credentials.get("password"))
                            self.assertNotIn(secret, output)
                            if "password" in credentials:
                                encoded = base64.b64encode((credentials["username"] + ":" + secret).encode()).decode()
                                self.assertNotIn(encoded, output)


if __name__ == "__main__":
    unittest.main()
