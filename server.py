#!/usr/bin/env python3
"""Predator control: a small private page to see and change which model owns Predator's 6 GB GPU.

Listens on 127.0.0.1:8793 (publish tailnet-only with `tailscale serve --bg --https=8793 http://127.0.0.1:8793`).

Everything goes to Predator over SSH (or, with PREDATOR_LOCAL=1 / auto on Windows, directly on this machine when the server
runs ON Predator) with FIXED commands only: the single piece of variable text is an LLM model key, and
it is accepted only if it appears in Predator's own model catalog (and matches a strict pattern). Mode switches use the
existing HermesMode mechanism (mode_request.txt + `schtasks /run /tn HermesMode`), the same as SysBear and chat-styles.
Nothing here reads or stores prompts or model text; the action log holds time, action, short detail (mode/model key), result.
Every /api call needs the one-time access token (~/.local/share/predator-control/token).
"""
import base64, collections, hmac, json, os, re, secrets, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import system_controls

IS_WIN = os.name == "nt"
# LOCAL mode: this server runs ON Predator, so every "remote" command is run directly on this machine instead of over ssh.
# PREDATOR_LOCAL=1 forces it, =0 forces ssh mode, unset = automatic (on when running on Windows).
_PL = os.environ.get("PREDATOR_LOCAL", "").strip().lower()
LOCAL = True if _PL in ("1", "true", "yes", "on") else False if _PL in ("0", "false", "no", "off") else IS_WIN
PORT = int(os.environ.get("PREDATOR_CONTROL_PORT", 8793))


def default_data():
    if LOCAL and IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "predator-control"
    return Path.home() / ".local/share/predator-control"


DATA = Path(os.environ.get("PREDATOR_CONTROL_DATA", default_data()))
PAGE = Path(__file__).with_name("index.html")
# Imagine job COUNTS only. On Predator, Imagine lives on dcmini: reach it by its tailnet address (unreachable = unknown).
IMAGINE_URL = os.environ.get("IMAGINE_URL", "http://100.96.201.27:8791" if LOCAL else "http://127.0.0.1:8791")
COMFY_DIR = os.environ.get("COMFY_DIR", r"D:\ComfyUI")
COMFY_TASK = os.environ.get("COMFY_TASK", "PredatorComfyUI")  # scheduled task that starts ComfyUI (see deploy/predator)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
HOST = os.environ.get("PREDATOR_HOST", "dcmini@100.105.14.41")
SSH = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes"]
PRED = r"C:\Users\Public\HermesPredator"
MODE_PS = rf"powershell -NoProfile -ExecutionPolicy Bypass -File {PRED}\mode.ps1 -Status"
CATALOG_PATH = r"C:\Users\gamin\Experiments\llm_gaming_setup\llama_models.json"
CATALOG_CMD = rf"cmd /c type {CATALOG_PATH} 2>nul"
QUEUE_CMD = ("powershell -NoProfile -Command \"try{(Invoke-RestMethod http://127.0.0.1:8188/prompt).exec_info.queue_remaining}"
             "catch{'none'}\"")
COMFY_COUNT_CMD = ("powershell -NoProfile -Command \"(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
                   "$_.CommandLine -like '*main.py --listen*' } | Measure-Object).Count\"")
COMFY_STOP_CMD = ("powershell -NoProfile -Command \"Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
                  "$_.CommandLine -like '*main.py --listen*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }\"")
COMFY_START_CMD = (r'cmd /c "cd /d D:\ComfyUI && venv\Scripts\python main.py --listen 127.0.0.1 --port 8188 --disable-metadata '
                   r'--cache-lru 40 > D:\ComfyUI\server.log 2>&1"')
LLAMA_STOP_CMD = r'cmd /c "taskkill /IM llama-server.exe /F"'
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
MODES = ("image", "idle", "gaming", "llm")

lock = threading.RLock()
status_lock = threading.Lock()
log_ram = collections.deque(maxlen=200)
action = {"name": "", "stage": "", "error": "", "ok": None, "running": False, "since": 0.0}
cache = {"t": 0.0, "v": None}
catalog_cache = {"t": 0.0, "v": []}
fails = []
TOKEN = ""


def local_run(cmd, timeout=25):
    """LOCAL mode: the same fixed command string, run by cmd.exe on this machine (what sshd's default shell would do)."""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW if IS_WIN else 0)


def remote(cmd, timeout=25):
    """A fixed command on Predator: over ssh, or directly on this machine in LOCAL mode (tests replace this)."""
    if LOCAL:
        return local_run(cmd, timeout).stdout
    return subprocess.run(SSH + [HOST, cmd], capture_output=True, text=True, timeout=timeout).stdout


def comfy_python():
    return str(Path(COMFY_DIR) / "venv" / "Scripts" / "python.exe") if IS_WIN else str(Path(COMFY_DIR) / "venv/bin/python")


