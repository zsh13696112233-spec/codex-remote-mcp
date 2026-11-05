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
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'developer_field', return_value=None), patch.object(JiraClient, 'search', side_effect=[{key: ('标题', people({}, None)) for key in keys}, {}]) as search:
            collect_sync(self.store, identifier)
        return search.call_count

    def dispatch(self, identifier=None):
        identifier = identifier or str(uuid.uuid4())
        return request_collection(self.store, {**self.task, 'requestId': identifier, 'source': 'schedule'})

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
        with patch.object(JiraClient, 'identity', return_value='bot'), patch.object(JiraClient, 'developer_field', return_value=None), patch.object(JiraClient, 'search', side_effect=[{'TEST-1': ('新标题', people({}, None))}, {}]):
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
