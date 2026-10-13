"""无需外部服务的开发人协议和中央持久化回归。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from workflow_outcomes import validate, parse, schema
from workflow_store import WorkflowStore


def result():
    return {"summary": "已停止", "outcome": "blocked", "reason": "缺少源码", "document": None,
            "jiraComment": {"status": "unknown", "issueKey": "TEST-1", "reference": "", "detail": "角色修改失败；备注未确认"},
            "jiraDeveloper": {"status": "resolved", "accountType": "key", "accountId": "dev-1", "displayName": "开发人", "detail": "已读取"}}


class DeveloperOutcomeTests(unittest.TestCase):
    def test_model_blocked_without_jira_is_normalized_without_changing_decision(self):
        value = result()
        value["jiraComment"].update(status="not_applicable", issueKey="", reference="", detail="未锁定工单")
        value["jiraDeveloper"].update(status="not_applicable", accountType="", accountId="", displayName="", detail="未锁定工单，无法读取开发人")
        raw = json.dumps(value)
        with self.assertLogs("workflow_outcomes", level="WARNING") as logs:
            parsed = parse(raw)
        self.assertEqual(parsed["jiraDeveloper"]["status"], "unavailable")
        self.assertEqual(parsed["outcome"], "blocked")
        self.assertEqual(parsed["reason"], value["reason"])
        self.assertEqual(parsed["jiraComment"], value["jiraComment"])
        self.assertEqual(json.loads(raw)["jiraDeveloper"]["status"], "not_applicable")
        self.assertIn("规范化", logs.output[0])
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory) / "state.db")
            from tests.registry_fixtures import seed_agents
            seed_agents(store, {"local": {"capabilities": ["supervisor", "executor"]}})
            store.create_workflow({"workflowId": "normalized", "supervisorAgentId": "local", "nodes": [
                {"id": "a", "executor": {"type": "local", "agentId": "local"}, "prompt": "检查"}]})
            store.claim_next_workflow("local")
            store.prepare_node_dispatch("normalized", "a")
            store.sync_node_job("normalized", "a", {"status": "completed", "businessResult": parsed})
            termination = WorkflowStore(store.path).get_workflow("normalized")["termination"]
            self.assertEqual(termination["outcome"], "blocked")
            self.assertEqual(termination["jiraDeveloper"]["status"], "unavailable")

    def test_normalization_is_narrow_and_full_validation_still_applies(self):
        base = result()
        base["jiraDeveloper"].update(status="not_applicable", accountType="", accountId="", displayName="", detail="无法读取")
        for changes in ({"accountId": "someone"}, {"accountType": "key"}, {"displayName": "某人"}, {"detail": None}, {"extra": "x"}):
            value = copy.deepcopy(base)
            value["jiraDeveloper"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse(json.dumps(value))
        for outcome in ("success", "no_task"):
            value = copy.deepcopy(base)
            value["outcome"] = outcome
            self.assertEqual(parse(json.dumps(value))["jiraDeveloper"]["status"], "not_applicable")
        for key in ("summary", "reason"):
            value = copy.deepcopy(base)
            value[key] = ""
            with self.assertRaises(ValueError): parse(json.dumps(value))
        with self.assertRaises(ValueError): parse(json.dumps(base), 1)
        with self.assertRaises(ValueError): parse("{")

    def test_versions_are_strict_and_separate(self):
        value = result()
        self.assertEqual(parse(json.dumps(value)), value)
        old = copy.deepcopy(value)
        del old["jiraDeveloper"]
        self.assertEqual(validate(old, 1), old)
        with self.assertRaises(ValueError): validate(old, 2)
        with self.assertRaises(ValueError): validate(value, 1)
        self.assertNotIn("jiraDeveloper", schema(1)["required"])
        self.assertIn("jiraDeveloper", schema(2)["required"])
        for version in (True, 0, 4, "2"):
            with self.assertRaises(ValueError): schema(version)

    def test_unresolved_never_carries_account(self):
        for state in ("empty", "multiple", "unavailable"):
            value = result()
            value["jiraDeveloper"]["status"] = state
            with self.assertRaises(ValueError): validate(value)
            value["jiraDeveloper"].update(accountType="", accountId="")
            self.assertEqual(validate(value)["outcome"], "blocked")
            value["jiraDeveloper"]["detail"] = ""
            self.assertEqual(validate(value)["jiraDeveloper"]["detail"], "")

    def test_developer_absence_and_blank_explanation_preserve_business_outcome(self):
        for outcome in ("success", "no_task", "blocked"):
            for state in ("empty", "multiple", "unavailable", "not_applicable"):
                for detail in ("", " "):
                    with self.subTest(outcome=outcome, state=state, detail=detail):
                        value = result()
                        value["outcome"] = outcome
                        value["jiraDeveloper"].update(status=state, accountType="", accountId="", displayName="", detail=detail)
                        parsed = parse(json.dumps(value))
                        self.assertEqual(parsed["outcome"], outcome)
                        expected = "unavailable" if outcome == "blocked" and state == "not_applicable" else state
                        self.assertEqual(parsed["jiraDeveloper"]["status"], expected)
                        self.assertEqual(parsed["jiraDeveloper"]["accountId"], "")
                        self.assertEqual(parsed["jiraDeveloper"]["detail"], detail)
                        self.assertEqual(validate(parsed), parsed)

    def test_empty_developer_success_survives_central_storage_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory) / "state.db")
            from tests.registry_fixtures import seed_agents
            seed_agents(store, {"local": {"capabilities": ["supervisor", "executor"]}})
            store.create_workflow({"workflowId": "empty-success", "supervisorAgentId": "local", "nodes": [
                {"id": "a", "executor": {"type": "local", "agentId": "local"}, "prompt": "检查"}]})
            store.claim_next_workflow("local")
            store.prepare_node_dispatch("empty-success", "a")
            value = result()
            value.update(outcome="success", reason="", summary="方案已生成并回读")
            value["jiraComment"].update(status="not_applicable", reference="", detail="本阶段成功")
            value["jiraDeveloper"].update(status="not_applicable", accountType="", accountId="", displayName="", detail="")
            store.sync_node_job("empty-success", "a", {"status": "completed", "businessResult": value})
            view = WorkflowStore(store.path).get_workflow("empty-success")
            self.assertIsNone(view["termination"])
            self.assertEqual(view["nodes"][0]["status"], "completed")
            self.assertEqual(view["nodes"][0]["response"], value["summary"])

    def test_rejects_invalid_identity_and_extra_fields(self):
        for changes in ({"accountId": ""}, {"accountType": "displayName"}, {"accountId": "x"*257}, {"status": "not_applicable"}, {"assignee": "wrong"}):
            value = result()
            value["jiraDeveloper"].update(changes)
            with self.assertRaises(ValueError): validate(value)

    def test_central_freezing_persistence_and_old_version(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory)/"state.db")
            spec = {"workflowId": "developer-test", "supervisorAgentId": "local", "nodes": [
                {"id": "a", "executor": {"type": "local", "agentId": "local"}, "prompt": "检查", "dependsOn": []}]}
            from tests.registry_fixtures import seed_agents
            seed_agents(store, {"local": {"capabilities": ["supervisor", "executor"]}})
            store.create_workflow(spec)
            self.assertEqual(store.get_spec("developer-test")["resultProtocolVersion"], 2)
            store.claim_next_workflow("local")
            store.prepare_node_dispatch("developer-test", "a")
            store.sync_node_job("developer-test", "a", {"status": "completed", "businessResult": result()})
            reopened = WorkflowStore(store.path)
            self.assertEqual(reopened.get_workflow("developer-test")["termination"]["jiraDeveloper"]["accountId"], "dev-1")
            reopened.recover_active_workflows_after_restart()
            self.assertEqual(reopened.get_workflow("developer-test")["status"], "failed")
            spec.update(workflowId="old-version", resultProtocolVersion=1)
            normalized = WorkflowStore.normalize_spec(spec)
            self.assertEqual(normalized["resultProtocolVersion"], 1)
            store.create_workflow(normalized)
            self.assertEqual(store.get_spec("old-version")["resultProtocolVersion"], 1)
            store.claim_next_workflow("local")
            store.prepare_node_dispatch("old-version", "a")
            old = result(); del old["jiraDeveloper"]
            store.sync_node_job("old-version", "a", {"status": "completed", "businessResult": old})
            self.assertNotIn("jiraDeveloper", store.get_workflow("old-version")["termination"])
