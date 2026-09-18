"""MCP 大文件分块传输；完全访问只用于平台固定合并代码。"""

import base64
import hashlib
import json
import logging
import uuid

from codex_orchestrator_mcp import AppServerRpcError
from mcp_packages import FILE_LIMIT, windows_path, within
from skill_packages import SkillError

LOGGER = logging.getLogger(__name__)
DIRECT_LIMIT = 8 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024
CACHE_NAME = ".mcp-transfer"
OWNER_NAME = ".mcp-install-owner.json"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# 通过 -EncodedCommand 发送平台代码和平台构造的参数，不执行远程缓存中的脚本。
# 参数由 Base64 JSON 注入，不将包路径插入 PowerShell 源码。
MERGE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest
$lockStream = $null
$partialCreated = $false
$partial = $null
function Check-Directory([string]$path) {
    $item = Get-Item -LiteralPath $path -Force
    while ($null -ne $item) {
        if (-not ($item.Attributes -band [IO.FileAttributes]::Directory) -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'directory' }
        $item = $item.Parent
    }
}
function Check-File([string]$path) {
    Check-Directory ([IO.Path]::GetDirectoryName($path))
    $item = Get-Item -LiteralPath $path -Force
    if (($item.Attributes -band [IO.FileAttributes]::Directory) -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'file' }
}
function Hash-File([string]$path) {
    Check-File $path
    $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $hash.Dispose() }
}
function Check-Owner([string]$path, [string]$expected) {
    Check-File $path
    if ((Get-Item -LiteralPath $path).Length -gt 8192) { throw 'owner' }
    if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($path)) -cne $expected) { throw 'owner' }
}
try {
    $root = [IO.Path]::GetFullPath($spec.root)
    $target = [IO.Path]::GetFullPath($spec.target)
    $stage = [IO.Path]::GetFullPath($spec.stage)
    $prefix = $root.TrimEnd('\') + '\'
    if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not $stage.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
        (Get-Location).Path -ne $root) { throw 'boundary' }
    if ($spec.size -lt 1 -or $spec.size -gt 33554432 -or
        $spec.sha256 -notmatch '^[a-f0-9]{64}$' -or
        $spec.partialId -notmatch '^[a-f0-9]{32}$') { throw 'spec' }
    Check-Directory $root
    Check-Directory $stage
    Check-Directory ([IO.Path]::GetDirectoryName($target))
    Check-Owner (Join-Path $root '.mcp-install-owner.json') $spec.rootOwner
    Check-Owner (Join-Path (Join-Path $root '.mcp-transfer') '.mcp-install-owner.json') $spec.rootOwner
    Check-Owner (Join-Path $stage '.mcp-install-owner.json') $spec.stageOwner
    $lockPath = Join-Path $stage 'merge.lock'
    if (Test-Path -LiteralPath $lockPath) { Check-File $lockPath }
    $lockStream = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    if (Test-Path -LiteralPath $target) {
        Check-File $target
        if ((Get-Item -LiteralPath $target).Length -ne $spec.size -or (Hash-File $target) -ne $spec.sha256) { throw 'changed' }
    } else {
        if ($spec.verifyOnly) { throw 'missing' }
        $count = [Math]::Ceiling($spec.size / 4194304)
        if ($spec.chunks.Count -ne $count) { throw 'count' }
        $partial = Join-Path $stage ($spec.partialId + '.partial')
        $output = [IO.File]::Open($partial, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $partialCreated = $true
        try {
            for ($index = 0; $index -lt $count; $index++) {
                $chunk = Join-Path $stage ('chunk-{0:D4}.bin' -f $index)
                Check-File $chunk
                $expectedSize = [Math]::Min(4194304, $spec.size - $index * 4194304)
                if ((Get-Item -LiteralPath $chunk).Length -ne $expectedSize -or
                    (Hash-File $chunk) -ne $spec.chunks[$index]) { throw 'chunk' }
                $inputStream = [IO.File]::Open($chunk, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
                try { $inputStream.CopyTo($output) } finally { $inputStream.Dispose() }
            }
        } finally { $output.Dispose() }
        if ((Get-Item -LiteralPath $partial).Length -ne $spec.size -or (Hash-File $partial) -ne $spec.sha256) { throw 'hash' }
        Check-Directory ([IO.Path]::GetDirectoryName($target))
        # 同卷发布，File.Move 两参数版本拒绝覆盖；目标出现时停止。
        [IO.File]::Move($partial, $target)
        $partialCreated = $false
    }
    @{ verified = $true; size = $spec.size; sha256 = $spec.sha256 } | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine('MCP transfer verification failed')
    exit 1
} finally {
    if ($partialCreated -and $null -ne $partial) {
        try {
            # 只删除本次确实创建的随机临时文件，不递归、不清理未知遗留文件。
            Check-File $partial
            [IO.File]::Delete($partial)
        } catch { [Console]::Error.WriteLine('MCP transfer partial retained') }
    }
    if ($null -ne $lockStream) { $lockStream.Dispose() }
}
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def merge_command(spec):
    encoded_spec = base64.b64encode(json.dumps(spec, ensure_ascii=True).encode("ascii")).decode("ascii")
    source = "$spec = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded_spec + "')))\n" + MERGE_SCRIPT
    command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand",
               base64.b64encode(source.encode("utf-16-le")).decode("ascii")]
    if sum(len(part) + 3 for part in command) > 30000:
        raise SkillError("合并命令路径参数过长，请使用较短的安装目录。", 409)
    return command


class McpFileTransfer:
    def __init__(self, client, fs, root, owner, *, allow_full_access=False):
        self.client, self.fs = client, fs
        self.root, self.owner = windows_path(str(root)), owner
        self.allow_full_access = allow_full_access

    async def owned_directory(self, path, owner):
        await self.fs.ancestors(path.parent)
        if path.name.casefold() not in await self.fs.names(path.parent):
            await self.client.request("fs/createDirectory", {"path": str(path), "recursive": False})
            await self.fs.write(path / OWNER_NAME, owner)
        else:
            await self.fs.metadata(path, True)
            if await self.fs.read(path / OWNER_NAME) != owner:
                raise SkillError("分块缓存目录归属不匹配，禁止覆盖。", 409)

    async def write_large(self, path, data):
        if not self.allow_full_access:
            raise SkillError("大文件合并需要执行机已授权完全访问；请更新机器权限后重新下发。", 409)
        path = windows_path(str(path))
        if not within(str(path), str(self.root)) or not DIRECT_LIMIT < len(data) <= FILE_LIMIT:
            raise SkillError("分块传输路径或文件大小不正确。", 409)
        await self.fs.ancestors(path.parent)
        await self.fs.metadata(windows_path(POWERSHELL), False)
        if await self.fs.read(self.root / OWNER_NAME) != self.owner:
            raise SkillError("临时目录归属发生变化，停止下发。", 409)
        cache = self.root / CACHE_NAME
        await self.owned_directory(cache, self.owner)
        sha = digest(data)
        stage = cache / digest(str(path.relative_to(self.root)).encode("utf-8"))
        stage_owner = json.dumps({"path": str(path), "sha256": sha, "size": len(data)}, sort_keys=True).encode()
        await self.owned_directory(stage, stage_owner)
        exists = path.name.casefold() in await self.fs.names(path.parent)
        hashes = [digest(data[offset:offset + CHUNK_SIZE]) for offset in range(0, len(data), CHUNK_SIZE)]
        if not exists:
            names = await self.fs.names(stage)
            for index, offset in enumerate(range(0, len(data), CHUNK_SIZE)):
                chunk = data[offset:offset + CHUNK_SIZE]
                part = stage / f"chunk-{index:04d}.bin"
                if part.name.casefold() in names:
                    if await self.fs.read(part) != chunk:
                        raise SkillError("已有分块内容发生变化，禁止覆盖。", 409)
                else:
                    await self.fs.write(part, chunk)
        else:
            await self.fs.metadata(path, False)
        spec = {"root": str(self.root), "target": str(path), "stage": str(stage), "size": len(data),
                "sha256": sha, "chunks": hashes, "verifyOnly": exists, "partialId": uuid.uuid4().hex,
                "rootOwner": base64.b64encode(self.owner).decode("ascii"),
                "stageOwner": base64.b64encode(stage_owner).decode("ascii")}
        LOGGER.info("MCP 大文件校验，size=%s，chunks=%s，existing=%s", len(data), len(hashes), exists)
        try:
            result = await self.client.request("command/exec", {
                "command": merge_command(spec), "cwd": str(self.root), "timeoutMs": 20000,
                "outputBytesCap": 4096, "sandboxPolicy": {"type": "dangerFullAccess"},
            })
        except AppServerRpcError as error:
            LOGGER.warning("MCP 固定合并接口失败，code=%s", error.code)
            raise SkillError("执行机无法运行固定合并程序，请检查执行服务版本和权限。", 409) from error
        if not isinstance(result, dict) or result.get("exitCode") != 0:
            raise SkillError("大文件合并或完整性校验失败，请核对目录占用及文件变化后重试。", 409)
        try:
            verified = json.loads(result["stdout"])
        except (KeyError, TypeError, ValueError) as error:
            raise SkillError("大文件校验响应不正确。", 409) from error
        if verified != {"verified": True, "size": len(data), "sha256": sha}:
            raise SkillError("大文件校验结果不匹配，停止下发。", 409)
