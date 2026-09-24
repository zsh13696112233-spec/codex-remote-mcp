import base64
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp_installation import validate_result, validate_terminal
from mcp_terminal import PATH_SCRIPT, POWERSHELL, install_terminal
from skill_packages import SkillError


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = r'C:\apps\package'
        self.result = {'kind': 'mcp', 'status': 'installed', 'name': 'tool',
                       'command': self.root + r'\server.exe', 'args': ['mcp'],
                       'cwd': self.root, 'skillPath': None,
                       'terminal': {'command': self.root + r'\tool.exe', 'args': []}}
        self.client = SimpleNamespace(request=AsyncMock(side_effect=[
            {'exitCode': 0, 'stdout': 'help'}, {'exitCode': 0, 'stdout': 'CODEX_TERMINAL_PATH_OK\r\n'}]))
        self.fs = SimpleNamespace(ancestors=AsyncMock(), metadata=AsyncMock())

    async def install(self, config=None):
        return await install_terminal(self.client, self.fs, self.result, self.root, 'package', 'task',
                                      config if config is not None else {'allow_write': True, 'allow_full_access': True})

    def test_hybrid_has_independent_terminal_and_legacy_mcp_has_none(self):
        self.assertEqual(validate_result(self.result, self.root), self.result)
        old = {k: v for k, v in self.result.items() if k != 'terminal'}
        self.assertNotIn('terminal', validate_result(old, self.root))

    def test_terminal_rejects_escape_arguments_and_path_separators(self):
        for value in ({'command': r'C:\outside\tool.exe', 'args': []},
                      {'command': self.root + r'\tool.exe', 'args': ['login']},
                      {'command': self.root + r'\tool.cmd', 'args': []},
                      {'command': self.root + r'\bad;dir\tool.exe', 'args': []},
                      {'command': r'C:\Python\python.exe', 'args': ['-c']},
                      {'command': r'C:\Python\python.exe', 'args': [self.root + r'\%BAD%\tool.py']},
                      {'command': self.root + r'\tool.exe', 'args': [], 'env': {}}):
            with self.subTest(value=value), self.assertRaises(SkillError):
                validate_terminal(value, self.root)

    async def test_pure_mcp_never_executes_or_changes_path(self):
        self.result['terminal'] = None
        self.assertFalse(await self.install(config={}))
        self.client.request.assert_not_awaited()

    async def test_hybrid_checks_cli_not_mcp_help_and_uses_fixed_script(self):
        self.assertTrue(await self.install())
        calls = self.client.request.await_args_list
        self.assertEqual(calls[0].args[1]['command'], [self.root + r'\tool.exe', '--help'])
        code = base64.b64decode(calls[1].args[1]['command'][-1]).decode('utf-16-le')
        self.assertIn(PATH_SCRIPT, code)
        self.assertNotIn(self.root, code)  # 路径通过编码数据传输，不插入脚本源码。

    async def test_denied_permission_has_no_side_effect(self):
        with self.assertRaisesRegex(SkillError, '授权完全访问'):
            await self.install(config={'allow_write': True})
        self.client.request.assert_not_awaited()

    async def test_failed_path_verification_cannot_report_success(self):
        self.client.request.side_effect = [{'exitCode': 0, 'stdout': 'help'},
                                          {'exitCode': 1, 'stdout': 'secret'}]
        with self.assertRaisesRegex(SkillError, 'PATH 写入或命令解析检查失败') as caught:
            await self.install()
        self.assertNotIn('secret', str(caught.exception))

    async def test_script_creates_fixed_launcher_not_runtime_path_entry(self):
        self.result['terminal'] = {'command': r'C:\Python\python.exe', 'args': [self.root + r'\tool.py']}
        await self.install()
        code = base64.b64decode(self.client.request.await_args.args[1]['command'][-1]).decode('utf-16-le')
        encoded = code.split("FromBase64String('", 1)[1].split("'", 1)[0]
        spec = json.loads(base64.b64decode(encoded))
        self.assertEqual(spec['entry'], self.root + r'\tool.cmd')
        launcher = base64.b64decode(spec['launcher']).decode('ascii')
        self.assertIn('"C:\\Python\\python.exe" "C:\\apps\\package\\tool.py" %*', launcher)
        self.assertIn('DisableDelayedExpansion', launcher)


