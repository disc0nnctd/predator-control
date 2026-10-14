@echo off
schtasks /end /tn PredatorControl 2>nul
schtasks /delete /tn PredatorControl /f
schtasks /delete /tn PredatorComfyUI /f
echo Tasks removed. Files stay in C:\Users\dcmini\predator-control (delete by hand if wanted).
