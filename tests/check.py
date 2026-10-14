#!/usr/bin/env python3
"""predator-control tests. ssh is STUBBED (a fake Predator in memory): nothing here switches or touches the real machine.
`python3 tests/check.py --live` additionally does ONE read-only real status read (mode.ps1 -Status + the catalog file)."""
import json, os, sys, tempfile, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ["PREDATOR_CONTROL_DATA"] = tmp + "/data"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as S

S.POLL_SECONDS = 0.02
CAT = {"models": [{"key": "a3b", "name": "Big", "requiredVramMB": 3200}, {"key": "9b", "name": "Nine", "requiredVramMB": 5800}]}
JOBS = []


class Fake:
    """Predator stand-in: understands only the fixed commands server.py sends; records everything."""
    def __init__(s):
        s.mode, s.model, s.comfy, s.llm, s.queue, s.pending, s.up = "image", None, True, [], 0, False, True
        s.calls, s.spawned = [], 0

    def __call__(s, cmd, timeout=25):
        s.calls.append(cmd)
        if not s.up:
            raise OSError("down")
        if "mode.ps1 -Status" in cmd:
            live = "conflict" if s.comfy and s.llm else "image" if s.comfy else "llm" if s.llm else s.mode
            return json.dumps({"mode": live, "set_mode": s.mode, "set_model": s.model, "set_at": "t", "image_engine": s.comfy,
                               "models_running": s.llm, "power_plan": "Balanced", "gpu_mem_mb": "1200 / 6144", "gpu_temp_c": "55",
                               "gpu_util": "7%", "pending": s.pending})
        if "llama_models.json" in cmd:
            return json.dumps(CAT)
        if "queue_remaining" in cmd:
            return str(s.queue) if s.comfy else "none"
        if "Measure-Object" in cmd:
            return str(int(s.comfy))
        if "Stop-Process" in cmd:
            s.comfy = False
            return ""
        if "taskkill" in cmd:
            s.llm = []
            return ""
        if "mode_request.txt" in cmd:
            req = cmd.split("echo ")[1].split(">")[0]
            if req.startswith("llm:"):
                s.mode, s.model, s.comfy, s.llm = "llm", req[4:], False, ["m"]
            else:
                s.mode, s.model = req, None
                s.llm = []
                s.comfy = s.comfy and req == "image"
            return ""
        raise AssertionError("unexpected command: " + cmd)


fake = Fake()
S.remote = fake
def spawn():
    fake.spawned += 1; fake.comfy = True
S.spawn_comfy = spawn


