param(
[ValidateRange(1, 65535)][int]$Port = 8767,
[switch]$Disable,
[switch]$Elevated
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$logPath = Join-Path $PSScriptRoot 'data\firewall-update.log'
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($Elevated) { throw 'Administrator permission was not granted.' }
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Port {1} -Elevated' -f $PSCommandPath, $Port
    if ($Disable) { $arguments += ' -Disable' }
    try {
        Write-Output 'Windows will request administrator permission to update this app firewall rule.'
        $process = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        if (Test-Path -LiteralPath $logPath) { Get-Content -LiteralPath $logPath }
        exit $process.ExitCode
    } catch {
        Write-Output 'Administrator request was cancelled or could not start. Firewall was not updated.'
        exit 1
    }
}
$exitStatus = 0
try {
    New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'data') -Force | Out-Null
    Set-Content -LiteralPath $logPath -Value ('Firewall update started: ' + (Get-Date -Format s)) -Encoding UTF8
    $ruleName = "GiftRegistry-LAN-$Port"
    $groupName = 'Gift Registry LAN'
    $existingRule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($existingRule -and $existingRule.Group -ne $groupName) {
        throw 'An unrelated firewall rule uses this name. No changes were made.'
    }
    if ($Disable) {
        if ($existingRule) { $existingRule | Set-NetFirewallRule -Enabled False }
        Add-Content -LiteralPath $logPath -Value 'Gift Registry LAN firewall rule disabled.'
    } else {
        $runtimePython = Join-Path $PSScriptRoot 'runtime\python.exe'
        if (-not (Test-Path -LiteralPath $runtimePython)) { $runtimePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
        $configuration = & $runtimePython -m gift_registry.lan_setup --check
        if ($LASTEXITCODE -ne 0) { throw 'Invalid LAN configuration. No firewall changes were made.' }
        $settings = $configuration | ConvertFrom-Json
        $adapter = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $settings.lan_ip -ErrorAction SilentlyContinue)
        if ($adapter.Count -ne 1) { throw 'Configured LAN IP is not on a unique local adapter. Run update-lan.bat first.' }
        $parameters = @{
            DisplayName = "Gift Registry LAN TCP $Port"
            Group = $groupName
            Enabled = 'True'
            Direction = 'Inbound'
            Action = 'Allow'
            Protocol = 'TCP'
            LocalPort = $Port
            LocalAddress = $settings.lan_ip
            RemoteAddress = $settings.subnet
            InterfaceAlias = $adapter[0].InterfaceAlias
            Profile = 'Any'
            EdgeTraversalPolicy = 'Block'
        }
        if ($existingRule) {
            $updates = $parameters.Clone()
            $updates.Remove('Group')
            $updates.Remove('DisplayName')
            Set-NetFirewallRule -Name $ruleName -NewDisplayName $parameters.DisplayName @updates | Out-Null
        } else {
            New-NetFirewallRule -Name $ruleName @parameters | Out-Null
        }
        Add-Content -LiteralPath $logPath -Value "SUCCESS: TCP $Port; local IP $($settings.lan_ip); remote subnet $($settings.subnet)."
    }
} catch {
    $exitStatus = 1
    Add-Content -LiteralPath $logPath -Value ('FAILED: ' + $_.Exception.Message)
}
if (-not $Elevated) { Get-Content -LiteralPath $logPath }
exit $exitStatus