@unittest.skipUnless(os.name == 'nt' and Path(POWERSHELL).is_file(), '需要 Windows PowerShell')
class TerminalPowerShellTests(unittest.TestCase):
    """真实执行固定 PATH 算法，仅替换注册表存储，绝不改机器/用户环境。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'program space'
        self.root.mkdir()
        self.entry = self.root / 'codex_terminal_probe.exe'
        self.entry.write_bytes(b'not executed')
        self.owner = b'{"packageId": "test", "taskId": "test"}'
        (self.root / '.mcp-install-owner.json').write_bytes(self.owner)
        self.spec = {'root': str(self.root), 'entry': str(self.entry), 'command': str(self.entry),
                     'script': None, 'launcher': None, 'owner': base64.b64encode(self.owner).decode()}

    def run_script(self, user='', machine='', spec=None, repeat=False, kind='ExpandString'):
        data = base64.b64encode(json.dumps({'spec': spec or self.spec, 'user': user, 'machine': machine, 'kind': kind}).encode()).decode()
        harness = r'''
$script:userPath = $data.user
$script:writes = 0
function Read-UserPath { return @{ value = $script:userPath; kind = $data.kind } }
function Write-UserPath($before, [string]$value) { $script:userPath = $value; $script:writes++ }
function Read-MachinePath { return $data.machine }
function Notify-PathChanged { }
try {
    $null = Invoke-TerminalPath $data.spec
    if (REPEAT) { $null = Invoke-TerminalPath $data.spec }
    @{ ok = $true; path = $script:userPath; writes = $script:writes } | ConvertTo-Json -Compress
} catch {
    @{ ok = $false; path = $script:userPath; writes = $script:writes; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
'''.replace('REPEAT', '$true' if repeat else '$false')
        code = "$ErrorActionPreference='Stop'\nSet-StrictMode -Version Latest\n" + PATH_SCRIPT
        code += "\n$data = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + data + "')) | ConvertFrom-Json\n" + harness
        response = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                   base64.b64encode(code.encode('utf-16-le')).decode()],
                                  cwd=self.temp.name, capture_output=True, timeout=30)
        self.assertEqual(response.returncode, 0, response.stderr.decode(errors='replace'))
        return json.loads(response.stdout)

    def test_append_preserves_existing_and_retry_is_idempotent(self):
        result = self.run_script(user=r'%LOCALAPPDATA%\OldBin', repeat=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['writes'], 1)
        self.assertEqual(result['path'], r'%LOCALAPPDATA%\OldBin;' + str(self.root))

    def test_existing_directory_case_and_trailing_separator_not_duplicated(self):
        result = self.run_script(user=str(self.root).upper() + '\\')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['writes'], 0)

    def test_registry_string_does_not_expand_environment_references(self):
        # REG_SZ 中的百分号是原值；不能按 REG_EXPAND_SZ 去重而漏加真实路径。
        with unittest.mock.patch.dict(os.environ, {'CODEX_PATH_TEST_ROOT': str(self.root)}):
            result = self.run_script(user='%CODEX_PATH_TEST_ROOT%', kind='String')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['writes'], 1)
        self.assertEqual(result['path'], '%CODEX_PATH_TEST_ROOT%;' + str(self.root))

    def test_empty_path_and_existing_separators_are_preserved(self):
        for original in ('', r'C:\existing;;'):
            result = self.run_script(user=original)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['path'], original + str(self.root))

    def test_shadowed_command_stops_before_registry_write(self):
        other = Path(self.temp.name) / 'conflict'
        other.mkdir()
        (other / self.entry.name).write_bytes(b'not executed')
        result = self.run_script(machine=str(other))
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['writes'], 0)
        self.assertEqual(result['error'], 'command-conflict')

    def test_owner_mismatch_and_outside_entry_stop_before_write(self):
        for change in ({'owner': base64.b64encode(b'wrong').decode()},
                       {'entry': str(Path(self.temp.name) / 'outside.exe')}):
            result = self.run_script(spec={**self.spec, **change})
            self.assertFalse(result['ok'], result)
            self.assertEqual(result['writes'], 0)

    def test_launcher_recovery_does_not_overwrite_conflicting_file(self):
        launcher = self.root / 'tool.cmd'
        launcher.write_text('existing', encoding='ascii')
        result = self.run_script(spec={**self.spec, 'entry': str(launcher),
                                      'launcher': base64.b64encode(b'@echo off\r\n').decode()})
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['writes'], 0)
        self.assertEqual(launcher.read_text(), 'existing')

    def test_generated_python_launcher_runs_from_another_directory(self):
        script = self.root / 'probe.py'
        script.write_text('import sys\nprint(repr(sys.argv[1:]))\nsys.exit(7)\n', encoding='ascii')
        client = SimpleNamespace(request=AsyncMock(side_effect=[
            {'exitCode': 0, 'stdout': 'help'}, {'exitCode': 0, 'stdout': 'CODEX_TERMINAL_PATH_OK'}]))
        fs = SimpleNamespace(ancestors=AsyncMock(), metadata=AsyncMock())
        asyncio.run(install_terminal(client, fs, {'kind': 'cli', 'command': sys.executable,
            'args': [str(script)], 'cwd': str(self.root)}, str(self.root), 'test', 'test',
            {'allow_write': True, 'allow_full_access': True}))
        code = base64.b64decode(client.request.await_args.args[1]['command'][-1]).decode('utf-16-le')
        encoded = code.split("FromBase64String('", 1)[1].split("'", 1)[0]
        spec = json.loads(base64.b64decode(encoded))
        result = self.run_script(spec=spec, repeat=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['writes'], 1)
        # 使用真实生成的启动器，通过命令名运行，检查空格参数、退出码和任意 cwd。
        env = {**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ.get('PATH', '')}
        response = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command',
                                   "& probe --help 'two words'; exit $LASTEXITCODE"],
                                  cwd=self.temp.name, env=env, capture_output=True, timeout=30)
        self.assertEqual(response.returncode, 7, response.stderr.decode(errors='replace'))
        self.assertIn(b"['--help', 'two words']", response.stdout)
