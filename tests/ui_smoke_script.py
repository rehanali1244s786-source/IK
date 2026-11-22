"""Runs the real App logic against a stub tkinter (no display needed). Executed in a subprocess by test_ui_smoke.py."""
import json, os, sys, tempfile, threading, time, types, http.client, hashlib

home = tempfile.mkdtemp()
os.environ["HOME"] = os.environ["USERPROFILE"] = home          # keep ~/.ik_downloader out of the real profile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return Any()
    def __call__(self, *a, **k): return Any()
    def __iter__(self): return iter(())
    def __bool__(self): return True
    def __sub__(self, o): raise TypeError("stub")


class Var(Any):
    def __init__(self, master=None, value=None, **k): self.v = value
    def get(self): return self.v
    def set(self, v): self.v = v


class TclError(Exception): pass


calls = {"askyesno": []}
tk = types.ModuleType("tkinter")
for n in ("Tk", "Toplevel", "PhotoImage", "Menu", "Label", "Frame", "Canvas", "Text"): setattr(tk, n, type(n, (Any,), {}))
tk.Toplevel.winfo_screenwidth, tk.Toplevel.winfo_screenheight = (lambda self: 1920), (lambda self: 1080)
tk.StringVar = tk.IntVar = tk.BooleanVar = Var
tk.TclError = TclError
ttk = types.ModuleType("tkinter.ttk")
for n in ("Style", "Frame", "Label", "Button", "Entry", "Notebook", "Treeview", "Scrollbar", "Progressbar", "Combobox",
          "Spinbox", "Checkbutton", "Separator", "Scale", "LabelFrame", "PanedWindow"): setattr(ttk, n, type(n, (Any,), {}))
fd, mb, sd = (types.ModuleType("tkinter." + n) for n in ("filedialog", "messagebox", "simpledialog"))
mb.askyesno = lambda *a, **k: calls["askyesno"].append(k) or True
mb.showinfo = mb.showwarning = mb.showerror = mb.askokcancel = lambda *a, **k: True
mb.askyesnocancel = lambda *a, **k: True
fd.askdirectory = lambda *a, **k: ""
fd.askopenfilename = lambda *a, **k: ""
sd.askstring = lambda *a, **k: None
tk.ttk, tk.filedialog, tk.messagebox, tk.simpledialog = ttk, fd, mb, sd
sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.filedialog": fd, "tkinter.messagebox": mb, "tkinter.simpledialog": sd})

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ikd.api, ikd.ui, ikd.engine, ikd.common
ikd.engine.Job.RETRY_DELAY = 0.01
ikd.ui.start_server = lambda inbox, api, pairing: ikd.api.start_server(inbox, api, pairing, port=0)

DATA = os.urandom(2_500_000)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        rng, total, a, code = self.headers.get("Range", ""), len(DATA), 0, 200
        b = total - 1
        if rng.startswith("bytes="):
            x, _, y = rng[6:].partition("-"); a, b, code = int(x), (int(y) if y else total - 1), 206
        body = DATA[a:b + 1]
        self.send_response(code); self.send_header("Content-Length", str(len(body)))
        if code == 206: self.send_header("Content-Range", f"bytes {a}-{b}/{total}")
        self.end_headers()
        try: self.wfile.write(body)
        except OSError: pass
web = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=web.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{web.server_address[1]}/big.bin"

app = ikd.ui.App()
app.v_show.set(False)
dl = os.path.join(home, "dl"); app.v_folder.set(dl)
port = app.srv.server_address[1]
ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def post(path, body, **h):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    c.request("POST", path, json.dumps(body), {"X-IK-Client": "1", "Origin": ORIGIN, **h})
    r = c.getresponse(); d = json.loads(r.read() or b"{}"); c.close(); return r.status, d


