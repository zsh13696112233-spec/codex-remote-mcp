import importlib.util
import io
import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "file_transfer_probe", Path(__file__).resolve().parents[3] / "scripts/probe_mcp_file_transfer.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Socket:
    def __init__(self, corrupt=False):
        self.calls = []
        self.content = None
        self.result = None
        self.corrupt = corrupt

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def send(self, payload):
        value = json.loads(payload)
        self.calls.append(value)
        method = value["method"]
        result = {}
        if method == "fs/getMetadata":
            result = {"isSymlink": False, "isDirectory": True}
        elif method == "fs/writeFile":
            self.content = value["params"]["dataBase64"]
        elif method == "fs/readFile":
            result = {"dataBase64": "YQ==" if self.corrupt else self.content}
        self.result = json.dumps({"id": value.get("id"), "result": result})

    async def recv(self):
        return self.result


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunk_merge_full_access_requires_explicit_selection(self):
        from pathlib import PureWindowsPath
        data = b"probe-content"
        for full_access in (False, True):
            files, commands = {}, []

            async def rpc(method, params):
                if method == "fs/getMetadata":
                    return {"isFile": True, "isSymlink": False}
                if method == "fs/writeFile":
                    files[params["path"]] = params["dataBase64"]
                    return {}
                if method == "fs/readFile":
                    return {"dataBase64": files[params["path"]]}
                if method == "command/exec":
                    commands.append(params)
                    return {"exitCode": 0, "stdout": json.dumps({"verified": True, "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})}
                self.assertEqual(method, "fs/remove")
                self.assertFalse(params["recursive"])
                return {}

            with redirect_stdout(io.StringIO()):
                await probe.chunked_probe(rpc, PureWindowsPath("C:/test/probe"), data, full_access=full_access)
            self.assertEqual(len(commands), 1)
            self.assertTrue(PureWindowsPath(commands[0]["command"][0]).is_absolute())
            policy = commands[0]["sandboxPolicy"]
            self.assertEqual(policy["type"], "dangerFullAccess" if full_access else "workspaceWrite")
            if not full_access:
                self.assertFalse(policy["networkAccess"])
                self.assertEqual(policy["writableRoots"], ["C:\\test\\probe"])

    def test_gateway_credential_reference_is_resolved_without_output(self):
        args = SimpleNamespace(gateway_credentials=True)
        config = json.dumps({"machine_defaults": {"token_env": "PROBE_TEST_TOKEN"}})
        output = io.StringIO()
        with patch.object(Path, "read_text", return_value=config), \
                patch.dict(probe.os.environ, {"PROBE_TEST_TOKEN": "private-test-value"}), \
                redirect_stdout(output):
            self.assertEqual(probe.connection_token(args), "private-test-value")
        self.assertEqual(output.getvalue(), "")

    async def test_eight_mib_roundtrip_cleans_only_its_own_paths(self):
        socket = Socket()
        args = SimpleNamespace(url="ws://unused", root="C:\\test", sizes=[8], token_env=None)
        output = io.StringIO()
        with patch.object(probe, "connect", return_value=socket), redirect_stdout(output):
            await probe.probe(args)
        self.assertIn('"verified": true', output.getvalue())
        removed = [c["params"] for c in socket.calls if c["method"] == "fs/remove"]
        self.assertEqual(len(removed), 2)
        for params in removed:
            self.assertFalse(params["recursive"])
            self.assertTrue(params["path"].startswith("C:\\test\\.transfer-probe-"))
        self.assertNotIn(socket.content[:100], output.getvalue())

    async def test_corrupt_readback_stops_before_larger_file(self):
        socket = Socket(corrupt=True)
        args = SimpleNamespace(url="ws://unused", root="C:\\test", sizes=[8, 12], token_env=None)
        with patch.object(probe, "connect", return_value=socket), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "回读哈希不一致"):
                await probe.probe(args)
        self.assertEqual(sum(c["method"] == "fs/writeFile" for c in socket.calls), 1)


POWERSHELL = shutil.which("powershell.exe") or str(
    Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")


@unittest.skipUnless(os.name == "nt" and Path(POWERSHELL).is_file(), "需要 Windows PowerShell")
class MergeScriptTests(unittest.TestCase):
    def run_merge(self, corrupt=False, existing=False):
        with tempfile.TemporaryDirectory(prefix="chunk-merge-test-") as directory:
            root = Path(directory)
            script = Path(probe.__file__).with_name("probe_merge_chunks.ps1")
            shutil.copyfile(script, root / "merge.ps1")
            data = b"a" * (4 * 1024 * 1024) + b"b" * 1024
            (root / "chunk-0000.bin").write_bytes(data[:4 * 1024 * 1024])
            (root / "chunk-0001.bin").write_bytes(data[4 * 1024 * 1024:])
            digest = hashlib.sha256(data).hexdigest()
            (root / "manifest.json").write_text(json.dumps({"count": 2, "size": len(data),
                "sha256": "0" * 64 if corrupt else digest}), encoding="ascii")
            if existing:
                (root / "merged.bin").write_bytes(b"preserve")
            result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(root / "merge.ps1")],
                                    cwd=root, capture_output=True, timeout=20)
            if corrupt or existing:
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b'"verified":true', result.stdout)
                self.assertNotIn(str(root).encode(), result.stderr)
                if existing:
                    self.assertEqual((root / "merged.bin").read_bytes(), b"preserve")
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"verified": True, "size": len(data), "sha256": digest})
                self.assertEqual((root / "merged.bin").read_bytes(), data)

    def test_merge_and_hash_match(self):
        self.run_merge()

    def test_bad_hash_is_rejected(self):
        self.run_merge(corrupt=True)

    def test_existing_output_is_not_overwritten(self):
        self.run_merge(existing=True)
