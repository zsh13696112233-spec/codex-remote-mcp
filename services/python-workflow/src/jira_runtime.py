"""通过监督机已有 Jira CLI 领取；执行机使用本地账号，凭据不进入模型上下文。"""

import asyncio
import base64
import hashlib
import json
import re
from pathlib import PureWindowsPath

from workflow_jira import ISSUE_KEY, run_directory, server_key

CONFIG_PATH = PureWindowsPath(r"C:\Users\admin\.jira_cli\config.json")
POWERSHELL = r"C:\Program Files\PowerShell\7\pwsh.exe"
FILE_LIMIT = 64 * 1024


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
        if not isinstance(account, dict):
            raise ValueError()
        for field in ("token", "username", "password"):
            value = account.get(field)
            if value is not None and value != "" and not isinstance(value, str):
                raise ValueError()
        token = account.get("token")
        username, password = account.get("username"), account.get("password")
        # 空字段视为未配置；有效 token 与 Basic Auth 必须二选一。
        if token:
            if username or password:
                raise ValueError()
            credentials = {"token": token}
        else:
            credentials = {"username": username, "password": password}
        if any(not isinstance(value, str) or not 1 <= len(value) <= 8192
               or any(ord(c) < 32 for c in value) for value in credentials.values()):
            raise ValueError()
        if (not isinstance(alias, str) or not 1 <= len(alias) <= 128 or any(ord(c) < 32 for c in alias)
                or type(account["readOnly"]) is not bool):
            raise ValueError()
        return alias, {"baseUrl": server_key(account["baseUrl"]), **credentials,
                       "readOnly": account["readOnly"]}
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("监督机 Jira 配置无效、账号不存在或存在多个账号；请检查 SOP 账号别名，并仅配置 token 或完整的用户名密码。") from None


def issue_keys(output):
    try:
        value = json.loads(output)
        if isinstance(value, dict) and "items" in value:
            rows = value["items"]
            # 每次排除已查编号后从首页查询；不能将非首页或不完整的空页当作无任务。
            if (type(value["total"]) is not int or value["total"] < 0
                    or type(value["start_at"]) is not int or value["start_at"] != 0
                    or type(value["returned"]) is not int or value["returned"] != len(rows)
                    or value["total"] < len(rows) or (not rows and value["total"] != 0)):
                raise ValueError()
        else:
            rows = value["issues"] if isinstance(value, dict) else value
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError()
        keys = [row["key"] for row in rows]
        if any(not isinstance(key, str) or not ISSUE_KEY.fullmatch(key) for key in keys):
            raise ValueError()
        return list(dict.fromkeys(keys))
    except (ValueError, TypeError, KeyError):
        raise ValueError("Jira CLI 搜索输出不是有效的工单数组、issues 数组或 items 首页，或超过 1000 项；请缩小查询范围并检查 CLI JSON 输出。") from None


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


def including_query(jql, key):
    return excluding_query(jql, [key]).replace('AND key NOT IN (' + key + ')', 'AND key IN (' + key + ')')


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


# 只更新当前子进程，保留服务额外 PATH；不修改用户或机器环境变量。
PROCESS_ENVIRONMENT = r"""
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
$env:PATH=[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')+';'+$env:PATH
"""

# 输入来自已校验的运行账号；旧的 token 专属账号文件仍可使用。
AUTH_ARGUMENTS = r"""
$authArgs=@('--base-url',$account.baseUrl)
if ($account.PSObject.Properties['accountAlias']) {
  $authArgs+=@('--user',$account.accountAlias)
}
if ($account.PSObject.Properties['token'] -and $account.token) {
  $authArgs+=@('--token',$account.token)
} else {
  $authArgs+=@('--username',$account.username,'--password',$account.password)
}
"""


class JiraCommandError(RuntimeError):
    """命令已返回退出码；失败不等于仍在远程运行。"""


