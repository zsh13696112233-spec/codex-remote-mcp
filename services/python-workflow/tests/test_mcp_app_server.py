"""隔离配置的真实协议测试；不使用账号、不启动模型、不调用业务工具。"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp_installation import sandbox_policy, sandbox_rejection
from mcp_deployment import McpDeployment


FIXTURE = '''import sys,json
for line in sys.stdin:
    msg=json.loads(line)
    if 'id' not in msg: continue
    method=msg['method']
    if method=='initialize':
        result={'protocolVersion':msg['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'installation-fixture','version':'1'}}
    elif method=='tools/list':
        result={'tools':[{'name':'ping','description':'Local test only','inputSchema':{'type':'object','properties':{}}}]}
    else: result={}
    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}),flush=True)
'''


@unittest.skipUnless(os.environ.get('MCP_TEST_CODEX'), '未指定隔离协议验收执行文件')
class LiveMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_configuration_sandbox_and_discovery(self):
        with tempfile.TemporaryDirectory(prefix='mcp-protocol-') as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            work, program = root / 'work', root / 'program'
            work.mkdir()
            program.mkdir()
            (home / 'config.toml').write_text("[windows]\nsandbox = \"unelevated\"\n[projects.'" + str(work) + "']\ntrust_level = \"trusted\"\n", encoding='utf-8')
            script = program / 'server.py'
            script.write_text(FIXTURE, encoding='utf-8')
            process = await asyncio.create_subprocess_exec(
                os.environ['MCP_TEST_CODEX'], 'app-server', '--listen', 'stdio://',
                cwd=str(work), env={**os.environ, 'CODEX_HOME': str(home)},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)

            class Client:
                sequence = 0

                async def request(self, method, params):
                    self.sequence += 1
                    key = self.sequence
                    process.stdin.write((json.dumps({'id': key, 'method': method, 'params': params}) + '\n').encode())
                    await process.stdin.drain()
                    while True:
                        line = await asyncio.wait_for(process.stdout.readline(), 30)
                        if not line:
                            raise RuntimeError('测试执行服务已退出')
                        message = json.loads(line)
                        if message.get('id') == key:
                            if 'error' in message:
                                raise RuntimeError('测试接口失败：' + method + ' code=' + str(message['error'].get('code')))
                            return message['result']

            client = Client()
            try:
                await client.request('initialize', {'clientInfo': {'name': 'mcp-test', 'version': '1'},
                                                   'capabilities': {'experimentalApi': False}})
                config = await client.request('config/read', {'includeLayers': True})
                layer = next(v for v in config['layers'] if v['name']['type'] == 'user')
                await client.request('config/batchWrite', {'edits': [{'keyPath': 'mcp_servers.installation_fixture',
                    'value': {'command': sys.executable, 'args': [str(script)], 'enabled': True}, 'mergeStrategy': 'replace'}],
                    'filePath': layer['name']['file'], 'expectedVersion': layer['version']})
                await client.request('config/mcpServer/reload', {})
                policy = sandbox_policy(str(work), str(program))
                thread = await client.request('thread/start', {'cwd': str(work), 'sandbox': 'workspace-write',
                    'approvalPolicy': 'never', 'config': {'sandbox_mode': 'workspace-write', 'sandbox_workspace_write': {
                        'writable_roots': policy['writableRoots'], 'network_access': False,
                        'exclude_tmpdir_env_var': True, 'exclude_slash_tmp': True}}})
                found = await McpDeployment.discover(client, thread['thread']['id'], 'installation_fixture')
                self.assertIsNotNone(found)
                self.assertEqual(found['runtimeStatus'], 'connected')
                self.assertIn('ping', found['tools'])
                self.assertIsNone(sandbox_rejection(thread, policy))
            finally:
                if process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.wait(), 10)


if __name__ == '__main__':
    unittest.main()