def spawn_comfy_local():
    """LOCAL: start ComfyUI so it outlives this request AND this server's restarts. Preferred: run the PredatorComfyUI scheduled
    task (its own session/job, so it is not killed when PredatorControl is restarted). Fallback: a detached child that breaks
    away from the task's job object."""
    r = local_run(f"schtasks /run /tn {COMFY_TASK}", timeout=20)
    if r.returncode == 0:
        return
    log_f = open(Path(COMFY_DIR) / "server.log", "ab")
    args = [comfy_python(), "main.py", "--listen", "127.0.0.1", "--port", "8188", "--disable-metadata", "--cache-lru", "40"]
    base = 0x00000008 | 0x00000200 | CREATE_NO_WINDOW  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | no window
    for flags in (base | 0x01000000, base):  # CREATE_BREAKAWAY_FROM_JOB first; some jobs forbid it
        try:
            subprocess.Popen(args, cwd=COMFY_DIR, stdout=log_f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
            return
        except OSError:
            continue
    raise RuntimeError("could not start ComfyUI (task and direct start both failed)")


def spawn_comfy():
    """Start ComfyUI held by a detached local ssh session (Windows tasks don't start it when nobody is logged on)."""
    if LOCAL:
        return spawn_comfy_local()
    subprocess.Popen(SSH + [HOST, COMFY_START_CMD], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


def imagine_jobs():
    """Imagine's public job counts, or None when it is not reachable."""
    try:
        with urllib.request.urlopen(IMAGINE_URL + "/api/jobs", timeout=2 if LOCAL else 3) as r:
            js = json.load(r).get("jobs") or []
    except Exception:
        return None
    js = [j for j in js if j.get("kind") != "chat"]
    return {"running": sum(1 for j in js if j.get("status") == "running"),
            "pending": sum(1 for j in js if j.get("status") in ("queued", "paused"))}


# ---- log: RAM + small file; never prompts or text -------------------------------------------------------------------
def log(act, detail, result, forced=False):
    e = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "action": act, "detail": str(detail)[:60], "result": str(result)[:160], "override": bool(forced)}
    with lock:
        log_ram.append(e)
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            f = DATA / "actions.log"
            with f.open("a") as fh:
                fh.write(json.dumps(e) + "\n")
            if f.stat().st_size > 200_000:
                keep = f.read_text().splitlines()[-300:]
                f.write_text("\n".join(keep) + "\n")
        except OSError:
            pass


def load_log():
    try:
        for line in (DATA / "actions.log").read_text().splitlines()[-200:]:
            log_ram.append(json.loads(line))
    except Exception:
        pass


# ---- reading Predator -----------------------------------------------------------------------------------------------
def catalog_full():
    """Predator's whole llama_models.json as a dict (every field), or None when it cannot be read."""
    try:
        d = json.loads(remote(CATALOG_CMD, timeout=20).lstrip("\ufeff"))
        return d if isinstance(d, dict) and isinstance(d.get("models"), list) else None
    except Exception:
        return None


def models_catalog():
    """[{key, name, vram_mb}] from Predator's own llama_models.json (cached 10 min)."""
    if time.time() - catalog_cache["t"] < 600 and catalog_cache["v"]:
        return catalog_cache["v"]
    d = catalog_full() or {}
    out = []
    for m in d.get("models", []):
        if KEY_RE.match(str(m.get("key", ""))):
            out.append({"key": m["key"], "name": str(m.get("name", m["key"]))[:80], "vram_mb": m.get("requiredVramMB"),
                        "default": m["key"] == d.get("defaultModel")})
    if out:
        catalog_cache.update(t=time.time(), v=out)
    return catalog_cache["v"]


def read_mode():
    """mode.ps1 -Status as a dict (read-only), or None when Predator cannot be reached."""
    try:
        raw = remote(MODE_PS, timeout=40).strip().splitlines()
        return json.loads(raw[-1]) if raw else None
    except Exception:
        return None


def gpu_numbers(d):
    try:
        used, total = (int(float(x)) for x in str(d.get("gpu_mem_mb", "")).split("/"))
    except Exception:
        used = total = None
    util = re.sub(r"[^0-9]", "", str(d.get("gpu_util", ""))) or None
    temp = d.get("gpu_temp_c")
    return {"used_mb": used, "total_mb": total, "util_pct": int(util) if util else None,
            "temp_c": int(temp) if str(temp).strip().isdigit() else None}


def build_status():
    d = read_mode()
    cat = models_catalog()
    base = {"models": cat, "action": dict(action), "imagine": imagine_jobs(), "time": time.strftime("%H:%M:%S")}
    if d is None:
        return {**base, "reachable": False, "mode": "unreachable", "busy": {"busy": False, "reasons": []}}
    running = d.get("models_running") or []
    running = [running] if isinstance(running, str) else [str(x)[:80] for x in running]
    comfy = bool(d.get("image_engine"))
    queue = None
    if comfy:
        try:
            o = remote(QUEUE_CMD, timeout=20).strip()
            queue = int(o) if o.isdigit() else None
        except Exception:
            queue = None
    im = base["imagine"]
    reasons = []
    if im and im["running"]:
        reasons.append(f"Imagine has {im['running']} picture job(s) running")
    if im and im["pending"]:
        reasons.append(f"Imagine has {im['pending']} picture job(s) waiting")
    if queue:
        reasons.append(f"ComfyUI has {queue} job(s) in its queue")
    model = d.get("set_model")
    name = next((m["name"] for m in cat if m["key"] == model), None)
    return {**base, "reachable": True, "mode": d.get("mode") or "unknown", "set_mode": d.get("set_mode"), "set_model": model,
            "model_name": name, "set_at": d.get("set_at"), "comfy_running": comfy, "comfy_queue": queue,
            "llama_running": running, "gpu": gpu_numbers(d), "power_plan": d.get("power_plan"), "pending": bool(d.get("pending")),
            "busy": {"busy": bool(reasons), "reasons": reasons}}


def status(fresh=False):
    """Status, at most one real Predator read at a time and cached for 2.5 s."""
    with status_lock:
        if fresh or time.time() - cache["t"] > 2.5 or cache["v"] is None:
            cache.update(v=build_status(), t=time.time())
        v = dict(cache["v"])
    v["action"] = dict(action)
    return v


# ---- actions --------------------------------------------------------------------------------------------------------
class Refuse(Exception):
    def __init__(self, msg, code=409, can_override=False):
        super().__init__(msg)
        self.msg, self.code, self.can_override = msg, code, can_override


def set_stage(s):
    action["stage"] = s


def poll_mode(want_mode, want_model, seconds):
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(POLL_SECONDS)
        d = read_mode()
        if d and not d.get("pending") and d.get("set_mode") == want_mode and (want_model is None or d.get("set_model") == want_model):
            return True
    return False


POLL_SECONDS = 5


def comfy_count():
    o = remote(COMFY_COUNT_CMD, timeout=25).strip()
    return int(o) if o.isdigit() else None


def a_switch(mode, model):
    req = f"llm:{model}" if mode == "llm" else mode
    set_stage(f"asking Predator to switch to {req}")
    remote(rf'cmd /c "echo {req}>{PRED}\mode_request.txt && schtasks /run /tn HermesMode"', timeout=30)
    set_stage("switching (an LLM can take a few minutes to load)")
    if not poll_mode(mode, model if mode == "llm" else None, 300 if mode == "llm" else 150):
        raise RuntimeError("Predator did not confirm the switch in time; check its mode before trusting it")


def a_comfy_stop():
    set_stage("stopping ComfyUI")
    remote(COMFY_STOP_CMD, timeout=30)
    time.sleep(min(2, POLL_SECONDS))
    if comfy_count() != 0:
        raise RuntimeError("ComfyUI could not be confirmed stopped")


def a_comfy_start():
    set_stage("starting ComfyUI (about a minute)")
    if comfy_count() == 0:
        spawn_comfy()
    for _ in range(60):
        time.sleep(POLL_SECONDS)
        o = remote(QUEUE_CMD, timeout=20).strip()
        if o.isdigit():
            return
    raise RuntimeError("ComfyUI did not answer in time; see D:\\ComfyUI\\server.log on Predator")


def a_llama_stop():
    set_stage("stopping llama-server")
    remote(LLAMA_STOP_CMD, timeout=30)
    time.sleep(min(2, POLL_SECONDS))
    d = read_mode()
    if d is None or d.get("models_running"):
        raise RuntimeError("llama-server could not be confirmed stopped")


def a_comfy_restart():
    a_comfy_stop()
    a_comfy_start()


def run_action(name, detail, fn, forced):
    try:
        fn()
        action.update(ok=True, error="", stage="done")
        log(name, detail, "ok", forced)
    except Exception as e:
        action.update(ok=False, error=str(e)[:200], stage="failed")
        log(name, detail, "FAILED: " + str(e)[:120], forced)
    finally:
        action["running"] = False
        cache["t"] = 0.0


def start_action(body):
    """Validate and refuse with a plain reason, else run the one fixed action in a thread. Returns the accepted record."""
    name = body.get("action")
    override = body.get("override") is True
    if body.get("confirm") is not True:
        raise Refuse("This needs an explicit confirmation.", 400)
    if name not in ("switch", "comfy_start", "comfy_stop", "comfy_restart", "llama_stop"):
        raise Refuse("Unknown action.", 400)
    mode = model = None
    if name == "switch":
        mode = body.get("mode")
        if mode not in MODES:
            raise Refuse("Unknown mode.", 400)
        if mode == "llm":
            model = body.get("model")
            if not isinstance(model, str) or not KEY_RE.match(model) or model not in [m["key"] for m in models_catalog()]:
                raise Refuse("Unknown model key (not in Predator's model list).", 400)
    with lock:
        if any(j["running"] and j["domain"] in ("predator", "image-model") for j in system_controls.jobs()["jobs"]):
            raise Refuse("A Predator system action is still running.")
        if action["running"]:
            raise Refuse(f"Another action is still running ({action['name']}: {action['stage']}).")
        st = status(fresh=True)
        if not st["reachable"]:
            raise Refuse("Predator cannot be reached (asleep, off or no tailnet).", 503)
        if st["pending"]:
            raise Refuse("A mode switch is already pending on Predator (someone else asked for one); wait a minute.")
        if name == "switch" and st["mode"] == mode and (mode != "llm" or st.get("set_model") == model):
            raise Refuse(f"Predator is already in {mode} mode.")
        if name == "comfy_start" and st["comfy_running"]:
            raise Refuse("ComfyUI is already running.")
        if name in ("comfy_stop", "comfy_restart") and not st["comfy_running"]:
            raise Refuse("ComfyUI is not running.")
        if name == "llama_stop" and not st["llama_running"]:
            raise Refuse("No llama-server is running.")
        if name == "comfy_start" and (st["llama_running"] or st["mode"] in ("llm", "gaming") or st["set_mode"] in ("llm", "gaming")):
            raise Refuse(f"Predator is in {st['mode']} mode or a model is running: ComfyUI would share the 6 GB GPU. "
                         "Switch to image mode first.")
        if name == "comfy_restart" and st["llama_running"]:
            raise Refuse("A model is running next to ComfyUI: stop it first.")
        if st["busy"]["busy"] and not override:
            raise Refuse("A picture job is running or waiting: " + "; ".join(st["busy"]["reasons"])
                         + ". Wait for it, or tick 'I know a job is running'.", 409, True)
        detail = f"{mode}{':' + model if model else ''}" if name == "switch" else ""
        fn = {"switch": lambda: a_switch(mode, model), "comfy_start": a_comfy_start, "comfy_stop": a_comfy_stop,
              "comfy_restart": a_comfy_restart, "llama_stop": a_llama_stop}[name]
        action.update(name=name + (f" {detail}" if detail else ""), stage="starting", error="", ok=None, running=True, since=time.time())
        log(name, detail, "started", st["busy"]["busy"] and override)
        threading.Thread(target=run_action, args=(name, detail, fn, st["busy"]["busy"] and override), daemon=True).start()
    return {"accepted": True, "action": action["name"]}


# ---- model management: the catalog, the GGUF files, downloads -------------------------------------------------------
# The catalog's args end up in a .bat that switch_mode.ps1 runs as SYSTEM, so every field is checked against a strict
# pattern, the model file and llama-server build must be ones actually found on Predator, and nothing a user types can
# reach a shell unquoted. PowerShell scripts are sent with -EncodedCommand (no quoting games over ssh/cmd).
ROOTS = (r"C:\Users\dcmini" + "\\", r"D:\models" + "\\")
DL_ROOT = r"D:\models"
DEFAULT_EXE = r"C:\Users\dcmini\turboquant-cuda\build\bin\llama-server.exe"  # switch_mode.ps1's default
UPLOAD = r"C:\Users\dcmini\predator-control-catalog.upload.json"  # staged here, then copied over the catalog
NEW_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
ARGS_RE = re.compile(r"^[A-Za-z0-9 ._=:,/\\+-]{0,400}$")  # no quotes, %, &, |, <, >, ^, ( )
PATH_RE = re.compile(r"^[A-Za-z]:\\[A-Za-z0-9 ._\\()+-]+$")
LOG_RE = re.compile(r"^[A-Za-z]:\\[A-Za-z0-9 ._\\()+-]+\.log$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
HF_RE = re.compile(r"^https://huggingface\.co/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/(?:resolve|blob)/([A-Za-z0-9._-]+)/"
                   r"((?:[A-Za-z0-9._-]+/)*([A-Za-z0-9._-]+\.gguf))(?:\?download=true)?$")
# set by switch_mode.ps1 itself, or would serve/write/fetch files outside what this page manages
FORBIDDEN_ARGS = {"-m", "--model", "--host", "--port", "--alias", "--log-file", "--path", "--slot-save-path", "-mu",
                  "--model-url", "-hf", "-hfr", "--hf-repo", "-hff", "--hf-file", "-hft", "--hf-token", "-dr", "--docker-repo",
                  "--ssl-key-file", "--ssl-cert-file", "--api-key-file"}
SCAN_PS = r"""$ProgressPreference='SilentlyContinue'
$r = foreach ($root in 'C:\Users\dcmini','D:\models') { Get-ChildItem -Path $root -Recurse -File -Include *.gguf,*.gguf.part,llama-server.exe -ErrorAction SilentlyContinue }
$f = @($r | Where-Object { $_.FullName -notmatch '\\(ggml-vocab|AppData|ComfyUI)' } | ForEach-Object { [pscustomobject]@{p=$_.FullName;s=$_.Length;t=$_.LastWriteTime.ToString('yyyy-MM-dd HH:mm')} })
$d = @(Get-PSDrive C,D -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{n=$_.Name;free=$_.Free} })
[pscustomobject]@{files=$f;disks=$d} | ConvertTo-Json -Compress -Depth 4"""
# (a directory listing shows 0 bytes for a file curl still holds open, so the size is read through a handle)
DL_PS = r"""$ProgressPreference='SilentlyContinue'
function Size($f) { try { $h = [IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite'); $n = $h.Length; $h.Close(); $n } catch { $f.Length } }
$p = @(Get-ChildItem -Path 'D:\models' -Recurse -File -Filter *.gguf.part -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{p=$_.FullName;s=(Size $_)} })
$c = @(Get-CimInstance Win32_Process -Filter "Name='curl.exe'" | ForEach-Object { "$($_.CommandLine)" })
[pscustomobject]@{parts=$p;curl=$c} | ConvertTo-Json -Compress -Depth 4"""
scan_cache = {"t": 0.0, "v": None}
downloads = {}  # part path -> {"url", "total"}; persisted in DATA/downloads.json


def ps(script):
    return "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16-le")).decode()