async def powershell(client, code, cwd, *, cap=4096):
    result = await client.request("command/exec", {
        "command": [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode((PROCESS_ENVIRONMENT + code).encode("utf-16-le")).decode()],
        "cwd": cwd, "sandboxPolicy": {"type": "dangerFullAccess"},
        "timeoutMs": 30000, "outputBytesCap": cap,
    })
    if isinstance(result, dict) and type(result.get("exitCode")) is int and result["exitCode"] not in {0, 124, 125}:
        raise JiraCommandError("Jira 固定命令执行失败，请检查账号、CLI 及网络。")
    if not isinstance(result, dict) or type(result.get("exitCode")) is not int or result["exitCode"] in {124, 125}:
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
# 使用当前 PowerShell 7 自带模块，避免继承其他版本的模块搜索路径。
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
foreach ($path in @($root,$target)) {
  $created=!(Test-Path -LiteralPath $path)
  if ($created) { New-Item -ItemType Directory -Path $path | Out-Null }
  Check-Path $path
  $existingAcl=Get-Acl -LiteralPath $path
  if (!$created -and $existingAcl.GetOwner([Security.Principal.SecurityIdentifier]) -ne $sid) { throw 'owner' }
  # 仅写入所有者和访问规则，不带入审计 ACL，避免要求 SeSecurityPrivilege。
  $acl=[Security.AccessControl.DirectorySecurity]::new()
  if ($created) { $acl.SetOwner($sid) }
  $acl.SetAccessRuleProtection($true,$false)
  $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
  [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($path),$acl)
}
foreach ($file in @('account.json','jira.ps1')) {
  $path=Join-Path $target $file
  if (Test-Path -LiteralPath $path) { Check-Path $path }
}
$cli=Get-Command jira_cli -CommandType Application -ErrorAction Stop
if (!$cli.Path) { throw 'cli' }
Write-Output $target
"""

LOCAL_ACCOUNT = r"""  $config=Get-Content -LiteralPath (Join-Path $env:USERPROFILE '.jira_cli\config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $account=$config.accounts.PSObject.Properties[$accountAlias].Value
  if ($null -eq $account -or $account.readOnly -isnot [bool]) { throw 'local account' }
"""


LAUNCHER = PROCESS_ENVIRONMENT + r"""$JiraArgs=@($args)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
try {
""" + LOCAL_ACCOUNT + r"""  foreach ($arg in $JiraArgs) {
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
  if ($supervisorReadOnly -or $account.readOnly) {
    if ($pair -notin @('issue get','issue transitions','issue transition-meta','attachment list','attachment download','field list','user get','user search')) { throw 'read only' }
  }

  $authArgs=@('--user',$accountAlias)
  $secrets=@()
  foreach ($field in @('token','password')) {
    if ($account.PSObject.Properties[$field] -and $account.$field) { $secrets+=@($account.$field) }
  }
  if ($account.PSObject.Properties['username'] -and $account.username) {
    $secrets+=@([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($account.username+':'+$account.password)))
  }
  $secrets=@($secrets | Sort-Object -Property Length -Descending)
  $output=& jira_cli @authArgs @JiraArgs 2>&1
  $code=$LASTEXITCODE
  foreach ($line in $output) {
    $text=$line.ToString()
    foreach ($secret in $secrets) { $text=$text.Replace($secret,'[已隐藏凭据]') }
    Write-Output $text
  }
  exit $code
} catch {
  Write-Output 'Jira 操作失败，请检查本轮账号、权限和命令参数；未自动重发。'
  exit 1
}
"""


def local_launcher(alias, read_only):
    # 别名作为 PowerShell 单引号字面量写入，不携带认证参数。
    return ("$accountAlias='" + alias.replace("'", "''") + "'\n"
            + "$supervisorReadOnly=$" + ("true" if read_only else "false") + "\n" + LAUNCHER)


def client_for(orchestrator, agent):
    if not agent.enabled or not agent.allow_full_access:
        raise PermissionError("Jira CLI 固定命令需要机器已启用并授权完全访问。")
    if not agent.cwd or not PureWindowsPath(agent.cwd).is_absolute():
        raise ValueError("当前 Jira CLI 适配需要 Windows 机器及其已登记工作目录。")
    return orchestrator._client_factory(agent.url, token=orchestrator._resolve_agent_token(agent), request_timeout_sec=40)


def account_script(raw, alias):
    data = base64.b64encode(json.dumps([alias, hashlib.sha256(raw).hexdigest()]).encode()).decode()
    return ("$ErrorActionPreference='Stop'\ntry {\n"
            "$a=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + data + "')) | ConvertFrom-Json\n"
            "$bytes=[IO.File]::ReadAllBytes('C:\\Users\\admin\\.jira_cli\\config.json')\n"
            "$sha=[Security.Cryptography.SHA256]::Create()\n"
            "$digest=-join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })\n"
            "$sha.Dispose()\nif ($digest -ne $a[1]) { throw 'changed' }\n"
            "$cfg=[Text.Encoding]::UTF8.GetString($bytes).TrimStart([char]0xFEFF) | ConvertFrom-Json\n"
            "$account=$cfg.accounts.PSObject.Properties[$a[0]].Value\n")


async def search_page(client, raw, alias, jql, cwd, *, people=None):
    query = base64.b64encode(jql.encode()).decode()
    code = (account_script(raw, alias) + AUTH_ARGUMENTS + "\n"
            "$query=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + query + "'))\n"
            "& jira_cli @authArgs --output json issue search $query 2>$null\n"
            "exit $LASTEXITCODE\n} catch { exit 1 }")
    output = await powershell(client, code, cwd, cap=1024 * 1024)
    keys = issue_keys(output)
    value = json.loads(output)
    rows = value.get('items', value.get('issues', [])) if isinstance(value, dict) else value
    if people is not None:
        for row in rows:
            people[row['key']] = row.get('assignee', row.get('fields', {}).get('assignee'))
    return {key: next((str(r.get('summary') or r.get('fields', {}).get('summary') or '')[:1000]
                      for r in rows if r['key'] == key), '') for key in keys}


async def account_identity(client, raw, alias, cwd):
    # 查询在监督机执行；中央不连接 Jira，也不持久化认证内容。
    code = account_script(raw, alias) + r"""
$headers=@{}
if ($account.token) { $headers.Authorization='Bearer '+$account.token }
else { $headers.Authorization='Basic '+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($account.username+':'+$account.password)) }
$user=Invoke-RestMethod -Uri ($account.baseUrl.TrimEnd('/')+'/rest/api/2/myself') -Headers $headers -TimeoutSec 20
$id=$user.accountId
if (-not $id) { $id=$user.key }
if (-not $id) { $id=$user.name }
if (-not $id) { exit 1 }
[Console]::Out.WriteLine($id)
} catch { exit 1 }
"""
    identity = await powershell(client, code, cwd)
    if not identity or len(identity) > 256 or any(ord(c) < 32 for c in identity):
        raise ValueError('无法确认 Jira 账号身份。')
    return identity


async def claim_next(store, orchestrator, workflow_id):
    context = await asyncio.to_thread(store.jira_context, workflow_id)
    if context["state"] in {"ready", "no_task"}:
        return {"issueKey": context["issueKey"], "state": context["state"]}
    executor = orchestrator.get_agent(context["executorId"])
    if not executor.allow_write:
        raise PermissionError("执行机未授权写入，无法准备 Jira 入口。")
    binding = context.get('reservation')
    if not binding or context['state'] != 'claimed' or not context['config'].get('connectionId'):
        raise ValueError('中央尚未确认固定工单，请使用已绑定账号的最新配置运行。')
    alias = binding['accountAlias']
    expected = base64.b64encode(json.dumps([binding['server'], binding['accountId']]).encode()).decode()
    verification = """
