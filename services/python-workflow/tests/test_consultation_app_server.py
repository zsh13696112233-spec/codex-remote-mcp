"""可选真实协议隔离验收；不复制登录凭据、不启动模型或业务步骤。"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("CONSULTATION_TEST_CODEX"), "未指定隔离协议验收执行文件")
class LiveConsultationTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_policy_and_disabled_external_tools(self):
        with tempfile.TemporaryDirectory(prefix="consultation-protocol-") as directory:
            root = Path(directory)
            home, work = root / "home", root / "work"
            home.mkdir()
            work.mkdir()
            (home / "config.toml").write_text('[windows]\nsandbox = "unelevated"\n', encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                os.environ["CONSULTATION_TEST_CODEX"], "app-server", "--listen", "stdio://",
                cwd=str(work), env={**os.environ, "CODEX_HOME": str(home)},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            sequence = 0

            async def request(method, params):
                nonlocal sequence
                sequence += 1
                process.stdin.write((json.dumps({"id": sequence, "method": method, "params": params}) + "\n").encode())
                await process.stdin.drain()
                while True:
                    line = await asyncio.wait_for(process.stdout.readline(), 30)
                    if not line:
                        raise RuntimeError("隔离测试执行服务退出。")
                    value = json.loads(line)
                    if value.get("id") == sequence:
                        if "error" in value:
                            raise RuntimeError(f"测试接口不兼容：{method}，code={value['error'].get('code')}")
                        return value["result"]

            try:
                await request("initialize", {"clientInfo": {"name": "consultation-test", "version": "1"}, "capabilities": {"experimentalApi": False}})
                response = await request("thread/start", {"cwd": str(work), "sandbox": "read-only", "approvalPolicy": "never",
                    "config": {"sandbox_mode": "read-only", "web_search": "disabled", "apps": {"_default": {"enabled": False}}, "features": {"apps": False}, "mcp_servers": {}, "plugins": {}}})
                self.assertEqual(response.get("approvalPolicy"), "never")
                self.assertEqual(response.get("sandbox", {}).get("type"), "readOnly")
                self.assertIs(response.get("sandbox", {}).get("networkAccess"), False)
                inventory = await request("mcpServerStatus/list", {"threadId": response["thread"]["id"], "limit": 100})
                self.assertFalse(inventory.get("nextCursor"))
                self.assertFalse(any(v.get("tools") for v in inventory["data"]))
            finally:
                if process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.wait(), 10)
