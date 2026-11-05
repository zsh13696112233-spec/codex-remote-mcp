"""交付协议 v3 的明确无提交及审核返工；无需外部执行服务。"""
import json
import unittest

from tests import test_jira_manual_review as fixtures
from workflow_jira import execution_result, node_instruction, validate_delivery
from workflow_outcomes import schema, validate
from workflow_store import WorkflowStore


class CompletionTests(unittest.TestCase):
    def setUp(self):
        fixtures.ManualReviewTests.setUp(self)
        self.db.execute('UPDATE workflow_jira_runs SET delivery_version=3')
        self.db.execute("ALTER TABLE workflow_nodes ADD COLUMN depends_on_json TEXT DEFAULT '[]'")
        # 存储顺序故意与依赖顺序相反。
        self.db.execute("INSERT INTO workflow_nodes VALUES('run','review','completed','',NULL,0,'[\"one\"]')")
        self.db.execute("INSERT INTO workflow_nodes VALUES('run','one','completed','',NULL,1,'[]')")
        self.receipt('review', 'completed_no_submission')

    def receipt(self, node, status, attempt=1, commits=None):
        self.db.execute('INSERT INTO workflow_jira_deliveries VALUES(?,?,?,?,?,?,?,?,?)',
                        ('run', node, attempt, status, '', '已核对，交接完成',
                         'full' if status == 'submitted' else None, json.dumps(commits or []), 'today'))

    def result(self):
        return execution_result(self.db, 'run')

    def test_explicit_final_confirmation_and_old_versions(self):
        self.assertEqual(self.result()['status'], 'completed_no_submission')
        self.assertIn('交接完成', self.result()['note'])
        self.db.execute('UPDATE workflow_jira_runs SET delivery_version=2')
        self.assertEqual(self.result()['status'], 'abnormal')
        self.db.execute('UPDATE workflow_jira_runs SET delivery_version=1')
        self.assertFalse(self.result()['recorded'])

    def test_intermediate_confirmation_is_not_final(self):
        self.db.execute("UPDATE workflow_jira_deliveries SET status=CASE WHEN node_id='one' THEN 'completed_no_submission' ELSE 'completed' END")
        self.assertEqual(self.result()['status'], 'abnormal')
        self.db.execute("UPDATE workflow_jira_deliveries SET status='completed'")
        self.assertEqual(self.result()['status'], 'abnormal')

    def test_all_leaves_and_receipts_required_for_no_submission(self):
        self.db.execute("INSERT INTO workflow_nodes VALUES('run','parallel','completed','',NULL,2,'[\"one\"]')")
        self.assertEqual(self.result()['status'], 'abnormal')
        self.receipt('parallel', 'completed')
        self.assertEqual(self.result()['status'], 'abnormal')
        self.receipt('parallel', 'completed_no_submission', 2)
        self.assertEqual(self.result()['status'], 'completed_no_submission')

    def test_rework_not_passed_and_uncertainty_not_hidden(self):
        self.receipt('review', 'rework_required', 2)
        self.assertEqual(self.result()['status'], 'rework_required')
        self.receipt('one', 'unknown', 2)
        self.assertEqual(self.result()['status'], 'abnormal')

    def test_latest_attempt_resolves_rework_without_losing_old_commits(self):
        self.receipt('review', 'rework_required', 2)
        self.receipt('review', 'completed_no_submission', 3)
        self.assertEqual(self.result()['status'], 'completed_no_submission')
        commits = [{'repository':'客户端','revision':'42'}]
        self.receipt('one', 'submitted', 2, commits)
        self.receipt('review', 'rework_required', 4)
        self.assertEqual((self.result()['status'], self.result()['commits']), ('rework_required', commits))
        self.receipt('review', 'completed_no_submission', 5)
        self.assertEqual(self.result()['status'], 'fully_submitted')

    def test_failed_unknown_or_unfulfilled_delivery_is_not_passed(self):
        for state in ('unknown', 'not_submitted', 'blocked'):
            self.db.execute('UPDATE workflow_jira_deliveries SET status=? WHERE node_id=?', (state, 'one'))
            self.assertEqual(self.result()['status'], 'abnormal')
        self.db.execute("UPDATE workflow_jira_deliveries SET status='completed' WHERE node_id='one'")
        self.db.execute("UPDATE workflows SET status='failed'")
        self.assertEqual(self.result()['status'], 'abnormal')
        self.db.execute("UPDATE workflows SET status='running'")
        self.assertIsNone(self.result()['status'])

    def test_protocol_requires_reason_no_commits_and_successful_handoff(self):
        for status in ('completed_no_submission', 'rework_required'):
            delivery = {'status':status, 'reference':'', 'summary':'已核对', 'submissionType':None, 'commits':[]}
            self.assertEqual(validate_delivery(delivery, 3), delivery)
            for version in (1, 2):
                with self.assertRaises(ValueError):
                    validate_delivery(delivery, version)
                self.assertNotIn(status, schema(2, jira=True, delivery_version=version)['properties']['jiraDelivery']['properties']['status']['enum'])
            self.assertIn(status, schema(2, jira=True, delivery_version=3)['properties']['jiraDelivery']['properties']['status']['enum'])
            with self.assertRaises(ValueError):
                validate_delivery({**delivery, 'summary':' '}, 3)
            with self.assertRaises(ValueError):
                validate_delivery({**delivery, 'commits':[{'repository':'客户端','revision':'42'}]}, 3)
            value = {'summary':'交接完成', 'outcome':'success', 'reason':'', 'document':None,
                     'jiraComment':{'status':'not_applicable','issueKey':'TEST-1','reference':'','detail':''},
                     'jiraDeveloper':{'status':'not_applicable','accountType':'','accountId':'','displayName':'','detail':''},
                     'jiraDelivery':delivery}
            self.assertEqual(validate(value, 2, jira=True, delivery_version=3), value)
            with self.assertRaises(ValueError):
                validate({**value, 'outcome':'blocked','reason':'未完成交接'}, 2, jira=True, delivery_version=3)

    def test_new_spec_freezes_version_and_prompt_explains_results(self):
        spec = {'workflowId':'test','dispatchMode':'group','groupId':'3b2dd2eb-6fdd-4498-a8e6-98e211b08330',
                'nodes':[{'id':'one','prompt':'任务'}],
                'jira':{'jql':'project = TEST','account':'bot'}, 'jiraDeliveryVersion':3}
        self.assertEqual(WorkflowStore.normalize_spec(spec)['jiraDeliveryVersion'], 3)
        self.assertEqual(WorkflowStore.normalize_spec({**spec,'jiraDeliveryVersion':2})['jiraDeliveryVersion'], 2)
        for column in ('state', 'issue_key', 'launcher_path'):
            self.db.execute(f'ALTER TABLE workflow_jira_runs ADD COLUMN {column} TEXT')
        self.db.execute("UPDATE workflow_jira_runs SET state='ready',issue_key='TEST-1',launcher_path='jira.ps1'")
        text = node_instruction(self.db, 'run')
        self.assertIn('rework_required', text)
        self.assertIn('completed_no_submission', text)
        self.assertIn('代码留待后续审核或提交', text)
