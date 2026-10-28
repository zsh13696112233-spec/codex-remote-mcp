"""定时任务的 Windows BAT/CMD 检查，不创建模型会话、不重试命令。"""

import asyncio
import base64
import json
import logging
import math
import re
import time
import uuid
from pathlib import PureWindowsPath

LOGGER = logging.getLogger(__name__)


def command_for(path, arguments, timeout):
    # ProcessStartInfo.Arguments 保留 cmd /s /c 所需的外层引号；argv 自动转义会破坏含空格的 BAT 路径。
    spec = base64.b64encode(json.dumps({"path": path, "args": arguments, "timeout": timeout}).encode()).decode()
    script = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -TypeDefinition @'
using System.IO;
using System.Text;
using System.Threading.Tasks;
public class ScheduleCapture {
    public string Text;
    public bool Truncated;
}
public static class ScheduleOutput {
    public static async Task<ScheduleCapture> Read(StreamReader reader) {
        var value = new StringBuilder();
        bool truncated = false;
        var buffer = new char[1024];
        int count;
        while ((count = await reader.ReadAsync(buffer, 0, buffer.Length)) > 0) {
            int keep = System.Math.Min(count, 8192 - value.Length);
            if (keep < count) truncated = true;
            value.Append(buffer, 0, keep);
        }
        return new ScheduleCapture { Text = value.ToString(), Truncated = truncated };
    }
}
'@
$spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('SPEC')) | ConvertFrom-Json
$info = New-Object Diagnostics.ProcessStartInfo
$info.FileName = Join-Path $env:SystemRoot 'System32\cmd.exe'
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$info.Arguments = '/d /v:off /s /c ""' + $spec.path + '" ' + $spec.args + '"'
$process = [Diagnostics.Process]::Start($info)
$stdout = [ScheduleOutput]::Read($process.StandardOutput)
$stderr = [ScheduleOutput]::Read($process.StandardError)
try {
    if (-not $process.WaitForExit([int]$spec.timeout * 1000)) {
        & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $process.Id /T /F *> $null
        exit 124
    }
    if (-not $stdout.Wait(1000) -or -not $stderr.Wait(1000)) { exit 125 }
    if ($stdout.Result.Truncated) { exit 126 }
    # 只回传已完整读取的判断值，避免执行服务按字节截断后把不完整首行变成 TRUE。
    $first = ''
    foreach ($line in ($stdout.Result.Text -split '\r\n|\n|\r')) {
        if ($line.Trim()) { $first = $line.Trim().TrimStart([char]0xfeff); break }
    }
    if ($first -ieq 'TRUE') { [Console]::Out.WriteLine('TRUE') }
    elseif ($first -ieq 'FALSE') { [Console]::Out.WriteLine('FALSE') }
    else { [Console]::Out.WriteLine('INVALID') }
    exit $process.ExitCode
} finally { $process.Dispose() }
'''.replace('SPEC', spec)
    return [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(script.encode('utf-16-le')).decode()]


def validate_spec(body):
    if not isinstance(body, dict):
        raise ValueError("检查配置必须是对象。")
    path, arguments = body.get("scriptPath"), body.get("arguments", "")
    if (not isinstance(path, str) or len(path) > 1024
            or not re.match(r"^[A-Za-z]:[\\/]", path)
            or PureWindowsPath(path).suffix.lower() not in {".bat", ".cmd"}
            or any(c in path for c in '\"%!?*<>|&^\r\n\x00')
            or ":" in path[2:] or ".." in PureWindowsPath(path).parts):
        raise ValueError("请填写 Windows 本地磁盘上的 BAT/CMD 绝对路径，不支持命令符号或相对目录。")
    if (not isinstance(arguments, str) or len(arguments) > 2048
            or any(c in arguments for c in '%!<>|&^\r\n\x00')
            or arguments.count('"') % 2):
        raise ValueError("参数最多 2048 字符，双引号须成对，不支持变量展开、管道、重定向或命令连接符。")
    timeout = body.get("timeoutSeconds", 60)
    if type(timeout) is not int or not 5 <= timeout <= 120:
        raise ValueError("检查超时必须为 5 至 120 秒。")
    return str(PureWindowsPath(path)), arguments, timeout


def result(status, reason):
    return {"status": status, "reason": reason}


def parse_result(response):
    code = response.get("exitCode")
    if type(code) is not int:
        return result("failed", "执行服务未返回有效退出码，检查结果不明确。")
    if code != 0:
        if code == 124:
            return result("failed", "检查超时或脚本返回超时退出码 124，未启动本次任务。")
        if code == 126:
            return result("failed", "检查输出超过容量限制或脚本返回退出码 126，未启动本次任务。")
        return result("failed", f"检查脚本退出码为 {code}，请在执行机核对脚本及运行环境。")
    output = response.get("stdout")
    first = next((line.strip().lstrip('\ufeff') for line in output.splitlines() if line.strip()), "") if isinstance(output, str) else ""
    if first.lower() == "true":
        return result("passed", "检查通过。")
    if first.lower() == "false":
        return result("skipped", "条件不满足，已跳过本次。")
    return result("failed", "脚本首条非空输出必须为 TRUE 或 FALSE。")


async def execute_check(gateway, body):
    path, arguments, timeout = validate_spec(body)
    deadline = time.monotonic() + timeout + 10
    agent_id, group_id = body.get("agentId"), body.get("groupId")
    if not isinstance(agent_id, str) or not isinstance(group_id, str) or not group_id:
        raise ValueError("请选择检查执行机及分组。")
    rows = await asyncio.to_thread(gateway.registry.rows)
    row = rows.get(agent_id)
    if row is None or row["group_id"] != group_id:
        raise ValueError("检查执行机不存在或不属于当前分组。")
    agent = gateway.registry.config_from_row(agent_id, row)
    if not agent.enabled or "executor" not in agent.capabilities or row["test_status"] != "passed":
        return result("failed", "检查执行机须启用、具备执行能力并通过连接检测。")
    # 任意用户脚本可能写入文件或访问网络，不能沿用固定平台命令的权限例外。
    if not agent.allow_write or not agent.allow_full_access:
        return result("failed", "执行检查脚本需要执行机已授权写入和完全访问。")
    if not re.match(r"^[A-Za-z]:[\\/]", agent.cwd):
        return result("failed", "检查仅支持 Windows 执行机。")
    check_cwd = str(PureWindowsPath(path).parent)
    if not agent.allow_cwd_override and PureWindowsPath(check_cwd) != PureWindowsPath(agent.cwd):
        return result("failed", "检查需使用脚本所在目录，请先授权执行机目录覆盖权限。")
    from codex_orchestrator_mcp import AppServerRpcTimeout
    from workflow_resources import claim_check, finish_check
    from workflow_store import utc_now
    operation_id = uuid.uuid4().hex
    if not await asyncio.to_thread(claim_check, gateway.store, agent_id, operation_id, utc_now()):
        return result("skipped", "检查执行机正在处理其他任务或执行状态待核实，本次不执行检查。")
    stopped = True
    async def run():
        nonlocal stopped
        client = gateway.orchestrator._client_factory(
            agent.url, token=gateway.orchestrator._resolve_agent_token(agent),
            request_timeout_sec=timeout + 5)
        async with client:
            metadata = await client.request("fs/getMetadata", {"path": path})
            if not isinstance(metadata, dict) or metadata.get("isFile") is not True or metadata.get("isSymlink") is not False:
                return result("failed", "检查脚本不存在、不是普通文件或为链接。")
            # 连接和元数据查询也消耗本次期限，给远程终止和返回保留 5 秒。
            remaining = min(timeout, math.floor(deadline - time.monotonic() - 5))
            if remaining < 1:
                return result("failed", "执行前检查超时，未启动检查脚本及本次任务。")
            # 仅本次检查使用脚本目录，不修改机器登记或其他任务；/d 禁用 AutoRun。
            stopped = False
            response = await client.request("command/exec", {
                "command": command_for(path, arguments, remaining),
                "cwd": check_cwd, "sandboxPolicy": {"type": "dangerFullAccess"},
                "timeoutMs": (remaining + 3) * 1000, "outputBytesCap": 8192,
            }, timeout_sec=remaining + 4)
            stopped = type(response.get("exitCode")) is int and response["exitCode"] not in {124, 125}
            return parse_result(response)
    try:
        return await asyncio.wait_for(run(), timeout=max(0, deadline - time.monotonic()))
    except (asyncio.TimeoutError, AppServerRpcTimeout):
        return result("failed", "执行前检查超时，未启动本次任务。")
    except Exception as error:
        # 远程边界隔离；不向页面传递凭据、地址、原始脚本输出或 RPC 错误。
        LOGGER.warning("定时检查执行失败，异常类型=%s", type(error).__name__)
        return result("failed", "无法完成检查，请核对执行机连接、脚本文件和运行权限。")
    finally:
        await asyncio.to_thread(finish_check, gateway.store, operation_id, stopped)
