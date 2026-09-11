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

$requiredFiles = @(
    @{ Name = "Python executable"; Path = $pythonExe },
    @{ Name = "workflow gateway"; Path = $gatewayScript }
)

foreach ($requiredFile in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $requiredFile.Path -PathType Leaf)) {
        throw "$($requiredFile.Name) not found: $($requiredFile.Path)"
    }
}

$arguments = @($gatewayScript, "--host", $ListenHost, "--port", [string]$Port)

Write-Host "Starting workflow gateway at http://${ListenHost}:$Port"
& $pythonExe @arguments

exit $LASTEXITCODE
