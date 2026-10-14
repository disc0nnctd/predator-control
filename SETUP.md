# Predator control

Private page to see and change which model owns Predator's GPU (image / LLM <key> / gaming / idle), start/stop ComfyUI,
stop a stray llama-server. Stdlib Python `server.py` + one `index.html`, `127.0.0.1:8793`. Data: `~/.local/share/predator-control`.

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

## Tests

`python3 ~/predator-control/tests/check.py` uses a fake Predator (never touches the real one). `--live` adds one read-only real status read.
