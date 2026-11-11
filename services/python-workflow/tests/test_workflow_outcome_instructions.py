import unittest

from workflow_outcomes import instruction


class OutcomeInstructionTests(unittest.TestCase):
    def test_automatic_resolves_problems_before_terminal_cleanup(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                prompt = instruction("w", "n", False, version, advance_mode="automatic")
                self.assertLess(prompt.index("最终输出前检查"), prompt.index("正式阻断收尾的业务操作"))
                for text in ("不能认定完成不等于不能继续执行", "实际修正调用", "不无限重试",
                             "已成功不重复执行", "不降低证据标准", "不授权恢复已结束的工作流",
                             "保留 SOP 的硬性停止条件", "最小外部恢复条件",
                             "平台不指定业务动作、接收人或执行顺序", "存在该标记则复用"):
                    self.assertIn(text, prompt)
                self.assertNotIn("blocked（必要条件不满足", prompt)

    def test_semi_automatic_and_closing_keep_existing_instructions(self):
        for version in (1, 2, 4, 5):
            for waiting in (False, True):
                with self.subTest(version=version, waiting=waiting):
                    original = instruction("w", "n", True, version, defer_blocked=waiting)
                    semi = instruction("w", "n", True, version, defer_blocked=waiting,
                                       advance_mode="semi_automatic")
                    self.assertEqual(original, semi)
                    self.assertNotIn("【全自动执行与结束条件】", semi)

    def test_sop_conclusion_and_submission_report_survive_all_prompt_branches(self):
        for version in (1, 2, 3, 4, 5):
            for capture in (False, True):
                for waiting in (False, True):
                    with self.subTest(version=version, capture=capture, waiting=waiting):
                        prompt = instruction("w", "n", capture, version, defer_blocked=waiting)
                        self.assertIn("沿用 SOP 原有的业务结论和说明格式", prompt)
                        self.assertIn("结论名称与正文结构由当前 SOP 决定", prompt)
                        self.assertIn("首句写简短业务结论，涉及提交时再说明本轮实际提交结果", prompt)
                        self.assertIn("不把历史版本当作本轮提交", prompt)
                        self.assertIn("不从流程阻断推断未提交", prompt)
                        self.assertIn("不涉及提交的任务不硬加 SVN 状态", prompt)
                        self.assertIn("不把流程阻断等同于审核不通过或返工", prompt)
                        self.assertIn("审核通过后提交或流转失败仍保留审核结论", prompt)
                        self.assertLess(prompt.index("首句写"), prompt.index("需要人工处理的事项紧接结论"))
                        self.assertLess(prompt.index("需要人工处理的事项紧接结论"), prompt.index("再按 SOP 原有格式"))
                        self.assertNotIn("四个短段", prompt)
                        self.assertNotIn("优先于职责中的旧汇报顺序", prompt)
                        for heading in ("【处理结论】", "【人工待办】", "【继续处理的条件】", "【当前状态】"):
                            self.assertNotIn(heading, prompt)

    def test_jira_business_policy_belongs_to_sop_in_all_prompt_branches(self):
        for version in (1, 2, 3, 4, 5):
            for mode in (None, "automatic", "semi_automatic"):
                for waiting in (False, True):
                    with self.subTest(version=version, mode=mode, waiting=waiting):
                        prompt = instruction("w", "n", False, version,
                                             advance_mode=mode, defer_blocked=waiting)
                        self.assertIn("平台不指定业务动作、接收人或执行顺序", prompt)
                        self.assertIn("该字段不要求新增评论", prompt)
                        for policy in ("Public.Jira.Manager", "Natural Person", "唯一开发人作为目标负责人",
                                       "评论未确认成功不得转交", "仅更新负责人", "Jira 状态和处理角色保持不变",
                                       "Jira 阻断评论也以", "之后必须在原授权内尝试转交负责人",
                                       "已获授权的 Jira 评论沿用"):
                            self.assertNotIn(policy, prompt)
                        if waiting:
                            self.assertIn("等待人工处理、本轮尚未结束", prompt)
                            self.assertIn("禁止执行停止评论和负责人转交", prompt)
                            self.assertNotIn("SOP-STOP-", prompt)
                            self.assertNotIn("本次返回 blocked 为正式阻断结束", prompt)
                        else:
                            self.assertIn("本轮因阻断停止，补齐条件不会自动恢复本轮", prompt)
                            self.assertIn("存在该标记则复用", prompt)
                            self.assertIn("提供标记不构成新增评论的要求或授权", prompt)
