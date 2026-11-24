[CmdletBinding()]
param(
    [string]$PythonPath = (Join-Path $PSScriptRoot '.build-venv\Scripts\python.exe'),
    [string]$IsccPath,
    [switch]$BundleOnly
)
$ErrorActionPreference = 'Stop'
$agentRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$buildPython = (Resolve-Path -LiteralPath $PythonPath).Path

function Assert-WorkspacePath([string]$Value) {
    $full = [IO.Path]::GetFullPath($Value)
    if (-not $full.StartsWith($agentRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build output must stay inside the print-agent workspace: $full"
    }
    $cursor = $full
    while ($cursor.Length -gt $agentRoot.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if (((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Build paths must not traverse a junction or symbolic link: $cursor"
            }
        }
        $cursor = Split-Path -Parent $cursor
    }
    return $full
}

function Assert-NoRuntimeFiles([string]$Directory) {
    if (-not (Test-Path -LiteralPath $Directory)) { return }
    $unexpected = @(Get-ChildItem -LiteralPath $Directory -File -Recurse | Where-Object {
        $_.Name -match '^(config\.json|journal.*\.sqlite3(?:-wal|-shm)?|agent\.log.*|credential\.dpapi|host-identity\.json|host-origin\.json|desktop-identity\.json|runtime-status\.json|multi-upgrade\.json|last-service-check\.json|pending-printer-mutation\.json)$' -or
        $_.Extension -in @('.sqlite', '.sqlite3', '.dpapi')
    })
    if ($unexpected.Count -gt 0) { throw 'The bundle contains runtime files. Stop and preserve them before rebuilding.' }
}

$buildRoot = Assert-WorkspacePath (Join-Path $agentRoot 'build\desktop')
$distRoot = Assert-WorkspacePath (Join-Path $agentRoot 'dist')
$outputRoot = Assert-WorkspacePath (Join-Path $agentRoot 'output')
$assetsRoot = Assert-WorkspacePath (Join-Path $agentRoot 'build\desktop-assets')
$bundleRoot = Assert-WorkspacePath (Join-Path $distRoot 'DeqinPrintAgent')
if (-not (Test-Path -LiteralPath (Join-Path $agentRoot 'desktop_launcher.py') -PathType Leaf)) {
    throw 'desktop_launcher.py is missing.'
}
& $buildPython -c 'import sys; assert sys.platform == "win32"; assert sys.maxsize > 2**32; import PyInstaller, tkinter, win32service, servicemanager'
if ($LASTEXITCODE -ne 0) { throw 'Use 64-bit Windows Python with requirements-build.txt installed.' }

Push-Location -LiteralPath $agentRoot
try {
    $version = (& $buildPython (Join-Path $agentRoot 'packaging\create_assets.py')).Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version asset generation failed.' }
    # PyInstaller --noconfirm replaces an existing generated bundle. Refuse if
    # someone has subsequently placed machine credentials or a journal there.
    Assert-NoRuntimeFiles $bundleRoot
    & $buildPython -m PyInstaller --noconfirm --clean --workpath $buildRoot --distpath $distRoot (Join-Path $agentRoot 'packaging\DeqinPrintAgent.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

    # Fail packaging if a future hook accidentally collects local credentials or execution evidence.
    Assert-NoRuntimeFiles $bundleRoot
    if ($BundleOnly) { Write-Output "Bundle ready: $bundleRoot"; return }

    if (-not $IsccPath) {
        $candidates = @(
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
        )
        $IsccPath = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    }
    if (-not $IsccPath) { throw 'Pass -IsccPath with an existing Inno Setup 6 compiler; the bundle is ready.' }
    $compiler = (Resolve-Path -LiteralPath $IsccPath).Path
    & $compiler "/DAppVersion=$version" "/DBundleDir=$bundleRoot" "/DOutputPath=$outputRoot" "/DAssetsDir=$assetsRoot" (Join-Path $agentRoot 'packaging\installer.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
    $installer = Join-Path $outputRoot "DeqinPrintAgent-Setup-$version.exe"
    $digest = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText($installer + '.sha256', $digest + '  ' + [IO.Path]::GetFileName($installer) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    Write-Output "Installer ready: $installer"
    Write-Output "SHA256: $digest"
    Write-Output 'No installer, Windows service, or physical printer was started.'
}
finally {
    Pop-Location
}