VOICE_STATUS_PS = r"""$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$t=Get-ScheduledTask -TaskName QwenVoiceStudio
$c=New-Object Net.Sockets.TcpClient
try {$up=$c.ConnectAsync('127.0.0.1',7865).Wait(700) -and $c.Connected} catch {$up=$false} finally {$c.Dispose()}
[pscustomobject]@{running=[bool]$up;enabled=[bool]$t.Settings.Enabled;state=$t.State.ToString()}|ConvertTo-Json -Compress
"""
voice_lock = threading.Lock()
voice_cache = {'t': 0, 'v': None}


def voice_status(fresh=False):
    if not fresh and voice_cache['v'] is not None and time.time()-voice_cache['t'] < 4:
        return voice_cache['v']
    try:
        raw = remote(ps(VOICE_STATUS_PS), timeout=15)
        value = json.loads(raw.strip().splitlines()[-1])
    except Exception:
        value = {'running': None, 'enabled': None, 'state': 'Unknown'}
    voice_cache.update(t=time.time(), v=value)
    return value


def voice_control(body):
    if body.get('confirm') is not True:
        raise Refuse('Confirm the Voice Studio action.', 400)
    op = body.get('action')
    if op not in ('start', 'stop', 'restart'):
        raise Refuse('Unknown Voice Studio action.', 400)
    if not voice_lock.acquire(blocking=False):
        raise Refuse('A Voice Studio action is already running.')
    try:
        script = "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'\n"
        if op in ('stop', 'restart'):
            script += r"""
Stop-ScheduledTask -TaskName QwenVoiceStudio
Disable-ScheduledTask -TaskName QwenVoiceStudio|Out-Null
for($i=0;$i -lt 20;$i++){if((Get-ScheduledTask QwenVoiceStudio).State -ne 'Running'){break};Start-Sleep -Milliseconds 300}
$pids=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'"|Where-Object {$_.CommandLine -match 'QwenVoice\\private_studio.py|QwenVoice\\app.py'}|ForEach-Object {$_.ProcessId})
foreach($targetPid in $pids){if(Get-Process -Id $targetPid -ErrorAction SilentlyContinue){$ErrorActionPreference='Continue';& taskkill /PID $targetPid /T /F 2>$null|Out-Null;$ErrorActionPreference='Stop'}}
"""
        if op in ('start', 'restart'):
            script += r"""
Enable-ScheduledTask -TaskName QwenVoiceStudio|Out-Null
Start-ScheduledTask -TaskName QwenVoiceStudio
for($i=0;$i -lt 30;$i++){$c=New-Object Net.Sockets.TcpClient;try{$up=$c.ConnectAsync('127.0.0.1',7865).Wait(300) -and $c.Connected}catch{$up=$false}finally{$c.Dispose()};if($up){break};Start-Sleep -Milliseconds 500}
"""
        script += "\n" + VOICE_STATUS_PS
        raw = remote(ps(script), timeout=45)
        try:
            result = json.loads(raw.strip().splitlines()[-1])
        except Exception:
            raise Refuse('Voice Studio action could not be verified. Check the scheduled task.', 502)
        expected = op != 'stop'
        if result.get('running') is not expected:
            raise Refuse('Voice Studio did not reach the requested state. Check its service log.', 502)
        voice_cache.update(t=time.time(), v=result)
        log('voice_' + op, '', 'completed')
        return result
    finally:
        voice_lock.release()


