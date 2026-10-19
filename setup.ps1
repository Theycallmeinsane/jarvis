# Jarvis installer for Windows 10/11. Run from this folder:
#     powershell -ExecutionPolicy Bypass -File setup.ps1          (asks a few questions)
#     powershell -ExecutionPolicy Bypass -File setup.ps1 -Yes     (accepts every recommendation)
# Safe to run again any time: it repairs the install and keeps your settings.
param([switch]$Yes, [switch]$NoWizard, [switch]$CpuOnly)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Ask($question) {
    if ($Yes) { return $true }
    $a = Read-Host "$question (Y/n)"
    return ($a -eq "" -or $a -match "^[yY]")
}
function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

# ---------------------------------------------------------------- 1. Python 3.10-3.12
Step "Python"
$py = $null
foreach ($v in "3.11", "3.12", "3.10") {
    $p = (& py "-$v" -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $p) { $py = $p.Trim(); break }
}
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (Ask "Python 3.11 isn't installed. Install it now with winget?") {
            winget install --id Python.Python.3.11 -e --scope user --accept-source-agreements --accept-package-agreements
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
            $py = (& py -3.11 -c "import sys; print(sys.executable)" 2>$null)
        }
    }
    if (-not $py) { throw "Please install Python 3.11 from https://www.python.org/downloads/ and run setup.ps1 again." }
}
Write-Host "Using $py"

# ---------------------------------------------------------------- 2. virtual environment + packages
Step "Packages (first run downloads ~1 GB)"
if (-not (Test-Path .venv\Scripts\python.exe)) { & $py -m venv .venv }
$vpy = (Resolve-Path .venv\Scripts\python.exe).Path
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ---------------------------------------------------------------- 3. exactly one speech runtime (CPU or GPU build)
Step "Speech runtime"
$hasNvidia = (-not $CpuOnly) -and [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue) -and `
             ((& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null) -ne $null)
# the CPU and GPU builds of onnxruntime share one Python module: never keep both
$ortVersion = (& $vpy -c "from importlib import metadata as m; print(m.version('onnxruntime'))" 2>$null)
if ($hasNvidia) {
    Write-Host "NVIDIA GPU found: installing the GPU build (Whisper, Kokoro and the wake word use it)"
    & $vpy -m pip install -r requirements-gpu.txt --quiet
    & $vpy -m pip uninstall -y onnxruntime onnxruntime-gpu --quiet 2>$null
    $spec = if ($ortVersion) { "onnxruntime-gpu[cuda,cudnn]==$ortVersion" } else { "onnxruntime-gpu[cuda,cudnn]" }
    & $vpy -m pip install $spec
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "GPU build failed to install; falling back to the CPU build."
        & $vpy -m pip install onnxruntime
    }
} else {
    Write-Host "No NVIDIA GPU: using the CPU build"
    & $vpy -m pip uninstall -y onnxruntime-gpu --quiet 2>$null
    & $vpy -m pip install --force-reinstall --no-deps onnxruntime --quiet
}

# ---------------------------------------------------------------- 4. Ollama (the local brain)
Step "Local brain (Ollama)"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Ollama isn't installed. Install it now with winget?")) {
        winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
        Start-Sleep -Seconds 5
    } else {
        Write-Warning "Skipping Ollama. Install it from https://ollama.com, or use Claude (--llm claude)."
    }
}
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    # any model you already have that can use tools works; only pull the suggestion for this PC if there's none
    $model = "$(& $vpy -c 'from jarvis.llm.ollama_models import model_to_pull; print(model_to_pull())')".Trim()
    if ($model) {
        Write-Host "Downloading the $model model for this PC..."
        ollama pull $model
    } else { Write-Host "Using a model you already have (list them: .\jarvis.bat --models)" }
}

# ---------------------------------------------------------------- optional: GitHub by voice
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    if ((-not $Yes) -and (Get-Command winget -ErrorAction SilentlyContinue) -and
        (Ask "Optional: install the GitHub CLI so Jarvis can check your repos and builds?")) {
        winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements
        Write-Host "Then sign in once (new window): gh auth login"
    }
}

# ---------------------------------------------------------------- optional: Chrome by voice
if ((-not $Yes) -and (Ask "Optional: link Chrome so Jarvis can control tabs, pages, videos and Claude?")) {
    & $vpy -m jarvis --chrome-setup
}

# ---------------------------------------------------------------- 5. personal settings
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not $NoWizard) {
    Step "Your settings"
    if ($Yes) { & $vpy -m jarvis --setup --yes } else { & $vpy -m jarvis --setup }
}

# ---------------------------------------------------------------- 6. check
Step "Systems check (downloads the speech models on first run)"
& $vpy -m jarvis --check

Write-Host ""
Write-Host "Done. Start Jarvis:" -ForegroundColor Green
Write-Host "  .\jarvis-tray.bat      (runs in the system tray; or use the Jarvis shortcut)"
Write-Host "  .\jarvis.bat           (runs in this console window)"
Write-Host 'Then say "Hey Jarvis" or press Ctrl+Alt+J.'
