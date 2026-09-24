"""平台固定的终端入口注册；不执行安装脚本，不修改机器级 PATH。"""

import base64
import json

from mcp_file_transfer import POWERSHELL
from mcp_installation import is_runtime, validate_terminal
from mcp_packages import windows_path
from skill_packages import SkillError


# 注册表仅访问当前执行账号的 Environment/Path。参数用 Base64 JSON 传入。
# 函数边界允许测试替换环境存储，不在测试中修改真实用户 PATH。
PATH_SCRIPT = r'''
function Read-UserPath {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $false)
    try {
        if ($null -eq $key -or $key.GetValueNames() -notcontains 'Path') {
            return @{ value = ''; kind = [Microsoft.Win32.RegistryValueKind]::ExpandString }
        }
        $kind = $key.GetValueKind('Path')
        if ($kind -notin @([Microsoft.Win32.RegistryValueKind]::String, [Microsoft.Win32.RegistryValueKind]::ExpandString)) { throw 'registry-kind' }
        return @{ value = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames); kind = $kind }
    } finally { if ($null -ne $key) { $key.Dispose() } }
}
function Write-UserPath($before, [string]$value) {
    $current = Read-UserPath
    if ($current.value -cne $before.value -or $current.kind -ne $before.kind) { throw 'concurrent-change' }
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try { $key.SetValue('Path', $value, $before.kind) } finally { $key.Dispose() }
}
function Read-MachinePath { return [Environment]::GetEnvironmentVariable('Path', 'Machine') }
function Notify-PathChanged {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CodexPathNotification {
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam,
        string lParam, uint flags, uint timeout, out UIntPtr result);
}
'@
    $result = [UIntPtr]::Zero
    $null = [CodexPathNotification]::SendMessageTimeout([IntPtr]65535, 0x001A, [UIntPtr]::Zero,
        'Environment', 2, 2000, [ref]$result)
}
function Assert-PathFile([string]$path, [bool]$directory) {
    $item = Get-Item -LiteralPath $path -Force
    if ([bool]($item.Attributes -band [IO.FileAttributes]::Directory) -ne $directory) { throw 'file-type' }
    while ($null -ne $item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'link' }
        if ($item.Attributes -band [IO.FileAttributes]::Directory) { $item = $item.Parent } else { $item = $item.Directory }
    }
}
function Normalize-PathEntry([string]$value, [bool]$expand = $true) {
    $value = $value.Trim().Trim('"')
    if ($expand) { $value = [Environment]::ExpandEnvironmentVariables($value) }
    return $value.TrimEnd('\').ToLowerInvariant()
}
function Invoke-TerminalPath($spec) {
    $mutex = [Threading.Mutex]::new($false, 'Local\CodexTerminalPath-' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    $held = $false
    try {
        try { $held = $mutex.WaitOne(10000) } catch [Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { throw 'busy' }
        $root = [IO.Path]::GetFullPath($spec.root)
        $entry = [IO.Path]::GetFullPath($spec.entry)
        if (-not $entry.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'boundary' }
        Assert-PathFile $root $true
        $owner = Join-Path $root '.mcp-install-owner.json'
        Assert-PathFile $owner $false
        if ((Get-Item -LiteralPath $owner).Length -gt 8192 -or
            [Convert]::ToBase64String([IO.File]::ReadAllBytes($owner)) -cne $spec.owner) { throw 'owner' }
        Assert-PathFile $spec.command $false
        if ($spec.script) { Assert-PathFile $spec.script $false }
        if ($spec.launcher) {
            Assert-PathFile ([IO.Path]::GetDirectoryName($entry)) $true
            $bytes = [Convert]::FromBase64String($spec.launcher)
            if (Test-Path -LiteralPath $entry) {
                Assert-PathFile $entry $false
                if ((Get-Item -LiteralPath $entry).Length -ne $bytes.Length -or
                    [Convert]::ToBase64String([IO.File]::ReadAllBytes($entry)) -cne $spec.launcher) { throw 'launcher-conflict' }
            } else {
                $stream = [IO.File]::Open($entry, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
            }
        }
        Assert-PathFile $entry $false
        $directory = [IO.Path]::GetDirectoryName($entry)
        $before = Read-UserPath
        $updated = $before.value
        $expand = $before.kind -eq [Microsoft.Win32.RegistryValueKind]::ExpandString
        $entries = @($updated.Split(';') | ForEach-Object { Normalize-PathEntry $_ $expand })
        if ($entries -notcontains (Normalize-PathEntry $directory)) {
            if ($updated -and -not $updated.EndsWith(';')) { $updated += ';' }
            $updated += $directory
        }
        if ($updated.Length -gt 32760) { throw 'path-size' }
        # 用持久环境构造验收进程环境；不把陈旧服务进程的 PATH 写回注册表。
        $effectiveUser = $updated
        if ($expand) { $effectiveUser = [Environment]::ExpandEnvironmentVariables($updated) }
        $env:Path = (Read-MachinePath) + ';' + $effectiveUser
        $name = [IO.Path]::GetFileNameWithoutExtension($entry)
        $resolved = @(Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue)
        if ($resolved.Count -eq 0 -or [IO.Path]::GetFullPath($resolved[0].Source) -ine $entry) { throw 'command-conflict' }
        if ($updated -cne $before.value) { Write-UserPath $before $updated }
        $after = Read-UserPath
        if ($after.value -cne $updated) { throw 'write-verification' }
        Notify-PathChanged
        return 'CODEX_TERMINAL_PATH_OK'
    } finally {
        if ($held) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
'''