def ps_quote(s):
    """A PowerShell single-quoted literal. Callers have already pattern-checked s (no quotes allowed anyway)."""
    return "'" + str(s).replace("'", "''") + "'"


def put_text(text, path):
    """Put a file on Predator: scp (ssh mode) or a plain write (LOCAL mode). Tests replace this."""
    if LOCAL:
        Path(path).write_text(text, encoding="utf-8")
        return
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        tmp = fh.name
    try:
        r = subprocess.run(["scp", "-q", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", tmp,
                            f"{HOST}:{path.replace(chr(92), '/')}"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError("upload to Predator failed: " + (r.stderr or "").strip()[:120])
    finally:
        os.unlink(tmp)


def jload(out):
    out = (out or "").strip().lstrip("\ufeff")
    return json.loads(out.splitlines()[-1]) if out else None


def as_list(x):
    return [] if x is None else x if isinstance(x, list) else [x]


def under_roots(p):
    return any(p.lower().startswith(r.lower()) for r in ROOTS)


def scan_files(fresh=False):
    """GGUF files, llama-server builds and free disk on Predator (a ~10 s walk, cached 2 min)."""
    if fresh or scan_cache["v"] is None or time.time() - scan_cache["t"] > 120:
        try:
            d = jload(remote(ps(SCAN_PS), timeout=90)) or {}
        except Exception:
            d = None
        if d is not None:
            files, servers = [], []
            for f in as_list(d.get("files")):
                p = str(f.get("p", ""))
                if not under_roots(p):
                    continue
                if p.lower().endswith("llama-server.exe"):
                    if PATH_RE.match(p):
                        servers.append(p)
                    continue
                low = p.lower().rsplit("\\", 1)[-1]
                kind = "part" if low.endswith(".part") else "mmproj" if low.startswith("mmproj") else "model"
                files.append({"path": p, "size": f.get("s"), "time": f.get("t"), "kind": kind, "name_ok": bool(PATH_RE.match(p))})
            disks = {str(x.get("n")): x.get("free") for x in as_list(d.get("disks"))}
            scan_cache.update(t=time.time(), v={"files": files, "servers": sorted(servers), "disks": disks,
                                                "scanned_at": time.strftime("%H:%M:%S")})
    return scan_cache["v"]


def load_downloads():
    try:
        downloads.update(json.loads((DATA / "downloads.json").read_text()))
    except Exception:
        pass


def save_downloads():
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "downloads.json").write_text(json.dumps(downloads))
    except OSError:
        pass


def download_status():
    """Unfinished downloads: each .gguf.part under D:\\models, its size, the expected size, and whether curl is still on it."""
    try:
        d = jload(remote(ps(DL_PS), timeout=40)) or {}
    except Exception:
        return None
    curl = [c.lower() for c in as_list(d.get("curl"))]
    out = []
    for p in as_list(d.get("parts")):
        path = str(p.get("p", ""))
        meta = downloads.get(path, {})
        out.append({"part": path, "file": path[:-5], "size": p.get("s"), "total": meta.get("total"), "url": meta.get("url"),
                    "running": any(path.lower() in c for c in curl)})
    return out


def model_view(fresh=False):
    cat = catalog_full()
    sc = scan_files(fresh) or {"files": [], "servers": [], "disks": {}, "scanned_at": None}
    used = {}
    for m in (cat or {}).get("models", []):
        for p in [m.get("path", "")] + [a for a in str(m.get("args", "")).split() if a.lower().endswith(".gguf")]:
            used.setdefault(str(p).lower(), []).append(m.get("key"))
    files = [{**f, "used_by": used.get(f["path"].lower(), [])} for f in sc["files"]]
    return {"catalog": cat, "files": files, "servers": sc["servers"], "default_exe": DEFAULT_EXE, "disks": sc["disks"],
            "scanned_at": sc["scanned_at"]}


def hf_total(url):
    """Size in bytes of a Hugging Face file (one-byte range request), or None."""
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "predator-control"})
        with urllib.request.urlopen(req, timeout=20) as r:
            cr = r.headers.get("Content-Range", "")
            if "/" in cr:
                return int(cr.rsplit("/", 1)[1])
            return int(r.headers.get("Content-Length") or 0) or None
    except Exception:
        return None


