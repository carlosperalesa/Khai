[CmdletBinding()]
param(
    [switch]$UpgradePip
)

$ErrorActionPreference = 'Stop'

$pythonCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
    (Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
    (Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
)
$pythonPath = $pythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) -and ($_ -notlike '*\\WindowsApps\\*') } | Select-Object -First 1
if (-not $pythonPath) {
    throw 'Python 3 was not found. Install Python 3.10+ and enable the launcher, then rerun this script.'
}

$khaiRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$venvDir = Join-Path $khaiRoot '.venv'
if (-not (Test-Path -LiteralPath $venvDir)) {
    & $pythonPath -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv.' }
}

$venvPython = Join-Path $venvDir 'Scripts\python.exe'
if ($UpgradePip) {
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Could not upgrade pip.' }
}

& $venvPython -m pip install azure-cli
if ($LASTEXITCODE -ne 0) { throw 'Could not install Azure CLI in .venv.' }

Write-Host 'Local dependencies installed.'
Write-Host 'Azure CLI: .\.venv\Scripts\az.bat'
Write-Host 'SSH:      Windows OpenSSH (ssh.exe)'
Write-Host 'Run:      .\.venv\Scripts\az.cmd login'