async def install_terminal(client, fs, result, program, package, task_id, config, runtimes=()):
    terminal = result.get('terminal')
    if terminal is None and result.get('kind') == 'cli':
        terminal = {'command': result['command'], 'args': result['args']}
    if terminal is None:
        return False
    terminal = validate_terminal(terminal, program, runtimes)
    if config.get('allow_write') is not True or config.get('allow_full_access') is not True:
        raise SkillError('终端 PATH 安装需要执行机已授权完全访问；请更新机器权限后重新下发。', 409)
    command, args = terminal['command'], terminal['args']
    for path in [command, *args, POWERSHELL]:
        await fs.ancestors(windows_path(path).parent)
        await fs.metadata(path, False)
    # 先验证真实入口，仅执行固定 --help，不执行登录或业务命令。
    response = await client.request('command/exec', {
        'command': [command, *args, '--help'], 'cwd': result['cwd'],
        'sandboxPolicy': {'type': 'dangerFullAccess'}, 'timeoutMs': 20000, 'outputBytesCap': 4096,
    })
    if (type(response.get('exitCode')) is not int or response['exitCode'] != 0
            or not any(isinstance(response.get(k), str) and response[k].strip() for k in ('stdout', 'stderr'))):
        raise SkillError('CLI 帮助命令验证失败，请检查入口及运行环境后重新检测。', 409)
    script, launcher = None, None
    entry = command
    if is_runtime(command, runtimes):
        script = args[0]
        entry = str(windows_path(script).with_suffix('.cmd'))
        try:
            content = ('@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
                       f'"{command}" "{script}" %*\r\nexit /b %errorlevel%\r\n').encode('ascii')
        except UnicodeEncodeError as error:
            raise SkillError('脚本终端启动器暂不支持非 ASCII 路径；请使用英文安装目录。', 409) from error
        launcher = base64.b64encode(content).decode('ascii')
    spec = {'root': program, 'entry': entry, 'command': command, 'script': script, 'launcher': launcher,
            'owner': base64.b64encode(json.dumps({'taskId': task_id, 'packageId': package}, sort_keys=True).encode()).decode()}
    data = base64.b64encode(json.dumps(spec).encode()).decode()
    code = ("$ErrorActionPreference='Stop'\nSet-StrictMode -Version Latest\n"
            + "$spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + data + "')) | ConvertFrom-Json\n"
            + PATH_SCRIPT + "\ntry { Invoke-TerminalPath $spec } catch { Write-Output 'CODEX_TERMINAL_PATH_FAILED'; exit 1 }")
    response = await client.request('command/exec', {
        'command': [POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand',
                    base64.b64encode(code.encode('utf-16-le')).decode()],
        'cwd': r'C:\Windows\System32', 'sandboxPolicy': {'type': 'dangerFullAccess'},
        'timeoutMs': 20000, 'outputBytesCap': 1024,
    })
    if (type(response.get('exitCode')) is not int or response['exitCode'] != 0
            or not isinstance(response.get('stdout'), str)
            or response['stdout'].strip() != 'CODEX_TERMINAL_PATH_OK'):
        raise SkillError('终端用户 PATH 写入或命令解析检查失败，请检查同名命令、目录归属及权限后重新检测。', 409)
    return True
