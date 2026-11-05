"""新交付协议与历史兼容回归。"""
import json
import unittest
from tests import test_workflow_jira as fixtures
from tests.test_workflow_jira import outcome
from workflow_jira import execution_result, validate_delivery
from workflow_outcomes import schema, validate


class DeliveryTests(unittest.TestCase):
    setUp = fixtures.JiraStoreTests.setUp
    create = fixtures.JiraStoreTests.create
    ready = fixtures.JiraStoreTests.ready
    claim = fixtures.JiraStoreTests.claim
    def test_new_protocol_and_old_snapshot(self):
        value = outcome(status='submitted', reference='r42')
        validate(value, 2, jira=True)
        with self.assertRaises(ValueError):
            validate(value, 2, jira=True, delivery_version=2)
        value['jiraDelivery'].update(submissionType='limited', commits=[{'repository':'客户端','revision':'r42'}])
        validate(value, 2, jira=True, delivery_version=2)
        from workflow_gateway import _sidecar_job_snapshot
        self.assertEqual(_sidecar_job_snapshot({'status':'completed','businessResult':value})['businessResult']['jiraDelivery'], value['jiraDelivery'])
        self.assertIn('commits', schema(2, jira=True, delivery_version=2)['properties']['jiraDelivery']['required'])
        with self.assertRaises(ValueError):
            validate(value, 2, jira=True)
        value['jiraDelivery']['commits'] = []
        with self.assertRaises(ValueError):
            validate_delivery(value['jiraDelivery'], 2)

    def test_persisted_result_and_conflict(self):
        self.create('delivery', jiraDeliveryVersion=2)
        self.ready('delivery')
        with self.store._connect() as db:
            self.assertEqual(execution_result(db, 'delivery')['status'], None)
            commits = [{'repository':'客户端','revision':'r42'}, {'repository':'服务端','revision':'r7'}]
            db.execute("INSERT INTO workflow_jira_deliveries (workflow_id,node_id,attempt,status,reference,summary,recorded_at,submission_type,commits_json) VALUES(?,?,?,?,?,?,?,?,?)", ('delivery','one',1,'submitted','r42,r7','需补验','2026-01-01','limited',json.dumps(commits)))
            db.execute("UPDATE workflows SET status='completed' WHERE workflow_id='delivery'")
            result = execution_result(db, 'delivery')
            self.assertEqual(result['status'], 'limited_submitted')
            self.assertEqual(result['commits'], commits)
            db.execute("UPDATE workflow_jira_deliveries SET submission_type='full'")
            self.assertEqual(execution_result(db, 'delivery')['status'], 'fully_submitted')
            db.execute("UPDATE workflows SET status='failed',termination_json=? WHERE workflow_id='delivery'", (json.dumps({'outcome':'blocked'}),))
            self.assertEqual(execution_result(db, 'delivery')['status'], 'abnormal')
            db.execute("DELETE FROM workflow_jira_deliveries")
            self.assertEqual(execution_result(db, 'delivery')['status'], 'blocked')

    def test_new_result_survives_actual_save_and_duplicate(self):
        self.create('saved', jiraDeliveryVersion=2)
        self.ready('saved')
        node = self.store.prepare_node_dispatch('saved', 'one')
        self.assertEqual(node['jiraDeliveryVersion'], 2)
        value = outcome(status='submitted', reference='r42')
        value['jiraDelivery'].update(submissionType='full', commits=[{'repository':'客户端','revision':'r42'}])
        snapshot = {'status':'completed', 'businessResult':value}
        self.store.sync_node_job('saved', 'one', snapshot)
        self.store.sync_node_job('saved', 'one', snapshot)
        self.store.finish_workflow('saved', supervisor_status='completed', response='完成', error=None)
        result = self.store.jira_runs('')['items'][0]['executionResult']
        self.assertEqual(result['status'], 'fully_submitted')
        self.assertEqual(len(result['commits']), 1)

    def test_manual_cancel_and_interruption(self):
        self.create('cancelled', jiraDeliveryVersion=2)
        self.store.cancel_workflow('cancelled')
        with self.store._connect() as db:
            self.assertIsNone(execution_result(db, 'cancelled')['status'])
            db.execute("DELETE FROM workflow_events WHERE workflow_id='cancelled' AND event_type='workflow.cancelled'")
            self.assertEqual(execution_result(db, 'cancelled')['status'], 'abnormal')

    def test_completion_receipts_persist_and_old_frozen_protocol_rejects_them(self):
        from workflow_gateway import _sidecar_job_snapshot
        for status in ('completed_no_submission', 'rework_required'):
            for version in (2, 3):
                wid = status + str(version)
                self.create(wid, jiraDeliveryVersion=version)
                self.ready(wid)
                self.store.prepare_node_dispatch(wid, 'one')
                value = outcome(status=status)
                value['jiraDelivery'].update(submissionType=None, commits=[])
                snapshot = _sidecar_job_snapshot({'status':'completed','businessResult':value})
                self.store.sync_node_job(wid, 'one', snapshot)
                if version == 3:
                    self.store.finish_workflow(wid, supervisor_status='completed', response='交接完成', error=None)
                    with self.store._connect() as db:
                        self.assertEqual(execution_result(db, wid)['status'], status)
                else:
                    with self.store._connect() as db:
                        self.assertEqual(db.execute('SELECT status FROM workflow_nodes WHERE workflow_id=?', (wid,)).fetchone()[0], 'failed')
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM workflow_jira_deliveries WHERE workflow_id=?', (wid,)).fetchone()[0], 0)
                    self.store.finish_workflow(wid, supervisor_status='failed', response='', error='旧协议拒绝新回执')

    def test_legacy_database_upgrade_keeps_receipt(self):
        from workflow_jira import initialize
        self.create('legacy')
        with self.store._connect() as db:
            db.execute('ALTER TABLE workflow_jira_deliveries DROP COLUMN submission_type')
            db.execute('ALTER TABLE workflow_jira_deliveries DROP COLUMN commits_json')
            db.execute('ALTER TABLE workflow_jira_runs DROP COLUMN delivery_version')
            db.execute("INSERT INTO workflow_jira_deliveries VALUES('legacy','one',1,'submitted','r1','历史','2026-01-01')")
        with self.store._connect() as db:
            initialize(db)
        with self.store._connect() as db:
            initialize(db)
            row = db.execute("SELECT reference,submission_type FROM workflow_jira_deliveries").fetchone()
            self.assertEqual(tuple(row), ('r1', None))
            self.assertFalse(execution_result(db, 'legacy')['recorded'])


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_schema_parses_in_executor_and_central_boundary(self):
        from codex_orchestrator_mcp import AgentConfig, Orchestrator
        from tests.mock_app_server import MockAppServer
        value = outcome(status='submitted', reference='r42')
        value['jiraDelivery'].update(submissionType='limited', commits=[{'repository':'客户端','revision':'r42'}])
        async with MockAppServer(structured_reply=json.dumps(value)) as server:
            runner = Orchestrator()
            runner.agent_provider = lambda: {'worker': AgentConfig.from_dict('worker', {'url':server.url,'cwd':'/work'})}
            job = await runner.dispatch(agent_id='worker', prompt='处理指定工单', output_schema=schema(2,jira=True,delivery_version=2), thread_id=None,cwd=None,write=False,model=None,timeout_sec=30)
            job.result_protocol_version = 2
            job.jira_enabled = True
            job.jira_delivery_version = 2
            await job.completed.wait()
            self.assertEqual(job.status, 'completed', job.error)
            self.assertEqual(job.business_result['jiraDelivery'], value['jiraDelivery'])
