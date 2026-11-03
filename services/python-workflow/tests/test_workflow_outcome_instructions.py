import unittest

from workflow_outcomes import instruction


class OutcomeInstructionTests(unittest.TestCase):
    def test_sop_conclusion_and_submission_report_survive_all_prompt_branches(self):
        for version in (1, 2, 3, 4, 5):
            for capture in (False, True):
                for waiting in (False, True):
                    with self.subTest(version=version, capture=capture, waiting=waiting):
                        prompt = instruction("w", "n", capture, version, defer_blocked=waiting)
                        self.assertIn("沿用 SOP 原有的业务结论和说明格式", prompt)
                        self.assertIn("审核步骤保留“审核结论：”开头", prompt)
                        self.assertIn("首句只写简短业务结论和本轮实际提交结果", prompt)
                        self.assertIn("不把历史版本当作本轮提交", prompt)
                        self.assertIn("不从流程阻断推断未提交", prompt)
                        self.assertIn("不涉及提交的任务不硬加 SVN 状态", prompt)
                        self.assertIn("不把流程阻断等同于审核不通过或返工", prompt)
                        self.assertIn("审核通过后提交或流转失败仍保留审核结论", prompt)
                        self.assertLess(prompt.index("首句只写"), prompt.index("需要人工处理的事项紧接结论"))
                        self.assertLess(prompt.index("需要人工处理的事项紧接结论"), prompt.index("再按 SOP 原有格式"))
                        self.assertNotIn("四个短段", prompt)
                        self.assertNotIn("优先于职责中的旧汇报顺序", prompt)
                        for heading in ("【处理结论】", "【人工待办】", "【继续处理的条件】", "【当前状态】"):
                            self.assertNotIn(heading, prompt)

    def test_report_format_does_not_authorize_comments_during_waiting(self):
        for version in (1, 2, 3, 4, 5):
            with self.subTest(version=version):
                prompt = instruction("w", "n", False, version)
                self.assertIn("Jira 阻断评论也以 SOP 的业务结论和本轮实际提交结果开头", prompt)
                self.assertIn("本轮因阻断停止，补齐条件不会自动恢复本轮", prompt)
                self.assertIn("不因此新增评论或业务操作", prompt)
                self.assertIn("评论先于负责人转交", prompt)
                self.assertIn("不得提前宣称转交成功或把未来操作写成已经尝试", prompt)
                self.assertIn("实际转交结果在最终 summary 和 jiraComment.detail 说明", prompt)
                self.assertIn("新评论将标记独立放在正文末尾", prompt)
                self.assertIn("存在该标记则复用", prompt)
                self.assertIn("评论未确认成功不得转交", prompt)
                self.assertIn("不为更新汇报格式修改或重复追加已有评论", prompt)
                waiting = instruction("w", "n", False, version, defer_blocked=True)
                self.assertIn("等待人工处理、本轮尚未结束", waiting)
                self.assertIn("禁止执行停止评论和负责人转交", waiting)
                self.assertNotIn("本次为正式阻断收尾", waiting)
                self.assertNotIn("先写入阻断评论并回查确认成功", waiting)
                self.assertNotIn("SOP-STOP-", waiting)
