# Installs two scheduled tasks as user dcmini (no password stored: S4U), then starts the control page.
#   PredatorControl : at boot, runs start_predator_control.ps1 hidden, restarts on failure (every minute, forever)
#   PredatorComfyUI : no trigger; started ON DEMAND by the control page (schtasks /run), runs start_comfyui.bat.
#                     It does NOT start ComfyUI itself and does nothing until the page asks.
# Run from an elevated PowerShell:  powershell -NoProfile -ExecutionPolicy Bypass -File install_predator_control.ps1
# Files are copied to C:\Users\dcmini\predator-control (data+token+logs: %LOCALAPPDATA%\predator-control).
param([string]$User = 'dcmini', [string]$Dest = 'C:\Users\dcmini\predator-control', [switch]$NoStart)
$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
foreach ($f in 'server.py','index.html','start_predator_control.ps1','start_comfyui.bat') { Copy-Item -Force (Join-Path $src $f) $Dest }

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

$a1 = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dest\start_predator_control.ps1`""
Register-ScheduledTask -TaskName 'PredatorControl' -Action $a1 -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal $principal -Settings $settings -Force | Out-Null

$a2 = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$Dest\start_comfyui.bat`""
$s2 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'PredatorComfyUI' -Action $a2 -Principal $principal -Settings $s2 -Force | Out-Null

if (-not $NoStart) { Start-ScheduledTask -TaskName 'PredatorControl'; Start-Sleep 4 }
$tok = Join-Path $env:LOCALAPPDATA 'predator-control\token'
"Installed. Tasks: PredatorControl, PredatorComfyUI. Log: $env:LOCALAPPDATA\predator-control\server.log"
"Token file (created on first start; read it as dcmini): $tok"
