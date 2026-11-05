import base64
import json
import unittest
from pathlib import PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jira_pool import prepare_run
from jira_runtime import claim_next
from workflow_jira import run_directory


class CentralRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return {'state':'claimed','issueKey':'TEST-1','executorId':'e',
                'config':{'connectionId':'account','jql':'project=TEST'},
                'reservation':{'connectionId':'account','accountId':'jira-user','server':'https://jira.test','accountAlias':'planner','issueKey':'TEST-1'}}

    async def test_only_executor_receives_launcher_and_identity_check_without_token(self):
        context = self.context()
        directory = 'C:\\Users\\worker\\AppData\\Local\\CodexFlowJira\\' + run_directory('run')
        client = AsyncMock()
        client.__aenter__.return_value = client
        stored = {}
        async def request(method, params):
            if method == 'command/exec':
                code = base64.b64decode(params['command'][-1]).decode('utf-16-le')
                self.assertIn('-MaximumRedirection 0', code)
                self.assertIn('identity mismatch', code)
                self.assertNotIn('issue search', code)
                return {'exitCode':0,'stdout':directory}
            if method == 'fs/writeFile':
                stored[params['path']] = base64.b64decode(params['dataBase64'])
                return {}
            if method == 'fs/getMetadata':
                return {'isSymlink':False,'isFile':params['path'] in stored,'isDirectory':params['path'] not in stored}
            if method == 'fs/readFile':
                return {'dataBase64':base64.b64encode(stored[params['path']]).decode()}
            self.fail(method)
        client.request.side_effect = request
        agent = SimpleNamespace(allow_write=True,cwd=r'C:\work')
        orchestrator = SimpleNamespace(get_agent=Mock(return_value=agent))
        store = SimpleNamespace(jira_context=Mock(return_value=context),jira_update=Mock(side_effect=[{'state':'claimed'}, {'state':'ready','issueKey':'TEST-1'}]))
        with patch('jira_runtime.client_for', return_value=client):
            self.assertEqual((await claim_next(store, orchestrator, 'run'))['state'], 'ready')
        orchestrator.get_agent.assert_called_once_with('e')
        self.assertEqual(list(stored), [str(PureWindowsPath(directory)/'jira.ps1')])
        self.assertEqual([c.args[1] for c in store.jira_update.call_args_list], ['stage','ready'])

    async def test_identity_failure_stops_before_file_upload(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.request.return_value = {'exitCode':1,'stdout':''}
        store = SimpleNamespace(jira_context=lambda _:self.context(),jira_update=Mock())
        orchestrator = SimpleNamespace(get_agent=lambda _:SimpleNamespace(allow_write=True,cwd=r'C:\work'))
        with patch('jira_runtime.client_for',return_value=client), self.assertRaisesRegex(RuntimeError,'实际身份'):
            await claim_next(store,orchestrator,'run')
        store.jira_update.assert_not_called()
        self.assertEqual(client.request.call_count,1)

    async def test_unverified_context_cannot_prepare_launcher(self):
        context = self.context();context['state']='waiting_claim'
        store=SimpleNamespace(jira_context=lambda _:context)
        orchestrator=SimpleNamespace(get_agent=lambda _:SimpleNamespace(allow_write=True,cwd=r'C:\work'))
        with self.assertRaisesRegex(ValueError,'中央尚未确认'):
            await claim_next(store,orchestrator,'run')

    def test_gateway_checks_frozen_issue_before_claim_and_marks_it_verified(self):
        context=self.context();context['state']='waiting_claim'
        store=SimpleNamespace(jira_context=lambda _:context,jira_update=Mock(return_value={'state':'claimed'}))
        spec={'workflowId':'run','groupId':'group','jira':context['config']}
        with patch('jira_pool.verify_issue') as verify:
            prepare_run(store,spec)
        verify.assert_called_once_with(store,spec['jira'],'group',context['reservation'],existing=True)
        self.assertEqual(store.jira_update.call_args.args[2]['candidates'], ['TEST-1'])
        self.assertTrue(store.jira_update.call_args.kwargs['central_verified'])

    def test_changed_eligibility_never_claims_another_issue(self):
        context=self.context();context['state']='waiting_claim'
        store=SimpleNamespace(jira_context=lambda _:context,jira_update=Mock())
        with patch('jira_pool.verify_issue',side_effect=ValueError('工单不符合条件')), self.assertRaises(ValueError):
            prepare_run(store,{'workflowId':'run','groupId':'group','jira':context['config']})
        store.jira_update.assert_not_called()
