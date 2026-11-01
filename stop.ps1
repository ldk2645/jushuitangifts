param([ValidateRange(1, 65535)][int]$Port = 8765)

$ErrorActionPreference = 'Stop'
$Base = "http://127.0.0.1:$Port"
$PidFile = Join-Path $PSScriptRoot 'data\server.pid'

function Test-LocalPort {
    param([int]$Number)
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Async = $Client.BeginConnect('127.0.0.1', $Number, $null, $null)
        if (-not $Async.AsyncWaitHandle.WaitOne(800)) { return $false }
        $Client.EndConnect($Async)
        return $true
    } catch {
        return $false
    } finally {
        $Client.Close()
    }
}

function Get-ServerId {
    if (-not (Test-Path -LiteralPath $PidFile)) { return 0 }
    $Value = 0
    $Text = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    if ([int]::TryParse($Text, [ref]$Value)) { return $Value }
    return 0
}

function Get-ServerProcess {
    param([int]$ServerId)
    if (-not $ServerId) { return $null }
    $Process = Get-Process -Id $ServerId -ErrorAction SilentlyContinue
    if (-not $Process -or $Process.ProcessName -notmatch '^python') { return $null }
    try {
        $Info = Get-CimInstance Win32_Process -Filter "ProcessId=$ServerId" -ErrorAction Stop
        if ($Info.CommandLine -notmatch 'main\.py') { return $null }
    } catch {
        # Without CIM access, fall back to the process name check above.
    }
    return $Process
}

$ServerId = Get-ServerId
$ServerProcess = Get-ServerProcess -ServerId $ServerId
if (-not (Test-LocalPort -Number $Port)) {
    Write-Host "No workbench is listening on port $Port."
    if (-not $ServerProcess -and (Test-Path -LiteralPath $PidFile)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    exit 0
}

try {
    $Config = Invoke-RestMethod -Uri "$Base/api/config" -TimeoutSec 5
    Invoke-RestMethod -Method Post -Uri "$Base/api/shutdown" -Headers @{ 'X-CSRF-Token' = $Config.token } -TimeoutSec 5 | Out-Null
    Write-Host "Graceful shutdown requested on port $Port."
} catch {
    Write-Warning "Shutdown request failed: $($_.Exception.Message)"
}

$Stopped = $false
$Deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $Deadline) {
    if (-not (Test-LocalPort -Number $Port)) {
        $Stopped = $true
        break
    }
    Start-Sleep -Milliseconds 400
}

if ($Stopped) {
    $ServerId = Get-ServerId
    $ServerProcess = Get-ServerProcess -ServerId $ServerId
    if ($ServerProcess) {
        $ExitDeadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $ExitDeadline -and (Get-Process -Id $ServerId -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 400
        }
    }
    if ($ServerId -and (Get-Process -Id $ServerId -ErrorAction SilentlyContinue)) {
        Write-Host "Shutdown requested; the workbench is still finishing an in-flight task (PID $ServerId)."
    } else {
        Write-Host "Workbench stopped."
    }
    exit 0
}

$ServerId = Get-ServerId
$ServerProcess = Get-ServerProcess -ServerId $ServerId
if ($ServerProcess) {
    Write-Host "Workbench PID $ServerId is still finishing accepted tasks. It was NOT force-stopped."
    Write-Host "The workbench remains read-only while closing. Run start.bat to wait and reopen automatically."
    exit 0
}

Write-Warning "Workbench is still running. No process was force-stopped; inspect the host before closing it manually."
exit 1
