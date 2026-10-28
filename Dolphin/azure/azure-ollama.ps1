[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'create', 'setup', 'on', 'off', 'status', 'tunnel', 'tunnel-stop', 'logs')]
    [string]$Command = 'help',

    [string]$SubscriptionId = $env:AZURE_SUBSCRIPTION_ID,
    [string]$ResourceGroup = $(if ($env:AZURE_RESOURCE_GROUP) { $env:AZURE_RESOURCE_GROUP } else { 'rg-ollama' }),
    [string]$VmName = $(if ($env:AZURE_VM_NAME) { $env:AZURE_VM_NAME } else { 'vm-dolphin' }),
    [string]$Location = $(if ($env:AZURE_LOCATION) { $env:AZURE_LOCATION } else { 'canadacentral' }),
    [string]$VmSize = $(if ($env:AZURE_VM_SIZE) { $env:AZURE_VM_SIZE } else { 'Standard_B8als_v2' }),
    [string]$VmUser = $(if ($env:AZURE_VM_USER) { $env:AZURE_VM_USER } else { $env:USERNAME }),
    [string]$Model = $(if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { 'dolphin3:8b' }),
    [int]$LocalPort = $(if ($env:OLLAMA_LOCAL_PORT) { $env:OLLAMA_LOCAL_PORT } else { 11434 }),
    [string]$SshKeyPath = $(if ($env:AZURE_SSH_KEY_PATH) { $env:AZURE_SSH_KEY_PATH } else { "$HOME\.ssh\id_rsa" })
)

$ErrorActionPreference = 'Stop'
$TunnelMarker = "azure-ollama-$LocalPort"
$AzCommand = $null

function Resolve-AzCommand {
    foreach ($localName in @('az.bat', 'az.cmd', 'az.exe')) {
        $localAz = Join-Path $PSScriptRoot "..\.venv\Scripts\$localName"
        if (Test-Path -LiteralPath $localAz) { return (Resolve-Path -LiteralPath $localAz).Path }
    }
    $globalAz = Get-Command 'az' -CommandType Application -ErrorAction SilentlyContinue
    if ($globalAz) { return $globalAz.Source }
    return $null
}

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found. Install Azure CLI and OpenSSH client first."
    }
}

function Require-Config {
    $script:AzCommand = Resolve-AzCommand
    if (-not $script:AzCommand) {
        throw "Azure CLI was not found. Run .\setup-dependencies.ps1 or install Azure CLI separately."
    }
    Require-Command 'ssh'
    if ([string]::IsNullOrWhiteSpace($SubscriptionId)) {
        throw 'Set AZURE_SUBSCRIPTION_ID or pass -SubscriptionId before running this command.'
    }
}

function Invoke-Az([string[]]$Arguments) {
    & $script:AzCommand @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI failed with exit code $LASTEXITCODE." }
}

function Get-Ip {
    $ip = & $script:AzCommand vm show -d --subscription $SubscriptionId -g $ResourceGroup -n $VmName --query publicIps -o tsv
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ip)) { throw 'The VM has no reachable public IP. Is it running?' }
    return $ip.Trim()
}

function Get-SshTarget([string]$Ip) { return "$VmUser@$Ip" }

function Get-SshArgs {
    @('-i', $SshKeyPath, '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ServerAliveInterval=60', '-o', 'ExitOnForwardFailure=yes')
}

function Stop-Tunnel {
    $connections = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        $process = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
        if ($process -and $process.ProcessName -eq 'ssh') {
            Stop-Process -Id $process.Id -Force
            Write-Host "Stopped SSH tunnel process $($process.Id)."
        }
    }
}

function Start-Tunnel {
    Require-Config
    Stop-Tunnel
    $ip = Get-Ip
    $sshArgs = Get-SshArgs
    $sshArgs += @('-f', '-N', '-L', "$LocalPort`:127.0.0.1:11434", (Get-SshTarget $ip))
    Write-Host "Opening localhost:$LocalPort -> $ip`:11434 ..."
    $process = Start-Process -FilePath 'ssh.exe' -ArgumentList $sshArgs -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "SSH tunnel failed with exit code $($process.ExitCode)." }
    Write-Host "Tunnel ready at http://localhost:$LocalPort."
}

function Show-Help {
    @"
Azure Ollama/Dolphin controller

Commands:
  make create       Create the resource group and CPU VM (one time)
  make setup        Install Ollama and pull the configured model (one time)
  make on           Start the VM and open the local SSH tunnel
  make off          Stop the tunnel and deallocate the VM
  make status       Show VM, IP, and local tunnel state
  make tunnel       Recreate only the SSH tunnel
  make tunnel-stop  Stop the local SSH tunnel
  make logs         Show Ollama service logs on the VM

Configuration environment variables:
  AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AZURE_VM_NAME, AZURE_LOCATION,
  AZURE_VM_SIZE, AZURE_VM_USER, AZURE_SSH_KEY_PATH, OLLAMA_MODEL, OLLAMA_LOCAL_PORT
"@
}

switch ($Command) {
    'help' { Show-Help; break }
    'create' {
        Require-Config
        Invoke-Az @('group', 'create', '--name', $ResourceGroup, '--location', $Location, '--subscription', $SubscriptionId, '-o', 'none')
        Invoke-Az @('vm', 'create', '--subscription', $SubscriptionId, '--resource-group', $ResourceGroup, '--name', $VmName, '--location', $Location, '--image', 'Ubuntu2404', '--size', $VmSize, '--admin-username', $VmUser, '--generate-ssh-keys', '--public-ip-sku', 'Standard', '--os-disk-size-gb', '64')
        Write-Host 'VM created. Run make setup next.'
        break
    }
    'setup' {
        Require-Config
        $ip = Get-Ip
        $remote = "curl -fsSL https://ollama.com/install.sh | sh && sudo systemctl enable --now ollama && ollama pull '$Model'"
        & ssh.exe @(Get-SshArgs) (Get-SshTarget $ip) $remote
        if ($LASTEXITCODE -ne 0) { throw "Remote setup failed with exit code $LASTEXITCODE." }
        break
    }
    'on' {
        Require-Config
        Invoke-Az @('vm', 'start', '--subscription', $SubscriptionId, '--resource-group', $ResourceGroup, '--name', $VmName)
        Start-Tunnel
        break
    }
    'off' {
        Require-Config
        Stop-Tunnel
        Invoke-Az @('vm', 'deallocate', '--subscription', $SubscriptionId, '--resource-group', $ResourceGroup, '--name', $VmName)
        Write-Host 'VM deallocated; compute billing has stopped.'
        break
    }
    'tunnel' { Start-Tunnel; break }
    'tunnel-stop' { Stop-Tunnel; break }
    'status' {
        Require-Config
        $state = & $script:AzCommand vm get-instance-view --subscription $SubscriptionId -g $ResourceGroup -n $VmName --query "instanceView.statuses[1].displayStatus" -o tsv 2>$null
        $ip = & $script:AzCommand vm show -d --subscription $SubscriptionId -g $ResourceGroup -n $VmName --query publicIps -o tsv 2>$null
        $listener = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        Write-Host "VM state : $($state.Trim())"
        Write-Host "Public IP: $(if ($ip) { $ip.Trim() } else { 'N/A' })"
        Write-Host "Tunnel   : $(if ($listener) { 'ACTIVE (localhost:' + $LocalPort + ')' } else { 'INACTIVE' })"
        break
    }
    'logs' {
        Require-Config
        $ip = Get-Ip
        & ssh.exe @(Get-SshArgs) (Get-SshTarget $ip) 'sudo journalctl -u ollama -n 100 --no-pager'
        break
    }
}

