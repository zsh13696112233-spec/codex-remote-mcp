[CmdletBinding()]
param(
    [string]$ListenHost = "0.0.0.0",
    [ValidateRange(1, 65535)]
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$gatewayScript = Join-Path $projectRoot "services\python-workflow\src\workflow_gateway.py"
$workflowDb = Join-Path $projectRoot "workflows.db"
$agentsFile = Join-Path $projectRoot "config\agents.json"

$requiredFiles = @(
    @{ Name = "Python executable"; Path = $pythonExe },
    @{ Name = "workflow gateway"; Path = $gatewayScript },
    @{ Name = "agent configuration"; Path = $agentsFile }
)

foreach ($requiredFile in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $requiredFile.Path -PathType Leaf)) {
        throw "$($requiredFile.Name) not found: $($requiredFile.Path)"
    }
}

$env:CODEX_WORKFLOW_DB = $workflowDb
$env:CODEX_AGENTS_FILE = $agentsFile

Write-Host "Starting workflow gateway at http://${ListenHost}:$Port"
Write-Host "Workflow database: $workflowDb"
Write-Host "Agent configuration: $agentsFile"

& $pythonExe $gatewayScript `
    --host $ListenHost `
    --port $Port `
    --db $workflowDb `
    --agents $agentsFile

exit $LASTEXITCODE
