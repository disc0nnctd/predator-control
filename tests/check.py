#!/usr/bin/env python3
"""predator-control tests. ssh is STUBBED (a fake Predator in memory): nothing here switches or touches the real machine.
`python3 tests/check.py --live` additionally does ONE read-only real status read (mode.ps1 -Status + the catalog file)."""
import base64, importlib.util, json, os, sys, tempfile, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ["PREDATOR_CONTROL_DATA"] = tmp + "/data"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as S

S.POLL_SECONDS = 0.02
U = "C:\\Users\\dcmini\\"
EXE = U + r"llama-b10453\llama-server.exe"
CAT = {"defaultModel": "a3b", "models": [
    {"key": "a3b", "name": "Big", "requiredVramMB": 3200, "path": U + "big.gguf", "port": 8085, "alias": "big", "args": "-ngl 99 --cpu-moe",
     "log": U + "llama-server-a3b.log", "serverExe": EXE, "notes": "keep"},
    {"key": "9b", "name": "Nine", "requiredVramMB": 5800, "path": U + "nine.gguf", "port": 8080, "alias": "nine", "args": "-ngl 99",
     "log": U + "llama-server.log"}]}
FILES = {U + "big.gguf": 20e9, U + "nine.gguf": 5e9, U + "spare.gguf": 4e9, r"D:\models\x\mmproj-f16.gguf": 9e8,
         U + "odd&name.gguf": 1e9, EXE: 9216, U + r"turboquant-cuda\build\bin\llama-server.exe": 4e6}
UPLOADED = []
GPU = {1580: {"name": "dwm.exe", "mb": 600, "util": 0, "cmd": "dwm.exe"},
       19420: {"name": "python.exe", "mb": 5300, "util": 94, "cmd": "python.exe main.py --listen 127.0.0.1 --port 8188"},
       10768: {"name": "stremio.exe", "mb": 246, "util": 5, "cmd": "stremio"}, 4972: {"name": "idle.exe", "mb": 0, "util": 0, "cmd": ""}}
JOBS = []


class Fake:
    """Predator stand-in: understands only the fixed commands server.py sends; records everything."""
    def __init__(s):
        s.mode, s.model, s.comfy, s.llm, s.queue, s.pending, s.up = "image", None, True, [], 0, False, True
        s.calls, s.spawned, s.scripts, s.curl, s.killed = [], 0, [], [], []

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
        if "-EncodedCommand" in cmd:
            return s.ps(base64.b64decode(cmd.split("-EncodedCommand ")[1]).decode("utf-16-le"))
        raise AssertionError("unexpected command: " + cmd)

    def ps(s, sc):
        s.scripts.append(sc)
        if "Get-PSDrive" in sc:
            return json.dumps({"files": [{"p": p, "s": n, "t": "2026-10-17 10:00"} for p, n in FILES.items()],
                               "disks": [{"n": "C", "free": 20e9}, {"n": "D", "free": 50e9}]})
        if "WriteAllText" in sc:
            CAT.clear(); CAT.update(json.loads(UPLOADED[-1])); return "WROTE"
        if "Win32_Process -MethodName Create" in sc:
            s.curl.append(sc.split("CommandLine='")[1].split("'}")[0]); part = s.curl[-1].split('-o "')[1].split('"')[0]
            FILES[part] = 1000; return "STARTED 0"
        if "Name='curl.exe'" in sc and "parts=" in sc:
            return json.dumps({"parts": [{"p": p, "s": n} for p, n in FILES.items() if p.endswith(".part")], "curl": s.curl})
        if "CANCELLED" in sc:
            p = sc.split("$p='")[1].split("'")[0]; FILES.pop(p, None); s.curl = [c for c in s.curl if p not in c]; return "CANCELLED"
        if "DELETED" in sc:
            FILES.pop(sc.split("-LiteralPath '")[1].split("'")[0]); return "DELETED"
        if "GPU Process Memory" in sc:
            return json.dumps([{"pid": p, **v} for p, v in GPU.items()])
        if "Stop-Process -Id" in sc and "KILLED" in sc:
            pid = int(sc.split("Stop-Process -Id ")[1].split()[0]); GPU.pop(pid, None); s.killed.append(pid); return "KILLED"
        if "Get-Content -LiteralPath" in sc:
            return "line1\nline2"
        raise AssertionError("unexpected script: " + sc[:200])


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
check(b"Control Centre" in page and b"sessionStorage" in page, "page served and keeps token in sessionStorage")
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

