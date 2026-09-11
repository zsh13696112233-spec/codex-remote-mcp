[CmdletBinding()]
param(
    [string]$AgentId = "",
    [string]$GatewayUrl = "",
    [string]$TokenEnv = "",
    [string]$TokenFile = "",
    [string]$ListenHost = "",
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$sidecarScript = Join-Path $projectRoot "services\python-workflow\src\workflow_sidecar.py"
foreach ($requiredFile in @($pythonExe, $sidecarScript)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required file not found: $requiredFile"
    }
}

# Python 统一读取 config/workflow-service.json 并校验参数。
# 只传递显式指定的启动参数，不用脚本默认值覆盖文件。
$arguments = @($sidecarScript)
$optionNames = @{
    AgentId = "--agent-id"; GatewayUrl = "--gateway-url"
    TokenEnv = "--token-env"; TokenFile = "--token-file"
    ListenHost = "--host"; Port = "--port"
}
foreach ($entry in $PSBoundParameters.GetEnumerator()) {
    if ($optionNames.ContainsKey($entry.Key)) {
        $arguments += @($optionNames[$entry.Key], [string]$entry.Value)
    }
}

Write-Host "Starting workflow Sidecar with service configuration."
& $pythonExe @arguments
exit $LASTEXITCODE