$expected=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('""" + expected + """')) | ConvertFrom-Json
if ($account.baseUrl.TrimEnd('/') -ne $expected[0]) { throw 'server mismatch' }
$headers=@{}
if ($account.PSObject.Properties['token'] -and $account.token) { $headers.Authorization='Bearer '+$account.token }
else { $headers.Authorization='Basic '+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($account.username+':'+$account.password)) }
$user=Invoke-RestMethod -Uri ($account.baseUrl.TrimEnd('/')+'/rest/api/2/myself') -Headers $headers -TimeoutSec 20 -MaximumRedirection 0
$id=$null
foreach ($field in @('accountId','key','name')) { if ($user.PSObject.Properties[$field] -and $user.$field) { $id=$user.$field; break } }
if ($id -cne $expected[1]) { throw 'identity mismatch' }
"""
    try:
        async with client_for(orchestrator, executor) as client:
            code = ("$runId='" + run_directory(workflow_id) + "'\n" + DIRECTORY_SCRIPT
                    + "\ntry {\n$accountAlias='" + alias.replace("'", "''") + "'\n"
                    + LOCAL_ACCOUNT + verification + INITIALIZE_SCRIPT + "\n} catch { exit 1 }")
            directory = PureWindowsPath(await powershell(client, code, executor.cwd))
            if not directory.is_absolute() or directory.name != run_directory(workflow_id) or directory.parent.name != "CodexFlowJira":
                raise ValueError("执行机返回的 Jira 运行目录无效。")
            launcher_path = str(directory / "jira.ps1")
            staged = await asyncio.to_thread(store.jira_update, workflow_id, "stage", {"launcherPath": launcher_path, "accountSource": "local"})
            if staged["state"] == "ready":
                return staged
            content = b"\xef\xbb\xbf" + local_launcher(alias, False).encode()
            path = directory / "jira.ps1"
            await client.request("fs/writeFile", {"path": str(path), "dataBase64": base64.b64encode(content).decode()})
            if await read_file(client, path) != content:
                raise ValueError("执行机 Jira 入口下发回读校验失败。")
        result = await asyncio.to_thread(store.jira_update, workflow_id, "ready", {"launcherPath": str(directory / "jira.ps1")})
        return result
    except (ValueError, PermissionError):
        raise
    except Exception:
        # 远程 RPC / CLI 异常可包含文件正文或命令参数，禁止穿透到工具结果与日志。
        raise RuntimeError("执行机 Jira 入口准备失败，请核对本地账号服务地址、实际身份、CLI、连接和授权；未启动业务步骤。") from None


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


async def collect_people(client, raw, alias, cwd, assignees):
    """展示查询只降级已确认结束的失败；超时或断连继续交给采集占用保护。"""
    from workflow_store import utc_now
    import re

    async def query(arguments):
        code = (account_script(raw, alias) + AUTH_ARGUMENTS + "\n"
                + "& jira_cli @authArgs --output json " + arguments + " 2>$null\n"
                + "exit $LASTEXITCODE\n} catch { exit 1 }")
        return json.loads(await powershell(client, code, cwd, cap=1024 * 1024))

    field = None
    try:
        fields = await query('field list --custom-only')
        if isinstance(fields, list):
            matches = [f.get('id') for f in fields if isinstance(f, dict) and f.get('name') == '开发人']
            if len(matches) == 1 and isinstance(matches[0], str) and re.fullmatch(r'customfield_[0-9]+', matches[0]):
                field = matches[0]
    except (JiraCommandError, json.JSONDecodeError):
        pass
    result = {}
    for key, assignee in assignees.items():
        names, status = [], 'unavailable'
        if field:
            try:
                value = await query("issue get " + key + " --fields '" + field + "'")
                fields = value.get('fields') if isinstance(value, dict) else None
                if isinstance(fields, dict) and field in fields:
                    value = fields[field]
                    users = [] if value is None else value if isinstance(value, list) else [value]
                    if len(users) <= 100 and all(isinstance(u, dict) and isinstance(u.get('displayName'), str)
                                              and 0 < len(u['displayName'].strip()) <= 160 for u in users):
                        names = list(dict.fromkeys(u['displayName'].strip() for u in users))
                        status = 'resolved' if names else 'empty'
            except (JiraCommandError, json.JSONDecodeError):
                pass
        if isinstance(assignee, dict):
            assignee = assignee.get('displayName')
        name = assignee.strip() if isinstance(assignee, str) and len(assignee) <= 160 else ''
        result[key] = {'developers': names, 'developerStatus': status, 'assignee': name,
                       'assigneeStatus': 'resolved' if name else 'unavailable', 'collectedAt': utc_now()}
    return result