def check_entry(e, cat, sc, new):
    """A clean catalog entry from the page's form, or Refuse with the first problem in plain words."""
    if not isinstance(e, dict):
        raise Refuse("Bad model entry.", 400)
    key = str(e.get("key", "")).strip()
    if new and not NEW_KEY_RE.match(key):
        raise Refuse("Key: lowercase letters, digits, dot, dash or underscore (max 40), e.g. qwen9b-q4.", 400)
    keys = [m.get("key") for m in cat["models"]]
    if new and key.lower() in [str(k).lower() for k in keys]:
        raise Refuse(f"There is already a model with key '{key}'.", 400)
    if not new and key not in keys:
        raise Refuse("That model is no longer in the catalog (reload).", 409)
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(e.get("name", ""))).strip()
    if not 1 <= len(name) <= 80:
        raise Refuse("Name: 1 to 80 characters.", 400)
    path = str(e.get("path", ""))
    if path not in [f["path"] for f in sc["files"] if f["kind"] == "model" and f["name_ok"]]:
        raise Refuse("Model file: pick one of the .gguf files found on Predator (reload the file list if it is new).", 400)
    exe = str(e.get("serverExe", ""))
    if exe not in sc["servers"]:
        raise Refuse("llama-server build: pick one of the builds found on Predator.", 400)
    args = " ".join(str(e.get("args", "")).split())
    if not ARGS_RE.match(args):
        raise Refuse("Arguments: only letters, digits, spaces and . _ = : , / \\ + - (no quotes or shell characters).", 400)
    bad = [a for a in args.split() if a.split("=", 1)[0] in FORBIDDEN_ARGS]
    if bad:
        raise Refuse(f"Arguments: {bad[0]} is set by Predator itself or not allowed here.", 400)
    for a in args.split():
        if a.lower().endswith(".gguf") and a not in [f["path"] for f in sc["files"] if f["name_ok"]]:
            raise Refuse(f"Arguments: {a} is not a .gguf file found on Predator.", 400)
    try:
        port = int(e.get("port"))
        vram = int(e.get("requiredVramMB") or 0)
    except (TypeError, ValueError):
        raise Refuse("Port and VRAM must be whole numbers.", 400)
    if not 8080 <= port <= 8099:
        raise Refuse("Port: 8080 to 8099.", 400)
    if not 0 <= vram <= 6144:
        raise Refuse("VRAM needed: 0 to 6144 MB (the card has 6 GB; with --cpu-moe count only what stays on the GPU).", 400)
    alias = str(e.get("alias", "")).strip() or key
    if not ALIAS_RE.match(alias):
        raise Refuse("Alias: letters, digits, dot, dash or underscore (max 60).", 400)
    notes = re.sub(r"[\x00-\x1f\x7f]", " ", str(e.get("notes", ""))).strip()[:600]
    return {"key": key, "name": name, "path": path, "port": port, "alias": alias, "args": args, "requiredVramMB": vram,
            "serverExe": exe, "notes": notes}


def write_catalog(cat):
    """Back up the live catalog next to itself (newest 10 kept), then overwrite it in place (keeps its ACL)."""
    text = json.dumps(cat, indent=2, ensure_ascii=False) + "\n"
    put_text(text, UPLOAD)
    out = remote(ps(f"""$ErrorActionPreference='Stop'
$up={ps_quote(UPLOAD)}; $dst={ps_quote(CATALOG_PATH)}
try {{
$t=[IO.File]::ReadAllText($up)
$null = $t | ConvertFrom-Json
Copy-Item -LiteralPath $dst -Destination ($dst + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
[IO.File]::WriteAllText($dst, $t)
Remove-Item -LiteralPath $up
Get-ChildItem -Path (Split-Path $dst) -Filter 'llama_models.json.bak-*' | Sort-Object Name -Descending | Select-Object -Skip 10 | Remove-Item
'WROTE' }} catch {{ 'ERR ' + $_.Exception.Message }}"""), timeout=60)
    if "WROTE" not in (out or ""):
        if LOCAL and "denied" in (out or "").lower():
            raise Refuse("This copy of the page runs on Predator without admin rights, so it cannot change the catalog: "
                         "use the dcmini page (dcmini-1...:8793) for that.", 403)
        raise Refuse("Predator did not confirm the catalog write; nothing was changed.", 502)
    if catalog_full() != json.loads(text):
        raise Refuse("The catalog on Predator does not match what was written; check it (a .bak copy was kept).", 502)
    catalog_cache["t"] = 0.0
    cache["t"] = 0.0


