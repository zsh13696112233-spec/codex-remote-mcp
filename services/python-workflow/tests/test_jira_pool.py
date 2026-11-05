import json
import tempfile
import unittest
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from workflow_store import WorkflowStore, utc_now
from jira_pool import request_collection, collection, reserve, bind, cancel, record_rework, rework_history, board


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WorkflowStore(Path(self.tmp.name) / 'test.db')
        self.task = str(uuid.uuid4())
        self.group = str(uuid.uuid4())
        from tests.test_jira_accounts import seed_account
        self.config = {'jql': 'project = TEST', 'connectionId': seed_account(self.store, self.group)}

    def batch(self, keys, source='schedule'):
        request = str(uuid.uuid4())
        body = {'requestId': request, 'taskDefinitionId': self.task, 'groupId': self.group,
                'source': source, 'jira': self.config, 'name': '测试任务'}
        request_collection(self.store, body)
        self.assertEqual(request_collection(self.store, body)['requestId'], request)
        with self.store._connect() as db:
            db.execute("UPDATE jira_collections SET state='ready',server_key='http://jira.test',account_id='bot',account_alias='bot',count=? WHERE request_id=?", (len(keys), request))
            for key in keys:
                db.execute("INSERT INTO jira_pool_items(request_id,server_key,issue_key,title) VALUES(?,'http://jira.test',?,'标题')", (request, key))
        return request

    def test_execution_detail_is_public_and_missing_is_explicit(self):
        from jira_pool import execution_detail
        self.assertIsNone(execution_detail(self.store, 'missing'))
        request = self.batch(['TEST-1'])
        self.bind_run(request, 'detail', 'completed')
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET response=? WHERE workflow_id='detail'", ('验证受限，需人工补验 Bearer secret',))
            db.execute("INSERT INTO workflow_jira_deliveries (workflow_id,node_id,attempt,status,reference,summary,recorded_at) VALUES('detail','step',1,'submitted','r25864','需人工补验',?)", (utc_now(),))
        result = execution_detail(self.store, 'detail')
        self.assertIn('需人工补验', result['summary'])
        self.assertNotIn('secret', result['summary'])
        self.assertEqual(result['deliveries'][0]['reference'], 'r25864')
        self.assertNotIn('spec', result)

    def test_zero_tasks_never_reserves(self):
        request = self.batch([])
        self.assertIsNone(reserve(self.store, request, str(uuid.uuid4())))
        self.assertEqual(collection(self.store, request)['pending'], 0)

    def test_two_issues_three_concurrent_reservations(self):
        request = self.batch(['TEST-1', 'TEST-2'])
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: reserve(self.store, request, str(uuid.uuid4())), range(3)))
        self.assertEqual({r['issueKey'] for r in results if r}, {'TEST-1', 'TEST-2'})
        self.assertEqual(results.count(None), 1)

    def test_idempotency_and_cross_source_mutex(self):
        a, b = self.batch(['TEST-1']), self.batch(['TEST-1'])
        identifier = str(uuid.uuid4())
        first = reserve(self.store, a, identifier)
        self.assertEqual(first, reserve(WorkflowStore(self.store.path), a, identifier))
        self.assertIsNone(reserve(self.store, b, str(uuid.uuid4())))

    def test_mapping_change_blocks_pending_dispatch_but_keeps_bound_snapshot(self):
        request = self.batch(['TEST-1', 'TEST-2'])
        frozen = reserve(self.store, request, str(uuid.uuid4()))
        self.workflow('frozen')
        with self.store._connect() as db:
            bind(db, {'workflowId': 'frozen', 'jiraReservation': frozen,
                      'taskDefinitionId': self.task, 'groupId': self.group, 'jira': self.config})
        pending = reserve(self.store, request, str(uuid.uuid4()))
        with self.store._connect() as db:
            db.execute("UPDATE jira_accounts SET username='new-login',executor_alias='new-login' WHERE id=?",
                       (self.config['connectionId'],))
        self.assertEqual(reserve(self.store, request, frozen['reservationId']), frozen)
        with self.assertRaisesRegex(ValueError, '映射已变化'):
            reserve(self.store, request, pending['reservationId'])
        with self.store._connect() as db:
            with self.assertRaisesRegex(ValueError, '快照不一致'):
                bind(db, {'workflowId': 'pending', 'jiraReservation': pending,
                          'taskDefinitionId': self.task, 'groupId': self.group, 'jira': self.config})

    def test_existing_pool_schema_adds_nullable_people_column(self):
        request = self.batch(['TEST-1'])
        with self.store._connect() as db:
            db.execute('ALTER TABLE jira_pool_items DROP COLUMN people_json')
        reopened = WorkflowStore(self.store.path)
        self.assertNotIn('people', reserve(reopened, request, str(uuid.uuid4())))
        self.assertEqual(board(reopened)['items'][0]['issue_key'], 'TEST-1')

    def test_people_snapshot_is_frozen_and_board_uses_latest_attempt(self):
        import json
        first = self.batch(['TEST-1'])
        people = {'developers': ['甲'], 'developerStatus': 'resolved', 'assignee': '乙',
                  'assigneeStatus': 'resolved', 'collectedAt': '2026-01-01T00:00:00+00:00'}
        with self.store._connect() as db:
            db.execute('UPDATE jira_pool_items SET people_json=? WHERE request_id=?', (json.dumps(people), first))
        reservation = reserve(self.store, first, str(uuid.uuid4()))
        self.assertEqual(reservation['people'], people)
        second = self.batch(['TEST-1'])
        unavailable = {**people, 'developers': [], 'developerStatus': 'unavailable', 'collectedAt': '2026-01-02T00:00:00+00:00'}
        with self.store._connect() as db:
            db.execute('UPDATE jira_pool_items SET people_json=? WHERE request_id=?', (json.dumps(unavailable), second))
        self.assertEqual(board(self.store)['items'][0]['people'], unavailable)
        self.assertEqual(reserve(WorkflowStore(self.store.path), first, reservation['reservationId'])['people'], people)
        with self.store._connect() as db:
            with self.assertRaisesRegex(ValueError, '快照不一致'):
                bind(db, {'workflowId': 'test', 'taskDefinitionId': self.task, 'groupId': self.group,
                          'jira': self.config, 'jiraReservation': {**reservation, 'people': unavailable}})

    def test_reservation_keeps_collected_title_after_reopen(self):
        request = self.batch(['TEST-1'])
        reservation = reserve(self.store, request, str(uuid.uuid4()))
        self.assertEqual(reservation['title'], '标题')
        self.assertEqual(reserve(WorkflowStore(self.store.path), request, reservation['reservationId']), reservation)

    def test_title_contract_accepts_legacy_and_rejects_invalid_titles(self):
        request = self.batch(['TEST-1'])
        reservation = reserve(self.store, request, str(uuid.uuid4()))
        spec = {'workflowId': 'run', 'dispatchMode': 'group', 'groupId': self.group,
                'jira': self.config, 'jiraReservation': reservation,
                'nodes': [{'id': 'one', 'prompt': '处理工单'}]}
        self.assertEqual(WorkflowStore.normalize_spec(spec)['jiraReservation']['title'], '标题')
        for title in (None, 123, '字' * 1001):
            with self.subTest(title_type=type(title).__name__), self.assertRaisesRegex(ValueError, '标题'):
                WorkflowStore.normalize_spec({**spec, 'jiraReservation': {**reservation, 'title': title}})
        reservation.pop('title')
        self.assertNotIn('title', WorkflowStore.normalize_spec(spec)['jiraReservation'])

    def test_binding_rejects_title_from_another_issue_and_accepts_legacy(self):
        request = self.batch(['TEST-1'])
        reservation = reserve(self.store, request, str(uuid.uuid4()))
        self.workflow('run')
        spec = {'workflowId': 'run', 'jiraReservation': {**reservation, 'title': '另一张工单'},
                'taskDefinitionId': self.task, 'groupId': self.group, 'jira': self.config}
        with self.store._connect() as db:
            with self.assertRaisesRegex(ValueError, '预约与提交快照不一致'):
                bind(db, spec)
            spec['jiraReservation'].pop('title')
            bind(db, spec)
        self.assertEqual(reserve(self.store, request, reservation['reservationId']), reservation)

    def test_manual_authorizes_only_one(self):
        request = self.batch(['TEST-1', 'TEST-2'], 'web')
        self.assertIsNotNone(reserve(self.store, request, str(uuid.uuid4())))
        self.assertIsNone(reserve(self.store, request, str(uuid.uuid4())))

    def test_cancel_preserves_records(self):
        request = self.batch(['TEST-1'])
        cancel(self.store, request)
        self.assertEqual(collection(self.store, request)['state'], 'cancelled')
        with self.assertRaises(ValueError):
            reserve(self.store, request, str(uuid.uuid4()))

    def workflow(self, name):
        with self.store._connect() as db:
            db.execute("INSERT INTO workflows(workflow_id,name,status,failure_policy,supervisor_agent_id,supervisor_status,created_at,spec_json) VALUES(?,?,'queued','stop','s','queued',?,'{}')",(name,name,utc_now()))
            db.execute("INSERT INTO workflow_nodes(workflow_id,node_id,position,agent_id,executor_type,prompt,depends_on_json,write_enabled,timeout_sec,status,created_at) VALUES(?,'a',0,'e','remote','处理','[]',0,60,'pending',?)",(name,utc_now()))
            db.execute("INSERT INTO workflow_jira_runs(workflow_id,server_key,issue_key) VALUES(?,'http://jira.test','TEST-1')", (name,))

    def test_binding_consumes_other_sources(self):
        a, b = self.batch(['TEST-1']), self.batch(['TEST-1'])
        reservation = reserve(self.store, a, str(uuid.uuid4()))
        self.workflow('run')
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            bind(db, {'workflowId':'run','jiraReservation':reservation,'taskDefinitionId':self.task,'groupId':self.group,'jira':self.config})
        self.assertEqual(collection(self.store, b)['pending'],0)
        self.assertEqual(reserve(self.store, a, reservation['reservationId']),reservation)

    def test_rework_once_and_reason_from_previous(self):
        self.workflow('old')
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET status='failed',finished_at=?,termination_json=? WHERE workflow_id='old'",(utc_now(),json.dumps({'outcome':'blocked','reason':'缺少开发权限'})))
            db.execute("UPDATE workflow_nodes SET job_id='job' WHERE workflow_id='old'")
        self.workflow('new')
        with self.store._connect() as db:
            record_rework(db,'new',utc_now())
            record_rework(db,'new',utc_now())
            history=rework_history(db,'http://jira.test','TEST-1')
        self.assertEqual(len(history),1)
        self.assertEqual(history[0]['reason'],'缺少开发权限')

    def test_legacy_claim_consumes_pending_pool_qualification(self):
        request=self.batch(['TEST-1'])
        self.workflow('legacy')
        with self.store._connect() as db:
            db.execute("UPDATE workflow_jira_runs SET issue_key=NULL,server_key=NULL WHERE workflow_id='legacy'")
            db.execute("INSERT INTO workflow_machine_claims VALUES('s','legacy',?,'occupied')",(utc_now(),))
        result=self.store.jira_update('legacy','claim',{'server':'http://jira.test','account':'bot','fingerprint':'a'*64,'candidates':['TEST-1']})
        self.assertEqual(result['issueKey'],'TEST-1')
        self.assertEqual(collection(self.store,request)['pending'],0)

    def test_reserved_issue_cannot_be_taken_by_legacy_claim(self):
        request=self.batch(['TEST-1'])
        reserve(self.store,request,str(uuid.uuid4()))
        self.workflow('legacy')
        with self.store._connect() as db:
            db.execute("UPDATE workflow_jira_runs SET issue_key=NULL,server_key=NULL WHERE workflow_id='legacy'")
            db.execute("INSERT INTO workflow_machine_claims VALUES('s','legacy',?,'occupied')",(utc_now(),))
        result=self.store.jira_update('legacy','claim',{'server':'http://jira.test','account':'bot','fingerprint':'a'*64,'candidates':['TEST-1']})
        self.assertEqual(result['state'],'contended')

    def test_board_project_and_task_filters_do_not_change_cursor(self):
        request=self.batch(['TEST-1','OTHER-2'])
        result=board(self.store,project='TEST')
        self.assertEqual([r['issue_key'] for r in result['items']],['TEST-1'])
        self.assertEqual([p['project'] for p in result['projects']],['TEST'])
        self.assertIsNone(result['nextCursor'])
        self.assertEqual(board(self.store,task='不存在')['items'],[])
        self.assertEqual(board(self.store,task='不存在')['projects'],[])

    def bind_run(self, request, name, status='running'):
        reservation = reserve(self.store, request, str(uuid.uuid4()))
        self.workflow(name)
        with self.store._connect() as db:
            bind(db, {'workflowId': name, 'jiraReservation': reservation,
                      'taskDefinitionId': self.task, 'groupId': self.group, 'jira': self.config})
            db.execute('UPDATE workflows SET status=? WHERE workflow_id=?', (status, name))
            db.execute('UPDATE workflow_jira_runs SET issue_key=? WHERE workflow_id=?', (reservation['issueKey'], name))
            db.execute('INSERT INTO jira_execution_history VALUES(?,?,?,?,NULL,?)',
                       (name, 'http://jira.test', reservation['issueKey'], utc_now(), ''))

    def test_board_shows_blocked_reason_without_rework_and_clears_it_on_recollection(self):
        request = self.batch(['TEST-1'])
        self.bind_run(request, 'blocked', 'failed')
        with self.store._connect() as db:
            db.execute('UPDATE workflows SET termination_json=? WHERE workflow_id=?',
                       (json.dumps({'outcome': 'blocked', 'reason': '缺少验证材料，执行地址 http://192.0.2.1:8080 token=example-secret'}), 'blocked'))
        item = board(self.store)['items'][0]
        self.assertEqual(item['outcome'], 'blocked')
        self.assertEqual(item['reworkCount'], 0)
        self.assertEqual(item['reworks'], [])
        self.assertIn('缺少验证材料', item['reason'])
        self.assertNotIn('192.0.2.1', item['reason'])
        self.assertNotIn('example-secret', item['reason'])
        self.assertNotIn('termination_json', item)
        self.batch(['TEST-1'])
        waiting = board(self.store)['items'][0]
        self.assertEqual(waiting['state'], 'pending')
        self.assertIsNone(waiting['outcome'])
        self.assertEqual(waiting['reason'], '')

    def test_board_merges_repeated_collections_without_changing_history(self):
        first = self.batch(['TEST-1','TEST-2'])
        self.bind_run(first, 'running')
        self.bind_run(first, 'failed', 'failed')
        second = self.batch(['TEST-1','TEST-2'])
        third = self.batch(['TEST-1'])
        with self.store._connect() as db:
            db.execute("UPDATE jira_pool_items SET state='consumed' WHERE request_id IN (?,?)", (second, third))
            for request, stamp in ((first,'2026-11-02T08:17:18+00:00'),
                                   (second,'2026-11-02T08:22:18+00:00'),
                                   (third,'2026-11-02T08:27:18+00:00')):
                db.execute('UPDATE jira_collections SET finished_at=? WHERE request_id=?', (stamp, request))
        result = board(self.store)
        items = {r['issue_key']: r for r in result['items']}
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(items['TEST-1']['run_status'], 'running')
        self.assertEqual(items['TEST-1']['workflow_id'], 'running')
        self.assertEqual(items['TEST-1']['collected_at'], '2026-11-02T08:27:18+00:00')
        self.assertEqual(items['TEST-2']['run_status'], 'failed')
        self.assertIsNotNone(items['TEST-2']['started_at'])
        self.assertEqual(len(items['TEST-1']['sources']), 1)
        self.assertEqual(result['projects'][0]['pending'], 0)
        self.assertEqual(result['projects'][0]['running'], 1)
        self.assertEqual(len(result['collections']), 3)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jira_pool_items').fetchone()[0], 5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workflows').fetchone()[0], 2)

    def test_board_execution_history_keeps_each_rework_result_and_reason(self):
        for index, status in enumerate(['failed', 'failed', 'completed']):
            request = self.batch(['TEST-1'])
            name = 'run-' + str(index)
            self.bind_run(request, name, status)
            with self.store._connect() as db:
                db.execute('UPDATE jira_execution_history SET started_at=?,previous_workflow_id=?,reason=? WHERE workflow_id=?',
                           (f'2026-11-02T0{index}:00:00+00:00', f'run-{index-1}' if index else None,
                            f'上一轮原因{index}' if index else '', name))
                db.execute('UPDATE workflows SET termination_json=?,finished_at=? WHERE workflow_id=?',
                           (json.dumps({'outcome': 'blocked', 'reason': f'本轮阻断{index} token=secret'}) if index < 2 else None,
                            f'2026-11-02T0{index}:30:00+00:00', name))
        self.workflow('not-started')
        result = board(self.store)['items'][0]
        self.assertEqual(result['reworkCount'], 2)
        executions = result['executions']
        self.assertEqual([e['workflowId'] for e in executions], ['run-0', 'run-1', 'run-2'])
        self.assertEqual([e['kind'] for e in executions], ['first', 'rework', 'rework'])
        self.assertEqual([e['reworkNumber'] for e in executions], [None, 1, 2])
        self.assertIn('本轮阻断1', executions[1]['reason'])
        self.assertNotIn('secret', executions[1]['reason'])
        self.assertEqual(executions[1]['reworkReason'], '上一轮原因1')
        self.assertEqual(executions[2]['status'], 'completed')
        self.assertIsNone(executions[2]['outcome'])
        self.assertEqual(executions[2]['reason'], '')

    def test_board_legacy_execution_remains_visible_without_inventing_rework_number(self):
        self.batch(['TEST-1'])
        self.workflow('legacy')
        with self.store._connect() as db:
            db.execute("UPDATE workflow_nodes SET job_id='job',started_at=? WHERE workflow_id='legacy'", (utc_now(),))
            db.execute("UPDATE workflows SET status='failed',termination_json=? WHERE workflow_id='legacy'",
                       (json.dumps({'outcome': 'blocked', 'reason': '历史阻断'}),))
        result = board(self.store)['items'][0]
        self.assertEqual(result['reworkCount'], 0)
        self.assertEqual(result['executions'][0]['kind'], 'legacy')
        self.assertIsNone(result['executions'][0]['reworkNumber'])
        self.assertEqual(result['executions'][0]['reason'], '历史阻断')

    def test_retry_count_uses_actual_runs_including_legacy_without_rework_results(self):
        self.batch(['TEST-1'])
        self.assertEqual(board(self.store)['items'][0]['retryCount'], 0)
        for index in range(7):
            name = f'run-{index}'
            self.workflow(name)
            with self.store._connect() as db:
                db.execute("UPDATE workflows SET status=? WHERE workflow_id=?",
                           ('running' if index == 6 else 'failed' if index >= 2 else 'completed', name))
                if index in (0, 1, 5, 6):
                    db.execute("UPDATE workflow_nodes SET job_id=?,started_at=?,attempt_count=? WHERE workflow_id=?",
                               (f'job-{index}', f'2026-11-03T0{index}:00:00+00:00', 3 if index == 5 else 0, name))
                if index in (1, 5, 6):
                    record_rework(db, name, f'2026-11-03T0{index}:00:00+00:00')
                    record_rework(db, name, f'2026-11-03T0{index}:01:00+00:00')
                if index == 1:
                    db.execute("INSERT INTO workflow_jira_deliveries (workflow_id,node_id,attempt,status,reference,summary,recorded_at) VALUES(?, 'a', 0, 'not_applicable', '', '审核结论：返工', ?)",
                               (name, utc_now()))
        # Other Jira servers and issue keys must not contribute to this issue's total.
        for name, server, issue in [('other-server', 'http://other.test', 'TEST-1'),
                                    ('other-issue', 'http://jira.test', 'TEST-2')]:
            self.workflow(name)
            with self.store._connect() as db:
                db.execute("UPDATE workflow_jira_runs SET server_key=?,issue_key=? WHERE workflow_id=?", (server, issue, name))
                db.execute("UPDATE workflow_nodes SET job_id='job' WHERE workflow_id=?", (name,))
        result = board(self.store)['items'][0]
        self.assertEqual(result['retryCount'], 3)
        self.assertEqual(result['reworkCount'], 0)
        self.assertEqual([e['workflowId'] for e in result['executions']], ['run-0', 'run-1', 'run-5', 'run-6'])
        self.assertEqual([e['retryNumber'] for e in result['executions']], [0, 1, 2, 3])
        self.assertEqual(board(self.store)['items'][0]['retryCount'], 3)

    def test_ledger_retry_count_matches_pool_across_groups(self):
        from unittest.mock import patch
        from agent_registry import AgentRegistry
        with patch('agent_registry.setting', return_value={}):
            AgentRegistry(self.store)
        self.batch(['TEST-1'])
        groups = [self.group, str(uuid.uuid4())]
        for name, group in zip(['first', 'retry'], groups):
            self.workflow(name)
            with self.store._connect() as db:
                db.execute("INSERT INTO workflow_allocations(workflow_id,group_id) VALUES(?,?)", (name, group))
        self.assertTrue(all(row['retryCount'] == 0 for row in self.store.jira_runs('')['items']))
        for name, time in [('first', '2026-11-03T00:00:00+00:00'), ('retry', '2026-11-03T01:00:00+00:00')]:
            with self.store._connect() as db:
                db.execute("UPDATE workflow_nodes SET job_id=?,started_at=? WHERE workflow_id=?", (name, time, name))
        pool_count = board(self.store)['items'][0]['retryCount']
        self.assertEqual(pool_count, 1)
        for group in ('', *groups):
            rows = self.store.jira_runs(group)['items']
            self.assertEqual(len(rows), 1 if group else 2)
            for row in rows:
                self.assertEqual(row['retryCount'], pool_count)
                self.assertEqual(row['reworkCount'], 0)
                self.assertEqual([e['retryNumber'] for e in row['executions']], [0, 1])

    def test_board_pending_keeps_wait_start_and_stable_cursor_across_recollection(self):
        first = self.batch(['TEST-1','TEST-2','TEST-3'])
        with self.store._connect() as db:
            db.execute("UPDATE jira_collections SET finished_at='2026-11-02T08:00:00+00:00' WHERE request_id=?", (first,))
        page = board(self.store, limit=1)
        again = self.batch(['TEST-2','TEST-3'])
        with self.store._connect() as db:
            db.execute("UPDATE jira_collections SET finished_at='2026-11-02T08:05:00+00:00' WHERE request_id=?", (again,))
        refreshed = board(self.store, limit=1)
        self.assertEqual(page['nextCursor'], refreshed['nextCursor'])
        self.assertEqual(refreshed['items'][0]['queued_at'], '2026-11-02T08:00:00+00:00')
        self.assertEqual(refreshed['items'][0]['collected_at'], '2026-11-02T08:05:00+00:00')
        second = board(self.store, before=page['nextCursor'], limit=1)
        last = board(self.store, before=second['nextCursor'], limit=1)
        self.assertEqual([p['items'][0]['issue_key'] for p in (page,second,last)], ['TEST-3','TEST-2','TEST-1'])
        self.assertIsNone(last['nextCursor'])
        self.assertEqual(second['projects'][0]['pending'], 3)

    def test_board_rework_updates_existing_issue_and_filters_current_state(self):
        first = self.batch(['TEST-1'])
        self.bind_run(first, 'old', 'failed')
        original_id = board(self.store)['items'][0]['id']
        retry = self.batch(['TEST-1'])
        waiting = board(self.store)['items'][0]
        self.assertEqual(waiting['id'], original_id)
        self.assertEqual(waiting['state'], 'pending')
        self.assertIsNone(waiting['workflow_id'])
        self.assertIsNone(waiting['started_at'])
        self.assertEqual(board(self.store,state='failed')['items'], [])
        self.bind_run(retry, 'new')
        current = board(self.store, state='running')['items'][0]
        self.assertEqual(current['id'], original_id)
        self.assertEqual(current['workflow_id'], 'new')
        self.assertEqual(board(self.store,state='failed')['items'], [])

    def test_board_merges_sources_and_keeps_global_run_when_filtering_other_group(self):
        owner = self.batch(['TEST-1'])
        other = self.batch(['TEST-1'])
        other_group, other_task = str(uuid.uuid4()), str(uuid.uuid4())
        with self.store._connect() as db:
            db.execute("UPDATE jira_collections SET name='另一任务',group_id=?,task_id=? WHERE request_id=?",
                       (other_group, other_task, other))
        self.bind_run(owner, 'owner')
        item = board(self.store)['items'][0]
        self.assertEqual(len(item['sources']), 2)
        scoped = board(self.store, group_id=other_group, task='另一任务', state='running')
        self.assertEqual(len(scoped['items']), 1)
        self.assertEqual(scoped['items'][0]['workflow_id'], 'owner')
        self.assertEqual(scoped['items'][0]['sources'], [{'taskId': other_task, 'groupId': other_group, 'name': '另一任务'}])
        self.assertEqual(scoped['projects'][0]['running'], 1)
        self.assertEqual(board(self.store, group_id=self.group, task='另一任务')['items'], [])

    def test_board_cancelled_source_does_not_hide_other_pending_source(self):
        first = self.batch(['TEST-1'])
        second = self.batch(['TEST-1'])
        cancel(self.store, second)
        item = board(self.store)['items'][0]
        self.assertEqual(item['state'], 'pending')
        self.assertEqual(item['request_id'], first)
        cancel(self.store, first)
        self.assertEqual(board(self.store)['items'][0]['state'], 'cancelled')
        self.assertEqual(board(self.store)['projects'][0]['pending'], 0)

    def test_board_reservation_remains_visible_when_collected_again(self):
        first = self.batch(['TEST-1'])
        reservation = reserve(self.store, first, str(uuid.uuid4()))
        repeated = self.batch(['TEST-1'])
        with self.store._connect() as db:
            db.execute("UPDATE jira_pool_items SET state='consumed' WHERE request_id=?", (repeated,))
        item = board(self.store, state='reserved')['items'][0]
        self.assertEqual(item['reservation_id'], reservation['reservationId'])
        self.assertIsNone(item['workflow_id'])
        self.assertEqual(board(self.store,state='consumed')['items'], [])
        self.assertEqual(board(self.store)['projects'][0]['pending'], 0)

    def test_board_separates_servers_and_does_not_invent_runs_for_unselected_manual_issues(self):
        manual = self.batch(['TEST-1','TEST-2'], 'web')
        self.bind_run(manual, 'manual')
        another_server = self.batch(['TEST-1'])
        with self.store._connect() as db:
            db.execute("UPDATE jira_pool_items SET server_key='http://other.test' WHERE request_id=?", (another_server,))
        result = board(self.store)
        self.assertEqual(len(result['items']), 3)
        item = next(r for r in result['items'] if r['issue_key']=='TEST-2')
        self.assertEqual(item['state'], 'consumed')
        self.assertIsNone(item['workflow_id'])
        self.assertEqual(result['projects'][0]['pending'], 1)
        self.assertEqual(result['projects'][0]['running'], 1)

    def test_board_follows_legacy_retry_and_retains_unconfirmed_stop(self):
        first = self.batch(['TEST-1'])
        self.bind_run(first, 'old', 'failed')
        self.workflow('retry')
        with self.store._connect() as db:
            db.execute("UPDATE jira_issue_claims SET workflow_id='retry'")
            db.execute("UPDATE workflows SET status='failed' WHERE workflow_id='retry'")
            db.execute("INSERT INTO workflow_machine_claims VALUES('s','retry',?,'occupied')", (utc_now(),))
        self.batch(['TEST-1'])
        item = board(self.store)['items'][0]
        self.assertEqual(item['workflow_id'], 'retry')
        self.assertTrue(item['stop_pending'])
        self.assertEqual(item['run_status'], 'failed')
        self.assertEqual(board(self.store)['projects'][0]['pending'], 0)
        with self.store._connect() as db:
            db.execute("DELETE FROM workflow_machine_claims WHERE workflow_id='retry'")
        self.assertEqual(board(self.store)['items'][0]['state'], 'pending')

    def test_board_new_invalid_check_is_not_hidden_by_old_finished_run(self):
        first = self.batch(['TEST-1'])
        self.bind_run(first, 'old', 'completed')
        check = self.batch(['TEST-1'])
        with self.store._connect() as db:
            db.execute("UPDATE jira_pool_items SET state='invalid' WHERE request_id=?", (check,))
        item = board(self.store)['items'][0]
        self.assertEqual(item['state'], 'invalid')
        self.assertIsNone(item['workflow_id'])
        self.assertEqual(board(self.store,state='completed')['items'], [])

    def test_explicit_non_submission_is_rework_but_completed_is_not(self):
        self.workflow('old')
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET status='completed',finished_at=? WHERE workflow_id='old'",(utc_now(),))
            db.execute("UPDATE workflow_nodes SET job_id='job' WHERE workflow_id='old'")
            db.execute("INSERT INTO workflow_jira_deliveries (workflow_id,node_id,attempt,status,reference,summary,recorded_at) VALUES('old','a',1,'not_submitted','','提交前检查失败',?)",(utc_now(),))
        self.workflow('new')
        with self.store._connect() as db:
            record_rework(db,'new',utc_now())
            self.assertEqual(rework_history(db,'http://jira.test','TEST-1')[0]['reason'],'提交前检查失败')

    def test_unknown_submission_not_rework(self):
        self.workflow('old')
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET status='failed',finished_at=? WHERE workflow_id='old'",(utc_now(),))
            db.execute("UPDATE workflow_nodes SET job_id='job' WHERE workflow_id='old'")
            db.execute("INSERT INTO workflow_jira_deliveries (workflow_id,node_id,attempt,status,reference,summary,recorded_at) VALUES('old','a',1,'unknown','','结果未知',?)",(utc_now(),))
        self.workflow('new')
        with self.store._connect() as db:
            record_rework(db,'new',utc_now())
            self.assertEqual(rework_history(db,'http://jira.test','TEST-1'),[])


    def test_cross_group_accounts_share_one_issue_mutex(self):
        first=self.batch(['TEST-1'])
        other_group=str(uuid.uuid4())
        from tests.test_jira_accounts import seed_account
        self.group=other_group
        self.config={**self.config,'connectionId':seed_account(self.store,other_group)}
        second=self.batch(['TEST-1'])
        self.assertIsNotNone(reserve(self.store,first,str(uuid.uuid4())))
        self.assertIsNone(reserve(self.store,second,str(uuid.uuid4())))

    def test_disabled_account_prevents_reservation_and_binding(self):
        request=self.batch(['TEST-1'])
        binding=reserve(self.store,request,str(uuid.uuid4()))
        with self.store._connect() as db:
            db.execute('UPDATE jira_accounts SET enabled=0 WHERE id=?',(self.config['connectionId'],))
        with self.assertRaisesRegex(ValueError,'未启用'):
            reserve(self.store,request,binding['reservationId'])
        with self.store._connect() as db, self.assertRaisesRegex(ValueError,'未启用'):
            bind(db, {'jira':self.config,'groupId':self.group,'jiraReservation':binding})

    def test_account_delete_checks_compressed_active_snapshot(self):
        from jira_accounts import delete, AccountConflict
        import zlib
        self.workflow('active')
        with self.store._connect() as db:
            db.execute('UPDATE workflows SET spec_json=?,spec_zlib=? WHERE workflow_id=?',('{}',zlib.compress(json.dumps({'jira':self.config}).encode()),'active'))
        with self.assertRaisesRegex(AccountConflict,'未完成运行'):
            delete(self.store,self.config['connectionId'],1)

    def test_restart_cancels_legacy_sources_without_deleting_history(self):
        from jira_pool import recover
        request=self.batch(['TEST-1'])
        with self.store._connect() as db:
            db.execute('UPDATE jira_collections SET config_json=? WHERE request_id=?',(json.dumps({'jql':'project=TEST','account':'bot'}),request))
        recover(self.store)
        self.assertEqual(collection(self.store,request)['state'],'cancelled')
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT state FROM jira_pool_items WHERE request_id=?',(request,)).fetchone()[0],'cancelled')

    def test_central_start_claim_is_authorized_idempotent_and_uses_frozen_alias(self):
        from unittest.mock import patch
        from jira_pool import prepare_run
        from jira_http import JiraClient
        request = self.batch(['TEST-1'])
        binding = reserve(self.store, request, str(uuid.uuid4()))
        self.workflow('central')
        spec = {'workflowId':'central','taskDefinitionId':self.task,'groupId':self.group,
                'jira':self.config,'jiraReservation':binding}
        with self.store._connect() as db:
            db.execute("UPDATE workflow_jira_runs SET issue_key=NULL WHERE workflow_id='central'")
            db.execute("INSERT INTO workflow_allocations(workflow_id,group_id,supervisor_id,executor_id,cwd) VALUES('central',?,'s','e','C:/work')",(self.group,))
            db.execute("INSERT INTO workflow_machine_claims VALUES('e','central',?,'occupied')",(utc_now(),))
            bind(db,spec)
            db.execute("UPDATE jira_accounts SET enabled=0,executor_alias='renamed' WHERE id=?",(self.config['connectionId'],))
        with patch.object(self.store,'get_spec',return_value=spec):
            with self.assertRaisesRegex(ValueError,'中央网关'):
                self.store.jira_update('central','claim',{})
            with patch.object(JiraClient,'identity',return_value='bot'), patch.object(JiraClient,'search',return_value={'TEST-1':('',{})}) as search:
                prepare_run(self.store,spec)
                prepare_run(self.store,spec)
                search.assert_called_once()
            context=self.store.jira_context('central')
        self.assertEqual(context['state'],'claimed')
        self.assertEqual(context['account'],'bot')
        self.assertEqual(context['issueKey'],'TEST-1')