# 1) an unpaired extension is refused, then pairs after the user clicks Yes in the (stubbed) dialog
assert post("/add", {"url": URL})[0] == 401
res = []
t = threading.Thread(target=lambda: res.append(post("/pair", {}))); t.start()
for _ in range(100):
    app._tick(); time.sleep(0.05)
    if res: break
t.join(5)
assert res and res[0][0] == 200, res
token = res[0][1]["token"]
ask = calls["askyesno"][-1]
assert ask.get("default") == "no" and ask.get("icon") == "warning", ask          # safe default button

# 2) paired: an unsafe folder is ignored, a normal download is queued, run and finishes byte-exact
unsafe = "C:\\Windows\\System32" if os.name == "nt" else "/etc"
assert post("/add", {"url": URL, "filename": "big.bin", "folder": unsafe, "cookie": "sid=SECRETCOOKIE"},
            **{"X-IK-Token": token})[0] == 200
for _ in range(400):
    app._tick(); time.sleep(0.05)
    if app.jobs and app.jobs[0].state in ("done", "error"): break
j = app.jobs[0]
assert j.state == "done", (j.state, j.msg)
assert not os.path.abspath(j.path).startswith(unsafe), j.path
assert hashlib.sha256(open(j.path, "rb").read()).hexdigest() == hashlib.sha256(DATA).hexdigest()

# 3) state + settings are saved atomically, cookies are not in the file, and a restart restores the list
app._quit()
state = open(ikd.common.STATE, encoding="utf-8").read()
assert "SECRETCOOKIE" not in state and "big.bin" in state
assert not os.path.exists(ikd.common.STATE + ".tmp")
app2 = ikd.ui.App()
assert len(app2.jobs) == 1 and app2.jobs[0].state == "done"
assert app2.pairing.count() == 1                                                 # pairing survives restart
port = app2.srv.server_address[1]                                                # the restarted app's own server
assert post("/info", {"extract": False}, **{"X-IK-Token": token})[0] == 200      # old token still valid after restart

# 4) statistics + history survived the restart (the finished download was recorded when it completed)
assert len(app2.hist.rows) == 1 and app2.hist.rows[0]["name"] == "big.bin", app2.hist.rows
assert app2.hist.search("BIG")[0]["size"] == len(DATA) and app2.hist.search("nothing-like-this") == []
life = app2.stats.summary()["life"]
assert life["files"] == 1 and life["bytes"] >= len(DATA) and life["secs"] > 0, life
assert app2.hist.rows[0]["avg"] > 0, app2.hist.rows[0]                                 # average speed stored per download


def pump(cond, n=600):
    for _ in range(n):
        app2._tick(); time.sleep(0.03)
        if cond(): return True
    return False


app2.v_show.set(False)
old = app2.jobs[0]
assert old.state == "done" and old.mirrors == []

# 5) duplicates: the policy decides (or, with "ask", the stubbed dialog answers "skip")
app2.v_dup.set("ask")
assert app2._add_url(URL, fname="big.bin") is None and app2.skipped and len(app2.jobs) == 1       # no answer = skip
app2.v_dup.set("skip")
assert app2._add_url(URL) is None and app2.skipped and len(app2.jobs) == 1
app2.v_dup.set("rename")
j2 = app2._add_url(URL, fname="big.bin")
assert j2 is not None and j2.fname == "big (1).bin" and len(app2.jobs) == 2, (j2 and j2.fname)
assert pump(lambda: j2.state in ("done", "error")) and j2.state == "done", (j2.state, j2.msg)
assert os.path.basename(j2.path) == "big (1).bin" and os.path.exists(old.path)
assert hashlib.sha256(open(j2.path, "rb").read()).hexdigest() == hashlib.sha256(DATA).hexdigest()
app2.v_dup.set("replace")
before = open(old.path, "rb").read()
open(old.path, "wb").write(b"stale contents")                                      # the old file is out of date
j3 = app2._add_url(URL, fname="big.bin")
assert j3 is not None and j3.replace and old not in app2.jobs, (j3, [j.name for j in app2.jobs])
assert pump(lambda: j3.state in ("done", "error")) and j3.state == "done", (j3.state, j3.msg)
assert j3.path == old.path and open(j3.path, "rb").read() == before                  # replaced in place, not numbered
assert not os.path.exists(j3.path + ".part")
n_before = len(app2.jobs)
app2.v_dup.set("ask")