def model_op(body):
    """One model-management change, done synchronously under the action lock. Returns a small result dict."""
    op = body.get("op")
    if body.get("confirm") is not True:
        raise Refuse("This needs an explicit confirmation.", 400)
    if op not in ("add", "edit", "delete", "default", "delete_file", "download", "cancel_download", "resume_download"):
        raise Refuse("Unknown model operation.", 400)
    with lock:
        if action["running"]:
            raise Refuse(f"Another action is still running ({action['name']}: {action['stage']}).")
        st = status(fresh=True)
        if not st["reachable"]:
            raise Refuse("Predator cannot be reached (asleep, off or no tailnet).", 503)
        if st["pending"]:
            raise Refuse("A mode switch is pending on Predator; wait a minute.")
        if op in ("download", "cancel_download", "resume_download"):
            return download_op(op, body)
        cat = catalog_full()
        if cat is None:
            raise Refuse("Could not read Predator's model catalog.", 502)
        loaded = st.get("set_model") if st.get("llama_running") else None
        if op in ("add", "edit"):
            sc = scan_files(fresh=False) or {"files": [], "servers": []}
            e = check_entry(body.get("entry"), cat, sc, op == "add")
            if op == "add":
                e["log"] = rf"C:\Users\dcmini\llama-server-{e['key']}.log"
                cat["models"].append({k: v for k, v in e.items() if v != "" or k == "args"})
            else:
                i = next(i for i, m in enumerate(cat["models"]) if m.get("key") == e["key"])
                old = dict(cat["models"][i])
                old.update({k: v for k, v in e.items() if k != "notes"})
                if e["notes"]:
                    old["notes"] = e["notes"]
                else:
                    old.pop("notes", None)
                cat["models"][i] = old
            write_catalog(cat)
            log("model_" + op, e["key"], "ok")
            return {"ok": True, "note": "Saved. A model that is loaded now keeps its old settings until it is loaded again."
                    if loaded == e["key"] else "Saved."}
        if op in ("delete", "default"):
            key = body.get("key")
            if key not in [m.get("key") for m in cat["models"]]:
                raise Refuse("Unknown model key.", 400)
            if op == "default":
                cat["defaultModel"] = key
            else:
                if loaded == key:
                    raise Refuse("That model is loaded right now: switch to another mode first.")
                if len(cat["models"]) == 1:
                    raise Refuse("It is the only model in the catalog; add another one first.")
                cat["models"] = [m for m in cat["models"] if m.get("key") != key]
                if cat.get("defaultModel") == key:
                    cat["defaultModel"] = cat["models"][0]["key"]
            write_catalog(cat)
            log("model_" + op, key, "ok")
            return {"ok": True, "note": "Removed from the catalog (the .gguf file is kept)." if op == "delete" else "Default set."}
        # delete_file
        path = body.get("path")
        sc = scan_files(fresh=True) or {"files": []}
        f = next((f for f in sc["files"] if f["path"] == path), None)
        if not isinstance(path, str) or f is None or not f["name_ok"] or not under_roots(path):
            raise Refuse("That file is not one of the .gguf files found on Predator.", 400)
        users = [m.get("key") for m in cat["models"]
                 if str(m.get("path", "")).lower() == path.lower() or path.lower() in str(m.get("args", "")).lower()]
        if users:
            raise Refuse(f"Model(s) {', '.join(users)} use this file: delete them from the catalog first.")
        if f["kind"] == "part" and any(d["running"] and d["part"] == path for d in (download_status() or [])):
            raise Refuse("That download is still running: cancel it instead.")
        out = remote(ps(f"$ErrorActionPreference='Stop'; Remove-Item -LiteralPath {ps_quote(path)}; 'DELETED'"), timeout=60)
        scan_cache["t"] = 0.0
        if "DELETED" not in (out or ""):
            raise Refuse("Predator did not confirm the delete.", 502)
        downloads.pop(path, None)
        save_downloads()
        log("delete_file", path.rsplit("\\", 1)[-1], "ok")
        return {"ok": True, "note": f"Deleted {path.rsplit(chr(92), 1)[-1]} (freed {round((f['size'] or 0) / 1e9, 1)} GB)."}


def start_curl(url, part, dest, resume):
    """curl in its own process via WMI, so it outlives this ssh call (and this server). Renames .part on success."""
    cmd = (f'cmd.exe /c curl.exe -L --fail -s -S {"-C - " if resume else ""}-o "{part}" "{url}" '
           f'&& move /y "{part}" "{dest}" >nul')
    folder = dest.rsplit("\\", 1)[0]
    out = remote(ps(f"""$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Force -Path {ps_quote(folder)} | Out-Null
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine={ps_quote(cmd)}}}
'STARTED ' + $r.ReturnValue"""), timeout=60)
    if "STARTED 0" not in (out or ""):
        raise Refuse("Predator could not start the download: " + (out or "").strip()[-120:], 502)


def download_op(op, body):
    if op == "download":
        url = str(body.get("url", "")).strip()
        m = HF_RE.match(url)
        if not m:
            raise Refuse("Paste a Hugging Face file link ending in .gguf, e.g. "
                         "https://huggingface.co/<user>/<repo>/resolve/main/<file>.gguf", 400)
        url = f"https://huggingface.co/{m[1]}/{m[2]}/resolve/{m[3]}/{m[4]}"
        folder = str(body.get("folder", "")).strip() or m[2]
        if not FOLDER_RE.match(folder):
            raise Refuse("Folder: letters, digits, dot, dash or underscore (max 60).", 400)
        dest = rf"{DL_ROOT}\{folder}\{m[5]}"
        part = dest + ".part"
        sc = scan_files(fresh=True) or {"files": [], "disks": {}}
        if any(f["path"].lower() in (dest.lower(), part.lower()) for f in sc["files"]):
            raise Refuse(f"{dest} (or an unfinished download of it) already exists on Predator.")
        total = hf_total(url)
        if not total:
            raise Refuse("Could not reach that file on Hugging Face (wrong link, or a gated model that needs a login).", 400)
        free = sc["disks"].get("D")
        if free is not None and total > free - 5_000_000_000:
            raise Refuse(f"Not enough room on D: ({round(free / 1e9, 1)} GB free, file is {round(total / 1e9, 1)} GB, "
                         "5 GB kept spare).")
        start_curl(url, part, dest, False)
        downloads[part] = {"url": url, "total": total}
        save_downloads()
        log("download", m[5], f"started ({round(total / 1e9, 1)} GB)")
        return {"ok": True, "note": f"Downloading {m[5]} ({round(total / 1e9, 1)} GB) to {dest}."}
    part = body.get("part")
    cur = next((d for d in (download_status() or []) if d["part"] == part), None)
    if not isinstance(part, str) or cur is None:
        raise Refuse("No such unfinished download.", 400)
    if op == "resume_download":
        meta = downloads.get(part)
        if cur["running"]:
            raise Refuse("It is still running.")
        if not meta or not HF_RE.match(meta.get("url", "")):
            raise Refuse("This page did not start that download, so it does not know the link: delete it and start again.")
        start_curl(meta["url"], part, part[:-5], True)
        log("download_resume", part.rsplit("\\", 1)[-1][:-5], "ok")
        return {"ok": True, "note": "Resumed."}
    out = remote(ps(f"""$p={ps_quote(part)}
Get-CimInstance Win32_Process -Filter "Name='curl.exe'" | Where-Object {{ "$($_.CommandLine)".ToLower().Contains($p.ToLower()) }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}
Start-Sleep -Seconds 1
Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $p) {{ 'LEFT' }} else {{ 'CANCELLED' }}"""), timeout=60)
    if "CANCELLED" not in (out or ""):
        raise Refuse("Predator did not confirm the cancel.", 502)
    downloads.pop(part, None)
    save_downloads()
    scan_cache["t"] = 0.0
    log("download_cancel", part.rsplit("\\", 1)[-1][:-5], "ok")
    return {"ok": True, "note": "Cancelled and the partial file deleted."}


