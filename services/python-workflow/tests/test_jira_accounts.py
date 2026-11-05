import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import jira_accounts as accounts
from jira_http import JiraClient, JiraHttpError, people
from jira_pool import request_collection, collection, collect_sync, reserve, verify_issue
from workflow_store import WorkflowStore


def seed_account(store, group, identity='bot'):
    identifier = str(uuid.uuid4())
    with store._connect() as db:
        db.execute("INSERT INTO jira_accounts(id,group_id,name,server,token,executor_alias,enabled,test_status,identity,username) VALUES(?,?,?,'http://jira.test','fixture-secret','bot',1,'passed',?,'bot')", (identifier, group, identifier, identity))
    return identifier


class AccountsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = WorkflowStore(Path(tmp.name) / 'accounts.db')
        self.group = str(uuid.uuid4())
        with self.store._connect() as db:
            db.execute('CREATE TABLE agent_groups(id TEXT PRIMARY KEY,name TEXT,deleted_at TEXT)')
            db.execute('INSERT INTO agent_groups VALUES(?,?,NULL)', (self.group, 'group'))
        self.body = dict(groupId=self.group, name='测试账号', server='http://jira.test/', token='fixture-secret', enabled=True)

    def create(self):
        return accounts.save(self.store, None, self.body)

    def passed(self):
        row = self.create()
        with patch.object(JiraClient, 'profile', return_value={'identity':'user-1','username':'login-user'}):
            return accounts.detect(self.store, row['id'], row['version'])

    def test_create_is_untested_and_never_returns_token(self):
        row = self.create()
        self.assertEqual(row['testStatus'], 'untested')
        self.assertNotIn('fixture-secret', json.dumps(accounts.listing(self.store)))
        with self.store._connect() as db, self.assertRaises(ValueError):
            accounts.usable(db, row['id'], self.group)
        self.assertEqual(accounts.listing(self.store, str(uuid.uuid4()))['accounts'], [])

    def test_name_unique_per_group_and_logical_delete_clears_token(self):
        row = self.create()
        with self.assertRaises(accounts.AccountConflict):
            self.create()
        accounts.delete(self.store, row['id'], row['version'])
        with self.store._connect() as db:
            self.assertIsNone(db.execute('SELECT token FROM jira_accounts').fetchone()[0])
        self.assertNotEqual(self.create()['id'], row['id'])

    def test_token_replacement_requires_same_identity_and_keeps_old_on_failure(self):
        row = self.passed()
        body = {**self.body, 'version': row['version'], 'token': 'replacement-secret'}
        with patch.object(JiraClient, 'profile', return_value={'identity':'other','username':'other-user'}), self.assertRaises(ValueError):
            accounts.save(self.store, row['id'], body)
        with self.store._connect() as db:
            self.assertEqual(accounts.get(db, row['id'])['token'], 'fixture-secret')
        with patch.object(JiraClient, 'profile', return_value={'identity':'user-1','username':'login-user'}):
            changed = accounts.save(self.store, row['id'], body)
        self.assertEqual(changed['version'], row['version'] + 1)
        self.assertNotIn('replacement-secret', json.dumps(changed))

    def test_blank_token_keeps_secret_and_version_prevents_overwrite(self):
        row = self.passed()
        changed = accounts.save(self.store, row['id'], {**self.body, 'token': '', 'version': row['version'], 'name': '新名称'})
        with self.store._connect() as db:
            self.assertEqual(accounts.get(db, row['id'])['token'], 'fixture-secret')
        with self.assertRaises(accounts.AccountConflict):
            accounts.detect(self.store, row['id'], row['version'])
        self.assertEqual(changed['identity'], 'user-1')
        self.assertEqual(changed['executorAlias'], 'login-user')

    def test_stale_detection_cannot_overwrite_concurrent_change(self):
        row = self.create()
        def changing():
            accounts.save(self.store, row['id'], {**self.body, 'token': '', 'version': row['version'], 'enabled': False})
            return {'identity':'user-1','username':'login-user'}
        with patch.object(JiraClient, 'profile', side_effect=changing), self.assertRaises(accounts.AccountConflict):
            accounts.detect(self.store, row['id'], row['version'])
        self.assertEqual(accounts.listing(self.store)['accounts'][0]['testStatus'], 'untested')

    def test_disable_blocks_new_use_but_existing_run_identity_survives(self):
        row = self.passed()
        accounts.save(self.store, row['id'], {**self.body, 'token': '', 'version': row['version'], 'enabled': False})
        with self.store._connect() as db:
            with self.assertRaises(ValueError): accounts.usable(db, row['id'], self.group)
            self.assertEqual(accounts.usable(db, row['id'], self.group, existing=True)['identity'], 'user-1')
            with self.assertRaises(ValueError): accounts.usable(db, row['id'], str(uuid.uuid4()), existing=True)

    def test_pending_source_prevents_delete(self):
        row = self.passed()
        request_collection(self.store, dict(requestId=str(uuid.uuid4()), taskDefinitionId=str(uuid.uuid4()), groupId=self.group, source='web', jira={'jql': 'project=TEST', 'connectionId': row['id']}))
        with self.assertRaises(accounts.AccountConflict): accounts.delete(self.store, row['id'], row['version'])

    def test_failed_test_keeps_identity(self):
        row = self.passed()
        with patch.object(JiraClient, 'profile', side_effect=JiraHttpError('无法连接 Jira。')):
            failed = accounts.detect(self.store, row['id'], row['version'])
        self.assertEqual(failed['testStatus'], 'failed')
        self.assertEqual(failed['identity'], 'user-1')

    def test_invalid_token_and_new_server_are_rejected(self):
        for token in ('bad\nheader', 'bad token', '中文'):
            with self.assertRaises(ValueError): accounts.save(self.store, None, {**self.body, 'token': token})
        row = self.create()
        with self.assertRaises(ValueError): accounts.save(self.store, row['id'], {**self.body, 'version': 1, 'server': 'https://other.test'})

    def test_existing_schema_keeps_identity_and_adds_username_on_detection(self):
        row = self.passed()
        with self.store._connect() as db:
            db.execute('ALTER TABLE jira_accounts DROP COLUMN username')
        self.store = WorkflowStore(self.store.path)
        old = accounts.listing(self.store)['accounts'][0]
        self.assertEqual(old['identity'], row['identity'])
        self.assertEqual(old['version'], row['version'])
        self.assertIsNone(old['username'])
        with patch.object(JiraClient, 'request', return_value={'key':'user-1','name':'renamed-login'}):
            detected = accounts.detect(self.store, row['id'], row['version'])
        self.assertEqual(detected['username'], 'renamed-login')
        self.assertEqual(detected['identity'], 'user-1')
        reopened = WorkflowStore(self.store.path)
        self.assertEqual(accounts.listing(reopened)['accounts'][0]['username'], 'renamed-login')

    def test_detection_failure_or_identity_change_keeps_verified_username(self):
        row = self.passed()
        for response in ({'key':'other-user','name':'another-login'}, JiraHttpError('连接失败')):
            with patch.object(JiraClient, 'request', side_effect=[response]):
                row = accounts.detect(self.store, row['id'], row['version'])
            self.assertEqual(row['testStatus'], 'failed')
            self.assertEqual(row['identity'], 'user-1')
            self.assertEqual(row['username'], 'login-user')

    def test_token_rotation_updates_login_name_without_changing_identity(self):
        row = self.passed()
        with patch.object(JiraClient, 'request', return_value={'key':'user-1','name':'new-login'}) as request:
            result = accounts.save(self.store, row['id'], {**self.body,'version':row['version'],'token':'new-fixture-token'})
        request.assert_called_once_with('myself')
        self.assertEqual(result['identity'], 'user-1')
        self.assertEqual(result['username'], 'new-login')
        self.assertEqual(result['executorAlias'], 'new-login')

    def test_executor_account_cannot_be_manually_set(self):
        with self.assertRaises(ValueError):
            accounts.save(self.store, None, {**self.body, 'executorAlias':'manual'})
        row = self.create()
        self.assertEqual(row['executorAlias'], '')

    def test_missing_login_fails_detection_and_does_not_fall_back_to_identity(self):
        row = self.create()
        for username in (None, 'x'*129):
            with patch.object(JiraClient, 'profile', return_value={'identity':'JIRAUSER123','username':username}):
                row = accounts.detect(self.store, row['id'], row['version'])
            self.assertEqual(row['testStatus'], 'failed')
            self.assertEqual(row['executorAlias'], '')
            self.assertIn('自动映射', row['testMessage'])

    def test_old_manual_alias_requires_redetection_and_is_not_exposed_as_mapping(self):
        row = self.passed()
        with self.store._connect() as db:
            db.execute("UPDATE jira_accounts SET executor_alias='old-manual' WHERE id=?", (row['id'],))
            with self.assertRaisesRegex(ValueError, '重新检测'):
                accounts.usable(db, row['id'], self.group)
            self.assertEqual(accounts.usable(db, row['id'], self.group, existing=True)['identity'], row['identity'])
        self.assertEqual(accounts.listing(self.store)['accounts'][0]['executorAlias'], '')
        with patch.object(JiraClient, 'profile', return_value={'identity':'user-1','username':'login-user'}):
            mapped = accounts.detect(self.store, row['id'], row['version'])
        self.assertEqual(mapped['executorAlias'], 'login-user')