# 6) magnet links are handed to a client, never downloaded; a repeat is detected through the history
import ikd.torrent as tor
sent = []
tor.send = lambda target, configured="", save_dir="": sent.append((target, configured, save_dir)) or "FakeClient"
BT = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
MAG = "magnet:?xt=urn:btih:" + BT + "&dn=Test%20Torrent"
assert app2._add_url(MAG) is None and len(sent) == 1 and len(app2.jobs) == n_before      # not added as an http job
assert app2.hist.last_for_btih(BT)["name"] == "Test Torrent" and app2.hist.search("", "torrent")
app2.v_dup.set("skip")
assert app2._add_magnet(MAG) is False and len(sent) == 1                                 # duplicate torrent skipped
app2.v_dup.set("ask")
n_ask = len(calls["askyesno"])
assert app2._add_magnet(MAG) and len(sent) == 2 and len(calls["askyesno"]) == n_ask + 1   # asked, answered yes
assert app2._add_magnet("magnet:?xt=urn:btih:abc evil --flag") is False and len(sent) == 2   # malformed: refused
assert post("/add", {"url": "magnet:?xt=urn:btih:" + "ab" * 20}, **{"X-IK-Token": token})[0] == 200   # from the browser
assert pump(lambda: len(sent) == 3) and sent[-1][0].endswith("ab" * 20)

# 7) a downloaded .torrent file is opened in the client automatically
TURL = URL.replace("big.bin", "t.torrent")
j4 = app2._add_url(TURL)
assert pump(lambda: j4.state in ("done", "error")) and j4.state == "done", (j4.state, j4.msg)
assert pump(lambda: len(sent) == 4) and sent[-1][0] == j4.path and j4.path.endswith(".torrent"), sent
app2.v_tauto.set(False)
j5 = app2._add_url(TURL.replace("t.torrent", "u.torrent"))
assert pump(lambda: j5.state == "done") and not pump(lambda: len(sent) == 5, 10)          # switched off: no auto-send

# 8) mirrors + a failed download end up in the history; settings keep the new options
j6 = app2._add_url(URL.replace("big.bin", "m.bin"), mirrors=[URL.replace("big.bin", "m2.bin"), "ftp://nope/x"], fname="m.bin")
assert j6.mirrors == [URL.replace("big.bin", "m2.bin")]
assert "m2.bin" in ikd.ui.source_text(j6) or "links" in ikd.ui.source_text(j6)
assert pump(lambda: j6.state == "done") and j6.srcs is not None and len(j6.source_rows()) == 2
assert "\u25cf" in ikd.ui.source_text(j6)
assert pump(lambda: any(r["name"] == "m.bin" for r in app2.hist.search("m.bin", "done")))     # recorded on the next tick
app2._save_settings()
cfg = json.load(open(ikd.common.CFG, encoding="utf-8"))
assert cfg["dup"] == "ask" and cfg["torrent_auto"] is False and "torrent_client" in cfg, cfg
app2.pairing.revoke_all()
assert post("/info", {"extract": False}, **{"X-IK-Token": token})[0] == 401

# 9) the history of an installation that predates this feature is filled from the finished downloads
app2._quit()
os.remove(ikd.common.HOME + os.sep + "history.json")
app3 = ikd.ui.App()
done = [j for j in app3.jobs if j.state == "done"]
assert done and len(app3.hist.rows) == len(done) and all(r["status"] == "done" for r in app3.hist.rows), app3.hist.rows
print("UI SMOKE OK")
os._exit(0)