# ---- GPU processes --------------------------------------------------------------------------------------------------
# nvidia-smi on WDDM reports N/A per-process memory, so Windows' own GPU counters are used (dedicated memory + engine %).
GPU_PS = r"""$ProgressPreference='SilentlyContinue'
$m=@{}; (Get-Counter '\GPU Process Memory(*)\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples | ForEach-Object { if ($_.InstanceName -match 'pid_(\d+)_') { $m[[int]$Matches[1]] += $_.CookedValue } }
$u=@{}; (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue).CounterSamples | ForEach-Object { if ($_.InstanceName -match 'pid_(\d+)_') { $u[[int]$Matches[1]] += $_.CookedValue } }
@($m.Keys | ForEach-Object { $p = Get-CimInstance Win32_Process -Filter "ProcessId=$_"; if ($p) {
  [pscustomobject]@{pid=[int]$_; name="$($p.Name)"; mb=[math]::Round($m[$_]/1MB); util=[math]::Round([double]$u[$_]); cmd="$($p.CommandLine)"} } }) | ConvertTo-Json -Compress"""
# never offered for termination: killing these crashes or logs out Windows
PROTECTED = {"system", "csrss.exe", "dwm.exe", "winlogon.exe", "wininit.exe", "services.exe", "lsass.exe", "smss.exe",
             "svchost.exe", "fontdrvhost.exe", "registry", "memory compression"}


def gpu_kind(name, cmd):
    n = name.lower()
    if n in PROTECTED:
        return "system"
    if n == "python.exe" and "main.py --listen" in cmd:
        return "comfyui"
    if n in ("llama-server.exe", "llama-bench.exe", "ollama.exe", "ollama app.exe"):
        return "model"
    return "app"


