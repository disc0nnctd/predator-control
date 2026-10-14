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
import collections, hmac, json, os, re, secrets, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
CATALOG_CMD = r"cmd /c type C:\Users\gamin\Experiments\llm_gaming_setup\llama_models.json 2>nul"
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
def models_catalog():
    """[{key, name, vram_mb}] from Predator's own llama_models.json (cached 10 min)."""
    if time.time() - catalog_cache["t"] < 600 and catalog_cache["v"]:
        return catalog_cache["v"]
    try:
        out = []
        for m in json.loads(remote(CATALOG_CMD, timeout=20).lstrip("\ufeff")).get("models", []):
            if KEY_RE.match(str(m.get("key", ""))):
                out.append({"key": m["key"], "name": str(m.get("name", m["key"]))[:80], "vram_mb": m.get("requiredVramMB")})
        if out:
            catalog_cache.update(t=time.time(), v=out)
    except Exception:
        pass
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
        if p in ("/", "/index.html"):
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if p == "/api/status" and self.auth():
            return self.send(200, status())
        if p == "/api/log" and self.auth():
            return self.send(200, {"log": list(log_ram)[-60:]})
        if not p.startswith("/api/"):
            self.send(404, {"error": "not found"})

    def do_POST(self):
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
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"predator-control on 127.0.0.1:{PORT} ({'LOCAL mode, commands run on this machine' if LOCAL else 'ssh to ' + HOST})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