class HttpTests(unittest.TestCase):
    def test_identity_and_login_name_are_separate_from_one_response(self):
        client = JiraClient('http://jira.test', 'fixture')
        with patch.object(client, 'request', return_value={'key':'JIRAUSER123','name':'login-user','displayName':'显示名称'}) as request:
            self.assertEqual(client.profile(), {'identity':'JIRAUSER123','username':'login-user'})
            request.assert_called_once_with('myself')
        for name in (None, '', 1, 'bad\nname', 'x'*257):
            with patch.object(client, 'request', return_value={'accountId':'stable','key':'other','name':name,'displayName':'显示名称'}):
                self.assertEqual(client.profile(), {'identity':'stable','username':None})
                self.assertEqual(client.identity(), 'stable')

    def test_people_empty_multiple_and_ambiguous_field(self):
        self.assertEqual(people({'customfield_1': None}, 'customfield_1')['developerStatus'], 'empty')
        self.assertEqual(people({'customfield_1': [{'displayName':'甲'}, {'displayName':'乙'}]}, 'customfield_1')['developers'], ['甲','乙'])
        self.assertEqual(people({}, None)['developerStatus'], 'unavailable')
        with patch.object(JiraClient, 'request', return_value=[{'name':'开发人','id':'customfield_1'}, {'name':'开发人','id':'customfield_2'}]):
            self.assertIsNone(JiraClient('http://jira.test', 'fixture').developer_field())

    def test_search_rejects_partial_or_invalid_pagination(self):
        client = JiraClient('http://jira.test', 'fixture')
        for value in ({'issues':[], 'total':2, 'startAt':0}, {'issues':[], 'total':0, 'startAt':1}, {}):
            with patch.object(client, 'request', return_value=value), self.assertRaises(JiraHttpError): client.search('project=TEST')

    def test_search_requests_all_personnel_fields_once(self):
        client = JiraClient('http://jira.test', 'fixture')
        with patch.object(client, 'request', return_value={'issues':[{'key':'TEST-1','fields':{'summary':'标题','assignee':{'displayName':'甲'},'customfield_1':None,'issuetype':{'name':'sub-bug'},'priority':{'name':'P4'}}}], 'total':1,'startAt':0}) as request:
            result = client.search('project=TEST', 'customfield_1')
        self.assertEqual(result['TEST-1'][1]['assignee'], '甲')
        self.assertEqual(result['TEST-1'][2], {'issueType':'sub-bug', 'priority':'P4'})
        self.assertEqual(request.call_args.args[1]['fields'], ['summary','assignee','issuetype','priority','customfield_1'])

    def test_redirects_are_not_followed(self):
        from jira_http import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'http://elsewhere.test'))

class HttpBoundaryTests(unittest.TestCase):
    def test_response_size_and_http_errors_do_not_expose_credentials(self):
        from unittest.mock import MagicMock
        from urllib.error import HTTPError
        import io
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = b'x' * (4 * 1024 * 1024 + 1)
        with patch('urllib.request.build_opener', return_value=opener), self.assertRaisesRegex(JiraHttpError, '过大'):
            JiraClient('http://jira.test', 'fixture-secret').request('myself')
        opener.open.side_effect = HTTPError('http://jira.test', 401, 'fixture-secret', {}, io.BytesIO(b'fixture-secret'))
        with patch('urllib.request.build_opener', return_value=opener), self.assertRaises(JiraHttpError) as error:
            JiraClient('http://jira.test', 'fixture-secret').request('myself')
        self.assertNotIn('fixture-secret', str(error.exception))

    def test_request_timeout_and_bearer_header(self):
        from unittest.mock import MagicMock
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value=b'{"key":"user-1"}'
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(JiraClient('http://jira.test', 'fixture-secret').identity(),'user-1')
        self.assertEqual(opener.open.call_args.kwargs['timeout'],20)
        self.assertEqual(opener.open.call_args.args[0].get_header('Authorization'),'Bearer fixture-secret')
