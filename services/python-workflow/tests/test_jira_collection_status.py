import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from workflow_store import WorkflowStore
from jira_collection_status import collection_status
from tests.test_jira_accounts import seed_account


class CollectionStatusTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = WorkflowStore(Path(tmp.name) / 'test.db')
        self.group = str(uuid.uuid4())
        self.account = seed_account(self.store, self.group)
        self.now = datetime.now(timezone.utc)
        self.config = json.dumps({'connectionId': self.account, 'jql': 'project = TEST'})

    def scope(self):
        task = str(uuid.uuid4())
        with self.store._connect() as db:
            db.execute('INSERT INTO jira_polling_scopes(task_id,group_id,config_json) VALUES(?,?,?)',
                       (task, self.group, self.config))
        return task

    def batch(self, task, count=0, state='ready', age=0):
        at = (self.now - timedelta(seconds=age)).isoformat()
        with self.store._connect() as db:
            db.execute('''INSERT INTO jira_collections(request_id,task_id,group_id,name,source,
                config_json,state,created_at,finished_at,count) VALUES(?,?,?,'test','poll',?,?,?,?,?)''',
                (str(uuid.uuid4()), task, self.group, self.config, state, at, at, count))
        return at

    def status(self):
        return collection_status(self.store, self.account, now=self.now)

    def test_all_ranges_must_be_complete_and_empty(self):
        self.assertEqual(self.status()['state'], 'unconfigured')
        first, second = self.scope(), self.scope()
        self.batch(first)
        self.assertEqual(self.status()['state'], 'unknown')
        self.batch(second)
        self.assertEqual(self.status()['state'], 'empty')

    def test_nonempty_history_survives_new_empty_result(self):
        task = self.scope()
        at = self.batch(task, count=1, age=30)
        self.assertEqual(self.status()['state'], 'available')
        self.batch(task)
        result = self.status()
        self.assertEqual(result['state'], 'empty')
        self.assertEqual(result['lastNonemptyAt'], at)

    def test_failure_partial_and_stale_are_not_empty(self):
        task = self.scope()
        self.batch(task, age=121)
        self.assertEqual(self.status()['state'], 'unknown')
        self.batch(task, state='failed', age=10)
        self.assertEqual(self.status()['state'], 'unknown')
        self.batch(task, state='collecting')
        self.assertEqual(self.status()['state'], 'unknown')

    def test_scope_change_and_disabled_account(self):
        task = self.scope()
        self.batch(task)
        old = self.status()['scopeVersion']
        self.scope()
        self.assertNotEqual(self.status()['scopeVersion'], old)
        with self.store._connect() as db:
            db.execute('UPDATE jira_accounts SET enabled=0 WHERE id=?', (self.account,))
        self.assertEqual(self.status()['state'], 'disabled')

    def test_account_version_and_deletion_invalidate_reminders(self):
        task = self.scope()
        self.batch(task)
        version = self.status()['scopeVersion']
        with self.store._connect() as db:
            db.execute('UPDATE jira_accounts SET version=version+1 WHERE id=?', (self.account,))
        self.assertNotEqual(self.status()['scopeVersion'], version)
        with self.store._connect() as db:
            db.execute('UPDATE jira_accounts SET deleted_at=? WHERE id=?',
                       (self.now.isoformat(), self.account))
        self.assertEqual(self.status()['state'], 'deleted')

    def test_current_work_overrides_other_range_failure(self):
        first, second = self.scope(), self.scope()
        self.batch(first, count=1)
        self.batch(second, state='failed')
        self.assertEqual(self.status()['state'], 'available')

    def test_http_contract_and_invalid_id(self):
        from types import SimpleNamespace
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        from jira_routes import jira_routes
        app = Starlette(routes=jira_routes())
        app.state.gateway = SimpleNamespace(store=self.store)
        with TestClient(app) as client:
            response = client.get('/jira-accounts/' + self.account + '/collection-status')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['state'], 'unconfigured')
            self.assertNotIn('fixture-secret', response.text)
            self.assertEqual(client.get('/jira-accounts/not-uuid/collection-status').status_code, 400)
