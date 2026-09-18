import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp_deployment import McpDeployment
from mcp_installation import validate_result, installation_prompt, RESULT_SCHEMA
from skill_packages import SkillError


class CliInstallationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.package = 'a' * 64
        self.program = r'C:\deploy\apps\mcp-' + self.package[:16]
        self.skill = r'C:\deploy\skills\mcp-' + self.package[:16]
        self.result = {'status': 'installed', 'kind': 'cli', 'name': 'gm-cli',
                       'command': self.program + r'\gm_cli.exe', 'args': [],
                       'cwd': self.program, 'skillPath': self.skill + r'\SKILL.md'}
        self.snapshot = {'config': json.dumps({'allow_write': True, 'allow_full_access': True}),
                         'settings': {'programRoot': r'C:\deploy\apps', 'runtimes': []},
                         'skill': {'enabled': True, 'root': r'C:\deploy\skills'}}
        self.task = {'id': 'task', 'batch_id': 'batch', 'registration': None,
                     'snapshot': json.dumps(self.snapshot)}
        self.saved = {'program_verified': 1, 'skill_verified': 1}
        self.manager = object.__new__(McpDeployment)
        self.manager.store = SimpleNamespace(update=lambda *a, **kw: self.saved.update(kw),
            batch=lambda _: {'package_id': self.package},
            package=lambda _: {'manifest': [{'path': 'gm_cli/skill/gm-cli/SKILL.md'}]})
        self.fs = SimpleNamespace(ancestors=AsyncMock(), metadata=AsyncMock(),
            skills=AsyncMock(return_value={'skills': [{'path': self.result['skillPath'].upper(),
                'name': 'gm-cli', 'enabled': True}], 'errors': []}))
        self.client = SimpleNamespace(request=AsyncMock(return_value={'exitCode': 0, 'stdout': 'Usage: gm_cli', 'stderr': ''}))

    async def verify(self):
        with patch('mcp_deployment.RemoteFiles', return_value=self.fs):
            await self.manager.register_and_check(self.client, self.task, self.result)

    def test_cli_validation_and_schema(self):
        self.assertEqual(validate_result(self.result, self.program, skill=self.skill), self.result)
        self.assertIn('kind', RESULT_SCHEMA['required'])
        for change in ({'skillPath': None}, {'kind': 'other'}, {'args': ['login']},
                       {'command': self.program + r'\install.bat'}, {'skillPath': r'C:\other\SKILL.md'}):
            with self.subTest(change=change), self.assertRaises(SkillError):
                validate_result({**self.result, **change}, self.program, skill=self.skill)
        script = {**self.result, 'command': r'C:\Python\python.exe', 'args': [self.program + r'\cli.py']}
        self.assertEqual(validate_result(script, self.program, skill=self.skill), script)
        with self.assertRaises(SkillError):
            validate_result({**script, 'args': script['args'] + ['login']}, self.program, skill=self.skill)

    def test_prompt_requires_both_destinations_and_stable_commands(self):
        prompt = installation_prompt('temp', self.program, self.skill, [])
        for text in ('kind=cli', '没有 MCP 入口不能直接判为不支持', '绝对路径', '附带 Skill', '复制到程序目录'):
            self.assertIn(text, prompt)

    async def test_cli_uses_only_help_and_skill_no_mcp_registration(self):
        await self.verify()
        self.assertEqual((self.saved['state'], self.saved['program_verified'], self.saved['skill_verified']), ('completed', 1, 1))
        self.assertEqual(json.loads(self.saved['result'])['kind'], 'cli')
        self.client.request.assert_awaited_once()
        method, params = self.client.request.await_args.args
        self.assertEqual(method, 'command/exec')
        self.assertEqual(params['command'], [self.result['command'], '--help'])
        self.assertEqual(params['timeoutMs'], 20000)
        self.fs.skills.assert_awaited_once_with(self.program)
        detail = json.loads(self.saved['diagnostics'])
        self.assertEqual(detail['kind'], 'cli')
        self.assertFalse(detail['pending'])
        self.assertIsNone(detail['toolCount'])

    async def test_authorized_skill_root_is_normalized_and_verified(self):
        self.result['skillPath'] = self.skill
        await self.verify()
        self.assertEqual(json.loads(self.saved['result'])['skillPath'], self.skill + r'\SKILL.md')
        self.fs.metadata.assert_any_await(self.skill + r'\SKILL.md', False)
        self.assertEqual(self.saved['state'], 'completed')

    async def test_normalized_skill_root_still_requires_existing_file(self):
        self.result['skillPath'] = self.skill
        async def metadata(path, directory):
            if str(path).endswith('SKILL.md'):
                raise SkillError('入口文件不存在')
        self.fs.metadata.side_effect = metadata
        with self.assertRaisesRegex(SkillError, '入口文件不存在'):
            await self.verify()
        self.assertEqual(self.saved['skill_verified'], 0)
        self.assertNotEqual(self.saved['state'], 'completed')

    def test_skill_path_compatibility_never_accepts_other_directories(self):
        for path in (self.skill + '-other', r'C:\deploy\skills', self.skill + r'\README.md', self.skill + r'\nested'):
            with self.subTest(path=path), self.assertRaises(SkillError):
                validate_result({**self.result, 'skillPath': path}, self.program, skill=self.skill)

    async def test_help_failures_clear_old_success_without_saving_output(self):
        for response in ({'exitCode': 1, 'stderr': 'secret-token'}, {'exitCode': 0, 'stdout': ''},
                         {'exitCode': False, 'stdout': 'Usage'}, {}):
            with self.subTest(response=response):
                self.client.request.return_value = response
                with self.assertRaisesRegex(SkillError, '帮助命令验证失败'):
                    await self.verify()
                self.assertEqual((self.saved['program_verified'], self.saved['skill_verified']), (0, 0))
                self.assertNotIn('secret-token', json.dumps(self.saved))
        self.fs.skills.assert_not_awaited()

    async def test_missing_program_does_not_execute_and_clears_old_success(self):
        self.fs.metadata.side_effect = SkillError('程序不存在')
        with self.assertRaises(SkillError):
            await self.verify()
        self.client.request.assert_not_awaited()
        self.assertEqual((self.saved['program_verified'], self.saved['skill_verified']), (0, 0))

    async def test_disabled_duplicate_or_broken_skill_prevents_completion(self):
        entry = {'name': 'gm-cli', 'path': self.result['skillPath'], 'enabled': True}
        for value in ({'skills': [{**entry, 'enabled': False}], 'errors': []},
                      {'skills': [entry, entry], 'errors': []},
                      {'skills': [entry], 'errors': ['parse error']}, {'skills': [], 'errors': []}):
            with self.subTest(value=value):
                self.fs.skills.return_value = value
                with self.assertRaises(SkillError):
                    await self.verify()
                self.assertEqual((self.saved['program_verified'], self.saved['skill_verified']), (1, 0))
                self.assertNotEqual(self.saved['state'], 'completed')

    async def test_no_implicit_full_access(self):
        for config in ({}, {'allow_write': True}, {'allow_full_access': True},
                       {'allow_write': True, 'allow_full_access': 'true'}):
            self.task['snapshot'] = json.dumps({**self.snapshot, 'config': json.dumps(config)})
            with self.assertRaisesRegex(SkillError, '授权完全访问'):
                await self.verify()
        self.client.request.assert_not_awaited()

    async def test_cli_diagnostic_failure_does_not_expose_raw_error(self):
        self.task['_installation_kind'] = 'cli'
        await self.manager.record_diagnostic(self.task, '验证 CLI 帮助命令', error=RuntimeError('secret-token'))
        detail = json.loads(self.saved['diagnostics'])
        self.assertEqual(detail['kind'], 'cli')
        self.assertNotIn('secret-token', detail['reason'])


if __name__ == '__main__':
    unittest.main()
