# Starts the Predator control server (LOCAL mode) and keeps its output in a log file. Run by the PredatorControl task.
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir = Join-Path $env:LOCALAPPDATA 'predator-control'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$log = Join-Path $dataDir 'server.log'
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 2MB)) { Move-Item -Force $log "$log.old" }

# Which python: PREDATOR_PYTHON, system Python 3.12/3.14, the py launcher, python on PATH, ComfyUI's venv (last: a ComfyUI rebuild must not break this).
function Find-Python {
    $c = @()
    if ($env:PREDATOR_PYTHON) { $c += $env:PREDATOR_PYTHON }
    $c += 'C:\Python312\python.exe', 'C:\Python314\python.exe'
    foreach ($p in $c) { if (Test-Path $p) { return $p } }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $r = (& $py.Source -3 -c "import sys;print(sys.executable)" 2>$null); if ($r -and (Test-Path $r)) { return $r } }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*WindowsApps*' } | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    if (Test-Path 'D:\ComfyUI\venv\Scripts\python.exe') { return 'D:\ComfyUI\venv\Scripts\python.exe' }
    return $null
}
$python = Find-Python
if (-not $python) { "$(Get-Date -Format s) no python found" | Add-Content $log; exit 2 }

$env:PREDATOR_LOCAL = '1'
if (-not $env:PREDATOR_CONTROL_PORT) { $env:PREDATOR_CONTROL_PORT = '8793' }
# Imagine (on dcmini) is read by its tailnet address; unreachable = unknown. Override here if it moves.
if (-not $env:IMAGINE_URL) { $env:IMAGINE_URL = 'http://100.96.201.27:8791' }
"$(Get-Date -Format s) starting with $python" | Add-Content $log
# stdlib only, no pip needed. cmd /c does the stdout+stderr redirect into the log.
& cmd.exe /c "`"$python`" -u `"$here\server.py`" >> `"$log`" 2>&1"
exit $LASTEXITCODE
