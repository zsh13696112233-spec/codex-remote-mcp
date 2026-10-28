"""通过监督机已有 Jira CLI 领取；凭据通过文件 RPC 下发，不进入模型上下文。"""

import asyncio
import base64
import hashlib
import json
import logging
import re
from pathlib import PureWindowsPath

from workflow_jira import ISSUE_KEY, run_directory, server_key

CONFIG_PATH = PureWindowsPath(r"C:\Users\admin\.jira_cli\config.json")
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
FILE_LIMIT = 64 * 1024
LOGGER = logging.getLogger(__name__)


def account_from_config(content, alias):
    try:
        config = json.loads(content.decode("utf-8-sig"))
        accounts = config["accounts"]
        if not isinstance(accounts, dict) or not accounts:
            raise ValueError()
        if not alias:
            if len(accounts) != 1:
                raise ValueError()
            alias = next(iter(accounts))
        account = accounts[alias]
        token = account["token"]
        if (not isinstance(alias, str) or not 1 <= len(alias) <= 128 or any(ord(c) < 32 for c in alias)
                or not isinstance(token, str) or not 1 <= len(token) <= 8192
                or any(ord(c) < 32 for c in token) or type(account["readOnly"]) is not bool):
            raise ValueError()
        return alias, {"baseUrl": server_key(account["baseUrl"]), "token": token,
                       "readOnly": account["readOnly"]}
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("监督机 Jira 配置无效、账号不存在或存在多个账号；请检查 SOP 账号别名及 token 配置。") from None


def issue_keys(output):
    try:
        value = json.loads(output)
        rows = value["issues"] if isinstance(value, dict) else value
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError()
        keys = [row["key"] for row in rows]
        if any(not isinstance(key, str) or not ISSUE_KEY.fullmatch(key) for key in keys):
            raise ValueError()
        return list(dict.fromkeys(keys))
    except (ValueError, TypeError, KeyError):
        raise ValueError("Jira CLI 搜索输出不是工单数组或 issues 数组，或超过 1000 项；请缩小查询范围并检查 CLI JSON 输出。") from None


def excluding_query(jql, keys):
    if not keys:
        return jql
    # ORDER BY 必须留在最外层；不把引号中的文本、转义字符当成排序子句。
    quote, escaped, depth, split = None, False, 0, len(jql)
    for index, char in enumerate(jql):
        if escaped:
            escaped = False
        elif quote:
            if char == "\\": escaped = True
            elif char == quote: quote = None
        elif char in "\"'": quote = char
        elif char == "(": depth += 1
        elif char == ")": depth -= 1
        elif depth == 0 and (index == 0 or not jql[index-1].isalnum()) and re.match(r"(?i)ORDER\s+BY\b", jql[index:]):
            split = index
            break
    return "(" + jql[:split].strip() + ") AND key NOT IN (" + ",".join(keys) + ") " + jql[split:]


