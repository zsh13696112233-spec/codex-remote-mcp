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
        for version in (True, 0, 3, "2"):
            with self.assertRaises(ValueError): schema(version)

    def test_unresolved_never_carries_account(self):
        for state in ("empty", "multiple", "unavailable"):
            value = result()
            value["jiraDeveloper"]["status"] = state
            with self.assertRaises(ValueError): validate(value)
            value["jiraDeveloper"].update(accountType="", accountId="")
            self.assertEqual(validate(value)["outcome"], "blocked")
            value["jiraDeveloper"]["detail"] = ""
            with self.assertRaises(ValueError): validate(value)

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