# ---- model management ----
S.put_text = lambda text, path: UPLOADED.append(text)
S.hf_total = lambda url: 3e9
def mop(**b):
    b.setdefault("confirm", True)
    return call("/api/models", b)
fake.mode, fake.comfy, fake.llm, fake.model = "idle", False, [], None; S.cache["t"] = 0
c, v = call("/api/models?fresh=1")
check(c == 200 and [m["key"] for m in v["catalog"]["models"]] == ["a3b", "9b"] and len(v["servers"]) == 2, "models view: catalog + builds")
fv = {f["path"]: f for f in v["files"]}
check(fv[U + "big.gguf"]["used_by"] == ["a3b"] and fv[r"D:\models\x\mmproj-f16.gguf"]["kind"] == "mmproj"
      and not fv[U + "odd&name.gguf"]["name_ok"], "files: used_by, mmproj kind, unsafe names flagged")
good = {"key": "spare", "name": "Spare 4B", "path": U + "spare.gguf", "serverExe": EXE, "args": "-ngl 99 -fa on -c 8192",
        "port": 8086, "requiredVramMB": 4700, "alias": "", "notes": ""}
check(mop(op="add", entry=good, confirm=False)[0] == 400, "model add needs confirm")
bad_cases = [({"args": "-ngl 99 & calc"}, "shell char in args"), ({"args": '-c 4096 "x'}, "quote in args"),
             ({"args": "-ngl 99 --port 9000"}, "forbidden --port"), ({"args": "--path=C:\\"}, "forbidden --path=..."),
             ({"args": "--mmproj C:\\Windows\\x.gguf"}, "gguf in args not on Predator"),
             ({"path": "C:\\Windows\\evil.gguf"}, "model file not found"), ({"path": U + "odd&name.gguf"}, "unsafe file name"),
             ({"serverExe": "C:\\Windows\\System32\\calc.exe"}, "unknown server exe"), ({"port": 22}, "port range"),
             ({"requiredVramMB": 99999}, "vram range"), ({"key": "A3B"}, "key pattern"), ({"key": "a3b"}, "duplicate key"),
             ({"alias": "x y"}, "alias pattern"), ({"name": ""}, "empty name")]
for patch, what in bad_cases:
    check(mop(op="add", entry={**good, **patch})[0] == 400, "refused: " + what)
check(not UPLOADED, "nothing written for refused entries")
c, j = mop(op="add", entry=good)
check(c == 200 and CAT["models"][-1]["key"] == "spare" and CAT["models"][-1]["alias"] == "spare"
      and CAT["models"][-1]["log"] == U + "llama-server-spare.log" and "notes" not in CAT["models"][-1], "model added (alias, log defaulted)")
check(any("bak-" in sc and "Copy-Item" in sc for sc in fake.scripts), "catalog backed up before write")
check([m["key"] for m in S.models_catalog()][-1] == "spare", "switch list sees the new model at once")
c, j = mop(op="edit", entry={**good, "key": "a3b", "path": U + "big.gguf", "name": "Big v2", "args": "-ngl 99 --cpu-moe -c 32768", "notes": "kept"})
a = CAT["models"][0]
check(c == 200 and a["name"] == "Big v2" and a["args"].endswith("32768") and a["log"] == U + "llama-server-a3b.log" and a["notes"] == "kept",
      "edit keeps log path, changes fields")
check(mop(op="edit", entry={**good, "key": "nope"})[0] == 409, "edit of unknown key refused")
c, j = mop(op="default", key="spare"); check(c == 200 and CAT["defaultModel"] == "spare", "default set")
fake.llm, fake.model, fake.mode = ["m"], "9b", "llm"; S.cache["t"] = 0
check(mop(op="delete", key="9b")[0] == 409, "loaded model cannot be removed")
c, j = mop(op="edit", entry={**good, "key": "9b", "path": U + "nine.gguf"})
check(c == 200 and "until it is loaded again" in j["note"], "editing the loaded model says it applies on next load")
fake.llm, fake.mode = [], "idle"; S.cache["t"] = 0
c, j = mop(op="delete", key="spare"); check(c == 200 and CAT["defaultModel"] == "a3b" and len(CAT["models"]) == 2, "delete moves default")
check(mop(op="delete_file", path=U + "big.gguf")[0] == 409, "file used by a model cannot be deleted")
check(mop(op="delete_file", path="C:\\Windows\\win.ini")[0] == 400, "unknown file cannot be deleted")
c, j = mop(op="delete_file", path=U + "spare.gguf"); check(c == 200 and U + "spare.gguf" not in FILES, "unused file deleted")
c, l = call("/api/model_log?key=a3b"); check(c == 200 and "line2" in l["text"], "model log tail")
check(call("/api/model_log?key=zzz")[0] == 400, "log of unknown key refused")
# downloads
for u in ("https://evil.com/a.gguf", "https://huggingface.co/a/b/resolve/main/x.gguf&calc", "https://huggingface.co/a/b/resolve/main/x.bin"):
    check(mop(op="download", url=u)[0] == 400, "download link refused: " + u[:50])
