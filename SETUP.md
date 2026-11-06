# Control Centre

Private page to see and change which model owns Predator's GPU (image / LLM <key> / gaming / idle), start/stop ComfyUI,
stop a stray llama-server. Python `server.py`, `system_controls.py` and `index.html`, `127.0.0.1:8793`. Data: `~/.local/share/predator-control`.

## Install (NOT done for you)

```bash
cp ~/predator-control/predator-control.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now predator-control
journalctl --user -u predator-control | grep "ACCESS TOKEN"      # token is printed ONCE, on first start; also in ~/.local/share/predator-control/token (mode 600)
sudo tailscale serve --bg --https=8793 http://127.0.0.1:8793
~/bin/homepage-add "Predator control" https://dcmini-1.tail1e3236.ts.net:8793/
```

Open the page, paste the token (kept in the tab's sessionStorage only). 10 wrong tokens in 5 minutes lock the API for 5 minutes.
New token: delete the token file and restart the service.

## Serving it from Predator itself

`deploy/predator/` is a ready bundle (LOCAL mode: same app, commands run on Predator, works while dcmini is down). See `deploy/predator/SETUP.md`.

## How it works

- Status: `mode.ps1 -Status` (read-only) + ComfyUI queue + the model catalog (`llama_models.json`) over ssh, Imagine's `/api/jobs` counts. Cached 2.5 s.
- Switching: `mode_request.txt` + `schtasks /run /tn HermesMode`, then polls `-Status` (LLM up to 5 min).
- Only fixed commands go over ssh; the only variable text is a model key that must be in Predator's own catalog.
- Refuses with 409 (and says why) while Imagine has running/waiting jobs or ComfyUI's queue is non-empty, while another switch is
  pending, or while another action runs. The "I know a job is running" tick overrides the job check only. ComfyUI start is never
  allowed beside a running model or in llm/gaming mode.
- Log: `~/.local/share/predator-control/actions.log` (time, action, mode/model key, result), also last 200 in RAM.

## Models section (full model management)

- **Catalog** (`C:\Users\gamin\Experiments\llm_gaming_setup\llama_models.json`): add, edit, remove, make default, load, view a
  model's llama-server log. Every write first copies the live file to `llama_models.json.bak-<time>` next to it (newest 10 kept),
  then overwrites it in place (keeps its ACL) and reads it back to confirm.
- **Files**: every `.gguf` under `C:\Users\dcmini` and `D:\models` (vocab/ComfyUI skipped), size, which models use it; delete
  unused ones. Builds: every `llama-server.exe` found there.
- **Download**: a Hugging Face `.gguf` link -> `D:\models\<folder>\`. curl runs in its own process (WMI), so it survives
  the page/ssh; `.part` until done, then renamed. Cancel / resume (`curl -C -`). Refused if D: would keep < 5 GB free.
- Safety: the catalog's args land in a .bat that `switch_mode.ps1` runs as SYSTEM, so: model file and build must be ones found
  on Predator; args only `[A-Za-z0-9 ._=:,/\+-]` (no quotes or shell characters), and `-m/--port/--alias/--host/--path/
  --log-file/-hf...` are refused; key/alias/port (8080-8099)/VRAM (<= 6144 MB) pattern-checked; key cannot be renamed; the
  loaded model cannot be removed. PowerShell goes over ssh as `-EncodedCommand`.
- The copy running ON Predator (LOCAL mode, non-admin task) can list/download/delete files but cannot write the catalog: use
  the dcmini page for catalog changes.

## GPU processes

"What is using the GPU" lists every process holding GPU memory or using the GPU (Windows GPU counters; nvidia-smi shows N/A
under WDDM), biggest first, with Terminate. Windows-critical processes (dwm, csrss, svchost...) are never offered. A kill
re-checks pid AND name in a fresh list; ending ComfyUI with jobs queued needs the override tick. The non-admin copy on
Predator can only end processes of its own user.

## Tests

`python3 ~/predator-control/tests/check.py` uses a fake Predator (never touches the real one). `--live` adds one read-only real status read.

## Memory and swap

Control Centre shows memory on the host running server.py, separately from the remote Predator GPU. The authenticated /api/memory endpoint reads Linux memory counters and the largest process swap users. Swap activity is calculated between samples. The optimization panel offers temporary swap preference commands requiring administrator access on that host; it never applies them automatically. Standard (60) restores the usual value; reboot restores the configured boot value. No privilege changes are required for the dashboard.

## Discord bot controls in Control Centre

The dcmini and Predator navigation buttons separate host controls. `system_controls.py` loads the existing SysBear handlers from `/home/notdc/projects/miniclaw/plugins/systembear-control` and the installed fan plugin; it does not use Discord or send messages. The router channel handler uses its existing local dependency and credential setup.

- dcmini: system health/events, disk usage and cleanup review, fan mode, expiring primary LLM service, media health/recovery/history, on-demand TradingView, Bluetooth, saved Wi-Fi, router surveys/channel selection, power profile, Tailscale devices/diagnostics/login/exit nodes.
- Predator: full status, wake, display on/off, lock, sleep and image-model selection. Existing mode, catalog, file, GPU and Voice Studio controls remain in this tab.
- Monitoring: host temperatures, load, disk use and the existing Discord health monitor's saved connectivity/pressure/fault state. Old monitor state is explicitly marked stale. Hardware fault details are available under System health. This page never runs the alert sender.
- Authenticated API: GET `/api/system/capabilities`, `/api/system/jobs`, `/api/system/monitor`; POST `/api/system/run`. The server validates domain/action, target length, wait/duration ranges and explicit confirmation for changes. No arbitrary commands or unit names are accepted.
- System actions execute in a bounded background queue, one at a time, so Wi-Fi/exit-node rollback can finish after a browser disconnects. Recent results survive tab changes/reloads during the server session; restart clears these results. Logs store domain/action/outcome, never targets or login links.
- The user service allows the existing sudo helpers to execute (`NoNewPrivileges=false`, `PrivateTmp=false`; a user-service private temporary directory otherwise creates a user namespace in which sudo cannot run). Sudo rights are still defined by the existing root-owned rules and helpers; no new sudoers grants are installed. Reinstall the updated service and reload systemd when upgrading an older deployment. Monitoring alone needs no elevated privileges.
- Windows bundles include the adapter so the app still starts; SysBear integration is marked unavailable there because its handlers live on dcmini. Use dcmini's Control Centre for the combined controls.

Validation: `python3 tests/system_controls_check.py`, `python3 tests/check.py`, `python3 tests/voice_check.py`.
