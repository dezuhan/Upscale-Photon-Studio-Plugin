param(
  [int]$Port = 8765,
  [ValidateSet("auto", "ncnn", "pillow")]
  [string]$Engine = "auto",
  [string]$Models = "",
  [string]$Binary = ""
)

# Starts the local upscale service that the Photon plugin talks to.
# Keep this window open while using the plugin.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$serviceDir = Join-Path $root "service"
$serverPath = Join-Path $serviceDir "server.py"

if (-not (Test-Path -LiteralPath $serverPath)) {
  throw "server.py was not found at $serverPath"
}

$pythonArgs = @()
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
  $python = (Get-Command py -ErrorAction SilentlyContinue).Source
  if ($python) { $pythonArgs = @("-3") }
}
if (-not $python) {
  throw "Python was not found. Install Python 3.10+ and make sure 'python' is on PATH."
}

$arguments = @($serverPath, "--port", "$Port", "--engine", $Engine)
if ($Models) { $arguments += @("--models", $Models) }
if ($Binary) { $arguments += @("--bin", $Binary) }

Write-Host "Running: $python $($pythonArgs -join ' ') $($arguments -join ' ')" -ForegroundColor DarkGray
Write-Host "Leave this window open while the plugin is in use." -ForegroundColor DarkGray

Push-Location $serviceDir
try {
  & $python @pythonArgs @arguments
} finally {
  Pop-Location
}
