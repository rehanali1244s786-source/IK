"""Download history (searchable) and statistics (total downloaded, average speed). No GUI code.

history.json  one record per finished / failed download or torrent hand-off, newest last, capped at MAX_HISTORY
stats.json    life-time counters; the session counters live in memory only
Both files are written atomically (see common.write_json) and set aside if they are damaged."""
import os, threading, time, urllib.parse
from typing import Dict, List, Optional

from .common import HOME, category, read_json, write_json
from .dupes import norm_url

HIST = os.path.join(HOME, "history.json")
STATS = os.path.join(HOME, "stats.json")
MAX_HISTORY = 5000
STATUSES = ("all", "done", "error", "torrent")


class History:
    def __init__(self, path: str = HIST):
        self.path, self.lock, self.dirty = path, threading.Lock(), False
        self.fresh = not os.path.exists(path)                       # lets the app import old downloads once
        data = read_json(path, [])
        self.rows: List[dict] = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    # ---- writing ----
    def add(self, name: str, url: str, status: str = "done", path: str = "", size: int = 0, secs: float = 0.0,
            mode: str = "direct", btih: str = "", note: str = "", ts: Optional[float] = None) -> dict:
        """status: done | error | torrent."""
        size, secs = max(0, int(size or 0)), max(0.0, float(secs or 0.0))
        host = "magnet" if url.lower().startswith("magnet:") else (urllib.parse.urlparse(url).hostname or "")
        rec = {"ts": float(ts if ts is not None else time.time()), "name": name or "", "url": url, "status": status,
               "path": path or "", "size": size, "secs": round(secs, 2), "avg": int(size / secs) if secs > 0.001 else 0,
               "mode": mode, "host": host, "cat": category(name or path) if status != "torrent" else "Torrents",
               "btih": btih, "note": (note or "")[:160]}
        with self.lock:
            self.rows.append(rec)
            if len(self.rows) > MAX_HISTORY:
                del self.rows[:len(self.rows) - MAX_HISTORY]
            self.dirty = True
        return rec

    def remove(self, recs: List[dict]) -> int:
        ids = {id(r) for r in recs}
        with self.lock:
            n = len(self.rows)
            self.rows = [r for r in self.rows if id(r) not in ids]
            self.dirty = self.dirty or len(self.rows) != n
            return n - len(self.rows)

    def clear(self) -> None:
        with self.lock:
            self.rows, self.dirty = [], True

    def save(self, force: bool = False) -> None:
        with self.lock:
            if not (self.dirty or force):
                return
            snap, self.dirty = list(self.rows), False
        try:
            write_json(self.path, snap)
        except (OSError, TypeError, ValueError):
            self.dirty = True

    # ---- reading ----
    def search(self, text: str = "", status: str = "all", limit: int = 0) -> List[dict]:
        """Newest first. Every word of `text` must appear in the name, link, folder, host, type or date."""
        words = [w for w in (text or "").lower().split() if w]
        with self.lock:
            rows = list(self.rows)
        out = []
        for r in reversed(rows):
            if status != "all" and r.get("status") != status:
                continue
            if words:
                hay = " ".join((str(r.get("name", "")), str(r.get("url", "")), str(r.get("path", "")),
                                str(r.get("host", "")), str(r.get("cat", "")), str(r.get("note", "")),
                                time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("ts", 0))))).lower()
                if not all(w in hay for w in words):
                    continue
            out.append(r)
            if limit and len(out) >= limit:
                break
        return out

    def last_for_url(self, url: str) -> Optional[dict]:
        n = norm_url(url)
        with self.lock:
            for r in reversed(self.rows):
                if r.get("status") == "done" and norm_url(r.get("url", "")) == n:
                    return r
        return None

    def last_for_btih(self, btih: str) -> Optional[dict]:
        if not btih:
            return None
        with self.lock:
            for r in reversed(self.rows):
                if r.get("status") == "torrent" and r.get("btih") == btih:
                    return r
        return None

    def per_category(self) -> Dict[str, dict]:
        """{'Video': {'files': 3, 'bytes': 123}, ...} for finished downloads."""
        out: Dict[str, dict] = {}
        with self.lock:
            for r in self.rows:
                if r.get("status") != "done":
                    continue
                d = out.setdefault(r.get("cat") or "Other", {"files": 0, "bytes": 0})
                d["files"] += 1
                d["bytes"] += int(r.get("size") or 0)
        return out

    def top_hosts(self, n: int = 5) -> List[tuple]:
        agg: Dict[str, int] = {}
        with self.lock:
            for r in self.rows:
                if r.get("status") == "done" and r.get("host"):
                    agg[r["host"]] = agg.get(r["host"], 0) + int(r.get("size") or 0)
        return sorted(agg.items(), key=lambda kv: -kv[1])[:n]


class Stats:
    """Bytes received and the time something was actually downloading. Average speed = bytes / active time, so
    idle hours never drag it down. Life-time totals persist; session totals start at zero on every launch."""

    def __init__(self, path: str = STATS):
        self.path = path
        d = read_json(path, {})
        d = d if isinstance(d, dict) else {}

        def num(k, default=0.0):
            try:
                return max(0.0, float(d.get(k, default)))
            except (TypeError, ValueError):
                return default
        self.life = {"bytes": num("bytes"), "secs": num("secs"), "files": int(num("files")), "peak": num("peak"),
                     "since": num("since", time.time())}
        self.sess = {"bytes": 0.0, "secs": 0.0, "files": 0, "peak": 0.0, "start": time.time()}
        self.lock, self.dirty, self.rate = threading.Lock(), False, 0.0

    def tick(self, dt: float, nbytes: float, active: bool) -> None:
        """Called about once a second: dt seconds passed, nbytes arrived."""
        if dt <= 0:
            return
        with self.lock:
            dt = min(dt, 5.0)
            inst = nbytes / dt
            self.rate = 0.6 * self.rate + 0.4 * inst if active else 0.0
            for d in (self.life, self.sess):
                d["bytes"] += nbytes
                if active:
                    d["secs"] += dt
                    if dt >= 0.3:
                        d["peak"] = max(d["peak"], self.rate)
            self.dirty = self.dirty or nbytes > 0 or active

    def file_done(self) -> None:
        with self.lock:
            self.life["files"] += 1
            self.sess["files"] += 1
            self.dirty = True

    @staticmethod
    def _avg(d: dict) -> float:
        return d["bytes"] / d["secs"] if d["secs"] >= 1 else 0.0

    def summary(self) -> dict:
        with self.lock:
            return {"session": dict(self.sess, avg=self._avg(self.sess)),
                    "life": dict(self.life, avg=self._avg(self.life))}

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            snap, self.dirty = dict(self.life), False
        try:
            write_json(self.path, snap)
        except (OSError, TypeError, ValueError):
            self.dirty = True

    def reset_life(self) -> None:
        with self.lock:
            self.life = {"bytes": 0.0, "secs": 0.0, "files": 0, "peak": 0.0, "since": time.time()}
            self.dirty = True


def format_summary(s: dict) -> list:
    """[(label, this session, all time), ...] as text, for the statistics window."""
    from .common import fmt, hms

    def col(d):
        return (fmt(d["bytes"]), hms(d["secs"]) if d["secs"] >= 1 else "0:00",
                fmt(d["avg"]) + "/s" if d["avg"] else "-", fmt(d["peak"]) + "/s" if d["peak"] else "-", str(d["files"]))
    labels = ("Total downloaded", "Time downloading", "Average speed", "Peak speed", "Files completed")
    return list(zip(labels, col(s["session"]), col(s["life"])))
