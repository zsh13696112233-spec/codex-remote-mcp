import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp_file_transfer import (CACHE_NAME, CHUNK_SIZE, DIRECT_LIMIT, OWNER_NAME, POWERSHELL,
                               McpFileTransfer, digest, merge_command)
from skill_deployment import RemoteFiles
from skill_packages import SkillError


def command_spec(command):
    source = base64.b64decode(command[-1]).decode("utf-16-le")
    encoded = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", source).group(1)
    return json.loads(base64.b64decode(encoded))


class Remote:
    def __init__(self):
        self.root = PureWindowsPath("C:/deploy/task")
        self.dirs = {self.root, *self.root.parents}
        self.files = {self.root / OWNER_NAME: b'owner', PureWindowsPath(POWERSHELL): b'exe'}
        self.links = set()
        self.calls, self.specs = [], []
        self.lose_chunk = False
        self.lose_merge = False

    async def request(self, method, params):
        self.calls.append((method, params))
        path = PureWindowsPath(params['path']) if 'path' in params else None
        if method == 'fs/getMetadata':
            if path not in self.files and path not in self.dirs:
                raise AssertionError('missing metadata: ' + str(path))
            return {'isFile': path in self.files, 'isDirectory': path in self.dirs, 'isSymlink': path in self.links}
        if method == 'fs/readDirectory':
            return {'entries': [{'fileName': p.name} for p in self.files.keys() | self.dirs if p.parent == path and p != path]}
        if method == 'fs/createDirectory':
            self.dirs.add(path)
            return {}
        if method == 'fs/writeFile':
            self.files[path] = base64.b64decode(params['dataBase64'])
            if self.lose_chunk and path.name == 'chunk-0000.bin':
                self.lose_chunk = False
                raise ConnectionError('response lost')
            return {}
        if method == 'fs/readFile':
            if len(self.files[path]) > DIRECT_LIMIT:
                raise AssertionError('whole large file was read')
            return {'dataBase64': base64.b64encode(self.files[path]).decode()}
        if method == 'command/exec':
            spec = command_spec(params['command'])
            self.specs.append(spec)
            target, stage = PureWindowsPath(spec['target']), PureWindowsPath(spec['stage'])
            if target not in self.files:
                self.files[target] = b''.join(self.files[stage / f'chunk-{i:04d}.bin'] for i in range(len(spec['chunks'])))
            data = self.files[target]
            if digest(data) != spec['sha256']:
                return {'exitCode': 1}
            if self.lose_merge:
                self.lose_merge = False
                raise ConnectionError('merge response lost')
            return {'exitCode': 0, 'stdout': json.dumps({'verified': True, 'size': len(data), 'sha256': digest(data)})}
        raise AssertionError(method)


class TransferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.remote = Remote()
        self.target = self.remote.root / 'gm.exe'
        self.data = b'x' * (9 * 1024 * 1024)

    def transfer(self, allowed=True):
        return McpFileTransfer(self.remote, RemoteFiles(self.remote), self.remote.root, b'owner', allow_full_access=allowed)

    async def test_large_upload_is_chunked_and_never_read_as_whole(self):
        await self.transfer().write_large(self.target, self.data)
        self.assertEqual(self.remote.files[self.target], self.data)
        for method, params in self.remote.calls:
            if method == 'fs/writeFile':
                self.assertLessEqual(len(base64.b64decode(params['dataBase64'])), CHUNK_SIZE)
            if method == 'command/exec':
                self.assertEqual(params['sandboxPolicy'], {'type': 'dangerFullAccess'})
                self.assertIn('-EncodedCommand', params['command'])
                self.assertNotIn('-File', params['command'])
                self.assertLess(len(subprocess.list2cmdline(params['command'])), 32767)

    async def test_permission_is_checked_before_any_remote_operation(self):
        with self.assertRaisesRegex(SkillError, '授权完全访问'):
            await self.transfer(False).write_large(self.target, self.data)
        self.assertEqual(self.remote.calls, [])

    async def test_chunk_response_loss_reuses_verified_chunk(self):
        self.remote.lose_chunk = True
        with self.assertRaises(ConnectionError):
            await self.transfer().write_large(self.target, self.data)
        await self.transfer().write_large(self.target, self.data)
        writes = [p for m, p in self.remote.calls if m == 'fs/writeFile' and p['path'].endswith('chunk-0000.bin')]
        self.assertEqual(len(writes), 1)

    async def test_changed_chunk_is_not_overwritten(self):
        self.remote.lose_chunk = True
        with self.assertRaises(ConnectionError):
            await self.transfer().write_large(self.target, self.data)
        chunk = next(p for p in self.remote.files if p.name == 'chunk-0000.bin')
        self.remote.files[chunk] = b'changed'
        with self.assertRaisesRegex(SkillError, '分块内容发生变化'):
            await self.transfer().write_large(self.target, self.data)
        self.assertEqual(self.remote.files[chunk], b'changed')

    async def test_lost_merge_response_only_verifies_published_target(self):
        self.remote.lose_merge = True
        with self.assertRaises(ConnectionError):
            await self.transfer().write_large(self.target, self.data)
        before = sum(m == 'fs/writeFile' for m, _ in self.remote.calls)
        await self.transfer().write_large(self.target, self.data)
        self.assertTrue(self.remote.specs[-1]['verifyOnly'])
        self.assertEqual(before, sum(m == 'fs/writeFile' for m, _ in self.remote.calls))

    async def test_changed_final_file_is_rejected(self):
        await self.transfer().write_large(self.target, self.data)
        self.remote.files[self.target] = b'changed'
        with self.assertRaisesRegex(SkillError, '完整性校验失败'):
            await self.transfer().write_large(self.target, self.data)
        self.assertEqual(self.remote.files[self.target], b'changed')

    async def test_unknown_cache_and_link_are_rejected(self):
        cache = self.remote.root / CACHE_NAME
        self.remote.dirs.add(cache)
        self.remote.files[cache / OWNER_NAME] = b'other'
        with self.assertRaisesRegex(SkillError, '归属不匹配'):
            await self.transfer().write_large(self.target, self.data)
        self.remote.links.add(cache)
        with self.assertRaisesRegex(SkillError, '链接'):
            await self.transfer().write_large(self.target, self.data)

    async def test_outside_destination_is_rejected(self):
        with self.assertRaises(SkillError):
            await self.transfer().write_large(PureWindowsPath('C:/outside/gm.exe'), self.data)
        self.assertEqual(self.remote.calls, [])


