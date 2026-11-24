[CmdletBinding()]
param(
    [string]$PythonPath = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
$agentRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$agentPython = (Resolve-Path -LiteralPath $PythonPath).Path
$serviceModule = Join-Path $agentRoot 'dq_print_agent\windows_service.py'
if (-not (Test-Path -LiteralPath $serviceModule -PathType Leaf)) { throw 'Agent service module not found.' }
if ($Stop) {
    & $agentPython $serviceModule stop
    if ($LASTEXITCODE -ne 0) { throw 'Service stop failed; removal was not attempted.' }
}
& $agentPython $serviceModule remove
if ($LASTEXITCODE -ne 0) { throw 'Service removal failed. No local files were removed.' }
# Deliberately retain all configuration, SQLite evidence, label files and logs.