check(mop(op="download", url="https://huggingface.co/u/r/resolve/main/m.gguf", folder="..\\x")[0] == 400, "folder pattern enforced")
c, j = mop(op="download", url="https://huggingface.co/u/Repo-GGUF/blob/main/sub/m-Q4.gguf?download=true")
part = r"D:\models\Repo-GGUF\m-Q4.gguf.part"
check(c == 200 and fake.curl and 'resolve/main/sub/m-Q4.gguf' in fake.curl[-1] and part in FILES, "download started via curl in its own process")
check(mop(op="download", url="https://huggingface.co/u/Repo-GGUF/resolve/main/m-Q4.gguf")[0] == 409, "same download twice refused")
c, d = call("/api/downloads"); dl = d["downloads"][0]
check(dl["running"] and dl["total"] == 3e9 and dl["part"] == part, "download progress listed")
check(mop(op="resume_download", part=part)[0] == 409, "resume of a running download refused")
fake.curl = []
c, j = mop(op="resume_download", part=part); check(c == 200 and "-C - " in fake.curl[-1], "stopped download resumes with curl -C -")
c, j = mop(op="cancel_download", part=part); check(c == 200 and part not in FILES, "cancel stops curl and deletes the .part")
S.hf_total = lambda url: 80e9
check(mop(op="download", url="https://huggingface.co/u/r/resolve/main/huge.gguf")[0] == 409, "download refused when D: is too small")
check(all("calc" not in sc for sc in fake.scripts) and all("calc" not in x for x in fake.calls), "no free text reached Predator")

# ---- GPU processes ----
c, g = call("/api/gpu_procs")
check(c == 200 and [p["pid"] for p in g["procs"]] == [19420, 1580, 10768], "gpu list: biggest first, idle processes hidden")
check({p["pid"]: p["kind"] for p in g["procs"]} == {19420: "comfyui", 1580: "system", 10768: "app"}, "gpu list: kinds")
kill = lambda **b: call("/api/kill", {"confirm": True, **b})
check(kill(pid=10768, name="stremio.exe", confirm=False)[0] == 400, "kill needs confirm")
check(kill(pid=1580, name="dwm.exe")[0] == 403, "Windows process cannot be ended")
check(kill(pid=10768, name="other.exe")[0] == 409, "pid with a different name refused (stale pid)")
check(kill(pid="10768; calc", name="stremio.exe")[0] == 400, "non-integer pid refused")
fake.comfy, fake.queue = True, 3; S.cache["t"] = 0
check(kill(pid=19420, name="python.exe")[0] == 409, "ComfyUI with queued jobs needs the override")
c, j = kill(pid=10768, name="stremio.exe"); check(c == 200 and fake.killed == [10768], "app terminated")
c, j = kill(pid=19420, name="python.exe", override=True); check(c == 200 and 19420 in fake.killed, "ComfyUI ended with override")
fake.comfy, fake.queue = False, 0; S.cache["t"] = 0

# ---- LOCAL mode (server runs ON Predator): commands run directly; Windows-path logic under a fake ----
import subprocess, importlib
calls, popens = [], []
class CP:  # CompletedProcess stand-in
    def __init__(s, out="", rc=0): s.stdout, s.returncode = out, rc
task_exists = [True]
def fake_run(cmd, **kw):
    calls.append((cmd, kw))
    assert not (isinstance(cmd, list) and cmd and cmd[0] == "ssh"), "ssh used in LOCAL mode"
    if "schtasks /run /tn PredatorComfyUI" in cmd:
        return CP("", 0 if task_exists[0] else 1)
    return CP("7\n")