@unittest.skipUnless(os.name == 'nt' and Path(POWERSHELL).is_file(), '需要 Windows PowerShell')
class MergeCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mcp-merge-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / CACHE_NAME / 'file'
        self.stage.mkdir(parents=True)
        for path in (self.root, self.stage.parent, self.stage):
            (path / OWNER_NAME).write_bytes(b'owner')
        self.data = b'a' * CHUNK_SIZE + b'b' * 1024
        chunks = [self.data[:CHUNK_SIZE], self.data[CHUNK_SIZE:]]
        for i, chunk in enumerate(chunks):
            (self.stage / f'chunk-{i:04d}.bin').write_bytes(chunk)
        self.spec = {'root': str(self.root), 'stage': str(self.stage), 'target': str(self.root / "file'中文.bin"),
                     'size': len(self.data), 'sha256': digest(self.data), 'partialId': 'f' * 32,
                     'chunks': [digest(c) for c in chunks], 'verifyOnly': False,
                     'rootOwner': base64.b64encode(b'owner').decode(), 'stageOwner': base64.b64encode(b'owner').decode()}

    def run_merge(self, success=True):
        result = subprocess.run(merge_command(self.spec), cwd=self.root, capture_output=True, timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        if success:
            self.assertEqual(json.loads(result.stdout), {'verified': True, 'size': len(self.data), 'sha256': digest(self.data)})
        else:
            self.assertNotIn(str(self.root).encode(), result.stderr)
        return result

    def test_real_merge_and_response_loss_recovery(self):
        self.run_merge()
        self.assertEqual(Path(self.spec['target']).read_bytes(), self.data)
        self.spec['verifyOnly'] = True
        self.run_merge()
        self.assertEqual(list(self.stage.glob('*.partial')), [])

    def test_bad_hash_does_not_publish_and_removes_only_own_partial(self):
        self.spec['sha256'] = '0' * 64
        orphan = self.stage / 'old.partial'
        orphan.write_bytes(b'preserve')
        self.run_merge(False)
        self.assertFalse(Path(self.spec['target']).exists())
        self.assertEqual(orphan.read_bytes(), b'preserve')
        self.assertEqual(list(self.stage.glob('*.partial')), [orphan])

    def test_existing_different_file_is_never_overwritten(self):
        Path(self.spec['target']).write_bytes(b'existing')
        self.run_merge(False)
        self.assertEqual(Path(self.spec['target']).read_bytes(), b'existing')

    def test_changed_owner_is_rejected_before_writes(self):
        (self.root / OWNER_NAME).write_bytes(b'changed')
        self.run_merge(False)
        self.assertFalse(Path(self.spec['target']).exists())

    def test_exclusive_merge_lock_stops_concurrent_merge(self):
        # Real FileShare.None behavior is covered by starting a holder process.
        lock = self.stage / 'merge.lock'
        script = "$f=[IO.File]::Open('" + str(lock).replace("'", "''") + "','OpenOrCreate','ReadWrite','None'); Write-Output ready; Start-Sleep 10"
        holder = subprocess.Popen([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', script], stdout=subprocess.PIPE)
        try:
            self.assertEqual(holder.stdout.readline().strip(), b'ready')
            self.run_merge(False)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            holder.stdout.close()


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_zip_and_executable_both_use_chunked_transfer(self):
        from mcp_deployment import McpDeployment
        from mcp_packages import parse_package
        from tests.test_mcp_installation import bundle
        with tempfile.TemporaryDirectory() as directory:
            content = bundle([('gm.exe', b'x' * (DIRECT_LIMIT + 1))])
            metadata, _ = parse_package(content)
            manager = object.__new__(McpDeployment)
            manager.root = Path(directory)
            (manager.root / (metadata['id'] + '.zip')).write_bytes(content)
            manager.store = SimpleNamespace(batch=lambda _: {'package_id': metadata['id']})
            client = SimpleNamespace(request=AsyncMock(side_effect=lambda method, params: (_ for _ in ()).throw(RuntimeError('transfer complete')) if method == 'config/read' else {}))
            fs = SimpleNamespace(ancestors=AsyncMock(), names=AsyncMock(return_value={}), write=AsyncMock())
            transfer = SimpleNamespace(write_large=AsyncMock())
            snapshot = {'settings': {'temporaryRoot': 'C:/deploy/temp', 'programRoot': 'C:/deploy/apps', 'runtimes': []}, 'skill': {'enabled': False}}
            task = {'id': 'task', 'batch_id': 'batch'}
            with patch('mcp_deployment.RemoteFiles', return_value=fs), patch('mcp_deployment.McpFileTransfer', return_value=transfer):
                with self.assertRaisesRegex(SkillError, '授权完全访问'):
                    await manager.install(client, task, snapshot, SimpleNamespace(allow_write=True, allow_full_access=False))
                client.request.assert_not_awaited()
                with self.assertRaisesRegex(RuntimeError, 'transfer complete'):
                    await manager.install(client, task, snapshot, SimpleNamespace(allow_write=True, allow_full_access=True))
            self.assertEqual({call.args[0].name for call in transfer.write_large.await_args_list}, {'.mcp-package.zip', 'gm.exe'})
            self.assertNotIn('fs/writeFile', [call.args[0] for call in client.request.await_args_list])

    def test_package_cannot_collide_with_transfer_cache(self):
        from mcp_packages import parse_package
        from tests.test_mcp_installation import bundle
        for path in ('.mcp-transfer/file', 'inside/.MCP-TRANSFER/file'):
            with self.assertRaisesRegex(SkillError, '保留'):
                parse_package(bundle([(path, b'bad')]))
