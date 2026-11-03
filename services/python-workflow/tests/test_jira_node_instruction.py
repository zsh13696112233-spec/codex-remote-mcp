import sqlite3
import unittest

from workflow_jira import node_instruction


class JiraNodeInstructionTests(unittest.TestCase):
    def test_step_handoff_does_not_require_a_different_jira_assignee(self):
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE workflow_jira_runs (workflow_id, state, issue_key, launcher_path)")
            db.execute("INSERT INTO workflow_jira_runs VALUES (?, ?, ?, ?)",
                       ("handoff", "ready", "TEST-1", r"C:\work\jira.ps1"))
            prompt = node_instruction(db, "handoff")

        self.assertIn("本轮唯一工单：TEST-1", prompt)
        self.assertIn("不因缺少下一步骤的接收账号而阻断", prompt)
        self.assertIn("仅在当前步骤职责明确要求时转交 Jira 负责人", prompt)
        self.assertIn("接收人可以与本轮领取账号相同", prompt)
        self.assertIn("不擅自选择接收人或虚报交接成功", prompt)
        self.assertNotIn("接收人不明确、转交失败或回查无法确认时，按既有结果协议报告遇阻", prompt)
        self.assertNotIn("且不再是本轮领取账号", prompt)