if __name__ == '__main__':
    unittest.main()


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=WorkflowStore(Path(self.tmp.name)/'pool.db')
        self.gateway=SimpleNamespace(store=self.store,orchestrator=object())
        self.agent=SimpleNamespace(cwd='C:\\work')
        self.client=AsyncMock()
        self.client.__aenter__.return_value=self.client
        self.request=str(uuid.uuid4())
        from tests.test_jira_accounts import seed_account
        self.group = str(uuid.uuid4())
        self.connection = seed_account(self.store, self.group, 'user-1')
        from jira_pool import sync_polling
        sync_polling(self.store, {'tasks': [{'taskDefinitionId': str(uuid.uuid4()), 'groupId': self.group, 'name': '测试', 'jira': {'jql': 'project = TEST AND assignee = currentUser()', 'connectionId': self.connection}}]})
        with self.store._connect() as db:
            self.request = db.execute("SELECT request_id FROM jira_collections WHERE source='poll'").fetchone()[0]

    async def test_complete_pages_only_and_identity_saved_without_credentials(self):
        from unittest.mock import patch
        from jira_pool import collect
        from jira_http import JiraClient, people
        with patch.object(JiraClient, 'identity', return_value='user-1'), patch.object(JiraClient, 'developer_field', return_value=None), patch.object(JiraClient, 'search', side_effect=[{'TEST-1':('标题',people({},None),{'issueType':None,'priority':None})},{}]):
            await collect(self.gateway,self.request)
        self.assertEqual(collection(self.store,self.request)['state'],'ready')
        with self.store._connect() as db:
            row=dict(db.execute('SELECT * FROM jira_collections').fetchone())
        self.assertEqual(row['account_id'],'user-1')
        self.assertNotIn('fixture-secret',str(row))

    async def test_failure_after_first_page_does_not_publish_partial_candidates(self):
        from unittest.mock import patch
        from jira_pool import collect
        from jira_http import JiraClient, JiraHttpError, people
        with patch.object(JiraClient,'identity',return_value='user-1'), patch.object(JiraClient,'developer_field',return_value=None), patch.object(JiraClient,'search',side_effect=[{'TEST-1':('标题',people({},None),{'issueType':None,'priority':None})},JiraHttpError('连接失败')]):
            await collect(self.gateway,self.request)
        self.assertEqual(collection(self.store,self.request)['state'],'failed')
        self.assertEqual(collection(self.store,self.request)['pending'],0)

    def test_bound_query_preserves_full_conditions_and_order(self):
        from jira_runtime import including_query
        query='project = TEST AND assignee = currentUser() AND "处理角色" = "Planner" ORDER BY created ASC'
        self.assertEqual(including_query(query,'TEST-1'),'(project = TEST AND assignee = currentUser() AND "处理角色" = "Planner") AND key IN (TEST-1) ORDER BY created ASC')
