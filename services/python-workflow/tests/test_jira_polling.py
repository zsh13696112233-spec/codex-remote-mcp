import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from jira_http import JiraClient, JiraHttpError, people
from jira_pool import sync_polling, request_collection, collect_sync, collection, reserve, board
from workflow_store import WorkflowStore
from tests.test_jira_accounts import seed_account


class PollingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = WorkflowStore(Path(tmp.name) / 'poll.db')
        group = str(uuid.uuid4())
        self.task = {'taskDefinitionId': str(uuid.uuid4()), 'groupId': group, 'name': '任务',
                     'jira': {'jql': 'project = TEST', 'connectionId': seed_account(self.store, group)}}

    def sync(self, tasks=None):
        sync_polling(self.store, {'tasks': [self.task] if tasks is None else tasks})
        with self.store._connect() as db:
            return db.execute("SELECT request_id FROM jira_collections WHERE source='poll' ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    def due(self):
        with self.store._connect() as db:
            db.execute("UPDATE jira_collections SET created_at='2020-01-01T00:00:00+00:00' WHERE source='poll'")

    def collect(self, identifier, keys=('TEST-1',)):
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'developer_field', return_value=None), patch.object(JiraClient, 'search', side_effect=[{key: ('标题', people({}, None), {'issueType': None, 'priority': None}) for key in keys}, {}]) as search:
            collect_sync(self.store, identifier)
        return search.call_count

    def dispatch(self, identifier=None):
        identifier = identifier or str(uuid.uuid4())
        return request_collection(self.store, {**self.task, 'requestId': identifier, 'source': 'schedule'})

    def test_sop_transition_retains_history_but_updates_current_sources(self):
        repair, acceptance = str(uuid.uuid4()), str(uuid.uuid4())
        self.task.update(sopId=repair, sopName='修复')
        self.collect(self.sync())
        before = board(self.store)['items'][0]
        self.assertEqual(before['currentSources'][0]['sopId'], repair)
        self.task.update(sopId=acceptance, sopName='验收')
        self.due()
        self.collect(self.sync())
        after = board(self.store)['items'][0]
        self.assertEqual(after['id'], before['id'])
        self.assertEqual([s['sopId'] for s in after['currentSources']], [acceptance])
        self.assertEqual({s['sopId'] for s in after['sources']}, {repair, acceptance})
        self.assertEqual(len(board(self.store, sop_id=repair)['items']), 1)
        self.assertEqual(len(board(self.store, sop_id=str(uuid.uuid4()))['items']), 0)
        self.sync([])
        self.assertEqual(board(self.store)['items'][0]['currentSources'], [])

    def test_manual_acceptance_entry_needs_no_repair_history(self):
        self.task.update(sopId=str(uuid.uuid4()), sopName='验收')
        self.collect(self.sync())
        item = board(self.store)['items'][0]
        self.assertEqual(item['currentSources'][0]['sopName'], '验收')
        self.assertEqual(item['executionCount'], 0)
        request = self.dispatch()
        self.due()
        self.collect(self.sync())
        ready = self.dispatch(request['requestId'])
        self.assertEqual(reserve(self.store, ready['requestId'], str(uuid.uuid4()))['issueKey'], 'TEST-1')

    def test_failed_scan_keeps_known_sop_and_marks_staleness(self):
        self.task.update(sopId=str(uuid.uuid4()), sopName='修复')
        self.collect(self.sync())
        self.due()
        failed = self.sync()
        with patch.object(JiraClient, 'identity', side_effect=JiraHttpError('查询失败')):
            collect_sync(self.store, failed)
        source = board(self.store)['items'][0]['currentSources'][0]
        self.assertEqual(source['sopName'], '修复')
        self.assertEqual(source['collectionState'], 'failed')

    def test_polling_never_authorizes_run_and_repeated_sync_does_not_overlap(self):
        first = self.sync()
        self.due()
        self.assertEqual(self.sync(), first)  # still waiting even after its interval
        self.collect(first)
        with self.assertRaisesRegex(ValueError, '来源已失效'):
            reserve(self.store, first, str(uuid.uuid4()))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workflows').fetchone()[0], 0)
        second = self.sync()
        self.assertNotEqual(first, second)
        self.assertEqual(second, self.sync())

    def test_dispatch_waits_for_post_trigger_snapshot_without_searching(self):
        first = self.sync()
        self.collect(first)
        request = self.dispatch()
        self.assertEqual(request['state'], 'waiting')
        with patch.object(JiraClient, 'search') as search:
            self.dispatch(request['requestId'])
        search.assert_not_called()
        self.due()
        second = self.sync()
        self.collect(second)
        ready = self.dispatch(request['requestId'])
        self.assertEqual(ready['state'], 'ready')
        self.assertEqual(ready['pending'], 1)
        self.assertEqual(reserve(self.store, ready['requestId'], str(uuid.uuid4()))['issueKey'], 'TEST-1')

    def test_success_removes_stale_candidates_but_failure_preserves_them(self):
        self.collect(self.sync())
        self.due()
        failed = self.sync()
        with patch.object(JiraClient, 'identity', side_effect=JiraHttpError('查询失败')):
            collect_sync(self.store, failed)
        self.assertEqual(board(self.store)['items'][0]['state'], 'pending')
        self.due()
        self.collect(self.sync(), ())
        self.assertEqual(board(self.store)['items'][0]['state'], 'invalid')

    def test_changed_config_or_disabled_task_invalidates_old_poll_and_inflight_publish(self):
        first = self.sync()
        with self.store._connect() as db:
            db.execute("UPDATE jira_collections SET state='collecting' WHERE request_id=?", (first,))
        self.task['jira']['jql'] = 'project = OTHER'
        second = self.sync()
        self.assertNotEqual(first, second)
        self.assertEqual(collection(self.store, first)['state'], 'cancelled')
        self.collect(second)
        self.sync([])
        self.assertEqual(collection(self.store, second)['state'], 'cancelled')
        self.assertEqual(board(self.store)['items'][0]['state'], 'cancelled')

    def test_same_account_query_shares_completed_scan_between_tasks(self):
        other = {**self.task, 'taskDefinitionId': str(uuid.uuid4()), 'name': '另一个任务'}
        self.sync([self.task, other])
        with self.store._connect() as db:
            ids = [r[0] for r in db.execute("SELECT request_id FROM jira_collections ORDER BY rowid")]
        self.collect(ids[0])
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'search') as search:
            collect_sync(self.store, ids[1])
        search.assert_not_called()
        self.assertEqual(collection(self.store, ids[1])['pending'], 1)

    def test_repeated_poll_reuses_candidate_and_wait_time_and_survives_reopen(self):
        self.collect(self.sync())
        before = board(self.store)['items'][0]
        self.due()
        self.collect(self.sync())
        self.store = WorkflowStore(self.store.path)
        after = board(self.store)['items'][0]
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['queued_at'], before['queued_at'])
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jira_pool_items').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jira_collections').fetchone()[0], 2)

    def test_poll_refresh_does_not_rewrite_dispatch_snapshot(self):
        request = self.dispatch()
        self.collect(self.sync())
        ready = self.dispatch(request['requestId'])
        binding = reserve(self.store, ready['requestId'], str(uuid.uuid4()))
        self.due()
        next_poll = self.sync()
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'developer_field', return_value=None), patch.object(JiraClient, 'search', side_effect=[{'TEST-1': ('新标题', people({}, None), {'issueType': None, 'priority': None})}, {}]):
            collect_sync(self.store, next_poll)
        self.assertEqual(reserve(self.store, ready['requestId'], binding['reservationId']), binding)
        self.assertEqual(board(self.store)['items'][0]['title'], '新标题')

    def test_disabling_during_http_query_prevents_publication(self):
        identifier = self.sync()
        def search(*args):
            self.sync([])
            return {}
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'developer_field', return_value=None), patch.object(JiraClient, 'search', side_effect=search):
            collect_sync(self.store, identifier)
        self.assertEqual(collection(self.store, identifier)['state'], 'cancelled')
        self.assertEqual(board(self.store)['items'], [])

    def test_existing_schema_adds_candidate_wait_time_without_changing_records(self):
        self.collect(self.sync())
        with self.store._connect() as db:
            db.execute('ALTER TABLE jira_pool_items DROP COLUMN candidate_queued_at')
        self.store = WorkflowStore(self.store.path)
        self.assertEqual(board(self.store)['items'][0]['issue_key'], 'TEST-1')
        self.due()
        self.collect(self.sync())
        self.assertIsNotNone(board(self.store)['items'][0]['queued_at'])

    def test_invalid_account_does_not_block_other_polling_tasks(self):
        first = self.sync()
        with self.store._connect() as db:
            db.execute('UPDATE jira_accounts SET enabled=0 WHERE id=?', (self.task['jira']['connectionId'],))
        collect_sync(self.store, first)
        self.assertEqual(collection(self.store, first)['state'], 'failed')
        self.assertEqual(collection(self.store, first)['pending'], 0)

    def test_details_are_collected_and_shared_without_changing_reservation(self):
        request = self.dispatch()
        identifier = self.sync()
        details = {'issueType': 'sub-bug', 'priority': 'P4'}
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'developer_field', return_value=None), patch.object(JiraClient, 'search', side_effect=[{'TEST-1': ('标题', people({}, None), details)}, {}]):
            collect_sync(self.store, identifier)
        ready = self.dispatch(request['requestId'])
        binding = reserve(self.store, ready['requestId'], str(uuid.uuid4()))
        item = board(self.store)['items'][0]
        self.assertEqual((item['issueType'], item['priority']), ('sub-bug', 'P4'))
        self.assertNotIn('issueType', binding)
        self.assertEqual(reserve(self.store, ready['requestId'], binding['reservationId']), binding)

    def old_details(self, keys=('TEST-1',)):
        self.collect(self.sync(), keys)
        self.sync([])  # Historical issues no longer qualify for automatic collection.
        with self.store._connect() as db:
            db.execute('UPDATE jira_pool_items SET details_json=NULL,details_checked_at=NULL')
            return [tuple(r) for r in db.execute('SELECT id,state,workflow_id,people_json,candidate_queued_at FROM jira_pool_items')]

    def test_backfill_covers_inactive_history_and_missing_issues_without_touching_state(self):
        from jira_issue_details import backfill
        before = self.old_details(('TEST-1', 'TEST-2'))
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'search', side_effect=[{'TEST-1': ('标题', {}, {'issueType':'sub-bug','priority':'P4'})}, {}]) as search:
            backfill(self.store)
        self.assertTrue(all('project = TEST' not in call.args[0] for call in search.call_args_list))
        items = {r['issue_key']: r for r in board(self.store)['items']}
        self.assertEqual(items['TEST-1']['issueType'], 'sub-bug')
        self.assertEqual(items['TEST-1']['priority'], 'P4')
        self.assertIsNone(items['TEST-2']['priority'])
        with self.store._connect() as db:
            after = [tuple(r) for r in db.execute('SELECT id,state,workflow_id,people_json,candidate_queued_at FROM jira_pool_items')]
            self.assertEqual(before, after)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workflows').fetchone()[0], 0)
        self.store = WorkflowStore(self.store.path)
        with patch.object(JiraClient, 'search') as search:
            backfill(self.store)
        search.assert_not_called()

    def test_backfill_failure_is_logged_and_deferred_without_hiding_missing_data(self):
        from jira_issue_details import backfill
        self.old_details()
        with patch.object(JiraClient, 'identity', side_effect=JiraHttpError('无法连接 Jira。')), self.assertLogs('jira_issue_details', level='WARNING'):
            backfill(self.store)
        with self.store._connect() as db:
            row = db.execute('SELECT details_json,details_checked_at FROM jira_pool_items').fetchone()
            self.assertIsNone(row[0])
            self.assertIsNotNone(row[1])
        with patch.object(JiraClient, 'identity') as identity:
            backfill(self.store)
        identity.assert_not_called()

    def test_backfill_handles_server_page_limit_and_does_not_overwrite_new_poll(self):
        from jira_issue_details import backfill
        self.old_details(('TEST-1', 'TEST-2'))
        calls = []
        def search(jql):
            calls.append(jql)
            key = 'TEST-1' if len(calls) == 1 else 'TEST-2'
            if len(calls) == 2:
                with self.store._connect() as db:
                    db.execute('UPDATE jira_pool_items SET details_json=? WHERE issue_key=?', ('{"issueType":"新类型","priority":"P1"}', 'TEST-1'))
            return {key: ('标题', {}, {'issueType':'sub-bug','priority':'P4'})}
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'search', side_effect=search):
            backfill(self.store)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('TEST-1', calls[1])
        items = {r['issue_key']: r for r in board(self.store)['items']}
        self.assertEqual(items['TEST-1']['priority'], 'P1')
        self.assertEqual(items['TEST-2']['priority'], 'P4')

    def test_old_schema_adds_details_columns_without_rewriting_history(self):
        self.old_details()
        with self.store._connect() as db:
            db.execute('ALTER TABLE jira_pool_items DROP COLUMN details_json')
            db.execute('ALTER TABLE jira_pool_items DROP COLUMN details_checked_at')
        self.store = WorkflowStore(self.store.path)
        item = board(self.store)['items'][0]
        self.assertEqual(item['issue_key'], 'TEST-1')
        self.assertIsNone(item['issueType'])

    def test_backfill_tries_other_same_group_accounts_and_rejects_changed_credentials(self):
        from jira_issue_details import backfill
        self.old_details()
        seed_account(self.store, self.task['groupId'])
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'search', side_effect=[{}, {'TEST-1': ('标题', {}, {'issueType':'sub-bug','priority':'P4'})}]):
            backfill(self.store)
        self.assertEqual(board(self.store)['items'][0]['priority'], 'P4')
        with self.store._connect() as db:
            db.execute('UPDATE jira_pool_items SET details_json=NULL,details_checked_at=NULL')
        def changed(jql):
            with self.store._connect() as db:
                db.execute('UPDATE jira_accounts SET enabled=0,version=version+1')
            return {'TEST-1': ('标题', {}, {'issueType':'sub-bug','priority':'P4'})}
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'search', side_effect=changed), self.assertLogs('jira_issue_details', level='WARNING'):
            backfill(self.store)
        self.assertIsNone(board(self.store)['items'][0]['priority'])

    def test_backfill_batches_all_history_and_ignores_other_group_accounts(self):
        from jira_issue_details import backfill
        self.old_details(tuple('TEST-'+str(i) for i in range(1, 52)))
        def search(jql):
            import re
            return {key: ('标题', {}, {'issueType':'bug','priority':'P4'}) for key in re.findall(r'"(TEST-\d+)"', jql)}
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'search', side_effect=search) as query:
            backfill(self.store)
            backfill(self.store)
        self.assertEqual(query.call_count, 2)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jira_pool_items WHERE details_json IS NOT NULL').fetchone()[0], 51)
            db.execute('UPDATE jira_pool_items SET details_json=NULL,details_checked_at=NULL')
            db.execute('UPDATE jira_accounts SET group_id=?', (str(uuid.uuid4()),))
        with patch.object(JiraClient, 'identity') as identity:
            backfill(self.store)
        identity.assert_not_called()

    def test_gateway_tick_backfills_even_without_enabled_collection_tasks(self):
        import asyncio
        from types import SimpleNamespace
        from jira_pool import tick
        self.old_details()
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'search', return_value={'TEST-1': ('标题', {}, {'issueType':'sub-bug','priority':'P4'})}):
            asyncio.run(tick(SimpleNamespace(store=self.store)))
        self.assertEqual(board(self.store)['items'][0]['priority'], 'P4')

    def test_invalid_or_absent_display_fields_are_nullable(self):
        from jira_http import issue_details
        for value in (None, {}, {'name':''}, {'name':1}, {'name':'x'*161}):
            self.assertEqual(issue_details({'issuetype':value,'priority':value}), {'jiraStatus':None,'issueType':None,'priority':None})

    def test_missing_details_from_another_source_do_not_hide_known_values(self):
        request = self.dispatch()
        self.collect(self.sync())
        self.dispatch(request['requestId'])
        with self.store._connect() as db:
            db.execute('UPDATE jira_pool_items SET details_json=?,details_checked_at=? WHERE request_id=?',
                       ('{"issueType":"sub-bug","priority":"P4"}', '2026-01-01T00:00:00+00:00', request['requestId']))
            db.execute('UPDATE jira_pool_items SET details_json=?,details_checked_at=? WHERE request_id<>?',
                       ('{"issueType":null,"priority":null}', '2026-02-01T00:00:00+00:00', request['requestId']))
        self.assertEqual(board(self.store)['items'][0]['priority'], 'P4')
