@echo off
rem Run by the PredatorComfyUI scheduled task (the control page triggers it with `schtasks /run`).
rem Same command line the control page's ssh mode used; "main.py --listen" is how the page finds/stops ComfyUI, keep it.
cd /d D:\ComfyUI
venv\Scripts\python.exe main.py --listen 127.0.0.1 --port 8188 --disable-metadata --cache-lru 40 > D:\ComfyUI\server.log 2>&1
