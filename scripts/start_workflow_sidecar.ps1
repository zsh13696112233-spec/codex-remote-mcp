[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateLength(1, 128)]
    [string]$AgentId,
    [Parameter(Mandatory = $true)]
    [string]$GatewayUrl,
    [string]$TokenEnv = "",
    [string]$TokenFile = "",
    [string]$AgentsFile = "",
    [string]$ListenHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8082
)

$ErrorActionPreference = "Stop"

if (($TokenEnv.Length -gt 0) -eq ($TokenFile.Length -gt 0)) {
    throw "Specify exactly one of -TokenEnv or -TokenFile."
}
if ($ListenHost -notin @("127.0.0.1", "::1", "localhost")) {
    throw "Workflow Sidecar may only listen on a loopback address."
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$sidecarScript = Join-Path $projectRoot "services\python-workflow\src\workflow_sidecar.py"
if (-not $AgentsFile) {
    $AgentsFile = Join-Path $projectRoot "config\agents.json"
}
$resolvedAgentsFile = (Resolve-Path -LiteralPath $AgentsFile).Path

foreach ($requiredFile in @($pythonExe, $sidecarScript, $resolvedAgentsFile)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required file not found: $requiredFile"
    }
}

$arguments = @(
    $sidecarScript,
    "--host", $ListenHost,
    "--port", $Port,
    "--agent-id", $AgentId,
    "--gateway-url", $GatewayUrl,
    "--agents", $resolvedAgentsFile
)
if ($TokenEnv) {
    $arguments += @("--token-env", $TokenEnv)
} else {
    $resolvedTokenFile = (Resolve-Path -LiteralPath $TokenFile).Path
    $arguments += @("--token-file", $resolvedTokenFile)
}

Write-Host "Starting workflow Sidecar on ${ListenHost}:$Port for agent $AgentId"
Write-Host "Agent configuration: $resolvedAgentsFile"

& $pythonExe @arguments
exit $LASTEXITCODE
