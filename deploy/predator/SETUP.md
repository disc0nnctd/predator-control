# Predator control, served from Predator itself (LOCAL mode)

Same app as on dcmini (`server.py` + `index.html`, same token, same safety rules), but run ON Predator, so the controls work
while dcmini is down. Every "remote" command is run directly with `cmd.exe` on the same machine instead of over ssh.
LOCAL mode turns on by itself on Windows (`PREDATOR_LOCAL=1` forces it, `0` forces ssh mode). dcmini's copy is untouched.

Facts checked read-only: Predator's python = `C:\Python312` (also 3.14, `py.exe`; ComfyUI venv is 3.12.10). Tailnet name:
`predator` (so `predator.tail1e3236.ts.net`, confirm with `tailscale status` on Predator). Server needs only the stdlib.

## Deploy (two commands, from dcmini)

```bash
scp -r ~/predator-control/deploy/predator dcmini@100.105.14.41:C:/Users/dcmini/predator-control-bundle
ssh dcmini@100.105.14.41 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\dcmini\predator-control-bundle\install_predator_control.ps1'
```

(Needs an admin account to register the tasks. If dcmini is not admin, run `install_predator_control.bat` elevated on Predator.)
It copies the files to `C:\Users\dcmini\predator-control`, registers the tasks below as user dcmini (S4U, no password stored)
and starts the page. Then:

1. Token: `ssh dcmini@100.105.14.41 type %LOCALAPPDATA%\predator-control\token` (made on first start, only that user can read it;
   ACL is the user profile's). Paste it in the page. A different token than dcmini's: that is intended.
2. Smoke test from dcmini: `ssh dcmini@100.105.14.41 "curl -s http://127.0.0.1:8793/"` (page HTML) and the page's status.
3. Publish on Predator's own tailnet name (run on Predator, elevated PowerShell or ssh as admin):
   `tailscale serve --bg --https=8793 http://127.0.0.1:8793`  -> `https://predator.tail1e3236.ts.net:8793/`
   (the existing `tailscale` CLI warning "already in use by Predator\gamin" means run it as that user or use
   `tailscale --socket` / an elevated shell; check `tailscale serve status`). Stop it: `tailscale serve --https=8793 off`.
4. dcmini tile: leave dcmini's tile as is (that copy keeps working). Optionally add a second tile:
   `~/bin/homepage-add "Predator control (on Predator)" https://predator.tail1e3236.ts.net:8793/`.

## Tasks

| Task | Trigger | Does |
|---|---|---|
| `PredatorControl` | at boot, hidden, restarts every minute on failure, no time limit | `start_predator_control.ps1`: finds python (PREDATOR_PYTHON, C:\Python312, C:\Python314, `py -3`, PATH, ComfyUI venv last), sets `PREDATOR_LOCAL=1`, logs to `%LOCALAPPDATA%\predator-control\server.log` |
| `PredatorComfyUI` | none (on demand) | `start_comfyui.bat`; the page runs it with `schtasks /run /tn PredatorComfyUI` |

## How ComfyUI starts (the ssh-session-dies problem)

A process started from an ssh session dies when the session ends. Here the page never uses ssh: it runs the on-demand
`PredatorComfyUI` task, so ComfyUI lives in the task's own session, independent of the control server (restarting
PredatorControl does not kill it). Only the page's existing rules decide when that may happen (never beside a running model,
never in llm/gaming mode). If the task is missing, the server falls back to a detached child (new process group, job breakaway).

## Config (env vars, set inside `start_predator_control.ps1`)

`IMAGINE_URL` (default `http://100.96.201.27:8791`, dcmini's tailnet address; Imagine must listen on it, or be published with
`tailscale serve`. Unreachable = "unknown": the job check is then skipped and only ComfyUI's own queue protects a switch),
`PREDATOR_CONTROL_PORT` (8793), `PREDATOR_CONTROL_DATA`, `COMFY_DIR` (D:\ComfyUI), `COMFY_TASK`, `PREDATOR_PYTHON`.

## Operate / remove

- Logs: `%LOCALAPPDATA%\predator-control\server.log` (server), `actions.log` (what the page did), `D:\ComfyUI\server.log`.
- Restart after an update: copy new files, end the python running `server.py` (`schtasks /end` alone leaves it running and
  the old code keeps serving the NEW index.html), then `schtasks /run /tn PredatorControl`.
- Remove: `uninstall_predator_control.bat`, and `tailscale serve --https=8793 off`.
- Updating the bundle copies from the repo: `~/predator-control/deploy/sync.sh` (tests fail if they drift).
