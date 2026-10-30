[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'nano', 'low', 'mid', 'high', 'on', 'on-low', 'on-mid', 'on-high', 'off', 'status')]
    [string]$Command = 'help',

    [string]$Project = 'gen-lang-client-0149640270',
    [string]$Zone = 'us-west1-a',
    [string]$Instance = 'dolphin-node',
    [string]$Disk = 'dolphin-boot-disk'
)

$ErrorActionPreference = 'Stop'
$DolphinDir = Split-Path -Parent $PSScriptRoot
$KhaiRoot = Split-Path -Parent $DolphinDir
$env:CLOUDSDK_PYTHON = Join-Path $KhaiRoot '.venv\Scripts\python.exe'
$env:CLOUDSDK_CONFIG = Join-Path $DolphinDir '.gcloud'
$GcloudCmd = Join-Path $DolphinDir '.tools\google-cloud-sdk\bin\gcloud.cmd'
if (-not (Test-Path -LiteralPath $GcloudCmd)) {
    $globalGcloud = Get-Command 'gcloud' -ErrorAction SilentlyContinue
    if (-not $globalGcloud) { throw 'gcloud CLI no encontrado en Dolphin/.tools ni en PATH.' }
    $GcloudCmd = $globalGcloud.Source
}

function Start-DolphinNode([string]$MachineType) {
    $existing = & $GcloudCmd compute instances list --project=$Project --filter="name=$Instance AND zone:$Zone" --format="value(machineType,status)"
    if ($existing) {
        $parts = $existing.Trim() -split '\s+'
        $currMt = $parts[0]
        $currStatus = $parts[-1]
        if ($currMt -eq $MachineType -and $currStatus -eq 'RUNNING') {
            Write-Host "Instancia $Instance ya está RUNNING con $MachineType."
            return
        }
        if ($currMt -eq $MachineType -and $currStatus -eq 'TERMINATED') {
            & $GcloudCmd compute instances start $Instance --project=$Project --zone=$Zone
            return
        }
        & $GcloudCmd compute instances delete $Instance --project=$Project --zone=$Zone --keep-disks=boot --quiet
    }
    & $GcloudCmd compute instances create $Instance `
        --project=$Project `
        --zone=$Zone `
        --machine-type=$MachineType `
        --provisioning-model=SPOT `
        --instance-termination-action=STOP `
        --maintenance-policy=TERMINATE `
        --disk="name=$Disk,boot=yes,auto-delete=no"
}

switch ($Command) {
    'help' {
        Write-Host 'Controlador Google Cloud Spot para Dolphin (disco único pd-standard 100 GB)'
        Write-Host '  make nano     -> Modo Gemini: elimina VM Spot conservando el disco de 100 GB'
        Write-Host '  make low      -> Modo Dolphin Low (CPU Spot e2-standard-8: dolphin3:8b)'
        Write-Host '  make mid      -> Modo Dolphin Mid (GPU NVIDIA L4 Spot g2-standard-4: dolphin3:8b)'
        Write-Host '  make high     -> Modo Dolphin High (GPU NVIDIA L4 Spot g2-standard-8: qwen2.5-32b-abliterated)'
        Write-Host '  make status   -> Muestra estado de la instancia y del disco en us-west1-a'
    }
    'low'     { Start-DolphinNode 'e2-standard-8' }
    'mid'     { Start-DolphinNode 'g2-standard-4' }
    'high'    { Start-DolphinNode 'g2-standard-8' }
    'on'      { Start-DolphinNode 'g2-standard-4' }
    'on-low'  { Start-DolphinNode 'e2-standard-8' }
    'on-mid'  { Start-DolphinNode 'g2-standard-4' }
    'on-high' { Start-DolphinNode 'g2-standard-8' }
    { $_ -in @('nano', 'off') } {
        $existing = & $GcloudCmd compute instances list --project=$Project --filter="name=$Instance AND zone:$Zone" --format="value(name)"
        if ($existing) {
            & $GcloudCmd compute instances delete $Instance --project=$Project --zone=$Zone --keep-disks=boot --quiet
        } else {
            Write-Host "[Mode = nano] Instancia $Instance ya está apagada/eliminada (disco $Disk en reposo)."
        }
    }
    'status' {
        Write-Host '=== INSTANCIA ==='
        & $GcloudCmd compute instances list --project=$Project --filter="name=$Instance"
        Write-Host '=== DISCO PERSISTENTE ==='
        & $GcloudCmd compute disks list --project=$Project --filter="name=$Disk"
    }
}