async def read_file(client, path):
    for parent in [*reversed(path.parents), path]:
        metadata = await client.request("fs/getMetadata", {"path": str(parent)})
        kind = "isFile" if parent == path else "isDirectory"
        if not isinstance(metadata, dict) or metadata.get("isSymlink") is not False or metadata.get(kind) is not True:
            raise ValueError("Jira 配置路径包含链接或不可读取。")
    value = await client.request("fs/readFile", {"path": str(path)})
    encoded = value.get("dataBase64") if isinstance(value, dict) else None
    if not isinstance(encoded, str) or len(encoded) > ((FILE_LIMIT + 2) // 3) * 4:
        raise ValueError("Jira 配置文件过大或读取响应无效。")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ValueError("Jira 配置文件响应无效。") from None


async def powershell(client, code, cwd, *, cap=4096):
    result = await client.request("command/exec", {
        "command": [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(code.encode("utf-16-le")).decode()],
        "cwd": cwd, "sandboxPolicy": {"type": "dangerFullAccess"},
        "timeoutMs": 30000, "outputBytesCap": cap,
    })
    if not isinstance(result, dict) or type(result.get("exitCode")) is not int or result["exitCode"] != 0:
        raise RuntimeError("Jira CLI 或运行账号准备失败，请检查远程 CLI、目录权限及网络；未自动重试命令。")
    output = result.get("stdout")
    if not isinstance(output, str):
        raise RuntimeError("Jira CLI 返回格式不兼容。")
    return output.strip()


# 仅删除指定运行的凭据文件；不删除业务目录，不递归清理任意文件。
DIRECTORY_SCRIPT = r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Check-Path([string]$path) {
  $item=Get-Item -LiteralPath $path -Force
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'link' }
  if ($item.PSIsContainer -and $item.Parent) { Check-Path $item.Parent.FullName }
}
$base=[Environment]::GetFolderPath('LocalApplicationData')
Check-Path $base
$root=Join-Path $base 'CodexFlowJira'
$target=Join-Path $root $runId
"""

INITIALIZE_SCRIPT = r"""
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
foreach ($path in @($root,$target)) {
  $created=!(Test-Path -LiteralPath $path)
  if ($created) { New-Item -ItemType Directory -Path $path | Out-Null }
  Check-Path $path
  $acl=Get-Acl -LiteralPath $path
  if ($created) { $acl.SetOwner($sid) }
  elseif ($acl.GetOwner([Security.Principal.SecurityIdentifier]) -ne $sid) { throw 'owner' }
  $acl.SetAccessRuleProtection($true,$false)
  foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
  $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
  Set-Acl -LiteralPath $path -AclObject $acl
}
foreach ($file in @('account.json','jira.ps1')) {
  $path=Join-Path $target $file
  if (Test-Path -LiteralPath $path) { Check-Path $path }
}
$cli=Get-Command jira_cli -CommandType Application -ErrorAction Stop
if (!$cli.Path) { throw 'cli' }
Write-Output $target
"""

LAUNCHER = r"""$JiraArgs=@($args)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
try {
  $account=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'account.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($arg in $JiraArgs) {
    if ($arg -match '^--(token|username|password|user|base-url)(=|$)') { throw 'auth override' }
  }
  $offset=0
  while ($offset -lt $JiraArgs.Count) {
    if ($JiraArgs[$offset] -in @('--output','--lang')) {
      if ($offset+1 -ge $JiraArgs.Count) { throw 'missing option' }
      $offset+=2
    } elseif ($JiraArgs[$offset] -match '^--(output|lang)=') { $offset++ }
    else { break }
  }
  if ($offset -ge $JiraArgs.Count -or $JiraArgs[$offset] -notin @('issue','attachment','field','user')) { throw 'command' }
  if ($offset+1 -ge $JiraArgs.Count) { throw 'subcommand' }
  $pair=$JiraArgs[$offset]+' '+$JiraArgs[$offset+1]
  if ($pair -eq 'issue search') { throw 'only assigned issue' }
  if ($account.readOnly) {
    if ($pair -notin @('issue get','issue transitions','issue transition-meta','attachment list','attachment download','field list','user get','user search')) { throw 'read only' }
  }
  $output=& jira_cli --base-url $account.baseUrl --token $account.token @JiraArgs 2>&1
  $code=$LASTEXITCODE
  foreach ($line in $output) { Write-Output ($line.ToString().Replace($account.token,'[已隐藏凭据]')) }
  exit $code
} catch {
  Write-Output 'Jira 操作失败，请检查本轮账号、权限和命令参数；未自动重发。'
  exit 1
}
"""


def client_for(orchestrator, agent):
    if not agent.enabled or not agent.allow_full_access:
        raise PermissionError("Jira CLI 固定命令需要机器已启用并授权完全访问。")
    if not agent.cwd or not PureWindowsPath(agent.cwd).is_absolute():
        raise ValueError("当前 Jira CLI 适配需要 Windows 机器及其已登记工作目录。")
    return orchestrator._client_factory(agent.url, token=orchestrator._resolve_agent_token(agent), request_timeout_sec=40)


async def claim_next(store, orchestrator, workflow_id):
    context = await asyncio.to_thread(store.jira_context, workflow_id)
    if context["state"] in {"ready", "no_task"}:
        return {"issueKey": context["issueKey"], "state": context["state"]}
    supervisor = orchestrator.get_agent(context["supervisorId"])
    executor = orchestrator.get_agent(context["executorId"])
    if not executor.allow_write:
        raise PermissionError("执行机未授权写入，无法接收本轮 Jira 账号。")
    upload_pending = False
    launcher_path = None
    try:
        async with client_for(orchestrator, supervisor) as client:
            raw_config = await read_file(client, CONFIG_PATH)
            alias, account = account_from_config(raw_config, context["config"]["account"])
            fingerprint = hashlib.sha256(json.dumps([alias, account], sort_keys=True).encode()).hexdigest()
            excluded = []
            while True:
                if context["issueKey"]:
                    keys = [context["issueKey"]]
                else:
                    query = excluding_query(context["config"]["jql"], excluded)
                    data = base64.b64encode(json.dumps([alias, hashlib.sha256(raw_config).hexdigest(), "--output", "json", "issue", "search", query]).encode()).decode()
                    code = ("$ErrorActionPreference='Stop'\ntry {\n"
                            "$a=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + data + "')) | ConvertFrom-Json\n"
                            "$bytes=[IO.File]::ReadAllBytes('C:\\Users\\admin\\.jira_cli\\config.json')\n"
                            "$sha=[Security.Cryptography.SHA256]::Create()\n"
                            "$digest=-join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })\n"
                            "$sha.Dispose()\nif ($digest -ne $a[1]) { throw 'changed' }\n"
                            "$cfg=[Text.Encoding]::UTF8.GetString($bytes).TrimStart([char]0xFEFF) | ConvertFrom-Json\n"
                            "$account=$cfg.accounts.PSObject.Properties[$a[0]].Value\n"
                            "$queryArgs=@($a | Select-Object -Skip 2)\n"
                            "& jira_cli --base-url $account.baseUrl --token $account.token @queryArgs 2>$null\n"
                            "exit $LASTEXITCODE\n} catch { exit 1 }")
                    keys = issue_keys(await powershell(client, code, supervisor.cwd, cap=1024 * 1024))
                result = await asyncio.to_thread(store.jira_update, workflow_id, "claim", {
                    "server": account["baseUrl"], "account": alias, "fingerprint": fingerprint, "candidates": keys})
                if result["state"] != "contended":
                    break
                unseen = [key for key in keys if key not in excluded]
                if not unseen or len(excluded) + len(unseen) > 1000:
                    raise ValueError("Jira 查询候选过多或分页未前进，请缩小 SOP 查询条件；本轮未派发步骤。")
                excluded.extend(unseen)
        if result["state"] == "no_task":
            return result
        upload_pending = True
        async with client_for(orchestrator, executor) as client:
            code = "$runId='" + run_directory(workflow_id) + "'\n" + DIRECTORY_SCRIPT + "\ntry {\n" + INITIALIZE_SCRIPT + "\n} catch { exit 1 }"
            directory = PureWindowsPath(await powershell(client, code, executor.cwd))
            if not directory.is_absolute() or directory.name != run_directory(workflow_id) or directory.parent.name != "CodexFlowJira":
                raise ValueError("执行机返回的 Jira 运行目录无效。")
            launcher_path = str(directory / "jira.ps1")
            staged = await asyncio.to_thread(store.jira_update, workflow_id, "stage", {"launcherPath": launcher_path})
            if staged["state"] == "ready":
                upload_pending = False
                return staged
            files = {"account.json": json.dumps(account, ensure_ascii=False).encode(), "jira.ps1": b"\xef\xbb\xbf" + LAUNCHER.encode()}
            for name, content in files.items():
                path = directory / name
                await client.request("fs/writeFile", {"path": str(path), "dataBase64": base64.b64encode(content).decode()})
                if await read_file(client, path) != content:
                    raise ValueError("执行机 Jira 账号下发回读校验失败。")
        result = await asyncio.to_thread(store.jira_update, workflow_id, "ready", {"launcherPath": str(directory / "jira.ps1")})
        upload_pending = False
        return result
    except (ValueError, PermissionError):
        raise
    except Exception:
        # 远程 RPC / CLI 异常可包含文件正文或命令参数，禁止穿透到工具结果与日志。
        raise RuntimeError("监督机 Jira 领取或账号下发失败，请检查 CLI、配置、执行机连接和授权；未启动业务步骤。") from None
    finally:
        if upload_pending:
            try:
                await cleanup(orchestrator, context["executorId"], workflow_id, launcher_path)
            except Exception as error:
                LOGGER.warning("下发未完成的 Jira 账号清理待核实，错误类型=%s。", type(error).__name__)


async def cleanup(orchestrator, agent_id, workflow_id, launcher_path=None):
    agent = orchestrator.get_agent(agent_id)
    async with client_for(orchestrator, agent) as client:
        expected = ("$expected=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
                    + base64.b64encode(str(PureWindowsPath(launcher_path).parent).encode()).decode()
                    + "'))\nif ($target -ne $expected) { exit 1 }\n") if launcher_path else ""
        code = ("$runId='" + run_directory(workflow_id) + "'\n" + DIRECTORY_SCRIPT + expected + r"""
try {
  if (Test-Path -LiteralPath $target) {
    Check-Path $target
    $path=Join-Path $target 'account.json'
    if (Test-Path -LiteralPath $path) { Check-Path $path; Remove-Item -LiteralPath $path -Force }
  }
  Write-Output 'CLEARED'
} catch { exit 1 }
""")
        if await powershell(client, code, agent.cwd) != "CLEARED":
            raise RuntimeError("本轮 Jira 账号清理未确认。")
