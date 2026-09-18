"""显式指定执行服务和已授权 Windows 目录的独立文件传输诊断。"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import uuid
from pathlib import Path, PureWindowsPath

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


def connection_token(args):
    if getattr(args, "gateway_credentials", False):
        # 仅在用户明确授权时使用；配置和凭据内容不得写入日志或结果。
        config = json.loads((Path(__file__).resolve().parents[1] /
                             "config/workflow-service.json").read_text(encoding="utf-8-sig"))
        defaults = config.get("machine_defaults", {})
        env_name, token_file = defaults.get("token_env"), defaults.get("token_file")
        if bool(env_name) == bool(token_file):
            raise ValueError("必须配置一个凭据引用")
        if env_name:
            token = os.environ[env_name]
        else:
            path = Path(token_file)
            if not path.is_absolute() or path.stat().st_size > 8192:
                raise ValueError("凭据文件引用无效")
            token = path.read_text(encoding="utf-8-sig").strip()
        if not token or any(c in token for c in ("\n", "\r", "\0")):
            raise ValueError("凭据格式无效")
        return token
    return os.environ[args.token_env] if args.token_env else None


async def probe(args):
    root = PureWindowsPath(args.root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("需要已授权的 Windows 绝对目录")
    target = root / (".transfer-probe-" + uuid.uuid4().hex)
    token = connection_token(args)
    headers = {"Authorization": "Bearer " + token} if token else None
    async with connect(args.url, additional_headers=headers, max_size=64 * 1024 * 1024,
                       open_timeout=15, ping_interval=20, ping_timeout=20) as socket:
        sequence = 0

        async def rpc(method, params):
            nonlocal sequence
            sequence += 1
            payload = json.dumps({"method": method, "id": sequence, "params": params}, ensure_ascii=False)
            print(json.dumps({"method": method, "request_bytes": len(payload.encode("utf-8"))}), flush=True)
            await socket.send(payload)
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), 30))
                if message.get("id") == sequence:
                    if "error" in message:
                        error = message["error"]
                        raw = str(error.get("message", "")).lower()
                        print(json.dumps({"rpc_error_code": error.get("code"),
                                          "windows_sandbox_unsupported": "windows sandbox" in raw and "not supported" in raw}), flush=True)
                        raise RuntimeError("RPC error code=" + str(message["error"].get("code")))
                    return message.get("result")

        await rpc("initialize", {"clientInfo": {"name": "file-transfer-probe", "version": "1"},
                                 "capabilities": {"experimentalApi": False}})
        await socket.send(json.dumps({"method": "initialized", "params": {}}))
        for parent in [*reversed(root.parents), root]:
            metadata = await rpc("fs/getMetadata", {"path": str(parent)})
            if metadata.get("isSymlink") is not False or metadata.get("isDirectory") is not True:
                raise RuntimeError("授权目录包含链接或类型不匹配")
        await rpc("fs/createDirectory", {"path": str(target), "recursive": False})
        print(json.dumps({"probe_directory": str(target)}), flush=True)
        for size in args.sizes:
            data = os.urandom(size * 1024 * 1024)
            if getattr(args, "chunked", False):
                await chunked_probe(rpc, target, data, full_access=getattr(args, "merge_full_access", False),
                                    powershell=args.powershell)
                continue
            path = target / (str(size) + "-MiB.bin")
            await rpc("fs/writeFile", {"path": str(path), "dataBase64": base64.b64encode(data).decode("ascii")})
            response = await rpc("fs/readFile", {"path": str(path)})
            actual = base64.b64decode(response["dataBase64"], validate=True)
            if hashlib.sha256(actual).digest() != hashlib.sha256(data).digest():
                raise RuntimeError("回读哈希不一致")
            print(json.dumps({"size_mib": size, "verified": True}), flush=True)
            # 仅删除本次生成的确切文件，禁止递归清理。
            await rpc("fs/remove", {"path": str(path), "recursive": False, "force": False})
        await rpc("fs/remove", {"path": str(target), "recursive": False, "force": False})


async def chunked_probe(rpc, target, data, *, full_access=False,
                        powershell="C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"):
    executable = PureWindowsPath(powershell)
    if not executable.is_absolute() or ".." in executable.parts:
        raise ValueError("需要 PowerShell 绝对路径")
    metadata = await rpc("fs/getMetadata", {"path": str(executable)})
    if metadata.get("isFile") is not True or metadata.get("isSymlink") is not False:
        raise RuntimeError("PowerShell 路径无效")
    chunk_size = 4 * 1024 * 1024
    files = []
    for index, offset in enumerate(range(0, len(data), chunk_size)):
        path = target / f"chunk-{index:04d}.bin"
        chunk = data[offset:offset + chunk_size]
        await rpc("fs/writeFile", {"path": str(path), "dataBase64": base64.b64encode(chunk).decode("ascii")})
        readback = await rpc("fs/readFile", {"path": str(path)})
        if base64.b64decode(readback["dataBase64"], validate=True) != chunk:
            raise RuntimeError("分块回读不一致")
        files.append(path)
    expected = hashlib.sha256(data).hexdigest()
    manifest = json.dumps({"count": len(files), "size": len(data), "sha256": expected}).encode("ascii")
    script = Path(__file__).with_name("probe_merge_chunks.ps1").read_bytes()
    for name, content in (("manifest.json", manifest), ("merge.ps1", script)):
        path = target / name
        await rpc("fs/writeFile", {"path": str(path), "dataBase64": base64.b64encode(content).decode("ascii")})
        readback = await rpc("fs/readFile", {"path": str(path)})
        if base64.b64decode(readback["dataBase64"], validate=True) != content:
            raise RuntimeError("合并程序回读不一致")
        files.append(path)
    policy = ({"type": "dangerFullAccess"} if full_access else
              {"type": "workspaceWrite", "writableRoots": [str(target)],
               "networkAccess": False, "excludeTmpdirEnvVar": True, "excludeSlashTmp": True})
    print(json.dumps({"merge_permission": policy["type"]}), flush=True)
    result = await rpc("command/exec", {
        "command": [str(executable), "-NoProfile", "-NonInteractive", "-File", str(target / "merge.ps1")],
        "cwd": str(target), "timeoutMs": 20000, "outputBytesCap": 4096,
        "sandboxPolicy": policy,
    })
    if result.get("exitCode") != 0:
        print(json.dumps({"merge_exit_code": result.get("exitCode"), "verified": False}), flush=True)
        raise RuntimeError("固定合并程序失败")
    verified = json.loads(result["stdout"].strip().lstrip("\ufeff"))
    if verified != {"verified": True, "size": len(data), "sha256": expected}:
        raise RuntimeError("合并结果不匹配")
    print(json.dumps({"size_mib": len(data) // 1024 // 1024, "chunked": True,
                      "chunk_count": (len(data) + chunk_size - 1) // chunk_size,
                      "verified": True, "sha256": expected}), flush=True)
    files.append(target / "merged.bin")
    for path in files:
        await rpc("fs/remove", {"path": str(path), "recursive": False, "force": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--root", required=True)
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--token-env", help="可选：令牌环境变量名，不接受令牌值")
    credentials.add_argument("--gateway-credentials", action="store_true",
                             help="需明确授权：仅在内存中复用中央网关凭据")
    parser.add_argument("--sizes", nargs="+", type=int, default=[8])
    parser.add_argument("--chunked", action="store_true", help="4 MiB 分块上传，在限定目录内合并并校验")
    parser.add_argument("--merge-full-access", action="store_true",
                        help="需明确授权：仅固定合并命令请求完全访问，不修改服务默认权限")
    parser.add_argument("--powershell", default="C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                        help="执行机上 Windows PowerShell 的绝对路径，仅分块合并使用")
    args = parser.parse_args()
    if args.merge_full_access and not args.chunked:
        parser.error("完全访问验证仅适用于分块合并试验")
    if not args.url.startswith(("ws://", "wss://")) or any(size < 1 or size > 20 for size in args.sizes):
        parser.error("需要 WebSocket 地址及 1–20 MiB 的测试大小")
    try:
        asyncio.run(probe(args))
    except ConnectionClosed as error:
        received = getattr(error, "rcvd", None)
        print(json.dumps({"error": "connection_closed", "close_code": getattr(received, "code", None)}))
        return 1
    except InvalidStatus as error:
        print(json.dumps({"error": "handshake_rejected", "http_status": error.response.status_code}))
        return 1
    except Exception as error:
        # 不输出连接地址、凭据、服务端原始错误或文件内容。
        print(json.dumps({"error": type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
