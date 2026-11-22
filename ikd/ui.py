"""Tkinter user interface: main window, progress window, download-info box."""
import os, re, sys, json, hashlib, queue, shutil, threading, time, subprocess, webbrowser
import urllib.request, urllib.parse

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
except ImportError:  # headless (tests)
    tk = None

from . import dupes, netsec, torrent
from .api import Api, Pairing, start_server
from .common import (APP, BUNDLE, KNOWN_EXT, VERSION, PORT, REPO, HOME, STATE, CFG, DEFAULT_DIR, UA, ALL_EXT, CATS, QUALITIES, asset, cancel_shutdown,
                     category, clean, ffmpeg_path, fmt, guess_mode, hms, log, read_json, safe_folder, setup_logging,
                     short_err, shutdown_pc, version_tuple, write_json)
from .engine import Counter, Job, Limiter, describe, probe_url
from .history import History, Stats, format_summary
from .netsec import ensure_http

GREEN = "#22c55e"
DUP_LABELS = {"ask": "Ask me what to do", "skip": "Skip the duplicate", "rename": "Keep both (rename the new file)",
              "replace": "Re-download (replace the old file)"}


def source_text(j) -> str:
    """The 'Sources' line of the progress window: every mirror with its measured speed and share."""
    rows = j.source_rows()
    if rows:
        out = []
        for r in rows[:4]:
            mark = {"ok": "\u25cf", "cooling": "\u25d0", "failed": "\u25cb"}.get(r["state"], "\u25cf")
            sp = (fmt(r["speed"]) + "/s") if r["speed"] else "measuring"
            out.append(f"{mark} {r['host']}   {sp}   {fmt(r['bytes'])}" + ("   (failed)" if r["state"] == "failed" else ""))
        if len(rows) > 4:
            out.append(f"... and {len(rows) - 4} more")
        return "\n".join(out)
    if j.dropped:
        return f"Main link only ({len(j.dropped)} mirror(s) refused: {j.dropped[0][1]})"
    if j.mirrors:
        return f"{1 + len(j.mirrors)} links (checked when the download starts)"
    return "1 (main link)"


