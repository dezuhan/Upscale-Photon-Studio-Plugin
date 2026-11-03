# Stops the hidden upscale service (started by install-autostart.ps1 or run-hidden).

$ErrorActionPreference = "Stop"

$stale = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
  $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and $_.CommandLine -match 'server\.py'
}

if (-not $stale) {
  Write-Host "No upscale service process is running." -ForegroundColor DarkGray
  return
}

foreach ($process in $stale) {
  try {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    Write-Host "Stopped pid $($process.ProcessId)." -ForegroundColor Green
  } catch {
    Write-Host "Could not stop pid $($process.ProcessId): $($_.Exception.Message)" -ForegroundColor DarkYellow
  }
}
