import base64
import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from agent_registry import AgentRegistry
from mcp_deployment import McpDeployment
from mcp_store import ensure_available
from skill_packages import SkillError
from skill_store import SkillStore
from workflow_store import WorkflowStore
from tests.test_mcp_installation import bundle


class McpStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = patch('workflow_service_config._load', return_value={"machine_defaults": {
            "cwd": r"C:\work\default", "protocol": "ws", "allow_write": True,
            "token_env": "TEST_MCP_TOKEN"}})
        self.config.start()
        self.addCleanup(self.config.stop)
        self.store = WorkflowStore(root / 'runtime.db')
        self.registry = AgentRegistry(self.store)
        SkillStore(self.store)
        self.manager = McpDeployment(SimpleNamespace(store=self.store, registry=self.registry),
                                     {"package_root": str(root / 'packages')})
        self.group = self.registry.save_group({"name": "测试组"})['id']
        self.machine = self.registry.save_agent({"ip": "127.0.0.1", "port": 4500, "groupId": self.group,
            "capabilities": ["executor"], "mcpInstallation": {"enabled": True,
            "temporaryRoot": r"C:\deploy\temp", "programRoot": r"C:\deploy\program"}})
        self.agent = self.machine['agentId']
        self.registry.record_test(self.agent, True)
        self.package = self.manager.upload(bundle([('demo.exe', b'binary')]), self.group)
        self.body = {"requestId": str(uuid.uuid4()), "packageId": self.package['id'],
                     "groupId": self.group, "agentIds": [self.agent]}

    def test_upload_and_request_idempotency(self):
        first = self.manager.create(self.body)
        self.assertEqual(first, self.manager.create(self.body))
        self.assertEqual(len(first['tasks']), 1)
        with self.assertRaises(SkillError):
            self.manager.create({**self.body, "agentIds": []})

    def test_occupied_machine_blocks_registration_and_business(self):
        self.manager.create(self.body)
        task = self.manager.store.claim()
        self.assertIsNotNone(task)
        with self.store._connect() as db, self.assertRaises(ValueError):
            ensure_available(db, [self.agent])
        with self.assertRaises(ValueError):
            self.registry.save_agent(self.machine, self.agent)
        self.assertIsNone(self.manager.store.claim())

    def test_recovery_preserves_occupation_and_requires_check(self):
        self.manager.create(self.body)
        task = self.manager.store.claim()
        self.manager.store.update(task['id'], execution_started=1)
        self.manager.store.recover()
        self.assertEqual(self.manager.store.tasks()[0]['state'], 'review')
        self.assertEqual(self.manager.store.tasks()[0]['occupied'], 1)
        with self.assertRaises(SkillError):
            self.manager.store.action(task['id'], str(uuid.uuid4()), 'retry')
        self.manager.store.action(task['id'], str(uuid.uuid4()), 'check')
        self.assertEqual(self.manager.store.claim()['mode'], 'check')

    def test_preparation_recovery_releases_machine_without_replaying(self):
        self.manager.create(self.body)
        self.manager.store.claim()
        self.manager.store.recover()
        task = self.manager.store.tasks()[0]
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(task['occupied'], 0)
        self.assertIsNone(self.manager.store.claim())

    def test_group_membership_is_required(self):
        other = self.registry.save_group({"name": "其他组"})['id']
        with self.assertRaises(SkillError):
            self.manager.create({**self.body, 'groupId': other})
        with self.assertRaises(ValueError):
            self.registry.delete_group(self.group)

    def test_workflow_submission_rework_and_central_dispatch_obey_occupation(self):
        with patch('agent_registry.sidecar_token_path', return_value=r'C:\test\sidecar.token'):
            self.registry.save_agent({**self.machine, 'capabilities': ['supervisor', 'executor']}, self.agent)
        self.registry.record_test(self.agent, True)
        spec = {'workflowId': 'mcp-interlock', 'name': '互斥测试', 'supervisorAgentId': self.agent,
                'nodes': [{'id': 'a', 'agentId': self.agent, 'prompt': 'test'}]}
        self.store.create_workflow(spec)
        self.manager.create(self.body)
        self.assertIsNone(self.manager.store.claim())
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET status='failed' WHERE workflow_id='mcp-interlock'")
        self.assertIsNotNone(self.manager.store.claim())
        with self.assertRaises(ValueError):
            self.store.create_workflow({**spec, 'workflowId': 'another'})
        with self.assertRaises(ValueError):
            self.store.restart_from_node('mcp-interlock', 'a', revision_instruction='重试')
        with self.assertRaises(ValueError):
            self.store.prepare_node_dispatch('mcp-interlock', 'a')

    def test_settings_do_not_elevate_machine_permissions(self):
        self.assertFalse(self.registry.configs()[self.agent].allow_full_access)
        self.assertTrue(self.registry.mcp_settings(self.agent)['enabled'])
        self.registry.save_agent({k: v for k, v in self.machine.items() if k != 'mcpInstallation'}, self.agent)
        self.assertTrue(self.registry.mcp_settings(self.agent)['enabled'])

    def test_revoked_authorization_cannot_be_reused_by_old_task(self):
        self.manager.create(self.body)
        task = self.manager.store.claim()
        self.manager.store.update(task['id'], state='failed', occupied=0)
        rule = self.registry.mcp_settings(self.agent)
        with self.store._connect() as db:
            db.execute('UPDATE agent_mcp_settings SET settings=? WHERE agent_id=?',
                       (json.dumps({**rule, 'enabled': False}), self.agent))
        with self.assertRaisesRegex(SkillError, '授权'):
            asyncio.run(self.manager.run(task))

    def test_action_idempotency_and_invalid_action(self):
        self.manager.create(self.body)
        task = self.manager.store.claim()
        self.manager.store.update(task['id'], state='failed', occupied=0)
        key = str(uuid.uuid4())
        self.manager.store.action(task['id'], key, 'retry')
        self.manager.store.action(task['id'], key, 'retry')
        with self.assertRaises(SkillError):
            self.manager.store.action(task['id'], key, 'check')

    def test_public_state_excludes_internal_fields(self):
        self.manager.create(self.body)
        task = self.manager.store.tasks()[0]
        public = self.manager.store.public(task)
        for key in ('snapshot', 'thread_id', 'turn_id', 'registration', 'result'):
            self.assertNotIn(key, public)

    def test_diagnostics_persist_and_restart_does_not_leave_pending(self):
        from mcp_diagnostics import diagnostic
        from mcp_store import McpStore
        self.manager.create(self.body)
        task = self.manager.store.claim()
        self.manager.store.update(task['id'], execution_started=1,
                                  diagnostics=json.dumps(diagnostic('连接与工具发现', pending=True)))
        reopened = McpStore(self.store)
        self.assertTrue(reopened.batch(task['batch_id'])['tasks'][0]['diagnostics']['pending'])
        reopened.recover()
        value = reopened.batch(task['batch_id'])['tasks'][0]['diagnostics']
        self.assertFalse(value['pending'])
        self.assertIn('中断', value['reason'])

    def test_http_upload_group_and_async_install(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        from mcp_routes import mcp_routes
        app = Starlette(routes=mcp_routes())
        app.state.mcp_deployment = self.manager
        with TestClient(app) as client:
            self.assertEqual(client.get('/mcp-packages?groupId=bad').status_code, 400)
            response = client.post('/mcp-packages?groupId=' + self.group,
                content=bundle([('other.exe', b'test')]), headers={'Content-Type': 'application/zip'})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(client.post('/mcp-packages', content=b'test').status_code, 415)
            response = client.post('/mcp-deployments', json=self.body)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(client.get('/mcp-deployments/' + response.json()['id']).status_code, 200)
            self.assertEqual(len(client.get('/mcp-packages/inventory').json()['items']), 1)


class McpVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def verification_fixture(self, connected=True, skill_enabled=True, conflict=False, legacy=False):
        manager = object.__new__(McpDeployment)
        captured, calls = {}, []
        package = 'a' * 64
        root = r'C:\deploy\program\mcp-' + package[:16]
        skill_root = r'C:\deploy\skills\mcp-' + package[:16]
        result = {'status': 'installed', 'name': 'demo', 'command': root + r'\demo.exe',
                  'args': ['mcp'], 'cwd': root, 'skillPath': skill_root + r'\SKILL.md'}
        desired = {key: result[key] for key in ('command', 'args', 'cwd')}
        desired['enabled'] = True
        task = {'id': 'x', 'batch_id': 'b', 'registration': json.dumps(desired), 'snapshot': json.dumps({
            'settings': {'programRoot': r'C:\deploy\program', 'runtimes': []},
            'skill': {'enabled': True, 'root': r'C:\deploy\skills'}})}
        manager.store = SimpleNamespace(update=lambda *a, **kw: captured.update(kw),
            batch=lambda _: {'package_id': package}, package=lambda _: {'manifest': [{'path': 'SKILL.md'}]})
        async def request(method, params):
            calls.append((method, params))
            if method == 'config/read':
                return {'config': {'mcp_servers': {} if conflict else {'demo': desired}},
                        'layers': [{'name': {'type': 'user', 'file': r'C:\test\config.toml'}, 'version': 'v1'}]}
            if method == 'config/batchWrite':
                self.assertEqual(params['expectedVersion'], 'v1')
                self.assertEqual(len(params['edits']), 1)
                raise RuntimeError('version conflict')
            if method == 'thread/start':
                return {'thread': {'id': 'verify'}}
            if method == 'mcpServerStatus/list':
                return {'data': [{'name': 'demo', 'runtimeStatus': None if legacy else ('connected' if connected else 'errored'),
                                  'tools': {'ping': {}} if connected else {}}]}
            return {}
        fs = SimpleNamespace(ancestors=AsyncMock(), metadata=AsyncMock(), skills=AsyncMock(return_value={
            'skills': [{'name': 'demo-skill', 'path': result['skillPath'], 'enabled': skill_enabled}], 'errors': []}))
        with patch('mcp_deployment.RemoteFiles', return_value=fs):
            if conflict:
                with self.assertRaisesRegex(RuntimeError, 'version conflict'):
                    await manager.register_and_check(SimpleNamespace(request=request), task, result)
            elif not skill_enabled:
                with self.assertRaises(SkillError):
                    await manager.register_and_check(SimpleNamespace(request=request), task, result)
            else:
                await manager.register_and_check(SimpleNamespace(request=request), task, result)
        return captured, calls, fs

    async def test_tool_initialization_and_bundled_skill_are_verified_independently(self):
        saved, _, fs = await self.verification_fixture(connected=False)
        self.assertEqual(saved['state'], 'needs_configuration')
        detail = json.loads(saved['diagnostics'])
        self.assertTrue(detail['found'])
        self.assertEqual(detail['toolCount'], 0)
        self.assertFalse(detail['pending'])
        self.assertEqual((saved['program_verified'], saved['skill_verified']), (0, 1))
        fs.skills.assert_awaited_once()
        saved, _, _ = await self.verification_fixture()
        self.assertEqual((saved['state'], saved['program_verified'], saved['skill_verified']), ('completed', 1, 1))

    async def test_disabled_bundled_skill_prevents_success(self):
        saved, _, _ = await self.verification_fixture(skill_enabled=False)
        self.assertEqual(saved['program_verified'], 1)
        self.assertEqual(saved['skill_verified'], 0)
        self.assertNotEqual(saved['state'], 'completed')

    async def test_catalog_without_runtime_status_passes_without_claiming_connection(self):
        saved, _, _ = await self.verification_fixture(legacy=True)
        self.assertEqual(saved['state'], 'completed')
        detail = json.loads(saved['diagnostics'])
        self.assertEqual(detail['toolCount'], 1)
        self.assertEqual(detail['connection'], '执行服务未提供状态')
        self.assertIn('尚未执行', detail['reason'])

    async def test_config_version_conflict_stops_without_reload(self):
        _, calls, _ = await self.verification_fixture(conflict=True)
        self.assertNotIn('config/mcpServer/reload', [method for method, _ in calls])

    async def installation_fixture(self, compatible=True, with_skill=False, unified=False):
        from mcp_installation import sandbox_policy
        from mcp_packages import parse_package
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        content = bundle([('demo.exe', b'binary')] + ([('SKILL.md', b'skill')] if with_skill else []))
        metadata, _ = parse_package(content)
        manager = object.__new__(McpDeployment)
        manager.root = Path(temporary.name)
        (manager.root / (metadata['id'] + '.zip')).write_bytes(content)
        captured, calls, remote = {}, [], {}
        manager.store = SimpleNamespace(batch=lambda _: {'package_id': metadata['id']},
            update=lambda *a, **kw: captured.update(kw))
        manager.register_and_check = AsyncMock()
        task = {'id': 'task', 'batch_id': 'batch', 'execution_started': 0}
        snapshot = {'settings': {'temporaryRoot': r'C:\deploy\temp', 'programRoot': r'C:\deploy\apps', 'runtimes': []},
                    'skill': {'enabled': False}}
        program = r'C:\deploy\apps\mcp-' + metadata['id'][:16]
        source = r'C:\deploy\temp\task'
        if unified:
            from mcp_packages import installation_settings
            snapshot['settings'] = installation_settings({'enabled': True, 'installRoot': r'C:\deploy\apps'})
            source = r'C:\deploy\apps\.mcp-tmp-task'
        result = {'status': 'installed', 'name': 'demo', 'command': program + r'\demo.exe',
                  'args': ['mcp'], 'cwd': program, 'skillPath': None}
        async def request(method, params):
            calls.append(method)
            if method == 'fs/writeFile':
                remote[params['path']] = base64.b64decode(params['dataBase64'])
            if method == 'config/read':
                return {'config': {}}
            if method == 'thread/start':
                return {'thread': {'id': 'install'}, 'cwd': source, 'approvalPolicy': 'never',
                        'sandbox': sandbox_policy(source, program) if compatible else {'type': 'readOnly'}}
            if method == 'turn/start':
                self.assertEqual(captured['execution_started'], 1)
                return {'turn': {'id': 'turn'}}
            if method == 'thread/read':
                return {'thread': {'turns': [{'status': 'completed', 'items': [
                    {'type': 'agentMessage', 'text': json.dumps(result)}]}]}}
            return {}
        async def write(path, data):
            remote[str(path)] = data
        fs = SimpleNamespace(ancestors=AsyncMock(), names=AsyncMock(return_value={}),
                             write=write, read=AsyncMock(side_effect=lambda path: remote[str(path)]))
        client = SimpleNamespace(request=request, next_notification=AsyncMock(return_value={
            'method': 'turn/completed', 'params': {'threadId': 'install'}}))
        with patch('mcp_deployment.RemoteFiles', return_value=fs):
            if with_skill:
                with self.assertRaises(SkillError):
                    await manager.install(client, task, snapshot, SimpleNamespace(model=None))
            else:
                await manager.install(client, task, snapshot, SimpleNamespace(model=None))
        return manager, captured, calls

    async def test_installation_requires_effective_sandbox_before_turn(self):
        manager, saved, calls = await self.installation_fixture(compatible=False)
        self.assertNotIn('turn/start', calls)
        self.assertEqual((saved['state'], saved['occupied']), ('failed', 0))
        manager.register_and_check.assert_not_awaited()

    async def test_installation_persists_validated_result_before_platform_acceptance(self):
        manager, saved, calls = await self.installation_fixture()
        self.assertIn('turn/start', calls)
        self.assertEqual(json.loads(saved['result'])['name'], 'demo')
        manager.register_and_check.assert_awaited_once()

    async def test_single_root_uses_separate_sandbox_subdirectories(self):
        manager, saved, calls = await self.installation_fixture(unified=True)
        self.assertIn('turn/start', calls)
        self.assertEqual(json.loads(saved['result'])['name'], 'demo')
        manager.register_and_check.assert_awaited_once()

    async def test_missing_bundled_skill_is_not_accepted(self):
        manager, _, _ = await self.installation_fixture(with_skill=True)
        manager.register_and_check.assert_not_awaited()

    async def test_reconnect_only_before_installation_start(self):
        manager = object.__new__(McpDeployment)
        manager.run = AsyncMock(side_effect=[ConnectionError(), None])
        with patch('mcp_deployment.asyncio.sleep', new_callable=AsyncMock):
            await manager.run_with_reconnect({'execution_started': 0})
        self.assertEqual(manager.run.await_count, 2)
        manager.run = AsyncMock(side_effect=ConnectionError())
        with self.assertRaises(ConnectionError):
            await manager.run_with_reconnect({'execution_started': 1})
        self.assertEqual(manager.run.await_count, 1)

    async def test_interruption_keeps_occupation_only_after_start_intent(self):
        manager = object.__new__(McpDeployment)
        captured = {}
        manager.store = SimpleNamespace(update=lambda *a, **kw: captured.update(kw))
        await manager.interrupted({'id': 'x', 'execution_started': 0}, 'interrupted')
        self.assertEqual((captured['state'], captured['occupied']), ('failed', 0))
        await manager.interrupted({'id': 'x', 'execution_started': 1}, 'interrupted')
        self.assertEqual((captured['state'], captured['occupied']), ('review', 1))
        await manager.interrupted({'id': 'x', 'execution_started': 1, 'execution_stopped': 1}, 'interrupted')
        self.assertEqual((captured['state'], captured['occupied']), ('failed', 0))

    async def test_pagination_and_repeated_cursor(self):
        client = SimpleNamespace(request=AsyncMock(side_effect=[
            {'data': [], 'nextCursor': 'two'},
            {'data': [{'name': 'demo', 'tools': {'ping': {}}, 'runtimeStatus': 'connected'}], 'nextCursor': None}]))
        found = await McpDeployment.discover(client, 'verification', 'demo')
        self.assertEqual(found['runtimeStatus'], 'connected')
        self.assertEqual(client.request.await_args_list[1].args[1]['cursor'], 'two')
        client.request = AsyncMock(return_value={'data': [], 'nextCursor': 'same'})
        with self.assertRaises(SkillError):
            await McpDeployment.discovery_page(client, 'verification', 'demo')

    async def test_registration_response_loss_reconciles_without_write(self):
        root = r'C:\apps\root\mcp-' + 'a' * 16
        result = {'status': 'installed', 'name': 'demo', 'command': root + r'\demo.exe',
                  'args': ['mcp'], 'cwd': root, 'skillPath': None}
        desired = {key: result[key] for key in ('command', 'args', 'cwd')}
        desired['enabled'] = True
        task = {'id': 'x', 'batch_id': 'b', 'registration': json.dumps(desired), 'snapshot': json.dumps({
            'settings': {'programRoot': r'C:\apps\root', 'runtimes': []}, 'skill': {'enabled': False}})}
        captured, calls = {}, []
        manager = object.__new__(McpDeployment)
        manager.store = SimpleNamespace(update=lambda *a, **kw: captured.update(kw),
            batch=lambda _: {'package_id': 'a' * 64}, package=lambda _: {'manifest': []})
        async def request(method, params):
            calls.append(method)
            if method == 'fs/getMetadata':
                return {'isSymlink': False, 'isDirectory': True, 'isFile': True}
            if method == 'config/read':
                return {'config': {'mcp_servers': {'demo': desired}}}
            if method == 'thread/start':
                return {'thread': {'id': 'verification'}}
            if method == 'mcpServerStatus/list':
                return {'data': [{'name': 'demo', 'tools': {'ping': {}}, 'runtimeStatus': 'connected'}]}
            return {}
        await manager.register_and_check(SimpleNamespace(request=request), task, result)
        self.assertNotIn('config/batchWrite', calls)
        self.assertEqual(captured['state'], 'completed')
        self.assertEqual(captured['occupied'], 0)

    async def test_unsupported_does_not_register(self):
        manager = object.__new__(McpDeployment)
        manager.store = SimpleNamespace(update=lambda *a, **kw: captured.update(kw))
        captured = {}
        client = SimpleNamespace(request=AsyncMock())
        await manager.register_and_check(client, {'id': 'x'}, {'status': 'unsupported'})
        self.assertEqual(captured['state'], 'unsupported')
        self.assertEqual(captured['occupied'], 0)
        client.request.assert_not_awaited()

    async def test_existing_unknown_configuration_is_not_overwritten(self):
        manager = object.__new__(McpDeployment)
        manager.store = SimpleNamespace(update=lambda *a, **kw: None, batch=lambda _: {'package_id': 'a' * 64},
                                        package=lambda _: {'manifest': []})
        root = r'C:\apps\root\mcp-' + 'a' * 16
        task = {'id': 'x', 'batch_id': 'b', 'registration': None, 'snapshot': json.dumps({
            'settings': {'programRoot': r'C:\apps\root', 'runtimes': []}, 'skill': {'enabled': False}})}
        result = {'status': 'installed', 'name': 'demo', 'command': root + r'\demo.exe',
                  'args': ['mcp'], 'cwd': root, 'skillPath': None}
        calls = []
        async def request(method, params):
            calls.append(method)
            if method == 'fs/getMetadata':
                return {'isSymlink': False, 'isDirectory': True, 'isFile': True}
            if method == 'config/read':
                return {'config': {'mcp_servers': {'demo': {'command': 'other'}}}}
        with self.assertRaises(SkillError):
            await manager.register_and_check(SimpleNamespace(request=request), task, result)
        self.assertNotIn('config/batchWrite', calls)


if __name__ == '__main__':
    unittest.main()
