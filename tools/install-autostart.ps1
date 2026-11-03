param(
  [switch]$Remove,
  [int]$Port = 8765,
  [ValidateSet("auto", "ncnn", "pillow")]
  [string]$Engine = "auto"
)

# Runs the upscale service without a console window. It starts now and, unless -Remove is
# passed, again at every sign-in through the per-user Run key (HKCU, no admin needed).
# Nothing is installed system-wide; everything lives under HKCU and the project folder.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$serviceDir = Join-Path $root "service"
$serverPath = Join-Path $serviceDir "server.py"
$launcher = Join-Path $PSScriptRoot "service-hidden.vbs"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runValue = "PhotonUpscaleService"

if (-not (Test-Path -LiteralPath $serverPath)) {
  throw "server.py was not found at $serverPath"
}

function Stop-RunningService {
  $stale = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and $_.CommandLine -match 'server\.py'
  }
  foreach ($process in $stale) {
    try {
      Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
      Write-Host "Stopped running service (pid $($process.ProcessId))." -ForegroundColor DarkGray
    } catch {
      Write-Host "Could not stop pid $($process.ProcessId): $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
  }
}

if ($Remove) {
  Remove-ItemProperty -Path $runKey -Name $runValue -ErrorAction SilentlyContinue
  if (Test-Path -LiteralPath $launcher) { Remove-Item -LiteralPath $launcher -Force }
  Stop-RunningService
  Write-Host "Autostart removed and the service was stopped." -ForegroundColor Green
  return
}

# pythonw.exe runs without a console window; it sits next to python.exe.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python was not found on PATH. Install Python 3.10+ first." }
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
  $pythonw = $python
  Write-Host "pythonw.exe not found; the console window may appear briefly." -ForegroundColor DarkYellow
}

# A VBS launcher starts the hidden process with window style 0 (no window, not even flicker).
$vbs = @"
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "$serviceDir"
shell.Run """$pythonw"" ""$serverPath"" --port $Port --engine $Engine", 0, False
"@
Set-Content -LiteralPath $launcher -Value $vbs -Encoding ASCII

if (-not (Test-Path -LiteralPath $runKey)) { New-Item -Path $runKey -Force | Out-Null }
Set-ItemProperty -Path $runKey -Name $runValue -Value "wscript.exe `"$launcher`""

Stop-RunningService
Start-Sleep -Milliseconds 300
Start-Process -FilePath "wscript.exe" -ArgumentList "`"$launcher`"" -WindowStyle Hidden

$ready = $false
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 500
  try {
    $health = (Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3).Content | ConvertFrom-Json
    $ready = $true
    break
  } catch { }
}

if ($ready) {
  Write-Host "Service running hidden on http://127.0.0.1:$Port (v$($health.version), $($health.models.Count) models)." -ForegroundColor Green
  Write-Host "It will start automatically at every sign-in."
  Write-Host "Log file: $(Join-Path $serviceDir 'logs\service.log')" -ForegroundColor DarkGray
  Write-Host "To undo: .\tools\install-autostart.ps1 -Remove" -ForegroundColor DarkGray
} else {
  Write-Host "The service did not answer yet. Check $(Join-Path $serviceDir 'logs\service.log')." -ForegroundColor DarkYellow
}
