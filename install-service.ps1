[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [string]$PythonPath = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
$agentRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$agentPython = (Resolve-Path -LiteralPath $PythonPath).Path
$agentConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$serviceModule = Join-Path $agentRoot 'dq_print_agent\windows_service.py'
if (-not (Test-Path -LiteralPath $serviceModule -PathType Leaf)) { throw 'Agent service module not found.' }
if (-not (Test-Path -LiteralPath $agentConfig -PathType Leaf)) { throw 'ConfigPath must identify a file.' }
& $agentPython $serviceModule install --config $agentConfig
if ($LASTEXITCODE -ne 0) { throw 'Service installation failed. Inspect the existing service before retrying.' }
if ($Start) {
    & $agentPython $serviceModule start
    if ($LASTEXITCODE -ne 0) { throw 'Service start failed. Local task evidence has been preserved.' }
}
