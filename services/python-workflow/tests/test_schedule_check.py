import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from schedule_check import command_for, execute_check, parse_result, validate_spec


class ScheduleCheckTest(unittest.TestCase):
    def test_contract_and_exit_code_precedence(self):
        for stdout, code, expected in [("\nTrUe\nreason", 0, "passed"), ("FALSE\nno work", 0, "skipped"),
                                       ("TRUE", 1, "failed"), ("TRUE", None, "failed"),
                                       ("log\nTRUE", 0, "failed"), ("", 0, "failed")]:
            with self.subTest(stdout=stdout, code=code):
                self.assertEqual(expected, parse_result({"stdout": stdout, "exitCode": code})["status"])

    def test_rejects_shell_operators_relative_paths_and_other_scripts(self):
        for path in ["check.bat", "C:check.bat", "C:/check.ps1", "C:/../check.bat", "C:/%TEMP%/check.cmd", "//host/check.cmd"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_spec({"scriptPath": path})
        for args in ['x & whoami', '%PATH%', '!x!', 'x\ny', '"unclosed', 'x|y', '>out']:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_spec({"scriptPath": "E:/Agent Bug/check.bat", "arguments": args})
        self.assertEqual(60, validate_spec({"scriptPath": "E:/Agent Bug/check.bat", "arguments": '-user "Plan Agent"'})[2])

    @unittest.skipUnless(os.name == "nt", "Windows BAT/CMD 执行验证")
    def test_actual_windows_quoting_and_exit_codes(self):
        with tempfile.TemporaryDirectory(prefix="schedule check ") as folder:
            path = Path(folder) / "check.cmd"
            path.write_text('@echo off\nif not "%~1"=="Plan Agent" exit /b 9\necho TRUE\nexit /b 0\n')
            response = subprocess.run(command_for(str(path), '"Plan Agent"', 5), capture_output=True, text=True, timeout=20)
            self.assertEqual(0, response.returncode, response.stderr)
            self.assertEqual("TRUE", response.stdout.strip())
            response = subprocess.run(command_for(str(path), 'wrong', 5), capture_output=True, text=True, timeout=20)
            self.assertEqual(9, response.returncode)

    @unittest.skipUnless(os.name == "nt", "Windows BAT/CMD 执行验证")
    def test_actual_timeout_and_large_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "check.bat"
            path.write_text('@echo off\nping -n 20 127.0.0.1 >nul\necho TRUE\n')
            response = subprocess.run(command_for(str(path), '', 1), capture_output=True, text=True, timeout=15)
            self.assertEqual(124, response.returncode)
            path.write_text('@echo off\necho FALSE\nfor /L %%i in (1,1,2000) do echo some output to drain\n')
            response = subprocess.run(command_for(str(path), '', 5), capture_output=True, text=True, timeout=20)
            self.assertEqual(126, response.returncode)
            self.assertNotIn("FALSE", response.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows BAT/CMD 执行验证")
    def test_truncated_first_line_cannot_become_true(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "check.cmd"
            # CMD 从文件读取输出，避免命令行自身 8191 字符限制影响复现。
            output = Path(folder) / "output.txt"
            output.write_text('TRUE' + ' ' * 9000 + 'FALSE\n')
            path.write_text('@echo off\ntype "' + str(output) + '"\nexit /b 0\n')
            response = subprocess.run(command_for(str(path), '', 5), capture_output=True, text=True, timeout=20)
            self.assertEqual(126, response.returncode)
            self.assertNotEqual("passed", parse_result({"stdout": response.stdout, "exitCode": response.returncode})["status"])


class RemoteCheckTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.request.side_effect = [{"isFile": True, "isSymlink": False}, {"exitCode": 0, "stdout": "FALSE"}]
        agent = SimpleNamespace(enabled=True, capabilities=["executor"], allow_write=True, allow_full_access=True, allow_cwd_override=True, cwd="E:/work", url="ws://private", agent_id="a")
        registry = Mock()
        registry.rows.return_value = {"a": {"group_id": "g", "test_status": "passed"}}
        registry.config_from_row.return_value = agent
        orchestrator = Mock()
        orchestrator._client_factory.return_value = client
        orchestrator._resolve_agent_token.return_value = None
        return SimpleNamespace(registry=registry, orchestrator=orchestrator), client, agent

    async def test_remote_command_uses_script_parent_without_changing_registered_cwd(self):
        gateway, client, agent = self.fixture()
        response = await execute_check(gateway, {"agentId": "a", "groupId": "g", "scriptPath": "E:/check.bat"})
        self.assertEqual("skipped", response["status"])
        self.assertEqual(["fs/getMetadata", "command/exec"], [c.args[0] for c in client.request.call_args_list])
        self.assertEqual("E:\\", client.request.call_args_list[-1].args[1]["cwd"])
        self.assertEqual("E:/work", agent.cwd)

    async def test_directory_override_permission_and_same_directory(self):
        gateway, client, agent = self.fixture()
        agent.allow_cwd_override = False
        body = {"agentId": "a", "groupId": "g", "scriptPath": "E:/other/check.bat"}
        response = await execute_check(gateway, body)
        self.assertEqual("failed", response["status"])
        self.assertIn("目录覆盖权限", response["reason"])
        client.request.assert_not_called()
        body["scriptPath"] = "e:/WORK/check.bat"
        self.assertEqual("skipped", (await execute_check(gateway, body))["status"])
        self.assertEqual("E:/work", agent.cwd)

    async def test_group_and_permission_boundaries(self):
        gateway, client, agent = self.fixture()
        body = {"agentId": "a", "groupId": "other", "scriptPath": "E:/check.bat"}
        with self.assertRaises(ValueError):
            await execute_check(gateway, body)
        body["groupId"] = "g"
        agent.allow_full_access = False
        self.assertEqual("failed", (await execute_check(gateway, body))["status"])
        client.request.assert_not_called()

    async def test_connection_errors_are_not_retried_or_exposed(self):
        gateway, client, agent = self.fixture()
        client.request.side_effect = ConnectionError("secret@private")
        response = await execute_check(gateway, {"agentId": "a", "groupId": "g", "scriptPath": "E:/check.bat"})
        self.assertEqual("failed", response["status"])
        self.assertNotIn("secret", response["reason"])
        self.assertEqual(1, client.request.call_count)

    async def test_slow_preflight_reduces_remote_budget_and_exhaustion_never_starts_script(self):
        for elapsed, expected in [(8, 57), (65, None)]:
            gateway, client, _ = self.fixture()
            # 仅替换模块的计时器，不修改事件循环使用的全局 monotonic。
            clock = SimpleNamespace(monotonic=Mock(side_effect=[100, 100, 100 + elapsed]))
            with patch('schedule_check.time', clock), patch('schedule_check.command_for', return_value=['command']) as command:
                response = await execute_check(gateway, {"agentId": "a", "groupId": "g", "scriptPath": "E:/check.bat"})
            if expected is None:
                command.assert_not_called()
                self.assertEqual(1, client.request.call_count)
                self.assertEqual("failed", response["status"])
            else:
                self.assertEqual(expected, command.call_args.args[2])
                self.assertEqual((expected + 3) * 1000, client.request.call_args.args[1]['timeoutMs'])

    async def test_registry_delay_exhausting_deadline_never_connects_to_executor(self):
        gateway, client, _ = self.fixture()
        clock = SimpleNamespace(monotonic=Mock(side_effect=[100, 171]))
        with patch('schedule_check.time', clock):
            response = await execute_check(gateway, {"agentId": "a", "groupId": "g", "scriptPath": "E:/check.bat"})
        self.assertEqual("failed", response["status"])
        gateway.orchestrator._client_factory.assert_not_called()
