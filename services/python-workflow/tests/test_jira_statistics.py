import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jira_statistics import statistics


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'test.db'
        self._connect = self.connect
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE jira_pool_items(id INTEGER PRIMARY KEY,server_key TEXT,issue_key TEXT,request_id TEXT,title TEXT,people_json TEXT);
              CREATE TABLE jira_collections(request_id TEXT,group_id TEXT,finished_at TEXT);
              CREATE TABLE workflow_jira_runs(workflow_id TEXT,server_key TEXT,issue_key TEXT);
              CREATE TABLE workflows(workflow_id TEXT,status TEXT,finished_at TEXT,termination_json TEXT,spec_json TEXT,spec_zlib BLOB,jira_context_json TEXT);
              CREATE TABLE jira_execution_history(workflow_id TEXT);
              CREATE TABLE workflow_nodes(workflow_id TEXT,job_id TEXT);
              INSERT INTO jira_collections VALUES('collection','group','2026-01-01');
              ALTER TABLE jira_collections ADD COLUMN sop_id TEXT;
              ALTER TABLE jira_collections ADD COLUMN sop_name TEXT;
              CREATE TABLE workflow_sop_context(workflow_id TEXT PRIMARY KEY,sop_id TEXT,sop_name TEXT);
              CREATE TABLE workflow_allocations(workflow_id TEXT,group_id TEXT);
            ''')
        self.results = {}
        mock = patch('jira_statistics.execution_result', side_effect=lambda db, wid: self.results[wid])
        mock.start()
        self.addCleanup(mock.stop)

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def issue(self, number, server='server'):
        with self.connect() as db:
            db.execute('INSERT INTO jira_pool_items(server_key,issue_key,request_id,title) VALUES(?,?,?,?)', (server, 'TEST-'+str(number), 'collection', '标题'))

    def execution(self, wid, state, finished, result, developer='甲'):
        self.results[wid] = {'status':result, 'flowStatus': 'passed' if state=='completed' else 'blocked' if state=='failed' else 'cancelled' if state=='cancelled' else None,
                             'submissionStatus': {'fully_submitted':'full','limited_submitted':'limited','completed_no_submission':'none','rework_required':'none'}.get(result,'unknown')}
        spec = {'jiraReservation':{'people':{'developers':[developer]}}}
        with self.connect() as db:
            db.execute('INSERT INTO workflows VALUES(?,?,?,?,?,?,?)', (wid,state,finished,'{}',json.dumps(spec),None,'{}'))
            db.execute("INSERT INTO workflow_jira_runs VALUES(?,'server','TEST-1')", (wid,))
            db.execute('INSERT INTO jira_execution_history VALUES(?)', (wid,))

    def test_sop_filter_uses_that_sops_latest_run_and_rate_excludes_pending(self):
        import uuid
        repair, acceptance = str(uuid.uuid4()), str(uuid.uuid4())
        self.issue(1)
        self.issue(2)
        self.execution('repair', 'completed', '2026-01-01', 'fully_submitted')
        self.execution('acceptance', 'failed', '2026-01-02', 'abnormal')
        with self.connect() as db:
            db.executemany('INSERT INTO workflow_sop_context VALUES(?,?,?)',
                           [('repair', repair, '修复'), ('acceptance', acceptance, '验收')])
        overall = statistics(self)['summary']
        self.assertEqual((overall['total'], overall['blocked'], overall['rateDenominator']), (2, 1, 1))
        selected = statistics(self, sop_id=repair)
        self.assertEqual((selected['summary']['total'], selected['summary']['passed']), (1, 1))
        self.assertEqual(statistics(self, sop_id=repair, items=True)['items'][0]['workflowId'], 'repair')
        self.assertEqual(statistics(self, sop_id=acceptance)['summary']['blocked'], 1)

    def test_latest_ended_and_reprocessing(self):
        self.issue(1)
        self.issue(1)
        self.execution('a','completed','2026-01-01','limited_submitted')
        self.execution('b','running',None,None,'乙')
        self.assertEqual(statistics(self)['summary']['passed'],1)
        detail = statistics(self, items=True)['items'][0]
        self.assertEqual(detail['developer'],'甲')
        self.assertTrue(detail['reprocessing'])
        with self.connect() as db:
            db.execute("UPDATE workflows SET status='failed',finished_at='2026-01-02' WHERE workflow_id='b'")
        self.results['b'].update(status='blocked',flowStatus='blocked')
        summary = statistics(self)['summary']
        self.assertEqual((summary['total'],summary['passed'],summary['blocked']),(1,0,1))

    def test_full_data_pagination_and_server_identity(self):
        for n in range(1,71):
            self.issue(n)
        self.issue(1,'another')
        self.assertEqual(statistics(self)['summary']['total'],71)
        first = statistics(self,items=True)
        second = statistics(self,items=True,before=first['nextCursor'])
        self.assertEqual((len(first['items']),len(second['items'])),(50,21))
        self.assertFalse({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
        self.assertEqual(statistics(self,project='OTHER')['summary']['total'],0)

    def test_cancelled_does_not_restore_previous_pass(self):
        self.issue(1)
        self.execution('a','completed','2026-01-01','fully_submitted')
        self.execution('b','cancelled','2026-01-02',None)
        self.assertEqual(statistics(self)['summary']['passed'],0)
        self.assertEqual(statistics(self,items=True,category='cancelled')['items'][0]['workflowId'],'b')

    def test_real_delivery_classifier(self):
        from workflow_jira import execution_result
        self.issue(1)
        self.execution('a', 'completed', '2026-01-01', None)
        with self.connect() as db:
            db.executescript("""
              ALTER TABLE workflow_jira_runs ADD COLUMN delivery_version INTEGER DEFAULT 2;
              CREATE TABLE workflow_jira_history_reviews(workflow_id TEXT,status TEXT);
              CREATE TABLE workflow_jira_deliveries(workflow_id TEXT,node_id TEXT,attempt INTEGER,status TEXT,submission_type TEXT,commits_json TEXT,recorded_at TEXT,summary TEXT);
            """)
            db.execute('INSERT INTO workflow_jira_deliveries VALUES(?,?,?,?,?,?,?,?)',
                       ('a','one',1,'submitted','limited',json.dumps([{'repository':'repo','revision':'42'}]),'2026-01-01','已核对'))
        with patch('jira_statistics.execution_result', execution_result):
            self.assertEqual(statistics(self)['summary']['limited'], 1)
            with self.connect() as db:
                db.execute("UPDATE workflow_jira_deliveries SET submission_type='full'")
            self.assertEqual(statistics(self)['summary']['full'], 1)

    def test_developer_totals_include_all_results_and_pending(self):
        for number in (1, 2, 2, 3, 4):
            self.issue(number)
        self.execution('passed','completed','2026-01-01','fully_submitted')
        self.execution('blocked','failed','2026-01-02','blocked','乙')
        with self.connect() as db:
            db.execute("UPDATE workflow_jira_runs SET issue_key='TEST-3' WHERE workflow_id='blocked'")
            db.execute("UPDATE jira_pool_items SET people_json=? WHERE issue_key IN ('TEST-1','TEST-2')",
                       (json.dumps({'developers':['乙'], 'collectedAt':'2026-01-03'}),))
        result = statistics(self)
        people = {r['developer']: r for r in result['developers']}
        self.assertEqual((people['甲']['total'],people['甲']['passed']),(1,1))
        self.assertEqual((people['乙']['total'],people['乙']['passed']),(2,0))
        self.assertEqual(people['开发人未获取']['total'],1)
        self.assertEqual(sum(r['total'] for r in people.values()),result['summary']['total'])
        items = statistics(self,items=True,developer='乙')['items']
        self.assertEqual({r['issueKey'] for r in items},{'TEST-2','TEST-3'})

    def test_manual_no_submission_counts_as_passed_across_all_scopes(self):
        self.issue(1)
        self.execution('manual', 'completed', '2026-01-01', 'completed_no_submission')
        result = statistics(self)
        for counts in [result['summary'], *result['groups'], *result['developers']]:
            self.assertEqual((counts['passed'], counts['other'], counts['noSubmission']), (1, 0, 1))
            self.assertEqual((counts['full'], counts['limited']), (0, 0))
        self.assertEqual(len(statistics(self, items=True, category='passed')['items']), 1)
        self.assertEqual(statistics(self, items=True, category='other')['items'], [])
        self.assertEqual(len(statistics(self, items=True, category='passed', developer='甲')['items']), 1)
        self.assertEqual(len(statistics(self, items=True, category='completed_no_submission')['items']), 1)

    def test_manual_pass_is_replaced_by_latest_ended_result(self):
        self.issue(1)
        self.execution('manual', 'completed', '2026-01-01', 'completed_no_submission')
        self.execution('retry', 'running', None, None)
        self.assertEqual(statistics(self)['summary']['passed'], 1)
        with self.connect() as db:
            db.execute("UPDATE workflows SET status='failed',finished_at='2026-01-02' WHERE workflow_id='retry'")
        self.results['retry'].update(status='abnormal',flowStatus='blocked')
        summary = statistics(self)['summary']
        self.assertEqual((summary['passed'], summary['noSubmission'], summary['blocked']), (0, 0, 1))
        self.assertEqual(statistics(self, items=True, category='passed')['items'], [])

    def test_review_rework_is_flow_pass_without_submission(self):
        self.issue(1)
        self.execution('rework', 'completed', '2026-01-01', 'rework_required')
        result = statistics(self)
        for counts in [result['summary'], *result['groups'], *result['developers']]:
            self.assertEqual((counts['passed'], counts['other'], counts['blocked']), (1, 0, 0))
        self.assertEqual(len(statistics(self, items=True, category='passed')['items']), 1)
        self.assertEqual(len(statistics(self, items=True, category='completed_no_submission')['items']), 1)
