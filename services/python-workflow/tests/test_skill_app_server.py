"""可选真实执行服务协议验收，不读取部署凭据，不创建模型会话。

设置 SKILL_TEST_CODEX 为本机 codex 可执行文件路径后运行此测试。
所有文件写入临时工作区，退出后清理。模型实际调用另行实机验收。
"""

import asyncio
import os
import json
import tempfile
import sys
import unittest
from pathlib import Path

from skill_deployment import RemoteFiles


@unittest.skipUnless(os.environ.get("SKILL_TEST_CODEX"), "未指定真实执行服务，跳过协议验收")
class LiveSkillProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_read_and_discover_skill_with_script(self):
        with tempfile.TemporaryDirectory(prefix="codex-skill-probe-") as directory:
            root = Path(directory)
            skills = root / ".agents" / "skills"
            skills.mkdir(parents=True)
            (root / ".git").mkdir()
            process = await asyncio.create_subprocess_exec(
                os.environ["SKILL_TEST_CODEX"], "app-server", "--listen", "stdio://",
                cwd=directory, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            class Client:
                sequence = 0

                async def request(self, method, params):
                    self.sequence += 1
                    key = self.sequence
                    process.stdin.write((json.dumps({"jsonrpc":"2.0", "id":key,"method":method,"params":params})+"\n").encode())
                    await process.stdin.drain()
                    while True:
                        line = await asyncio.wait_for(process.stdout.readline(), 30)
                        if not line: raise RuntimeError("测试执行服务已退出")
                        response = json.loads(line)
                        if response.get("id") == key:
                            if "error" in response:
                                # 不回显可能包含本机配置的错误正文。
                                raise RuntimeError(f"测试接口 {method} 不可用，code={response['error'].get('code')}")
                            return response["result"]
            client = Client()
            try:
                await client.request("initialize", {"clientInfo":{"name":"skill-probe","version":"1.0"},"capabilities":{"experimentalApi":False}})
                process.stdin.write(b'{"jsonrpc":"2.0","method":"initialized","params":{}}\n')
                await process.stdin.drain()
                fs = RemoteFiles(client)
                await fs.ancestors(skills)
                target = skills / "platform-protocol-probe"
                await fs.call("fs/createDirectory", path=str(target), recursive=False)
                await fs.write(target / "probe.py", b'print("skill-protocol-ok")\n')
                await fs.write(target / "SKILL.md", b'---\nname: platform-protocol-probe\ndescription: Use only for the platform protocol test.\n---\nRun probe.py only when explicitly requested.\n')
                result = await fs.skills(directory)
                matches = [s for s in result["skills"] if s["name"] == "platform-protocol-probe"]
                self.assertEqual(len(matches), 1)
                self.assertTrue(matches[0]["enabled"])
                self.assertEqual(Path(matches[0]["path"]), target / "SKILL.md")
                script = await asyncio.create_subprocess_exec(sys.executable, str(target / "probe.py"),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                output, _ = await asyncio.wait_for(script.communicate(), 10)
                self.assertEqual(script.returncode, 0)
                self.assertEqual(output.strip(), b"skill-protocol-ok")
            finally:
                if process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.wait(), 10)