class Imagine(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps({"jobs": JOBS}).encode(); self.send_response(200); self.end_headers(); self.wfile.write(b)


im = ThreadingHTTPServer(("127.0.0.1", 0), Imagine); threading.Thread(target=im.serve_forever, daemon=True).start()
S.IMAGINE_URL = f"http://127.0.0.1:{im.server_address[1]}"
S.load_token(); S.load_log()
srv = ThreadingHTTPServer(("127.0.0.1", 0), S.H); threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def call(path, body=None, token=None):
    h = {"X-Token": S.TOKEN if token is None else token}
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(ok, what):
    if not ok:
        raise AssertionError(what)
    print("ok  ", what)


def wait_idle():
    for _ in range(300):
        if not S.action["running"]:
            return
        time.sleep(0.02)
    raise AssertionError("action never finished")


def act(**b):
    b.setdefault("confirm", True)
    c, j = call("/api/action", b)
    if c == 202:
        wait_idle()
    S.cache["t"] = 0
    return c, j


# ---- token ----
tokfile = Path(tmp) / "data/token"
check(tokfile.exists() and oct(tokfile.stat().st_mode & 0o777) == "0o600", "token file created, mode 600")
check(call("/api/status", token="")[0] == 401, "no token -> 401")
check(call("/api/status", token="nope")[0] == 401, "wrong token -> 401")
check(call("/api/action", {"action": "idle"}, token="nope")[0] == 401, "action needs token")
check(call("/api/status")[0] == 200, "right token -> 200")
page = urllib.request.urlopen(BASE + "/").read()
check(b"Predator control" in page and b"sessionStorage" in page, "page served and keeps token in sessionStorage")
for _ in range(12): call("/api/status", token="x")
check(call("/api/status")[0] == 429, "brute force locks out (429)")
S.fails.clear()

# ---- status ----
c, st = call("/api/status")
check(st["mode"] == "image" and st["comfy_running"] and st["comfy_queue"] == 0, "status: image mode, ComfyUI queue 0")
check(st["gpu"] == {"used_mb": 1200, "total_mb": 6144, "util_pct": 7, "temp_c": 55}, "status: gpu numbers parsed")
check([m["key"] for m in st["models"]] == ["a3b", "9b"], "status: model keys from catalog")
check(st["imagine"] == {"running": 0, "pending": 0}, "status: imagine counts")
check(not st["busy"]["busy"], "status: not busy")
fake.up = False; S.cache["t"] = 0
check(call("/api/status")[1]["reachable"] is False, "unreachable Predator reported plainly")
fake.up = True; S.cache["t"] = 0

# ---- validation ----
check(act(action="switch", mode="idle", confirm=False)[0] == 400, "no confirm -> 400")
check(act(action="rm -rf")[0] == 400, "unknown action -> 400")
check(act(action="switch", mode="llm", model="x; calc")[0] == 400, "free-text model key refused")
check(act(action="switch", mode="llm", model="zzz")[0] == 400, "model key not in catalog refused")
n = len(fake.calls)
check(not any("calc" in c for c in fake.calls), "nothing from free text reached ssh")

# ---- busy guards ----
JOBS[:] = [{"status": "running", "kind": "image"}, {"status": "queued"}]
S.cache["t"] = 0
c, j = act(action="switch", mode="gaming")
check(c == 409 and j["can_override"] and "Imagine" in j["error"], "switch refused (409) while Imagine runs")
check(act(action="comfy_stop")[0] == 409, "comfy stop refused while Imagine runs")
JOBS[:] = [{"status": "running", "kind": "chat"}]
check(act(action="switch", mode="gaming")[0] == 202 and fake.mode == "gaming", "chat jobs do not block; switch to gaming works")
JOBS[:] = []
fake.mode, fake.comfy, fake.queue = "image", True, 2
c, j = act(action="llama_stop"); check(c == 409, "no llama running -> 409")
c, j = act(action="switch", mode="idle")
check(c == 409 and "ComfyUI has 2" in j["error"], "switch refused while ComfyUI queue non-empty")
c, j = act(action="switch", mode="idle", override=True)
check(c == 202 and fake.mode == "idle" and not fake.comfy, "override tick lets it through")
check(any(e["override"] for e in S.log_ram), "override recorded in log")
fake.queue = 0

# ---- switches ----
fake.mode, fake.comfy, fake.llm = "image", True, []
c, j = act(action="switch", mode="llm", model="a3b")
check(c == 202 and fake.mode == "llm" and fake.model == "a3b" and S.action["ok"] is True, "switch to llm a3b ok")
check(any("echo llm:a3b>" in x and "schtasks /run /tn HermesMode" in x for x in fake.calls), "uses mode_request.txt + HermesMode task")
check(act(action="switch", mode="llm", model="a3b")[0] == 409, "same model again -> 409")
check(act(action="comfy_start")[0] == 409, "ComfyUI start refused beside a running model")
c, j = act(action="llama_stop"); check(c == 202 and fake.llm == [], "stray llama-server stopped")
fake.pending = True; S.cache["t"] = 0
check(act(action="switch", mode="idle")[0] == 409, "pending switch -> 409")
fake.pending = False
fake.llm = []; fake.mode = "idle"; fake.comfy = False; S.cache["t"] = 0
c, j = act(action="comfy_start"); check(c == 202 and fake.spawned == 1 and fake.comfy, "comfy start")
check(act(action="comfy_start")[0] == 409, "comfy already running -> 409")
c, j = act(action="comfy_restart"); check(c == 202 and fake.spawned == 2, "comfy restart = stop + start")
c, j = act(action="comfy_stop"); check(c == 202 and not fake.comfy, "comfy stop")
check(act(action="comfy_stop")[0] == 409, "comfy not running -> 409")
fake.mode = "gaming"; S.cache["t"] = 0
check(act(action="comfy_start")[0] == 409, "comfy start refused in gaming mode")

# failed switch is reported
fake.mode = "image"; fake.comfy = True; S.cache["t"] = 0
orig = S.poll_mode; S.poll_mode = lambda *a: False
c, j = act(action="switch", mode="gaming")
check(c == 202 and S.action["ok"] is False and "did not confirm" in S.action["error"], "unconfirmed switch reported as failed")
S.poll_mode = orig

# ---- log: no free text, persisted ----
lf = Path(tmp) / "data/actions.log"
lines = [json.loads(x) for x in lf.read_text().splitlines()]
check(len(lines) >= 8 and all(set(e) == {"t", "action", "detail", "result", "override"} for e in lines), "log file has only time/action/detail/result")
check(call("/api/log")[1]["log"], "log endpoint works")

if "--live" in sys.argv:
    import importlib; importlib.reload(S)  # real remote(), read-only calls only
    d = S.read_mode(); cat = S.models_catalog()
    print("live mode:", d and (d["mode"], d["set_mode"], d["models_running"]), "catalog:", [m["key"] for m in cat])
    check(d is not None and len(cat) > 0, "live read-only status + catalog")
print("ALL PASSED")
