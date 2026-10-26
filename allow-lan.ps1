param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$Disable
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script in an administrator PowerShell window.'
}
$ruleName = "GiftRegistry-LAN-$Port"
$groupName = 'Gift Registry LAN'
$existingRule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existingRule -and $existingRule.Group -ne $groupName) {
    throw 'An unrelated firewall rule uses this name. No changes were made.'
}
if ($Disable) {
    if ($existingRule) { $existingRule | Set-NetFirewallRule -Enabled False }
    Write-Output 'Gift Registry LAN firewall rule disabled.'
    exit 0
}
& '.\.venv\Scripts\python.exe' -c 'from gift_registry.network import load_network; s=load_network(); assert s.enabled, "LAN configuration is disabled"'
if ($LASTEXITCODE -ne 0) { throw 'Invalid LAN configuration. No firewall changes were made.' }
$settings = Get-Content -LiteralPath 'config\network.local.json' -Raw | ConvertFrom-Json
$adapter = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $settings.lan_ip -ErrorAction Stop)
if ($adapter.Count -ne 1) { throw 'The configured LAN address does not uniquely identify a local adapter.' }
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
    Set-NetFirewallRule -Name $ruleName -NewDisplayName $parameters.DisplayName @updates
} else {
    New-NetFirewallRule -Name $ruleName @parameters | Out-Null
}
Write-Output "Gift Registry LAN TCP $Port allowed only from $($settings.subnet) on $($adapter[0].InterfaceAlias)."
