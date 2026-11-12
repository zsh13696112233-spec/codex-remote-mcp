import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from jira_http import JiraClient, JiraHttpError
from jira_options import options, role_options, project_options
from jira_accounts import detect, AccountConflict
from workflow_store import WorkflowStore
from tests.test_jira_accounts import seed_account


class JiraOptionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WorkflowStore(Path(self.tmp.name) / 'test.db')
        self.account = seed_account(self.store, str(uuid.uuid4()))
        self.project = {'id': '10', 'key': 'TEST', 'name': '测试项目',
                        'issueTypes': [{'id': '6', 'name': 'sub-bug'}, {'id': '7', 'name': '任务'}]}
        self.calls = []
        self.mock = patch.object(JiraClient, 'request', side_effect=self.request).start()
        self.addCleanup(patch.stopall)

    def request(self, path, body=None):
        self.calls.append(path)
        if path == 'project':
            return [self.project]
        if path == 'project/TEST':
            return self.project
        if path == 'project/TEST/statuses':
            return [{'id': '6', 'statuses': [{'id': '1', 'name': 'Open'}]},
                    {'id': '7', 'statuses': [{'id': '2', 'name': '待办'}]}]
        if path.startswith('issue/createmeta/TEST/issuetypes/'):
            return {'startAt': 0, 'total': 1, 'values': [{'name': '处理角色', 'fieldId': 'customfield_1',
                'allowedValues': [{'id': '101', 'value': 'Planner'}, {'id': '102', 'value': 'Reviewer'}]}]}
        raise AssertionError(path)

    def test_scoped_options_and_persistent_cache_have_no_credentials(self):
        result = options(self.store, self.account, 'TEST', '6')
        self.assertEqual(result['statuses'], [{'id': '1', 'name': 'Open'}])
        self.assertEqual([r['name'] for r in result['roles']], ['Planner', 'Reviewer'])
        calls = len(self.calls)
        reopened = WorkflowStore(self.store.path)
        self.assertEqual(options(reopened, self.account, 'TEST', '6'), result)
        self.assertEqual(len(self.calls), calls)
        with self.store._connect() as db:
            value = db.execute('SELECT payload_json FROM jira_account_options').fetchone()[0]
        self.assertNotIn('fixture-secret', value)
        options(self.store, self.account, 'TEST', '6', True)
        self.assertGreater(len(self.calls), calls)

    def test_unrestricted_type_unions_real_options_and_invalid_type_retains_types(self):
        result = options(self.store, self.account, 'TEST')
        self.assertEqual({s['id'] for s in result['statuses']}, {'1', '2'})
        self.assertEqual(len(result['roles']), 2)
        result = options(self.store, self.account, 'TEST', 'removed')
        self.assertEqual(len(result['issueTypes']), 2)
        self.assertTrue(result['rolesError'])

    def test_multiple_types_union_options_cache_and_reject_partial_matches(self):
        result = options(self.store, self.account, 'TEST', 'sub-bug, 7')
        self.assertEqual({s['id'] for s in result['statuses']}, {'1', '2'})
        self.assertEqual(len(result['roles']), 2)
        calls = len(self.calls)
        self.assertEqual(options(self.store, self.account, 'TEST', 'sub-bug, 7'), result)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(len(options(self.store, self.account, 'TEST', '6')['statuses']), 1)
        invalid = options(self.store, self.account, 'TEST', '6, removed')
        self.assertTrue(invalid['rolesError'])
        self.assertEqual(invalid['statuses'], [])
        self.assertEqual(invalid['roles'], [])

    def test_multiple_types_do_not_return_partial_roles_on_failure(self):
        def partial(path, body=None):
            if '/issuetypes/7?' in path:
                raise JiraHttpError('无法读取角色选项。')
            return self.request(path, body)
        self.mock.side_effect = partial
        result = options(self.store, self.account, 'TEST', '6, 7')
        self.assertTrue(result['rolesError'])
        self.assertEqual(result['roles'], [])
        self.assertEqual(len(result['statuses']), 2)

    def test_version_change_and_disable_invalidate_cache(self):
        options(self.store, self.account)
        with self.store._connect() as db:
            db.execute('UPDATE jira_accounts SET version=version+1 WHERE id=?', (self.account,))
        options(self.store, self.account)
        self.assertEqual(self.calls.count('project'), 2)
        with self.store._connect() as db:
            db.execute('UPDATE jira_accounts SET enabled=0 WHERE id=?', (self.account,))
        with self.assertRaises(ValueError):
            options(self.store, self.account)

    def test_concurrent_account_change_cannot_cache_old_results(self):
        def changing(path, body=None):
            with self.store._connect() as db:
                db.execute('UPDATE jira_accounts SET version=version+1 WHERE id=?', (self.account,))
            return [self.project]
        self.mock.side_effect = changing
        with self.assertRaises(AccountConflict):
            options(self.store, self.account)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jira_account_options').fetchone()[0], 0)

    def test_failed_refresh_preserves_cache_and_role_failure_is_explicit(self):
        original = options(self.store, self.account)
        self.mock.side_effect = JiraHttpError('无法连接 Jira。')
        with self.assertRaises(JiraHttpError):
            options(self.store, self.account, refresh=True)
        self.assertEqual(options(self.store, self.account), original)
        def partial(path, body=None):
            if 'createmeta' in path:
                raise JiraHttpError('无法读取角色选项。')
            return self.request(path, body)
        self.mock.side_effect = partial
        result = options(self.store, self.account, 'TEST', '6')
        self.assertTrue(result['rolesError'])
        self.assertEqual(result['roles'], [])
        self.assertTrue(result['statuses'])

    def test_role_pagination_is_complete_or_fails(self):
        pages = [{'startAt': 0, 'total': 2, 'values': [{'name': '标题'}]},
                 {'startAt': 1, 'total': 2, 'values': [{'name': '处理角色', 'allowedValues': [{'id': '1', 'value': 'Planner'}]}]}]
        client = SimpleNamespace(request=lambda path: pages.pop(0))
        self.assertEqual(role_options(client, 'TEST', '6'), [{'id': '1', 'name': 'Planner'}])
        client = SimpleNamespace(request=lambda path: {'startAt': 0, 'total': 2, 'values': []})
        with self.assertRaises(JiraHttpError):
            role_options(client, 'TEST', '6')

    def test_detection_fetches_projects_and_keeps_auth_success_on_metadata_failure(self):
        with patch.object(JiraClient, 'profile', return_value={'identity': 'bot', 'username': 'bot'}):
            result = detect(self.store, self.account, 1)
            self.assertEqual(result['projectCount'], 1)
            self.mock.side_effect = JiraHttpError('无法读取项目。')
            result = detect(self.store, self.account, result['version'])
            self.assertEqual(result['testStatus'], 'passed')
            self.assertIn('项目列表读取失败', result['optionsMessage'])

    def test_invalid_identifiers_and_metadata_are_rejected(self):
        for project, issue_type in [('', '6'), ('TEST\n', ''), ('x' * 1001, '')]:
            with self.assertRaises(ValueError):
                options(self.store, self.account, project, issue_type)
        self.mock.side_effect = lambda path, body=None: {'unexpected': True}
        with self.assertRaises(JiraHttpError):
            options(self.store, self.account)

    def test_http_options_contract_and_invalid_refresh(self):
        import asyncio
        from jira_routes import jira_account_options
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway=SimpleNamespace(store=self.store))),
            path_params={'account_id': self.account}, query_params={'project': 'TEST', 'issueType': '6'})
        response = asyncio.run(jira_account_options(request))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)['statuses'][0]['id'], '1')
        request.query_params['issueType'] = '6, 7'
        response = asyncio.run(jira_account_options(request))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(json.loads(response.body)['statuses']), 2)
        request.query_params['refresh'] = 'invalid'
        self.assertEqual(asyncio.run(jira_account_options(request)).status_code, 400)