def gpu_procs():
    """Processes holding GPU memory or using the GPU, biggest first, or None when Predator cannot be reached."""
    try:
        d = jload(remote(ps(GPU_PS), timeout=60))
    except Exception:
        return None
    out = []
    for p in as_list(d):
        try:
            pid, mb, util = int(p.get("pid")), int(p.get("mb") or 0), int(p.get("util") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 4 or (mb < 1 and util < 1):
            continue
        name, cmd = str(p.get("name", ""))[:80], str(p.get("cmd", ""))
        out.append({"pid": pid, "name": name, "mem_mb": mb, "util_pct": min(util, 100), "cmd": cmd[:240], "kind": gpu_kind(name, cmd)})
    return sorted(out, key=lambda x: (-x["mem_mb"], -x["util_pct"]))


def kill_proc(body):
    """Terminate one GPU process. It must be in a fresh GPU list with the same pid AND name (no stale-pid kills)."""
    if body.get("confirm") is not True:
        raise Refuse("This needs an explicit confirmation.", 400)
    pid, name = body.get("pid"), body.get("name")
    if not isinstance(pid, int) or not isinstance(name, str):
        raise Refuse("Bad request.", 400)
    with lock:
        if action["running"]:
            raise Refuse(f"Another action is still running ({action['name']}: {action['stage']}).")
        procs = gpu_procs()
        if procs is None:
            raise Refuse("Predator cannot be reached.", 503)
        p = next((p for p in procs if p["pid"] == pid and p["name"] == name), None)
        if p is None:
            raise Refuse("That process is no longer using the GPU (or its id changed); refresh the list.", 409)
        if p["kind"] == "system":
            raise Refuse(f"{name} is part of Windows; ending it would crash or log out Predator.", 403)
        if p["kind"] == "comfyui" and body.get("override") is not True:
            st = status(fresh=True)
            if st["busy"]["busy"]:
                raise Refuse("A picture job is running or waiting: " + "; ".join(st["busy"]["reasons"])
                             + ". Tick 'I know a job is running' to end it anyway.", 409)
        out = remote(ps(f"""try {{ Stop-Process -Id {pid} -Force -ErrorAction Stop; Start-Sleep -Seconds 1
if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'STILL' }} else {{ 'KILLED' }} }} catch {{ 'ERR ' + $_.Exception.Message }}"""),
                     timeout=40)
        cache["t"] = 0.0
        if "KILLED" not in (out or ""):
            log("kill", f"{name} {pid}", "FAILED")
            raise Refuse(f"Could not end {name}: " + (out or "no answer").strip()[-150:], 502)
        log("kill", f"{name} {pid}", "ok", body.get("override") is True)
        note = " If it was a model, the mode will show idle; HermesMode does not restart it." if p["kind"] == "model" else ""
        return {"ok": True, "note": f"Ended {name} (pid {pid}, was using {p['mem_mb']} MB).{note}"}


def model_log(key):
    """Last 80 lines of one catalog model's llama-server log (path from the catalog, pattern-checked)."""
    cat = catalog_full() or {"models": []}
    m = next((m for m in cat["models"] if m.get("key") == key), None)
    if m is None:
        raise Refuse("Unknown model key.", 400)
    lp = str(m.get("log", ""))
    if not LOG_RE.match(lp):
        raise Refuse("That model has no readable log path in the catalog.", 400)
    out = remote(ps(f"""$p={ps_quote(lp)}
if (Test-Path -LiteralPath $p) {{ Get-Content -LiteralPath $p -Tail 80 }} else {{ '(no log file yet: ' + $p + ')' }}"""), timeout=40)
    return {"key": key, "path": lp, "text": (out or "")[-20000:]}


# ---- access token ---------------------------------------------------------------------------------------------------
def load_token():
    global TOKEN
    f = DATA / "token"
    if f.exists() and f.read_text().strip():
        TOKEN = f.read_text().strip()
        return
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    TOKEN = secrets.token_urlsafe(24)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(TOKEN + "\n")
    print(f"predator-control: NEW ACCESS TOKEN (shown once here; also in {f}): {TOKEN}", flush=True)


def token_ok(h):
    now = time.time()
    fails[:] = [t for t in fails if now - t < 300]
    if len(fails) >= 10:
        return None  # locked
    ok = bool(TOKEN) and hmac.compare_digest(h.encode(), TOKEN.encode())
    if not ok:
        fails.append(now)
    return ok


def memory_status():
    """Read this server host's Linux memory; never confuse it with remote Predator."""
    if IS_WIN or not Path("/proc/meminfo").exists():
        return {"supported": False, "host": __import__("socket").gethostname()}
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0]) * 1024
    procs = []
    for status_file in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(line.split(":", 1) for line in status_file.read_text().splitlines() if ":" in line)
            swapped = int(fields.get("VmSwap", "0 kB").split()[0]) * 1024
            if swapped:
                procs.append({"pid": int(status_file.parent.name), "name": fields["Name"].strip(), "swap": swapped})
        except (OSError, ValueError, KeyError):
            continue
    counters = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    return {"supported": True, "host": __import__("socket").gethostname(),
            "total": info["MemTotal"], "available": info["MemAvailable"],
            "swap_total": info["SwapTotal"], "swap_used": info["SwapTotal"] - info["SwapFree"],
            "swappiness": int(Path("/proc/sys/vm/swappiness").read_text()),
            "swap_in": int(counters["pswpin"]), "swap_out": int(counters["pswpout"]),
            "page_size": os.sysconf("SC_PAGE_SIZE"), "sample_time": time.monotonic(),
            "procs": sorted(procs, key=lambda p: p["swap"], reverse=True)[:10]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def auth(self):
        r = token_ok(self.headers.get("X-Token", ""))
        if r is None:
            self.send(429, {"error": "Too many wrong tokens; wait a few minutes."})
        elif not r:
            self.send(401, {"error": "Wrong or missing token."})
        return bool(r)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/api/system/capabilities", "/api/system/jobs", "/api/system/monitor"):
            if not self.auth():
                return
            fn = {"/api/system/capabilities": system_controls.capabilities,
                  "/api/system/jobs": system_controls.jobs,
                  "/api/system/monitor": system_controls.monitor_status}[p]
            return self.send(200, fn())
        if p in ("/", "/index.html"):
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if p == "/api/memory" and self.auth():
            return self.send(200, memory_status())
        if p == "/api/voice" and self.auth():
            return self.send(200, voice_status())
        if p == "/api/status" and self.auth():
            return self.send(200, status())
        if p == "/api/log" and self.auth():
            return self.send(200, {"log": list(log_ram)[-60:]})
        q = dict(x.split("=", 1) for x in self.path.partition("?")[2].split("&") if "=" in x)
        if p == "/api/models" and self.auth():
            return self.send(200, model_view(fresh=q.get("fresh") == "1"))
        if p == "/api/gpu_procs" and self.auth():
            return self.send(200, {"procs": gpu_procs()})
        if p == "/api/downloads" and self.auth():
            return self.send(200, {"downloads": download_status()})
        if p == "/api/model_log" and self.auth():
            try:
                return self.send(200, model_log(q.get("key", "")))
            except Refuse as e:
                return self.send(e.code, {"error": e.msg})
        if not p.startswith("/api/"):
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/system/run":
            if not self.auth():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    return self.send(400, {"error": "Invalid request size."})
                body = json.loads(self.rfile.read(length))
                request, mutates = system_controls.validate(body)
                with lock:
                    if mutates and request['domain'] in ('predator', 'image-model'):
                        if action['running'] or voice_lock.locked():
                            raise Refuse("Another Predator action is running.")
                        if request['domain'] == 'image-model' or request['action'] == 'sleep':
                            st = status(fresh=True)
                            if st['busy']['busy']:
                                raise Refuse("A picture job is running. Wait before changing Predator.")
                    return self.send(202, system_controls.submit(body, log))
            except (ValueError, TypeError):
                return self.send(400, {"error": "Invalid control request."})
            except system_controls.ControlError as e:
                return self.send(e.code, {"error": str(e)})
            except Refuse as e:
                return self.send(e.code, {"error": e.msg})
            except Exception:
                return self.send(500, {"error": "Could not start system control."})
        if self.path == "/api/password":  # the owner picks their own password (replaces the token; the file stays 600)
            if not self.auth():
                return None
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)) or b"{}")
                new = str(body.get("password", "")).strip()
            except Exception:
                return self.send(400, {"error": "Bad request."})
            if len(new) < 8 or len(new) > 200:
                return self.send(400, {"error": "Use 8 to 200 characters."})
            global TOKEN
            fd = os.open(DATA / "token", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(new + "\n")
            TOKEN = new
            log("password", "", "changed")
            return self.send(200, {"ok": True})
        if self.path in ("/api/models", "/api/kill", "/api/voice"):
            if not self.auth():
                return None
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 16384)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except Exception:
                return self.send(400, {"error": "Bad request."})
            try:
                with lock:
                    if any(j["running"] and j["domain"] in ("predator", "image-model") for j in system_controls.jobs()["jobs"]):
                        raise Refuse("A Predator system action is still running.")
                    return self.send(200, voice_control(body) if self.path == "/api/voice" else model_op(body) if self.path == "/api/models" else kill_proc(body))
            except Refuse as e:
                return self.send(e.code, {"error": e.msg})
            except Exception as e:
                return self.send(500, {"error": "Unexpected error: " + str(e)[:100]})
        if self.path != "/api/action" or not self.auth():
            return None if self.path == "/api/action" else self.send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self.send(400, {"error": "Bad request."})
        try:
            self.send(202, start_action(body))
        except Refuse as e:
            self.send(e.code, {"error": e.msg, "can_override": e.can_override})
        except Exception as e:
            self.send(500, {"error": "Unexpected error: " + str(e)[:100]})


def main():
    load_token()
    load_log()
    load_downloads()
    threading.Thread(target=scan_files, daemon=True).start()  # warm the ~10 s file walk so the Models tab opens fast
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"predator-control on 127.0.0.1:{PORT} ({'LOCAL mode, commands run on this machine' if LOCAL else 'ssh to ' + HOST})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
