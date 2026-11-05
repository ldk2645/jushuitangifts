# 修复业务浏览器无法启动：把项目目录从沙箱残留的“低完整性级别”标签恢复为普通级别。
# 只调整 Windows 权限标签（不修改、不删除任何文件内容或所有者）。
[CmdletBinding()]
param(
    [string]$Root
)

$ErrorActionPreference = 'Stop'
if (-not $Root) {
    $scriptDirectory = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Root = Split-Path -Parent $scriptDirectory
}
$Root = [System.IO.Path]::GetFullPath($Root)
if (-not (Test-Path -LiteralPath (Join-Path $Root 'main.py')) -or
    -not (Test-Path -LiteralPath (Join-Path $Root 'gift_registry'))) {
    throw "此目录不像本项目的根目录，已停止：$Root"
}

function Get-LowIntegrityLabelPaths {
    param([string[]]$Paths)
    $low = @()
    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $lines = & icacls $path 2>$null
        if ($lines -match 'Low Mandatory Level' -or $lines -match '低强制级别') {
            $low += $path
        }
    }
    return $low
}

function Get-FailureCount {
    param([string[]]$Lines)
    $summary = ($Lines | Where-Object { $_ -match 'Failed processing\s+(\d+)' } | Select-Object -Last 1)
    if ($summary -and $summary -match 'Failed processing\s+(\d+)') { return [int]$Matches[1] }
    return -1
}

$keyPaths = @(
    $Root,
    (Join-Path $Root '.venv\Scripts\python.exe'),
    (Join-Path $Root '.venv\Scripts\pythonw.exe'),
    (Join-Path $Root 'chrome\chrome-win64\chrome.exe'),
    (Join-Path $Root '.tmp\drivers\chromedriver.exe'),
    (Join-Path $Root '.tmp\drivers\msedgedriver.exe'),
    (Join-Path $Root 'data\selenium')
)
$lowPaths = Get-LowIntegrityLabelPaths -Paths $keyPaths
if ($lowPaths.Count -eq 0) {
    Write-Host '未发现“低完整性级别”标签，业务浏览器启动环境无需修复。'
    Write-Host '请双击 diagnose-selenium.bat 复查；若仍失败，请把该检查输出发给维护者。'
    exit 0
}

Write-Host "发现低完整性级别标签：$($lowPaths.Count) 个关键位置。"
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
Write-Host "修复目标：$Root"
Write-Host "只调整 Windows 完整性级别标签，不修改文件内容；文件所有者保持不变。"
Write-Host '正在恢复当前用户的完整控制权限（修复标签需要该权限）...'
$grantOutput = & icacls $Root /grant:r "${identity}:(OI)(CI)F" 2>&1
if ($LASTEXITCODE -ne 0 -or ($grantOutput -join ' ') -match 'Failed processing [1-9]') {
    throw "无法为 $identity 添加完整控制权限，请确认当前用户是项目目录的所有者，或在资源管理器的项目目录上右键取得所有权后重试。`n$($grantOutput -join [Environment]::NewLine)"
}

Write-Host '正在把项目文件恢复为普通完整性级别（可能需要几十秒）...'
$labelOutput = & icacls $Root /setintegritylevel Medium /t /c 2>&1
$failures = Get-FailureCount -Lines $labelOutput
$remaining = Get-LowIntegrityLabelPaths -Paths $keyPaths
if ($remaining.Count -gt 0) {
    Write-Host '以下关键位置仍是低完整性级别：'
    $remaining | ForEach-Object { Write-Host "  $_" }
    Write-Host '修复未完成。请关闭本窗口，重新双击 fix-browser-integrity.bat；仍失败时请联系维护者。'
    exit 1
}
if ($failures -gt 0) {
    Write-Host "修复完成；有 $failures 个辅助文件（多为缓存或诊断输出）权限受限未调整，不影响业务浏览器。"
} else {
    Write-Host '修复完成。'
}
Write-Host '下一步：双击 diagnose-selenium.bat 验证 Chrome 与 Edge 是否能独立启动。'
exit 0