class DupDialog(tk.Toplevel if tk else object):
    """'This looks like a duplicate' box: Skip / Keep both (rename) / Re-download (replace)."""
    W = 540

    def __init__(self, app, head, text):
        super().__init__(app)
        self.result, self.remember = None, False
        bg = app.palette()[0]
        self.title("Duplicate - " + APP)
        self.configure(bg=bg)
        self.resizable(False, False)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{self.W}x250+{(sw - self.W) // 2}+{sh // 3}")
        pad = {"padx": 14}
        ttk.Label(self, text="This looks like a duplicate", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(12, 2), **pad)
        ttk.Label(self, text=head, wraplength=self.W - 28, foreground="#888").pack(anchor="w", **pad)
        ttk.Label(self, text=text, wraplength=self.W - 28, justify="left").pack(anchor="w", pady=10, **pad)
        self.v_rem = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Always do this (change later in Options > General)", variable=self.v_rem).pack(anchor="w", **pad)
        bt = ttk.Frame(self); bt.pack(fill="x", pady=12, **pad)
        ttk.Button(bt, text="Skip", command=lambda: self.pick("skip")).pack(side="left")
        ttk.Button(bt, text="Keep both (rename)", command=lambda: self.pick("rename")).pack(side="left", padx=6)
        ttk.Button(bt, text="Re-download (replace)", command=lambda: self.pick("replace")).pack(side="left")
        self.bind("<Escape>", lambda e: self.pick("skip"))
        self.protocol("WM_DELETE_WINDOW", lambda: self.pick("skip"))
        self.transient(app)
        self.lift(); self.attributes("-topmost", True); self.focus_force()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:                              # some window managers refuse the grab: the box still works
            pass

    def pick(self, choice):
        self.result, self.remember = choice, bool(self.v_rem.get())
        self.destroy()

class ProgressWin(tk.Toplevel if tk else object):
    """IDM-style download window: green progress bar, per-connection strip, speed, ETA, speed limiter."""
    W = 540

    def __init__(self, app, job):
        super().__init__(app)
        self.app, self.job, self.samples = app, job, []
        bg, fg, fld, btn = app.palette()
        track = "#2f3440" if app.v_dark.get() else "#d1d5db"
        self.configure(bg=bg)
        self.resizable(False, False)
        n = len(app.wins)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{self.W}x530+{max(0, (sw - self.W) // 2 + (n % 6) * 26)}+{max(0, (sh - 530) // 3 + (n % 6) * 26)}")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        pad = {"padx": 12}
        self.v_name, self.v_pct = tk.StringVar(), tk.StringVar(value="0%")
        ttk.Label(self, textvariable=self.v_name, font=("Segoe UI", 11, "bold"), wraplength=self.W - 24).pack(anchor="w", pady=(10, 0), **pad)
        ttk.Label(self, text=job.url, wraplength=self.W - 24, foreground="#888").pack(anchor="w", **pad)

        row = ttk.Frame(self); row.pack(fill="x", pady=(10, 2), **pad)
        self.bar = tk.Canvas(row, height=22, bg=track, highlightthickness=1, highlightbackground="#888")
        self.bar.pack(side="left", fill="x", expand=True)
        ttk.Label(row, textvariable=self.v_pct, width=6, anchor="e", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.seg = tk.Canvas(self, height=12, bg=track, highlightthickness=1, highlightbackground="#888")
        self.seg.pack(fill="x", pady=(0, 8), **pad)

        g = ttk.Frame(self); g.pack(fill="x", **pad)
        self.v = {}
        for i, (k, lbl) in enumerate((("status", "Status"), ("size", "File size"), ("got", "Downloaded"),
                                      ("rate", "Transfer rate"), ("eta", "Time left"), ("conn", "Connections"), ("src", "Sources"),
                                      ("resume", "Resume capability"), ("save", "Save to"))):
            self.v[k] = tk.StringVar()
            ttk.Label(g, text=lbl).grid(row=i, column=0, sticky="w", pady=1)
            ttk.Label(g, textvariable=self.v[k], wraplength=self.W - 190).grid(row=i, column=1, sticky="w", padx=14)

        lf = ttk.LabelFrame(self, text="Speed limiter", padding=6); lf.pack(fill="x", pady=8, **pad)
        self.v_use = tk.BooleanVar(value=job.limit > 0)
        self.v_kb = tk.StringVar(value=str(job.limit or 500))
        ttk.Checkbutton(lf, text="Use speed limiter", variable=self.v_use).pack(side="left")
        ttk.Label(lf, text="   Max speed (KB/s)").pack(side="left")
        ttk.Spinbox(lf, from_=10, to=1000000, increment=50, width=8, textvariable=self.v_kb).pack(side="left", padx=6)
        ttk.Button(lf, text="Apply", command=self.apply_limit).pack(side="left")
        self.v_lim = tk.StringVar()
        ttk.Label(lf, textvariable=self.v_lim, foreground="#888").pack(side="left", padx=8)

        bt = ttk.Frame(self); bt.pack(fill="x", pady=(0, 10), **pad)
        self.b_toggle = ttk.Button(bt, text="Pause", command=self.toggle); self.b_toggle.pack(side="left", padx=(0, 6))
        ttk.Button(bt, text="Cancel", command=self.cancel).pack(side="left", padx=(0, 6))
        self.b_open = ttk.Button(bt, text="Open", command=self.open_file); self.b_open.pack(side="left", padx=(0, 6))
        self.b_dir = ttk.Button(bt, text="Open folder", command=self.open_dir); self.b_dir.pack(side="left")
        ttk.Button(bt, text="Hide", command=self.destroy).pack(side="right")
        self.lift(); self.attributes("-topmost", True)
        self.after(500, lambda: self.winfo_exists() and self.attributes("-topmost", False))
        self.refresh()

    def apply_limit(self):
        try:
            kb = int(float(self.v_kb.get()))
        except ValueError:
            return messagebox.showwarning(APP, "Enter the speed in KB/s (a number).", parent=self)
        self.job.set_limit(kb if self.v_use.get() and kb > 0 else 0)

    def toggle(self):
        j = self.job
        if j.state in ("downloading", "queued"):
            j.pause()
        elif j.state in ("paused", "error"):
            j.state = "queued"

    def cancel(self):
        self.job.pause()
        self.destroy()

    def open_file(self):
        if self.job.state == "done" and os.path.exists(self.job.path):
            self.app._launch(self.job.path)

    def open_dir(self):
        j = self.job
        if j.path and os.path.exists(j.path) and sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(j.path)])
        else:
            self.app._launch(os.path.dirname(j.path) if j.path else self.app.v_folder.get())

    def _fill(self, c, parts):
        c.delete("all")
        w, h = max(c.winfo_width(), 2), int(float(c["height"]))
        for a, b in parts:
            if b > a:
                c.create_rectangle(a * w, 0, max(a * w + 1, b * w), h, fill=GREEN, width=0)

    def refresh(self):
        if not self.winfo_exists():
            return
        j, now = self.job, time.time()
        d = j.done_bytes
        self.samples = [x for x in self.samples + [(now, d)] if now - x[0] <= 4]
        t0, d0 = self.samples[0]
        sp = max(0.0, (d - d0) / (now - t0)) if j.state == "downloading" and now - t0 > 0.2 else 0.0
        p = j.frac
        size = j.size or (j.dl_total if j.mode == "site" else 0)
        st = {"downloading": "Receiving data...", "queued": "Waiting in queue", "paused": "Paused",
              "done": "Download complete", "error": "Error"}[j.state]
        if j.state == "downloading" and not d:
            st = "Connecting..."
        elif j.state == "downloading" and j.mode == "site" and p >= 0.999:
            st = "Finishing (merging audio + video)..."
        if j.msg and j.state == "error":
            st += ": " + j.msg
        self.title(f"{p * 100:.0f}% {j.name}" if j.state != "done" else f"Complete - {j.name}")
        self.v_name.set(j.name or j.url)
        self.v_pct.set(f"{p * 100:.0f}%")
        self.v["status"].set(st)
        self.v["size"].set(("~" if j.mode == "site" else "") + fmt(size) if size else "Unknown")
        self.v["got"].set(fmt(d) + (f"  ({p * 100:.1f}%)" if size else ""))
        self.v["rate"].set(fmt(sp) + "/s" if sp else "")
        self.v["eta"].set(hms((size - d) / sp) if sp > 0 and size and size > d and not j.is_hls else "")
        self.v["conn"].set(str(len(j.segs)) if j.ranged and j.mode == "direct" and not j.is_hls else (
            str(max(1, min(j.conns, 16))) if j.mode != "direct" or j.is_hls else "1"))
        self.v["src"].set(source_text(j))
        self.v["resume"].set("Yes" if j.ranged or j.mode != "direct" or j.is_hls else (
            "No (server does not allow it)" if j.segs else "Checking..."))
        self.v["save"].set(os.path.dirname(j.path) if j.path else j.folder)
        eff = j.limit
        self.v_lim.set(f"active: {eff} KB/s" if eff else ("global limit: %d KB/s" % (Limiter.rate // 1024) if Limiter.rate else ""))
        self.b_toggle.config(text="Pause" if j.state in ("downloading", "queued") else "Resume",
                             state="disabled" if j.state == "done" else "normal")
        for b in (self.b_open, self.b_dir):
            b.config(state="normal" if j.state == "done" else "disabled")
        self._fill(self.bar, [(0, p)])
        if j.mode == "direct" and j.ranged and size and not j.is_hls and j.segs:
            parts = [(x[0] / size, min(x[2], x[1] + 1) / size) for x in j.segs]
            self._fill(self.seg, parts)
            w = max(self.seg.winfo_width(), 2)
            for x in j.segs[1:]:
                self.seg.create_line(x[0] / size * w, 0, x[0] / size * w, 12, fill="#555")
        else:
            self._fill(self.seg, [(0, p)])
        self.after(300, self.refresh)


class FileDialog(tk.Toplevel if tk else object):
    """IDM-style 'Download File Info' box: detects the file first, then Start / Later / Cancel."""
    W = 560

    def __init__(self, app, url, headers, hint):
        super().__init__(app)
        self.app, self.url, self.headers = app, url, headers
        hn = hint.get("filename") if isinstance(hint.get("filename"), str) else ""
        hn = re.split(r"[\\/]", hn.strip())[-1]
        self.hint_name = clean(hn) if hn else ""
        try:
            self.hint_size = int(hint.get("size") or 0)
        except (TypeError, ValueError):
            self.hint_size = 0
        self.edited_name = self.edited_dir = False
        self.title("Download File Info - " + APP)
        self.configure(bg=app.palette()[0])
        self.resizable(False, False)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{self.W}x430+{(sw - self.W) // 2}+{sh // 5}")
        pad = {"padx": 12}
        guess = self.hint_name or clean(os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path)) or "download")
        self.v_name = tk.StringVar(value=guess)
        self.v_dir = tk.StringVar(value=self._default_dir(guess))
        self.v_info = tk.StringVar(value="Detecting file...")
        ttk.Label(self, text="URL").pack(anchor="w", pady=(10, 0), **pad)
        e = ttk.Entry(self); e.insert(0, url); e.config(state="readonly"); e.pack(fill="x", **pad)
        ttk.Label(self, textvariable=self.v_info, wraplength=self.W - 24, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=8, **pad)
        ttk.Label(self, text="File name").pack(anchor="w", **pad)
        self.e_name = ttk.Entry(self, textvariable=self.v_name); self.e_name.pack(fill="x", **pad)
        ttk.Label(self, text="Save to").pack(anchor="w", pady=(8, 0), **pad)
        r = ttk.Frame(self); r.pack(fill="x", **pad)
        e_dir = ttk.Entry(r, textvariable=self.v_dir); e_dir.pack(side="left", fill="x", expand=True)
        e_dir.bind("<Key>", lambda ev: setattr(self, "edited_dir", True))
        ttk.Button(r, text="Browse...", command=self.browse).pack(side="left", padx=(6, 0))
        ttk.Label(self, text="Mirror links (optional, one per line): the fastest source is used for each part").pack(
            anchor="w", pady=(8, 0), **pad)
        self.t_mir = tk.Text(self, height=3, width=10)
        self.t_mir.pack(fill="x", **pad)
        bt = ttk.Frame(self); bt.pack(fill="x", pady=14, **pad)
        self.b_start = ttk.Button(bt, text="Start Download", command=lambda: self.go(True)); self.b_start.pack(side="left")
        ttk.Button(bt, text="Download Later", command=lambda: self.go(False)).pack(side="left", padx=6)
        ttk.Button(bt, text="Cancel", command=self.close).pack(side="right")
        self.e_name.bind("<Key>", lambda ev: setattr(self, "edited_name", True))
        self.bind("<Return>", lambda ev: self.go(True))
        self.bind("<Escape>", lambda ev: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.lift(); self.attributes("-topmost", True); self.focus_force()
        self.after(600, lambda: self.winfo_exists() and self.attributes("-topmost", False))
        self.e_name.focus_set()
        if self.hint_size or self.hint_name:
            self.v_info.set(f"{describe(guess)}  |  {fmt(self.hint_size) if self.hint_size else 'size unknown'}  |  checking server...")
        threading.Thread(target=self._probe, daemon=True).start()

    def _default_dir(self, name):
        return os.path.join(self.app.v_folder.get(), category(name))

    def _probe(self):
        try:
            res = probe_url(self.url, self.headers)
        except Exception as e:
            res = {"error": short_err(e)}
        self.app.cmds.put(("probed", self, res))

    def apply(self, res):
        if "error" in res:
            self.v_info.set("Could not read file info (" + res["error"] + "). You can still try to download.")
            return
        name = self.hint_name or res["name"]
        if not self.edited_name:
            self.v_name.set(name)
        if not self.edited_dir:
            self.v_dir.set(self._default_dir(self.v_name.get()))
            self.edited_dir = False
        size = res["size"] or self.hint_size
        parts = [describe(self.v_name.get(), res["ctype"]), fmt(size) if size else "size unknown",
                 "Resume supported" if res["ranged"] else "No resume support"]
        if res["html"] and os.path.splitext(self.v_name.get())[1].lower() in ALL_EXT:
            parts.append("WARNING: server sent a web page (login needed?)")
        if torrent.is_torrent_url(self.url, res["ctype"]):
            parts.append("Torrent file (opens in your torrent client when done)")
        folder = self.v_dir.get().strip()
        dd = dupes.find_dupes(self.url, clean(self.v_name.get()), folder, True, self.app.jobs) if os.path.isabs(folder) else []
        if dd:
            parts.append("DUPLICATE: " + {"url": "link is already in the list", "name": "same file name is in the list",
                                         "disk": "file already exists in that folder"}[dd[0].kind])
        self.v_info.set("  |  ".join(parts))

    def browse(self):
        p = filedialog.askdirectory(parent=self, initialdir=self.v_dir.get() if os.path.isdir(self.v_dir.get()) else None)
        if p:
            self.v_dir.set(os.path.normpath(p)); self.edited_dir = True

    def go(self, now):
        name, folder = clean(self.v_name.get()), self.v_dir.get().strip()
        if not os.path.isabs(folder):
            return messagebox.showwarning(APP, "Choose a full folder path to save to.", parent=self)
        mirrors = [l.strip() for l in self.t_mir.get("1.0", "end").splitlines() if l.strip()]
        j = self.app._add_url(self.url, self.headers, title=name, start=now, fname=name, folder=folder,
                              mode="direct", keepname=True, mirrors=mirrors)
        if j is None and self.app.skipped:           # the person chose Skip: keep this box open so the name can be changed
            return
        self.close()
        if j and now:
            self.app._show_progress(j)

    def close(self):
        self.app.asking.pop(self.url, None)
        self.destroy()


class App(tk.Tk if tk else object):
    def __init__(self):
        super().__init__()
        self.title(f"{APP} {VERSION}")
        self.geometry("1040x600")
        self.minsize(840, 420)
        self.withdraw()
        self.sp = None
        try:
            lp, ico = asset("logo.png"), asset("icon.ico")
            if lp:
                self._icon = tk.PhotoImage(file=lp)
                self.iconphoto(True, self._icon)
            if ico and sys.platform.startswith("win"):
                self.iconbitmap(ico)
        except Exception as e:
            log.info("window icon skipped: %s", e)
        self._splash()
        self.jobs, self.last, self.inbox = [], {}, queue.Queue()
        self.wins, self.asking = {}, {}
        self.last_clip, self.was_active, self.link = "", False, ""
        cfg = read_json(CFG, {})
        cfg = cfg if isinstance(cfg, dict) else {}

        def num(key, default, lo, hi):
            try: return max(lo, min(hi, int(cfg.get(key, default))))
            except (TypeError, ValueError): return default
        self.v_folder = tk.StringVar(value=cfg.get("folder", DEFAULT_DIR))
        self.v_conns = tk.IntVar(value=num("conns", 8, 1, 32))
        self.v_active = tk.IntVar(value=num("active", 3, 1, 10))
        self.v_limit = tk.IntVar(value=num("limit", 0, 0, 1000000))
        self.v_clip = tk.BooleanVar(value=cfg.get("clip", False))
        self.v_show = tk.BooleanVar(value=cfg.get("show", True))
        self.v_cats = tk.BooleanVar(value=True)
        self.v_off = tk.BooleanVar(value=False)
        self.v_filter = tk.StringVar(value="All")
        self.v_proxy = tk.StringVar(value=cfg.get("proxy", ""))
        self.v_sched = tk.StringVar(value="")
        self.v_dark = tk.BooleanVar(value=cfg.get("dark", False))
        self.v_quality = tk.StringVar(value=cfg.get("quality", "Best"))
        self.v_browser = tk.StringVar(value=cfg.get("browser", "None"))
        pol = cfg.get("dup", "ask")
        self.v_dup = tk.StringVar(value=pol if pol in dupes.POLICIES else "ask")
        self.v_dup_lbl = tk.StringVar(value=DUP_LABELS[self.v_dup.get()])
        self.v_tclient = tk.StringVar(value=str(cfg.get("torrent_client", "")))
        self.v_tauto = tk.BooleanVar(value=bool(cfg.get("torrent_auto", True)))
        self.skipped, self._dup_override = False, None
        self.hist, self.stats = History(), Stats()
        self._ctr, self._st_t = Counter.total, time.time()
        self.cmds, self.pub_folder, self.pub_ffmpeg = queue.Queue(), self.v_folder.get(), bool(ffmpeg_path())
        self._proxy, self._sched_txt, self._sched_at, self.toast = None, "", None, ("", 0)
        self._last_save, self.pairing = 0.0, Pairing(ask=self._ask_pair)
        st = ttk.Style()
        try: st.theme_use("clam")
        except tk.TclError: pass
        st.configure("Treeview", rowheight=26)
        self._ui()
        self._theme()
        self._load()
        self._seed_history()
        self._server()
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self._tick()
        self.after(1800 if self.sp is not None else 0, self._show_main)

    # ---- UI ----
    def _ui(self):
        st = ttk.Style()
        st.configure("Tool.TButton", padding=(8, 3), font=("Segoe UI", 9))
        self._menu()
        tb = ttk.Frame(self, padding=(6, 6, 6, 2)); tb.pack(fill="x")
        for item in (("\uff0b", "Add URL", self._add_dialog), ("\u25b6", "Resume", self._resume),
                        ("\u25a0", "Stop", self._pause), ("\u25a0\u25a0", "Stop All", self._pause_all),
                        ("\u2716", "Delete", self._delete), ("\u2714", "Delete Completed", lambda: self._delete(True)),
                        None,
                        ("\u2699", "Options", self._options), ("\u25f7", "Scheduler", self._scheduler),
                        ("\u25b6\u25b6", "Start Queue", self._resume_all), ("\u25a0", "Stop Queue", self._pause_all)):
            if item is None:
                ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6, pady=2)
                continue
            g, t, f = item
            ttk.Button(tb, text=f"{g}\n{t}", style="Tool.TButton", command=f).pack(side="left", padx=2)
        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=(4, 0))

        self.status = ttk.Label(self, anchor="w", padding=(8, 4)); self.status.pack(side="bottom", fill="x")
        self.paned = ttk.PanedWindow(self, orient="horizontal"); self.paned.pack(fill="both", expand=True)
        self.left = ttk.Frame(self, width=190)
        self.cats = ttk.Treeview(self.left, show="tree", selectmode="browse")
        self.cats.pack(fill="both", expand=True)
        self.cats.insert("", "end", "All", text="All Downloads", open=True)
        for c in list(CATS) + ["Other"]:
            self.cats.insert("All", "end", c, text=c)
        self.cats.insert("", "end", "Unfinished", text="Unfinished")
        self.cats.insert("", "end", "Completed", text="Finished")
        self.cats.insert("", "end", "Queues", text="Queues", open=True)
        self.cats.insert("Queues", "end", "Active", text="Main download queue")
        self.cats.selection_set("All")
        self.cats.bind("<<TreeviewSelect>>", self._cat_pick)
        right = ttk.Frame(self)
        self.paned.add(self.left, weight=0)
        self.paned.add(right, weight=1)

        cols = ("name", "size", "progress", "speed", "eta", "status")
        titles = {"name": "File name", "size": "Size", "progress": "Progress", "speed": "Transfer rate",
                  "eta": "Time left", "status": "Status"}
        self.tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="extended")
        for c, w in zip(cols, (330, 90, 170, 100, 80, 190)):
            self.tree.heading(c, text=titles[c]); self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._open_or_progress())
        self.tree.bind("<Delete>", lambda e: self._delete())
        self.menu = tk.Menu(self, tearoff=0)
        for t, f in (("Show progress window", self._open_or_progress), ("Resume", self._resume), ("Stop", self._pause),
                     ("Open file", self._open_file), ("Open folder", self._open_folder), ("Copy link", self._copy),
                     ("Edit link (refresh expired)", self._edit_url), ("Add mirror links...", self._add_mirrors),
                     ("Send to torrent client", self._send_selected_torrent), ("Verify checksum", self._verify),
                     ("Move up", lambda: self._move(-1)), ("Move down", lambda: self._move(1)),
                     ("Delete", self._delete)):
            self.menu.add_command(label=t, command=f)
        self.tree.bind("<Button-3>", lambda e: (self.tree.identify_row(e.y) and
                       (self.tree.selection_set(self.tree.identify_row(e.y)), self.menu.tk_popup(e.x_root, e.y_root))))

    def _menu(self):
        mb = tk.Menu(self)
        self.config(menu=mb)

        def sub(label, items):
            m = tk.Menu(mb, tearoff=0)
            for it in items:
                if it is None: m.add_separator()
                elif len(it) == 2: m.add_command(label=it[0], command=it[1])
                else: m.add_checkbutton(label=it[0], command=it[1], variable=it[2])
            mb.add_cascade(label=label, menu=m)
        sub("Tasks", [("Add new download...", self._add_dialog), ("Add batch download...", self._batch),
                      ("Add torrent file...", self._add_torrent_file), None,
                      ("Download history...", self._history_win), ("Statistics...", self._stats_win), None,
                      ("Options...", self._options), ("Scheduler...", self._scheduler), None, ("Exit", self._quit)])
        sub("File", [("Open file", self._open_file), ("Open folder", self._open_folder), None,
                     ("Copy link", self._copy), ("Edit link (refresh expired)...", self._edit_url),
                     ("Add mirror links...", self._add_mirrors), ("Send to torrent client", self._send_selected_torrent),
                     ("Verify checksum...", self._verify)])
        sub("Downloads", [("Resume", self._resume), ("Stop", self._pause), ("Stop all", self._pause_all), None,
                          ("Delete", self._delete), ("Delete all completed", lambda: self._delete(True)), None,
                          ("Move up", lambda: self._move(-1)), ("Move down", lambda: self._move(1)), None,
                          ("Start queue", self._resume_all), ("Stop queue", self._pause_all)])
        sub("View", [("Categories panel", self._toggle_cats, self.v_cats), ("Dark mode", self._theme, self.v_dark),
                     ("Show progress window", lambda: None, self.v_show)])
        sub("Help", [("Quick guide", self._guide), ("Install the browser extension", self._ext_help), None,
                     ("Check for updates", self._check_update), ("Open data and log folder", self._open_data), None,
                     ("About " + APP, self._about)])

    def _cat_pick(self, e=None):
        s = self.cats.selection()
        if s:
            self.v_filter.set("Active" if s[0] == "Queues" else s[0])

    def _toggle_cats(self):
        if self.v_cats.get():
            self.paned.insert(0, self.left, weight=0)
        else:
            self.paned.forget(self.left)

    def _top(self, title, w, h):
        t = tk.Toplevel(self)
        t.title(title); t.configure(bg=self.palette()[0]); t.resizable(False, False)
        t.geometry(f"{w}x{h}+{self.winfo_rootx() + 60}+{self.winfo_rooty() + 60}")
        return t

    def _options(self):
        o = getattr(self, "_opt", None)
        if o is not None and o.winfo_exists():
            return o.lift()
        w = self._opt = self._top("Options", 560, 440)
        nb = ttk.Notebook(w); nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        def page(t):
            f = ttk.Frame(nb, padding=14); nb.add(f, text=t); return f

        def spin(f, r, lbl, var, lo, hi, inc):
            ttk.Label(f, text=lbl).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Spinbox(f, from_=lo, to=hi, increment=inc, width=9, textvariable=var).grid(row=r, column=1, sticky="w", padx=10)
        g = page("General")
        spin(g, 0, "Connections per download (1-32)", self.v_conns, 1, 32, 1)
        spin(g, 1, "Downloads at the same time", self.v_active, 1, 10, 1)
        for r, (t, v, c) in enumerate((("Show the progress window when a download starts", self.v_show, None),
                                       ("Watch the clipboard for download links", self.v_clip, None),
                                       ("Shut down the PC when all downloads finish", self.v_off, None),
                                       ("Dark mode", self.v_dark, self._theme)), start=2):
            ttk.Checkbutton(g, text=t, variable=v, command=c).grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(g, text="When a duplicate is found").grid(row=6, column=0, sticky="w", pady=(10, 3))
        cb = ttk.Combobox(g, textvariable=self.v_dup_lbl, values=list(DUP_LABELS.values()), width=32, state="readonly")
        cb.grid(row=6, column=1, sticky="w", padx=10, pady=(10, 3))
        cb.bind("<<ComboboxSelected>>", lambda e: self.v_dup.set(
            next((k for k, v in DUP_LABELS.items() if v == self.v_dup_lbl.get()), "ask")))
        sv = page("Save to")
        ttk.Label(sv, text="Default download folder (files are sorted into sub-folders by type)").pack(anchor="w")
        r = ttk.Frame(sv); r.pack(fill="x", pady=8)
        ttk.Entry(r, textvariable=self.v_folder).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="Browse...", command=lambda: self.v_folder.set(
            os.path.normpath(filedialog.askdirectory(parent=w) or self.v_folder.get()))).pack(side="left", padx=(6, 0))
        cn = page("Connection")
        spin(cn, 0, "Speed limit for all downloads, KB/s (0 = none)", self.v_limit, 0, 1000000, 100)
        ttk.Label(cn, text="Proxy (http://host:port)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(cn, textvariable=self.v_proxy, width=28).grid(row=1, column=1, sticky="w", padx=10)
        vd = page("Video")
        ttk.Label(vd, text="Default video quality").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(vd, textvariable=self.v_quality, values=list(QUALITIES), width=18, state="readonly").grid(row=0, column=1, padx=10)
        ttk.Label(vd, text="Use login cookies from").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(vd, textvariable=self.v_browser, values=["None", "firefox", "chrome", "edge", "brave"],
                     width=18, state="readonly").grid(row=1, column=1, padx=10)
        ttk.Label(vd, text="Needed for private videos (e.g. some Instagram posts). Firefox works best.",
                  foreground="#888").grid(row=2, column=0, columnspan=2, sticky="w", pady=6)
        tp = page("Torrents")
        ttk.Label(tp, text="Magnet links and .torrent files are handed to a torrent client.\n"
                           "IK DOWNLOADER does not download torrents itself.", foreground="#888").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Label(tp, text="Torrent client (empty = detect)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(tp, textvariable=self.v_tclient, width=30).grid(row=1, column=1, sticky="w", padx=8)
        found = tk.StringVar(value="Using: " + torrent.describe_client(self.v_tclient.get()))

        def pick_client():
            f = filedialog.askopenfilename(parent=w, title="Choose your torrent program")
            if f:
                self.v_tclient.set(os.path.normpath(f))
                found.set("Using: " + torrent.describe_client(self.v_tclient.get()))
        ttk.Button(tp, text="Browse...", command=pick_client).grid(row=1, column=2)
        ttk.Label(tp, textvariable=found, wraplength=500, foreground="#888").grid(row=2, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Checkbutton(tp, text="Open downloaded .torrent files in the torrent client automatically",
                        variable=self.v_tauto).grid(row=3, column=0, columnspan=3, sticky="w", pady=6)
        br = page("Browser")
        n = self.pairing.count()
        self._pair_txt = tk.StringVar(value=f"Connected browser extensions: {n}")
        ttk.Label(br, textvariable=self._pair_txt).grid(row=0, column=0, sticky="w", pady=4)

        def unpair():
            if messagebox.askyesno(APP, "Disconnect all browser extensions?\n\nThey must be approved again "
                                        "the next time they send a download.", parent=w):
                self.pairing.revoke_all()
                self._pair_txt.set("Connected browser extensions: 0")
        ttk.Button(br, text="Disconnect all browsers", command=unpair).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(br, text="The extension must be approved here once. Only approve an extension you installed yourself.",
                  wraplength=470, foreground="#888").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Button(w, text="OK", command=w.destroy).pack(side="right", padx=10, pady=10)

    def _scheduler(self):
        u = simpledialog.askstring("Scheduler", "Start the download queue at (HH:MM, 24-hour clock).\n"
                                   "Leave empty to cancel the schedule.", initialvalue=self.v_sched.get(), parent=self)
        if u is None:
            return
        u = u.strip()
        if u and not re.fullmatch(r"(\d{1,2}):(\d{2})", u):
            return messagebox.showwarning(APP, "Use the format HH:MM, for example 23:30.")
        self.v_sched.set(u)
        if u:
            for j in self.jobs:
                if j.state in ("paused", "error"): j.state = "queued"
            messagebox.showinfo(APP, f"All unfinished downloads will start at {u}.")

    def _guide(self):
        messagebox.showinfo(APP + " - Quick guide",
            "Videos: play a video in your browser and click the Download bar on it, pick a quality, press Start.\n\n"
            "Any other file (zip, exe, pdf ...): press the website's download button. A Download File Info box "
            "opens - choose Start Download, Download Later or Cancel.\n\n"
            "Each running download has its own window with a speed limiter. Double-click a download in the list to open it.\n\n"
            "Downloads > Start queue / Stop queue starts or pauses everything. Tasks > Scheduler starts the queue at a set time.\n\n"
            "Mirrors: add more links of the same file (in the Download File Info box or right-click > Add mirror links) - the fastest source is used for each part.\n\n"
            "Magnet links and .torrent files are handed to your torrent client (Options > Torrents).\n\n"
            "Tasks > Download history searches everything you downloaded; Tasks > Statistics shows totals and average speed.\n\n"
            "Keep this program open while you browse.")

    def _ext_dir(self):
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else BUNDLE
        for d in (os.path.join(base, "browser-extension"), os.path.join(base, "extension")):
            if os.path.isdir(d): return d
        return None

    def _open_data(self):
        os.makedirs(HOME, exist_ok=True)
        self._launch(HOME)

    def _ask_pair(self, origin):
        """Called from the local server thread: ask the user (in the main thread) whether to trust an extension."""
        ev, box = threading.Event(), {}
        self.cmds.put(("pair", origin, ev, box))
        ev.wait(120)
        return bool(box.get("ok"))

    def _ext_help(self):
        d = self._ext_dir()
        messagebox.showinfo(APP, "1. Open  chrome://extensions  (or edge://extensions)\n2. Turn on Developer mode\n"
                            "3. Click Load unpacked and choose the browser-extension folder\n"
                            "4. The first download you send asks for approval here - click Yes" +
                            ("\n\nThe folder opens now." if d else "\n\n(Download IK-DOWNLOADER-browser-extension.zip from Releases and unzip it.)"))
        if d: self._launch(d)

    def _check_update(self):
        def run():
            try:
                if "YOUR-GITHUB-USER" in REPO:
                    raise ValueError("This build has no GitHub repository set (REPO in ikd/common.py).")
                req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                             headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
                with netsec.urlopen(req, timeout=10) as r:
                    d = json.loads(r.read())
                tag = str(d.get("tag_name") or "")
                url = f"https://github.com/{REPO}/releases/latest"
                if version_tuple(tag) > version_tuple(VERSION):
                    self.cmds.put(("update", f"A newer version is available: {tag}\nYou have {VERSION}.", url))
                else:
                    self.cmds.put(("update", f"You have the latest version ({VERSION}).", ""))
            except Exception as e:
                log.info("update check failed: %s", short_err(e))
                self.cmds.put(("update", "Could not check for updates:\n" + short_err(e), ""))
        threading.Thread(target=run, daemon=True).start()

    def _about(self):
        w = self._top("About " + APP, 380, 300)
        lp = asset("logo.png")
        if lp:
            try:
                w._img = tk.PhotoImage(file=lp).subsample(2)
                ttk.Label(w, image=w._img).pack(pady=(16, 4))
            except tk.TclError:
                pass
        ttk.Label(w, text=APP, font=("Segoe UI", 15, "bold")).pack()
        ttk.Label(w, text=f"Version {VERSION}").pack()
        ttk.Label(w, text="Fast multi-connection download manager\nwith a browser video bar.", justify="center").pack(pady=8)
        ttk.Label(w, text="Free software (MIT license). Download only content you have the right to.",
                  wraplength=340, justify="center", foreground="#888").pack()
        ttk.Button(w, text="OK", command=w.destroy).pack(pady=12)

    def _splash(self):
        self.sp = None
        p = asset("splash.png")
        if not p:
            return
        try:
            img = tk.PhotoImage(file=p)
            sp = tk.Toplevel(self)
            sp.overrideredirect(True)
            tk.Label(sp, image=img, borderwidth=0).pack()
            sp.img = img
            w, h = img.width(), img.height()
            sp.geometry(f"{w}x{h}+{(self.winfo_screenwidth() - w) // 2}+{(self.winfo_screenheight() - h) // 2}")
            sp.attributes("-topmost", True)
            sp.update()
            self.sp = sp
        except Exception as e:
            log.info("splash screen skipped: %s", e)
            self.sp = None

    def _show_main(self):
        if self.sp is not None:
            self.sp.destroy(); self.sp = None
        self.deiconify(); self.lift()

    # ---- helpers ----
    def _i(self, var, default):
        try: return int(var.get())
        except (tk.TclError, ValueError): return default

    def _sel(self):
        m = {str(id(j)): j for j in self.jobs}
        return [m[i] for i in self.tree.selection() if i in m]

    def _launch(self, p):
        if sys.platform.startswith("win"): os.startfile(p)
        elif sys.platform == "darwin": subprocess.Popen(["open", p])
        else: subprocess.Popen(["xdg-open", p])

    def _find(self, url):
        return next((j for j in self.jobs if j.url == url and j.state != "done"), None)

    def _show_progress(self, j, force=False):
        """Open (or bring forward) the IDM-style progress window of a download."""
        if not (force or self.v_show.get()):
            return
        w = self.wins.get(id(j))
        try:
            if w is not None and w.winfo_exists():
                w.deiconify(); w.lift(); return
        except tk.TclError:
            pass
        self.wins = {k: v for k, v in self.wins.items() if v.winfo_exists()}
        self.wins[id(j)] = ProgressWin(self, j)

    def _add_show(self, *a, **k):
        j = self._add_url(*a, **k)
        if j and j.state == "queued":
            self._show_progress(j)
        return j

    def _ask(self, url, headers, hint=None):
        """Browser/Add-URL download of a plain file: detect it first, then show the Start / Cancel box."""
        url = url.strip()
        if not re.match(r"https?://", url, re.I):
            return
        j = self._find(url)
        if j:
            return self._show_progress(j, force=True)
        if url in self.asking:
            try: self.asking[url].lift(); return
            except tk.TclError: pass
        self.asking[url] = FileDialog(self, url, headers, hint or {})

    def _guess_name(self, url, mode, fname="", title="", keepname=False):
        """The file name a download will most likely get, or "" when it is not known before the transfer."""
        if fname and fname.strip():
            return clean(fname)
        if mode != "direct":
            return ""
        if keepname and title:
            return clean(title)
        base = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
        return clean(base) if base and os.path.splitext(base)[1].lower() in KNOWN_EXT else ""

    def _drop_job(self, j):
        """Remove a list entry (and its unfinished .part file); a finished file on disk is left alone."""
        j.pause()
        w = self.wins.pop(id(j), None)
        try:
            if w is not None and w.winfo_exists(): w.destroy()
        except tk.TclError:
            pass
        if j.state != "done" and j.path and os.path.exists(j.path + ".part"):
            try: os.remove(j.path + ".part")
            except OSError: pass
        if j in self.jobs: self.jobs.remove(j)
        self.last.pop(id(j), None)

    def _ask_dup(self, head, dups):
        dlg = DupDialog(self, head, dupes.summary(dups))
        self.wait_window(dlg)
        if dlg.remember and dlg.result in dupes.POLICIES:
            self.v_dup.set(dlg.result); self.v_dup_lbl.set(DUP_LABELS[dlg.result])
        return dlg.result

    def _dup_decision(self, url, name, folder, exact):
        """Duplicate check for one new download. None = skip it; otherwise (file name to use, replace the old file?)."""
        dd = dupes.find_dupes(url, name, folder, exact, self.jobs)
        if not dd:
            return name, False
        pol = self._dup_override or self.v_dup.get()
        pol = pol if pol in dupes.POLICIES else "ask"
        choice = dupes.choose(pol, self._ask_dup(name or url, dd) if pol == "ask" else None)
        label = (name or url)[:70]
        if choice == dupes.SKIP:
            self.skipped = True
            live = next((d.job for d in dd if d.job is not None and d.job.state != "done"), None)
            if live is not None:
                self._show_progress(live, force=True)
            self.toast = (f"Skipped duplicate: {label}", time.time())
            return None
        if choice == dupes.RENAME:
            return (dupes.unique_name(name, folder, exact, self.jobs) if name else name), False
        for d in dd:
            if d.job is not None:
                self._drop_job(d.job)
        return name, True

    def _add_url(self, url, headers=None, title="", start=True, site=False,
                 quality=None, fname="", folder=None, mode=None, keepname=False, mirrors=None, check=True):
        """Add a download. Returns the new Job, or None (invalid link, magnet link, or skipped duplicate:
        `self.skipped` tells the last one apart)."""
        url = url.strip()
        self.skipped = False
        if torrent.is_magnet(url):
            self._add_magnet(url)
            return None
        if not re.match(r"https?://", url, re.I):
            return None
        mode = mode or ("site" if site else guess_mode(url))
        fol, exact = folder or self.v_folder.get(), bool(folder)
        name, replace = self._guess_name(url, mode, fname, title, keepname), False
        if check:
            dec = self._dup_decision(url, name, fol, exact)
            if dec is None:
                return None
            new, replace = dec
            if new != name:                                  # "keep both": the new file gets a free name
                fname, keepname = new, True
        j = Job(url, fol, self._i(self.v_conns, 8), headers, title)
        j.exact, j.fname = exact, clean(fname) if fname.strip() else ""
        j.keepname, j.replace = keepname, replace
        j.mode = mode
        j.quality = quality or QUALITIES.get(self.v_quality.get(), "best")
        j.set_mirrors(mirrors)
        host = urllib.parse.urlparse(url).netloc
        j.name = clean(title) if title else (os.path.basename(urllib.parse.unquote(
            urllib.parse.urlparse(url).path)) or url)
        if j.mode == "site":
            j.name = j.fname or "Video: " + host
        self.jobs.append(j)
        if start:
            j.state = "queued"
        return j

    # ---- torrents and magnet links ----
    def _add_magnet(self, uri):
        """A magnet link: hand it to a torrent client (warn first if it was already sent)."""
        uri = uri.strip()
        if not torrent.valid_magnet(uri):
            self.toast = ("Invalid magnet link", time.time())
            return False
        info = torrent.parse_magnet(uri)
        prev = self.hist.last_for_btih(info["btih"])
        if prev:
            pol = self._dup_override or self.v_dup.get()
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(prev.get("ts", 0)))
            if pol == dupes.SKIP or (pol not in (dupes.RENAME, dupes.REPLACE) and not messagebox.askyesno(
                    APP, f"This torrent was already sent to the torrent client on {when}.\n\nSend it again?")):
                self.skipped = True
                self.toast = ("Skipped duplicate torrent", time.time())
                return False
        return self._send_torrent(uri, torrent.display_name(uri), info["btih"])

    def _send_torrent(self, target, name="", btih=""):
        """Hand a magnet link or .torrent file to the client. Never raises: failures become a message."""
        try:
            who = torrent.send(target, self.v_tclient.get().strip(), self.v_folder.get())
        except torrent.HandoffError as e:
            log.info("torrent hand-off failed: %s", e)
            if torrent.is_magnet(target):
                try:
                    self.clipboard_clear(); self.clipboard_append(target)
                except tk.TclError:
                    pass
            messagebox.showwarning(APP, f"Could not open the torrent in a client: {e}.\n\n"
                                        "Install a torrent program (for example qBittorrent) or choose one in "
                                        "Options > Torrents." + ("\n\nThe magnet link was copied to the clipboard."
                                                                  if torrent.is_magnet(target) else ""))
            return False
        self.hist.add(name or os.path.basename(target), target, "torrent", mode="torrent", btih=btih, note="sent to " + who)
        self.toast = (f"Sent to {who}: {(name or target)[:60]}", time.time())
        return True

    def _add_torrent_file(self):
        f = filedialog.askopenfilename(parent=self, title="Choose a .torrent file",
                                       filetypes=[("Torrent files", "*.torrent"), ("All files", "*.*")])
        if f:
            self._send_torrent(f, os.path.basename(f))

    def _send_selected_torrent(self):
        for j in self._sel()[:1]:
            if j.state == "done" and j.path and j.path.lower().endswith(".torrent") and os.path.exists(j.path):
                self._send_torrent(j.path, j.name)
            else:
                messagebox.showinfo(APP, "Select a finished .torrent download first.")

    def _add_mirrors(self):
        s = self._sel()
        if not s:
            return messagebox.showinfo(APP, "Select a download first.")
        j = s[0]
        if j.mode == "site" or j.is_hls:
            return messagebox.showinfo(APP, "Mirror links work for normal file downloads (not video sites or streams).")
        u = simpledialog.askstring(APP, "Mirror links of the SAME file (separate several with spaces).\n"
                                        "The fastest source is used for each part. Empty = remove mirrors.",
                                   initialvalue=" ".join(j.mirrors), parent=self)
        if u is None:
            return
        j.set_mirrors(u.split())
        messagebox.showinfo(APP, f"{len(j.mirrors)} mirror link(s) saved. They are checked and used the next time "
                                 "this download starts" + (" (press Stop, then Resume)." if j.state == "downloading" else "."))

    # ---- history and statistics ----
    def _record(self, j, status):
        done = status == "done"
        self.hist.add(j.name, j.url, status, path=j.path if done else "", size=j.size if done else j.done_bytes,
                      secs=j.active, mode=j.mode, note="" if done else j.msg)

    def _seed_history(self):
        """First start with this feature: put the downloads that are already finished into the history."""
        if not self.hist.fresh:
            return
        for j in self.jobs:
            if j.state == "done":
                try: ts = os.path.getmtime(j.path)
                except OSError: ts = time.time()
                self.hist.add(j.name, j.url, "done", path=j.path, size=j.size, secs=j.active, mode=j.mode, ts=ts)
        self.hist.save(force=True)

    def _stats_win(self):
        o = getattr(self, "_sw", None)
        if o is not None and o.winfo_exists():
            return o.lift()
        w = self._sw = self._top("Statistics", 560, 560)
        pad = {"padx": 14}
        ttk.Label(w, text="Statistics", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(12, 6), **pad)
        g = ttk.Frame(w); g.pack(fill="x", **pad)
        for c, t in enumerate(("", "This session", "All time")):
            ttk.Label(g, text=t, font=("Segoe UI", 9, "bold")).grid(row=0, column=c, sticky="w", padx=(0, 28))
        cells = []
        for r in range(5):
            row = [ttk.Label(g) for _ in range(3)]
            for c, lab in enumerate(row):
                lab.grid(row=r + 1, column=c, sticky="w", padx=(0, 28), pady=2)
            cells.append(row)
        self._since = tk.StringVar()
        ttk.Label(w, textvariable=self._since, foreground="#888").pack(anchor="w", pady=(6, 0), **pad)
        ttk.Label(w, text="Average speed = data received / time something was actually downloading.",
                  foreground="#888", wraplength=520).pack(anchor="w", **pad)
        ttk.Label(w, text="By file type (finished downloads)", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(12, 2), **pad)
        v_cat = tk.StringVar()
        ttk.Label(w, textvariable=v_cat, justify="left").pack(anchor="w", **pad)
        ttk.Label(w, text="Biggest sources", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(12, 2), **pad)
        v_host = tk.StringVar()
        ttk.Label(w, textvariable=v_host, justify="left").pack(anchor="w", **pad)

        def refresh():
            if not w.winfo_exists():
                return
            for r, (lab, a, b) in enumerate(format_summary(self.stats.summary())):
                cells[r][0].config(text=lab); cells[r][1].config(text=a); cells[r][2].config(text=b)
            since = self.stats.life["since"]
            self._since.set("Counting since " + time.strftime("%Y-%m-%d", time.localtime(since)))
            pc = self.hist.per_category()
            v_cat.set("\n".join(f"{k}:  {v['files']} file(s), {fmt(v['bytes'])}"
                                for k, v in sorted(pc.items(), key=lambda kv: -kv[1]["bytes"])) or "nothing yet")
            v_host.set("\n".join(f"{h}:  {fmt(b)}" for h, b in self.hist.top_hosts()) or "nothing yet")
            w.after(1000, refresh)

        def reset():
            if messagebox.askyesno(APP, "Reset the all-time statistics to zero?\n\nThe download history is not touched.", parent=w):
                self.stats.reset_life()
        bt = ttk.Frame(w); bt.pack(side="bottom", fill="x", pady=10, **pad)
        ttk.Button(bt, text="Reset all-time statistics", command=reset).pack(side="left")
        ttk.Button(bt, text="Close", command=w.destroy).pack(side="right")
        refresh()

    def _history_win(self):
        o = getattr(self, "_hw", None)
        if o is not None and o.winfo_exists():
            return o.lift()
        w = self._hw = tk.Toplevel(self)
        w.title("Download history - " + APP); w.configure(bg=self.palette()[0]); w.geometry("960x540")
        top = ttk.Frame(w, padding=(8, 8, 8, 4)); top.pack(fill="x")
        ttk.Label(top, text="Search").pack(side="left")
        q, st = tk.StringVar(), tk.StringVar(value="All")
        e = ttk.Entry(top, textvariable=q); e.pack(side="left", fill="x", expand=True, padx=8)
        kinds = {"All": "all", "Completed": "done", "Failed": "error", "Torrents": "torrent"}
        ttk.Combobox(top, textvariable=st, values=list(kinds), width=11, state="readonly").pack(side="left")
        cols = ("when", "name", "size", "speed", "status", "where")
        tv = ttk.Treeview(w, columns=cols, show="headings", selectmode="extended")
        for c, t, wd in (("when", "Date", 120), ("name", "File name", 270), ("size", "Size", 80),
                         ("speed", "Avg speed", 90), ("status", "Status", 110), ("where", "Folder / link", 270)):
            tv.heading(c, text=t); tv.column(c, width=wd, anchor="w")
        sb = ttk.Scrollbar(w, orient="vertical", command=tv.yview); tv.configure(yscrollcommand=sb.set)
        info = ttk.Label(w, anchor="w", padding=(8, 4)); info.pack(side="bottom", fill="x")
        bt = ttk.Frame(w, padding=(8, 4)); bt.pack(side="bottom", fill="x")
        sb.pack(side="right", fill="y"); tv.pack(side="left", fill="both", expand=True)
        rows = {}

        def fill(*_):
            found = self.hist.search(q.get(), kinds.get(st.get(), "all"))
            shown = found[:2000]
            rows.clear()
            tv.delete(*tv.get_children())
            for i, r in enumerate(shown):
                rows[str(i)] = r
                stat = {"done": "Completed", "error": "Failed", "torrent": "Torrent"}.get(r.get("status"), "?")
                if r.get("note") and r.get("status") != "done":
                    stat += ": " + r["note"][:40]
                tv.insert("", "end", iid=str(i), values=(
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("ts", 0))), r.get("name", ""),
                    fmt(r["size"]) if r.get("size") else "", fmt(r["avg"]) + "/s" if r.get("avg") else "", stat,
                    os.path.dirname(r["path"]) if r.get("path") else r.get("url", "")[:80]))
            tot = sum(int(r.get("size") or 0) for r in found if r.get("status") == "done")
            info.config(text=f"{len(found)} match(es)" + (f", showing the newest {len(shown)}" if len(found) > len(shown) else "")
                        + (f"   |   {fmt(tot)} in completed downloads" if tot else ""))
        q.trace_add("write", fill); st.trace_add("write", fill)

        def chosen():
            return [rows[i] for i in tv.selection() if i in rows]

        def again():
            for r in chosen():
                u = r.get("url", "")
                if torrent.is_magnet(u): self._add_magnet(u)
                elif re.match(r"https?://", u, re.I):
                    if guess_mode(u) == "direct" and ".m3u8" not in u.lower(): self._ask(u, {})
                    else: self._add_show(u)
                else: messagebox.showinfo(APP, "This entry cannot be downloaded again.", parent=w)

        def open_file():
            for r in chosen()[:1]:
                if r.get("path") and os.path.exists(r["path"]): self._launch(r["path"])
                else: messagebox.showinfo(APP, "The file is no longer there.", parent=w)

        def open_dir():
            for r in chosen()[:1]:
                d = os.path.dirname(r.get("path", ""))
                if d and os.path.isdir(d): self._launch(d)

        def copy():
            for r in chosen()[:1]:
                self.clipboard_clear(); self.clipboard_append(r.get("url", ""))

        def remove():
            sel = chosen()
            if sel and messagebox.askyesno(APP, f"Remove {len(sel)} entr{'y' if len(sel) == 1 else 'ies'} from the history?\n\n"
                                                "Downloaded files are not deleted.", parent=w):
                self.hist.remove(sel); self.hist.save(); fill()

        def clear():
            if messagebox.askyesno(APP, "Clear the whole download history?\n\nDownloaded files and statistics are not touched.", parent=w):
                self.hist.clear(); self.hist.save(); fill()
        for t, f in (("Download again", again), ("Open file", open_file), ("Open folder", open_dir),
                     ("Copy link", copy), ("Remove from history", remove), ("Clear history", clear)):
            ttk.Button(bt, text=t, command=f).pack(side="left", padx=(0, 6))
        ttk.Button(bt, text="Close", command=w.destroy).pack(side="right")
        tv.bind("<Double-1>", lambda ev: open_file())
        e.focus_set()
        fill()

    # ---- actions ----
    def _add_dialog(self):
        try: clip = self.clipboard_get().strip()
        except tk.TclError: clip = ""
        u = simpledialog.askstring(APP, "Paste download link (http, https or magnet):",
                                   initialvalue=clip if clip.startswith(("http", "magnet:")) else "")
        if not u:
            return
        u = u.strip()
        if torrent.is_magnet(u):
            return self._add_magnet(u)
        if not re.match(r"https?://", u, re.I):
            return messagebox.showwarning(APP, "Invalid link.")
        if guess_mode(u) == "direct" and ".m3u8" not in u.lower():
            self._ask(u, {})                      # plain file: detect + confirm first
        else:
            self._add_show(u)                     # a duplicate is handled (asked about) inside

    def _batch(self):
        w = tk.Toplevel(self); w.title("Add multiple links"); w.geometry("620x360"); w.transient(self)
        ttk.Label(w, text="One link per line. Extra links on the same line (separated by a space) are mirrors of that file:",
                  wraplength=590).pack(anchor="w", padx=8, pady=(8, 0))
        t = tk.Text(w, height=12); t.pack(fill="both", expand=True, padx=8, pady=8)

        def go():
            items = []
            for l in t.get("1.0", "end").splitlines():
                parts = l.split()
                if parts:
                    items.append((parts[0], parts[1:]))
            w.destroy()
            dd = []
            for u, _m in items:
                if re.match(r"https?://", u, re.I):
                    mode = guess_mode(u)
                    dd += dupes.find_dupes(u, self._guess_name(u, mode), self.v_folder.get(), False, self.jobs)
            self._dup_override = None
            if dd:
                pol = self.v_dup.get()
                self._dup_override = pol if pol in (dupes.SKIP, dupes.RENAME, dupes.REPLACE) else dupes.choose(
                    "ask", self._ask_dup(f"{len(dd)} of {len(items)} link(s) in this batch", dd))
            try:
                for u, mirrors in items:
                    self._add_url(u, mirrors=mirrors)
            finally:
                self._dup_override = None
        ttk.Button(w, text="Download all", command=go).pack(pady=(0, 8))

    def _resume(self):
        for j in self._sel():
            if j.state in ("paused", "error"): j.state = "queued"

    def _pause(self): [j.pause() for j in self._sel()]
    def _pause_all(self): [j.pause() for j in self.jobs]

    def _resume_all(self):
        for j in self.jobs:
            if j.state in ("paused", "error"): j.state = "queued"

    def palette(self):
        return ("#1b1d23", "#e8e8e8", "#262a33", "#2f3440") if self.v_dark.get() else ("#f0f0f0", "#000000", "#ffffff", "#e1e1e1")

    def _theme(self):
        bg, fg, fld, btn = self.palette()
        st = ttk.Style()
        self.configure(bg=bg)
        for w in ("TFrame", "TLabel", "TCheckbutton", "TLabelframe", "TLabelframe.Label"):
            st.configure(w, background=bg, foreground=fg)
        st.configure("TButton", background=btn, foreground=fg)
        st.configure("Treeview", background=fld, fieldbackground=fld, foreground=fg)
        st.configure("Treeview.Heading", background=btn, foreground=fg)
        for w in ("TEntry", "TSpinbox", "TCombobox"):
            st.configure(w, fieldbackground=fld, foreground=fg)

    def _edit_url(self):
        s = self._sel()
        if not s: return
        j = s[0]
        u = simpledialog.askstring(APP, "New link (use for expired / refreshed links):", initialvalue=j.url)
        if u and re.match(r"https?://", u, re.I):
            j.pause(); j.url = u.strip(); j.hls = []; j.srcs = None
            if j.state == "error": j.state = "paused"

    def _verify(self):
        s = self._sel()
        if not s or s[0].state != "done" or not os.path.exists(s[0].path):
            return messagebox.showinfo(APP, "Select a completed download first.")
        exp = simpledialog.askstring(APP, "Paste the expected checksum (MD5, SHA-1 or SHA-256):")
        if not exp: return
        exp = exp.strip().lower()
        algo = {32: "md5", 40: "sha1", 64: "sha256"}.get(len(exp))
        if not algo: return messagebox.showwarning(APP, "Not a valid MD5 / SHA-1 / SHA-256 value.")
        h = hashlib.new(algo)
        with open(s[0].path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
        ok = h.hexdigest() == exp
        (messagebox.showinfo if ok else messagebox.showerror)(
            APP, ("Checksum matches." if ok else "MISMATCH - file may be corrupted.") + f"\n{algo}: {h.hexdigest()}")

    def _move(self, d):
        for j in (self._sel() if d < 0 else reversed(self._sel())):
            i = self.jobs.index(j); k = i + d
            if 0 <= k < len(self.jobs): self.jobs[i], self.jobs[k] = self.jobs[k], self.jobs[i]

    def _delete(self, only_done=False):
        sel = [j for j in self.jobs if j.state == "done"] if only_done else self._sel()
        if not sel:
            return messagebox.showinfo(APP, "Nothing to delete." if only_done else "Select a download first.")
        what = f"{len(sel)} download(s)"
        ans = messagebox.askyesnocancel(APP, f"Delete {what} from the list?\n\nYes = also delete the file(s) from disk\n"
                                        "No = remove from the list only\nCancel = do nothing")
        if ans is None:
            return
        for j in sel:
            j.pause()
            w = self.wins.pop(id(j), None)
            if w is not None and w.winfo_exists(): w.destroy()
            if ans:
                for f in (j.path, (j.path + ".part") if j.path else ""):
                    if f and os.path.exists(f):
                        try: os.remove(f)
                        except OSError: pass
            if j in self.jobs: self.jobs.remove(j)

    def _open_or_progress(self):
        for j in self._sel()[:1]:
            if j.state == "done": self._open_file()
            else: self._show_progress(j, force=True)

    def _open_file(self):
        for j in self._sel()[:1]:
            if j.state == "done" and os.path.exists(j.path): self._launch(j.path)

    def _open_folder(self):
        s = self._sel()
        p = os.path.dirname(s[0].path) if s and s[0].path else self.v_folder.get()
        os.makedirs(p, exist_ok=True)
        self._launch(p)

    def _copy(self):
        s = self._sel()
        if s: self.clipboard_clear(); self.clipboard_append(s[0].url)

    # ---- browser link ----
    def _server(self):
        try:
            self.srv = start_server(self.inbox, Api(self), self.pairing)
            self.link = f"Browser link ON (port {PORT})"
        except OSError as e:
            log.warning("local server could not start: %s", e)
            self.link = "Browser link OFF (port busy - another copy running?)"

    # ---- main loop ----
    def _tick(self):
        Limiter.rate = self._i(self.v_limit, 0) * 1024
        px = self.v_proxy.get().strip()
        Job.proxy_url = px
        b = self.v_browser.get().lower()
        Job.browser = "" if b == "none" else b
        if px != self._proxy:
            self._proxy = px
            netsec.set_proxy(px)
        self.pub_folder = self.v_folder.get()
        while True:
            try: c = self.cmds.get_nowait()
            except queue.Empty: break
            if c[0] == "update":
                if c[2] and messagebox.askyesno(APP, c[1] + "\n\nOpen the download page?"):
                    webbrowser.open(c[2])
                elif not c[2]:
                    messagebox.showinfo(APP, c[1])
            elif c[0] == "pair":
                self.deiconify(); self.lift()
                c[3]["ok"] = messagebox.askyesno(
                    APP, f"A browser extension wants to send downloads to {APP}.\n\nExtension: {c[1]}\n\n"
                         "Only allow this if you installed the IK DOWNLOADER extension yourself.\n\nAllow it?",
                    icon="warning", default="no")
                c[2].set()
            elif c[0] == "probed":
                try:
                    if c[1].winfo_exists(): c[1].apply(c[2])
                except tk.TclError: pass
            elif c[0] == "browse":
                self.deiconify(); self.lift(); self.attributes("-topmost", True)
                init = c[1] if os.path.isdir(c[1]) else self.pub_folder
                pth = filedialog.askdirectory(parent=self, initialdir=init)
                self.attributes("-topmost", False)
                c[3]["path"] = os.path.normpath(pth) if pth else None
                c[2].set()
        while True:
            try: d = self.inbox.get_nowait()
            except queue.Empty: break
            if torrent.is_magnet(str(d.get("url", ""))):          # magnet link from the browser: hand it to a client
                self._add_magnet(str(d["url"]))
                self.bell()
                continue
            h = {"Referer": d.get("referer"), "Cookie": d.get("cookie"), "User-Agent": d.get("ua")}
            h = {k: v for k, v in h.items() if isinstance(v, str) and v}
            t = d.get("title") if isinstance(d.get("title"), str) else ""
            q = d.get("quality") if isinstance(d.get("quality"), str) else ""
            q = q if re.fullmatch(r"best|audio|\d{3,4}p", q) else None
            fn = d.get("filename") if isinstance(d.get("filename"), str) else ""
            fo = safe_folder(d.get("folder"))
            if d.get("folder") and not fo:
                self.toast = ("Blocked an unsafe save folder sent by the browser", time.time())
            if d.get("ask") and not d.get("site"):   # captured browser download: detect, then ask
                self._ask(str(d.get("url", "")), h, d)
                self.bell()
            elif self._add_show(str(d.get("url", "")), h, fn or t, site=bool(d.get("site")),
                                quality=q, fname=fn, folder=fo):
                self.bell()

        sc = self.v_sched.get().strip()
        if sc != self._sched_txt:
            self._sched_txt, self._sched_at = sc, None
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", sc)
            if m and int(m[1]) < 24 and int(m[2]) < 60:
                t = time.localtime()
                tgt = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(m[1]), int(m[2]), 0, 0, 0, -1))
                self._sched_at = tgt if tgt > time.time() else tgt + 86400
        hold = self._sched_at is not None and time.time() < self._sched_at
        if self._sched_at is not None and not hold:
            self._sched_at, self._sched_txt = None, ""
            self.v_sched.set("")
        active = sum(j.state == "downloading" for j in self.jobs)
        cap = max(1, self._i(self.v_active, 3))
        for j in self.jobs:
            if j.state == "queued" and active < cap and not hold:
                j.start(); active += 1

        flt, now, total = self.v_filter.get(), time.time(), 0.0
        shown = []
        for j in self.jobs:
            c = category(j.name)
            if (flt == "All" or (flt == "Active" and j.state in ("downloading", "queued")) or
                    (flt == "Completed" and j.state == "done") or
                    (flt == "Unfinished" and j.state != "done") or flt == c):
                shown.append(j)
        ids = {str(id(j)) for j in shown}
        for iid in self.tree.get_children():
            if iid not in ids: self.tree.delete(iid)
        for j in shown:
            d = j.done_bytes
            t0, d0 = self.last.get(id(j), (now, d))
            sp = max(0.0, (d - d0) / (now - t0)) if now > t0 and j.state == "downloading" else 0.0
            self.last[id(j)] = (now, d)
            total += sp
            p = j.frac
            bar = "█" * int(p * 10) + "░" * (10 - int(p * 10)) + f" {p * 100:.0f}%"
            eta = hms((j.size - d) / sp) if sp > 0 and j.size and not j.is_hls else ""
            st = {"downloading": "Downloading", "queued": "Queued", "paused": "Paused",
                  "done": "Complete", "error": "Error"}[j.state] + (f": {j.msg}" if j.msg else "")
            vals = (j.name, fmt(j.size) if j.size else (fmt(d) if d else "?"), bar,
                    fmt(sp) + "/s" if sp else "", eta, st)
            iid = str(id(j))
            if self.tree.exists(iid): self.tree.item(iid, values=vals)
            else: self.tree.insert("", "end", iid=iid, values=vals)
        for n, j in enumerate(shown):
            self.tree.move(str(id(j)), "", n)
        for j in self.jobs:
            if j.state == "done" and not j.notified:
                j.notified = True
                self.toast = (f"Finished: {j.name}", now)
                self.bell()
                self._last_save = 0.0                      # save the list right away
                self._record(j, "done")
                self.stats.file_done()
                if j.path and j.path.lower().endswith(".torrent") and self.v_tauto.get():
                    self._send_torrent(j.path, j.name)     # .torrent file arrived: open it in the client
            elif j.state == "error":
                if getattr(j, "logged_err", None) != j.msg:
                    j.logged_err = j.msg
                    self._record(j, "error")
            else:
                j.logged_err = None
        got, self._ctr = Counter.total - self._ctr, Counter.total
        self.stats.tick(now - self._st_t, got, any(j.state == "downloading" for j in self.jobs))
        self._st_t = now
        if now - self._last_save > 15:                     # crash-safe: never lose the queue
            self._last_save = now
            self._save_state()
            self._save_settings()
            self.hist.save()
            self.stats.save()
        extra = f"   |   Session: {fmt(self.stats.sess['bytes'])}"
        extra += f"   |   Scheduled for {sc}" if hold else ""
        extra += f"   |   {self.toast[0]}" if now - self.toast[1] < 10 else ""
        self.status.config(text=f"{fmt(total)}/s   |   {active} active, "
                                f"{sum(j.state == 'done' for j in self.jobs)} done, {len(self.jobs)} total   |   {self.link}{extra}")

        if self.v_clip.get():
            try: c = self.clipboard_get().strip()
            except tk.TclError: c = ""
            if c != self.last_clip:
                self.last_clip = c
                path = urllib.parse.urlparse(c).path.lower() if c.startswith("http") and " " not in c else ""
                if torrent.is_magnet(c) and not re.search(r"\s", c):
                    self.after(50, lambda u=c: messagebox.askyesno(
                        APP, f"Send this magnet link to your torrent client?\n\n{u[:200]}") and self._add_magnet(u))
                elif os.path.splitext(path)[1] in (list(ALL_EXT) + [".m3u8", ".torrent"]) or (
                        c.startswith("http") and " " not in c and guess_mode(c) == "site"):
                    self.after(50, lambda u=c: messagebox.askyesno(APP, f"Download this link?\n\n{u[:200]}")
                               and (self._ask(u, {}) if guess_mode(u) == "direct" and ".m3u8" not in u.lower()
                                    else self._add_show(u)))

        pending = any(j.state in ("downloading", "queued") for j in self.jobs)
        if pending:
            self.was_active = True
        elif self.was_active:
            self.was_active = False
            if self.v_off.get():
                self.v_off.set(False)
                shutdown_pc(60)
                if not messagebox.askokcancel(APP, "All downloads finished.\nThe PC shuts down in 60 seconds.\n\n"
                                                   "Press Cancel to abort."):
                    cancel_shutdown()
        self.after(700, self._tick)

    # ---- persistence (atomic writes; cookies are encrypted inside Job.to_dict) ----
    def _load(self):
        data = read_json(STATE, [])
        jobs = []
        for d in data if isinstance(data, list) else []:
            try:
                ensure_http(d["url"])
                jobs.append(Job(d["url"], d["folder"], d.get("conns", 8), d.get("headers"), d.get("title", ""), d))
            except Exception as e:
                log.warning("skipped a damaged download entry: %s", short_err(e))
        self.jobs = jobs

    def _save_state(self):
        try:
            write_json(STATE, [j.to_dict() for j in self.jobs])
        except (OSError, TypeError, ValueError) as e:
            log.warning("could not save the download list: %s", e)

    def _save_settings(self):
        try:
            write_json(CFG, {"folder": self.v_folder.get(), "conns": self._i(self.v_conns, 8),
                             "active": self._i(self.v_active, 3), "limit": self._i(self.v_limit, 0),
                             "clip": self.v_clip.get(), "show": self.v_show.get(), "proxy": self.v_proxy.get().strip(),
                             "dark": self.v_dark.get(), "quality": self.v_quality.get(),
                             "browser": self.v_browser.get(), "dup": self.v_dup.get(),
                             "torrent_client": self.v_tclient.get().strip(), "torrent_auto": self.v_tauto.get()})
        except (OSError, tk.TclError) as e:
            log.warning("could not save settings: %s", e)

    def _quit(self):
        busy = any(j.state == "downloading" for j in self.jobs)
        for j in self.jobs: j.pause()
        time.sleep(0.5)
        now = time.time()                                   # last pass: whatever finished since the previous tick
        self.stats.tick(now - self._st_t, Counter.total - self._ctr, busy)
        for j in self.jobs:
            if j.state == "done" and not j.notified:
                j.notified = True
                self._record(j, "done")
                self.stats.file_done()
        self._save_state()
        self._save_settings()
        self.hist.save()
        self.stats.save()
        self.destroy()


def selftest(out_path: str) -> None:
    """Used by the release workflow: `IK-DOWNLOADER.exe --selftest result.json` checks that the built program
    really contains everything it needs (assets, video engine, ffmpeg, deno, Tk) without opening a window."""
    res = {"version": VERSION, "assets": bool(asset("logo.png") and asset("icon.ico") and asset("splash.png")),
           "tk": tk is not None, "ffmpeg": bool(ffmpeg_path()), "deno": bool(shutil.which("deno"))}
    try:
        import yt_dlp
        res["yt_dlp"] = yt_dlp.version.__version__
    except Exception as e:
        res["yt_dlp"] = False
        res["yt_dlp_error"] = short_err(e)
    write_json(out_path, res, private=False)


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        return selftest(sys.argv[2])
    setup_logging()
    if tk is None:
        sys.exit("Tkinter is missing. Reinstall Python with the 'tcl/tk' option enabled.")
    log.info("%s %s starting", APP, VERSION)
    App().mainloop()
