# One-time setup:  powershell -ExecutionPolicy Bypass -File setup.ps1 [-Gpu]
param([switch]$Gpu)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = (& py -3.11 -c "import sys; print(sys.executable)" 2>$null)
if (-not $py) { $py = "python" }
Write-Host "Using $py"

if (-not (Test-Path .venv)) { & $py -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($Gpu) { .\.venv\Scripts\python.exe -m pip install -r requirements-gpu.txt }

if (-not (Test-Path .env)) { Copy-Item .env.example .env }

if (Get-Command ollama -ErrorAction SilentlyContinue) {
    $model = "qwen3.5:4b"
    if (-not ((ollama list) -match [regex]::Escape($model))) { ollama pull $model }
} else {
    Write-Warning "Ollama not found. Install it from https://ollama.com for the local brain."
}

Write-Host "`nRunning systems check (downloads the speech models on first run)...`n"
.\.venv\Scripts\python.exe -m jarvis --check
Write-Host "`nDone. Start Jarvis with:  .\jarvis.bat"