def fake_popen(args, **kw):
    popens.append((args, kw))
    if kw.get("creationflags", 0) & 0x01000000 and BREAKAWAY_DENIED[0]:
        raise PermissionError("breakaway denied")
BREAKAWAY_DENIED = [False]
real_run, real_popen = subprocess.run, subprocess.Popen
with tempfile.TemporaryDirectory() as cd:
    saved = (S.LOCAL, S.IS_WIN, S.COMFY_DIR)
    S.LOCAL, S.IS_WIN, S.COMFY_DIR = True, True, cd
    subprocess.run, subprocess.Popen = fake_run, fake_popen
    try:
        # real remote()/spawn_comfy() from source, not the stubs
        src = importlib.util.spec_from_file_location("server_local", S.__file__)
        L = importlib.util.module_from_spec(src); os.environ["PREDATOR_LOCAL"] = "1"; src.loader.exec_module(L)
        L.IS_WIN, L.COMFY_DIR = True, cd
        check(L.LOCAL is True, "PREDATOR_LOCAL=1 turns LOCAL mode on")
        check(L.IMAGINE_URL.startswith("http://100.96.201.27:"), "LOCAL default Imagine URL is dcmini's tailnet address")
        out = L.remote(L.MODE_PS)
        c, kw = calls[-1]
        check(out == "7\n" and c == L.MODE_PS and kw["shell"] is True and kw["creationflags"] == L.CREATE_NO_WINDOW,
              "LOCAL remote(): same fixed command, run directly via cmd shell, no window, no ssh")
        L.spawn_comfy()
        check(calls[-1][0] == "schtasks /run /tn PredatorComfyUI" and not popens, "LOCAL comfy start = run the PredatorComfyUI task")
        task_exists[0] = False
        L.spawn_comfy()
        a, kw = popens[-1]
        check(a[0].replace("\\", "/").endswith("venv/Scripts/python.exe") and a[1:3] == ["main.py", "--listen"] and kw["cwd"] == cd
              and kw["creationflags"] & 0x01000000 and kw["creationflags"] & 0x00000008, "no task: detached direct start with job breakaway")
        BREAKAWAY_DENIED[0] = True
        L.spawn_comfy()
        check(not popens[-1][1]["creationflags"] & 0x01000000 and len(popens) == 3, "breakaway forbidden: retries without it")
        check((Path(cd) / "server.log").exists(), "ComfyUI log file opened in COMFY_DIR")
        os.environ["LOCALAPPDATA"] = r"C:\Users\dcmini\AppData\Local"
        os.environ.pop("PREDATOR_CONTROL_DATA", None)
        check(str(L.default_data()).replace("\\", "/").endswith("AppData/Local/predator-control"), "data dir derived from LOCALAPPDATA")
        os.environ["PREDATOR_LOCAL"] = "0"
        src.loader.exec_module(L)
        check(L.LOCAL is False and L.IMAGINE_URL == "http://127.0.0.1:8791", "PREDATOR_LOCAL=0 keeps ssh mode defaults")
    finally:
        subprocess.run, subprocess.Popen = real_run, real_popen
        S.LOCAL, S.IS_WIN, S.COMFY_DIR = saved
        os.environ.pop("PREDATOR_LOCAL", None)
# Imagine unreachable = unknown, not an error
S.IMAGINE_URL = "http://127.0.0.1:9"
check(S.imagine_jobs() is None, "Imagine unreachable -> None (unknown)")
S.cache["t"] = 0
check(call("/api/status")[1]["imagine"] is None, "status still works with Imagine unreachable")
# deploy bundle copies must match the source
for f in ("server.py", "index.html", "system_controls.py"):
    b = Path(S.__file__).parent / "deploy/predator" / f
    check(b.exists() and b.read_bytes() == (Path(S.__file__).parent / f).read_bytes(), f"deploy/predator/{f} is in sync (run deploy/sync.sh)")

if "--live" in sys.argv:
    import importlib; importlib.reload(S)  # real remote(), read-only calls only
    d = S.read_mode(); cat = S.models_catalog()
    print("live mode:", d and (d["mode"], d["set_mode"], d["models_running"]), "catalog:", [m["key"] for m in cat])
    check(d is not None and len(cat) > 0, "live read-only status + catalog")
print("ALL PASSED")
