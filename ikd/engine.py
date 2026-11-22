"""Download engine: probing, multi-connection downloads, HLS, video sites (yt-dlp). No GUI code in here."""
import os, re, errno, shutil, threading, time, mimetypes, urllib.request, urllib.parse, urllib.error

from . import netsec
from .common import (UA, ALL_EXT, KNOWN_EXT, MEDIA_EXT, MIME_EXT, SENSITIVE_HEADERS, category, clean, fmt,
                     ffmpeg_path, log, seal_headers, short_err, unseal_headers)
from .netsec import ensure_http, same_site


class NeedEngine(Exception):
    """The stream can't be handled by the built-in HLS downloader but yt-dlp can do it."""


def probe_url(url: str, headers: dict = None, timeout: float = 20) -> dict:
    """Ask the server what a link really is (name, size, type, resume support) without downloading it."""
    def open_(extra):
        h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        h.update({k: v for k, v in (headers or {}).items() if v})
        h.update(extra)
        return netsec.urlopen(urllib.request.Request(ensure_http(url), headers=h), timeout=timeout)
    try:
        r = open_({"Range": "bytes=0-0"})
    except urllib.error.HTTPError as e:   # some servers refuse Range / tiny requests: plain GET (headers only)
        if e.code not in (400, 403, 405, 416, 501):
            raise
        r = open_({})
    with r:
        cr = r.headers.get("Content-Range", "")
        cl = r.headers.get("Content-Length", "")
        ctype = (r.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        if r.status == 206 and "/" in cr and cr.split("/")[1].isdigit():
            size, ranged = int(cr.split("/")[1]), True
        else:
            size = int(cl) if cl.isdigit() else 0
            ranged = False   # only trust ranges the server confirmed with a 206
        cd = r.headers.get("Content-Disposition", "") or ""
        name, final = "", r.geturl()
    m = re.search(r"filename\*\s*=\s*([^';]*)'[^']*'([^;]+)", cd, re.I)
    if m:
        name = urllib.parse.unquote(m[2].strip().strip('"'), encoding=m[1] or "utf-8", errors="replace")
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        if m:
            name = m[1].strip()
    if not name:
        name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(final).path))
    name = clean(name) if name else ""
    ext = MIME_EXT.get(ctype) or (mimetypes.guess_extension(ctype) if ctype and ctype != "application/octet-stream" else "")
    if not name:
        name = "download" + (ext or "")
    elif os.path.splitext(name)[1].lower() not in KNOWN_EXT and not re.search(r"\.[A-Za-z0-9]{1,5}$", name) and ext:
        name += ext
    return {"name": name, "size": size, "ranged": ranged, "ctype": ctype, "url": final,
            "html": ctype in ("text/html", "application/xhtml+xml")}


def describe(name, ctype=""):
    ext = os.path.splitext(name)[1].lower()
    cat = category(name)
    kind = f"{ext[1:].upper()} file" if ext else (ctype or "File")
    return kind + (f"  ({cat})" if cat != "Other" else "")


def fetch_info(url: str) -> dict:
    """Read title + available video resolutions of a page (used by the browser quality picker)."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    if Job.proxy_url: opts["proxy"] = Job.proxy_url
    if Job.browser: opts["cookiesfrombrowser"] = (Job.browser,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):
        info = next(iter(info["entries"]))
    fl = info.get("formats") or []
    aud = max([f.get("filesize") or f.get("filesize_approx") or 0 for f in fl
               if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")] or [0])
    vids = {}
    for f in fl:
        if f.get("vcodec") not in (None, "none") and f.get("height"):
            size = f.get("filesize") or f.get("filesize_approx") or 0
            if size and f.get("acodec") in (None, "none"):
                size += aud
            vids[f["height"]] = max(vids.get(f["height"], 0), size)
    out = [{"label": f"{h}p", "quality": f"{h}p", "size": vids[h]} for h in sorted(vids, reverse=True)]
    if not out:
        out = [{"label": "Best available", "quality": "best",
                "size": info.get("filesize") or info.get("filesize_approx") or 0}]
    return {"title": clean(info.get("title") or "video"), "duration": info.get("duration") or 0, "formats": out}



class Limiter:
    """Global speed limit shared by all downloads (bytes/sec, 0 = unlimited)."""
    rate, t, lock = 0, 0.0, threading.Lock()

    @classmethod
    def wait(cls, n):
        if cls.rate <= 0:
            return
        with cls.lock:
            now = time.time()
            cls.t = max(cls.t, now) + n / cls.rate
            d = cls.t - now
        if d > 0:
            time.sleep(d)



class Counter:
    """Every byte received by any download (all modes), for the statistics."""
    total, lock = 0, threading.Lock()

    @classmethod
    def add(cls, n):
        with cls.lock:
            cls.total += n


class MirrorError(IOError):
    """A source answered, but it cannot be used for this file (wrong size, no ranges ...)."""


SLICE = 4 << 20          # a worker re-chooses its source after every slice of this size (multi-source downloads)
PROBE_SLICE = 512 << 10  # first / refresh measurement of a source: small, so a slow mirror cannot stall a segment


class Sources:
    """Speed book-keeping for the several URLs (mirrors) of one file. Every segment asks pick() for its next slice:
    unmeasured sources are tried first (spread over the segments), then the one with the best measured speed wins,
    and a source that has not been measured for STALE seconds is sampled again so changes are noticed.
    Failed sources cool down (or are dropped for good on 401/403/404/410 / wrong file) and the others take over."""
    STALE = 20.0

    def __init__(self, urls):
        self.urls = list(dict.fromkeys(urls))
        self.lock = threading.Lock()
        z = {u: 0.0 for u in self.urls}
        self.speed, self.last, self.bad_until = dict(z), dict(z), dict(z)
        self.bytes, self.fails, self.dead, self.err = {u: 0 for u in self.urls}, {u: 0 for u in self.urls}, set(), {}

    def _live(self, now):
        return [u for u in self.urls if u not in self.dead and self.bad_until[u] <= now]

    def alive(self) -> int:
        with self.lock:
            return len(self._live(time.time()))

    def pick(self, idx=0):
        """-> (url, measuring). `measuring` = use a small slice."""
        now = time.time()
        with self.lock:
            live = self._live(now)
            if not live:
                cand = [u for u in self.urls if u not in self.dead] or self.urls
                return min(cand, key=lambda u: self.bad_until[u]), True
            fresh = [u for u in live if self.last[u] == 0.0]
            if fresh:
                return fresh[idx % len(fresh)], True
            stale = [u for u in live if now - self.last[u] > self.STALE]
            if stale and len(live) > 1:
                return min(stale, key=lambda u: self.last[u]), True
            return max(live, key=lambda u: self.speed[u]), False

    def report(self, url, nbytes, secs):
        """A slice finished: nbytes in secs (connection time included, speed-limiter waits excluded)."""
        sp = nbytes / max(secs, 0.001)
        with self.lock:
            self.speed[url] = sp if self.speed[url] <= 0 else 0.5 * self.speed[url] + 0.5 * sp
            self.last[url] = time.time()
            self.bytes[url] += nbytes
            self.fails[url], self.bad_until[url] = 0, 0.0

    def fail(self, url, hard, why=""):
        with self.lock:
            self.fails[url] += 1
            self.err[url] = short_err(why)[:80] if why else ""
            if hard:
                self.dead.add(url)
            else:
                self.bad_until[url] = time.time() + min(30.0, 2.0 ** self.fails[url])

    def rows(self):
        now = time.time()
        with self.lock:
            return [{"url": u, "host": urllib.parse.urlparse(u).hostname or u, "speed": self.speed[u],
                     "bytes": self.bytes[u], "err": self.err.get(u, ""),
                     "state": "failed" if u in self.dead else ("cooling" if self.bad_until[u] > now else "ok")}
                    for u in self.urls]


class Throttle:
    """Per-download speed limit (bytes/sec, 0 = unlimited). Can be changed while downloading."""
    def __init__(self):
        self.rate, self.t, self.lock = 0, 0.0, threading.Lock()

    def wait(self, n):
        if self.rate <= 0:
            return
        with self.lock:
            now = time.time()
            self.t = max(self.t, now) + n / self.rate
            d = self.t - now
        if d > 0:
            time.sleep(d)



class Job:
    FIELDS = ("url folder conns name path size ranged segs state headers title "
              "hls_pos hls_bytes hls_total mode quality fname exact limit keepname mirrors active replace").split()
    proxy_url, browser = "", ""
    RETRY_DELAY = 1.5                            # seconds between retries (tests lower it)

    def __init__(self, url, folder, conns=8, headers=None, title="", d=None):
        self.url, self.folder, self.conns = url, folder, conns
        self.headers, self.title = headers or {}, title
        self.name = self.path = ""
        self.size, self.ranged, self.segs = 0, False, []
        self.state, self.msg = "paused", ""
        self.hls, self.hls_pos, self.hls_bytes, self.hls_total = [], 0, 0, 0
        self.mode, self.quality, self.dl_done, self.dl_total = "direct", "best", 0, 0
        self.fname, self.exact, self.limit, self.keepname = "", False, 0, False
        self.mirrors, self.active, self.replace = [], 0.0, False   # extra URLs of the same file / seconds spent downloading / overwrite
        self.srcs, self.dropped = None, []                         # Sources (multi-source runs) / mirrors that were refused
        self.hls_map = None                      # fMP4 init segment (HLS)
        for k in self.FIELDS:
            if d and k in d:
                setattr(self, k, d[k])
        if d is not None:                        # loaded from disk: cookies were stored encrypted
            self.headers = unseal_headers(self.headers or {})
        self.mirrors = [m for m in (self.mirrors if isinstance(self.mirrors, list) else [])
                        if isinstance(m, str) and re.match(r"https?://", m, re.I)]
        try:
            self.active = max(0.0, float(self.active))
        except (TypeError, ValueError):
            self.active = 0.0
        self.throttle = Throttle()
        self.throttle.rate = max(0, int(self.limit or 0)) * 1024
        if self.state in ("downloading", "queued"):
            self.state = "paused"
        self.notified = self.state == "done"

    # ---- info ----
    @property
    def is_hls(self):
        return ".m3u8" in urllib.parse.urlparse(self.url).path.lower()

    @property
    def done_bytes(self):
        if self.mode == "site":
            return self.dl_done
        if self.is_hls:
            return self.hls_bytes
        return sum((min(s[2], s[1] + 1) if s[1] >= 0 else s[2]) - s[0] for s in self.segs)

    @property
    def frac(self):
        if self.state == "done":
            return 1.0
        if self.mode == "site":
            return min(1.0, self.dl_done / self.dl_total) if self.dl_total else 0.0
        if self.is_hls:
            return self.hls_pos / self.hls_total if self.hls_total else 0.0
        return min(1.0, self.done_bytes / self.size) if self.size else 0.0

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.FIELDS}
        d["segs"] = [list(s) for s in self.segs]            # copy: worker threads keep updating them
        d["headers"] = seal_headers(self.headers)           # login cookies never hit the disk in plaintext
        return d

    def set_mirrors(self, urls):
        """Replace the extra sources of this download (takes effect the next time it starts)."""
        seen, out = {self.url}, []
        for u in urls or []:
            u = (u or "").strip()
            if re.match(r"https?://", u, re.I) and u not in seen:
                seen.add(u)
                out.append(u)
        self.mirrors, self.srcs, self.dropped = out, None, []

    def source_rows(self):
        """For the progress window: one dict per source (empty when the download has only one URL)."""
        return self.srcs.rows() if self.srcs is not None else []

    def set_limit(self, kbps):
        """Per-download speed limiter in KB/s (0 = off). Applies immediately."""
        self.limit = max(0, int(kbps))
        self.throttle.rate = self.limit * 1024

    def _chunk(self):
        rs = [r for r in (Limiter.rate, self.throttle.rate) if r > 0]
        return max(2048, min(131072, min(rs) // 8)) if rs else 131072

    def hdr(self, extra=None, url=None):
        """Request headers. Cookie / Authorization only go to the site the download came from."""
        h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        h.update({k: v for k, v in self.headers.items() if v})
        if url and not same_site(url, self.url):
            for k in SENSITIVE_HEADERS:
                h.pop(k, None)
        h.update(extra or {})
        return h

    # ---- setup ----
    def _assign_path(self, name):
        """Pick a free file name and reserve it atomically (two downloads can never get the same name)."""
        folder = self.folder if self.exact else os.path.join(self.folder, category(name))
        os.makedirs(folder, exist_ok=True)
        base, ext = os.path.splitext(name)
        path, i = os.path.join(folder, name), 1
        while True:
            if not os.path.exists(path) or (self.replace and i == 1 and os.path.isfile(path)):
                try:
                    os.close(os.open(path + ".part", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    break
                except FileExistsError:
                    pass
            path = os.path.join(folder, f"{base} ({i}){ext}")
            i += 1
        self.name, self.path = os.path.basename(path), path

    def probe(self):
        info = probe_url(self.url, self.headers)
        self.size, self.ranged = info["size"], info["ranged"]
        srv = info["name"]
        user = self.fname or (clean(self.title) if self.title else "")
        if user:
            name = clean(user)
            if not self.keepname and os.path.splitext(name)[1].lower() not in KNOWN_EXT:
                ext = os.path.splitext(srv)[1]
                if ext.lower() not in KNOWN_EXT:
                    ext = ".mp3" if info["ctype"].startswith("audio/") else ".mp4"
                name += ext
        else:
            name = srv
        if info["html"] and os.path.splitext(name)[1].lower() in ALL_EXT:
            raise IOError("Server sent a web page, not the file (login needed or link expired?)")
        self._assign_path(name)
        part = self.path + ".part"
        try:
            if self.size:                                    # refuse early instead of failing at 99 %
                free = shutil.disk_usage(os.path.dirname(self.path)).free
                if free < self.size + (64 << 20):
                    raise IOError(f"Not enough disk space (need {fmt(self.size)}, free {fmt(free)})")
            if self.ranged and self.size > 0:
                n = max(1, min(self.conns, self.size // (1 << 20)))
                step = self.size // n
                self.segs = [[i * step, (i + 1) * step - 1 if i < n - 1 else self.size - 1, i * step]
                             for i in range(n)]
            else:
                self.segs = [[0, self.size - 1 if self.size else -1, 0]]
            with open(part, "wb") as f:
                if self.size:
                    f.truncate(self.size)
        except Exception:
            try: os.remove(part)
            except OSError: pass
            raise

    def _setup_sources(self):
        """Check every mirror (same size, resume supported, not a web page) and build self.srcs. Needs a primary
        source that supports ranges and reports its size; otherwise the mirrors are ignored."""
        self.srcs, self.dropped = None, []
        ms = [m for m in dict.fromkeys(self.mirrors or []) if m != self.url]
        if not ms:
            return
        if not (self.ranged and self.size > 0):
            self.dropped = [(m, "the main link has no resume support or unknown size") for m in ms]
            log.info("mirrors ignored: main link has no ranges / unknown size")
            return
        res, lock = {}, threading.Lock()

        def check(m):
            try:
                ensure_http(m, base=self.url)
                info = probe_url(m, self.hdr(url=m), timeout=15)
                why = ("no resume support" if not info["ranged"] else
                       f"different size ({info['size']} bytes, expected {self.size})" if info["size"] != self.size else
                       "server sent a web page" if info["html"] else None)
            except Exception as e:
                why = short_err(e)
            with lock:
                res[m] = why
        ts = [threading.Thread(target=check, args=(m,), daemon=True) for m in ms]
        for t in ts: t.start()
        for t in ts: t.join(25)
        ok = [m for m in ms if m in res and res[m] is None]
        self.dropped = [(m, res.get(m) or "no answer") for m in ms if m not in ok]
        for m, why in self.dropped:
            log.info("mirror refused (%s): %s", why, urllib.parse.urlparse(m).hostname)
        if ok:
            self.srcs = Sources([self.url] + ok)

    # ---- control ----
    def start(self):
        if self.state in ("downloading", "done"):
            return
        self.state, self.msg = "downloading", ""
        if self.mirrors:
            self.srcs = None                             # mirrors are checked afresh on every start
        threading.Thread(target=self._run, daemon=True).start()

    def pause(self):
        if self.state in ("downloading", "queued"):
            self.state = "paused"

    def _fail(self, e):
        if self.state == "downloading":
            self.state, self.msg = "error", str(e)[:110]

    def _run(self):
        t0 = time.time()
        try:
            self._run_inner()
        finally:
            self.active += time.time() - t0              # seconds spent downloading, for the statistics

    def _run_inner(self):
        try:
            if self.mode in ("site", "auto"):
                if self._run_site():
                    return
                self.mode = "direct"
            if self.is_hls:
                try:
                    return self._run_hls()
                except NeedEngine as e:                       # AES-128 / live / byte-range streams: yt-dlp can do these
                    log.info("HLS needs the video engine: %s", e)
                    self.mode = "site"
                    if self._run_site():
                        return
                    raise IOError(f"This stream ({e}) needs the video engine")
            if not self.segs or not os.path.exists(self.path + ".part"):
                self.probe()
            if self.mirrors and self.srcs is None and self.state == "downloading":
                self._setup_sources()
            if not self.ranged:
                for s in self.segs:
                    s[2] = 0
            ts = [threading.Thread(target=self._worker, args=(s,), daemon=True)
                  for s in self.segs if s[1] < 0 or s[2] <= s[1]]
            for t in ts: t.start()
            for t in ts: t.join()
            if self.state != "downloading":
                return
            if all(s[1] >= 0 and s[2] > s[1] for s in self.segs):
                part = self.path + ".part"
                real = os.path.getsize(part)
                if self.size and real != self.size:
                    raise IOError(f"Size mismatch ({real} of {self.size} bytes)")
                if not self.size:
                    self.size = real
                os.replace(part, self.path)
                self.state = "done"
            else:
                self._fail("Incomplete download")
        except Exception as e:
            log.info("download failed: %s", short_err(e))
            self._fail(e)

    # ---- video sites (YouTube, TikTok, Instagram, ...) via yt-dlp ----
    def _run_site(self):
        """Returns True if handled, False to fall back to a normal file download (auto mode)."""
        try:
            import yt_dlp
        except ImportError:
            if self.mode == "auto":
                return False
            self._fail("Video engine missing (pip install yt-dlp)")
            return True
        audio = self.quality == "audio"
        ff = ffmpeg_path()
        m = re.fullmatch(r"(\d+)p", self.quality or "")
        h = int(m[1]) if m else 0
        hs = f"[height<={h}]" if h else ""
        fm = "ba/b" if audio else (f"bv*{hs}+ba/b{hs}" if ff else f"b{hs}/b")
        cancel = getattr(yt_dlp.utils, "DownloadCancelled", Exception)
        files = {}

        def hook(d):
            if self.state != "downloading":
                raise cancel("paused")
            fn = d.get("filename") or d.get("tmpfilename") or ""
            files[fn] = (d.get("downloaded_bytes") or 0,
                         d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            before = self.dl_done
            self.dl_done = sum(a for a, b in files.values())
            if self.dl_done > before:
                Counter.add(self.dl_done - before)
            if self.throttle.rate > 0 and self.dl_done > before:
                self.throttle.wait(self.dl_done - before)
            self.dl_total = self.size = sum(b for a, b in files.values())
            t = (d.get("info_dict") or {}).get("title")
            if t and not self.path:
                self.name = clean(t)

        base = self.folder if self.exact else os.path.join(self.folder, "Music" if audio else "Video")
        tmpl = (clean(self.fname).replace("%", "%%") if self.fname else "%(title).150B") + ".%(ext)s"
        opts = {"format": fm, "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
                "progress_hooks": [hook], "continuedl": True, "retries": 10, "fragment_retries": 10,
                "concurrent_fragment_downloads": max(1, min(self.conns, 16)),
                "format_sort": ["res", "fps", "vcodec:h264", "acodec:aac"],
                "merge_output_format": "mp4",
                "outtmpl": os.path.join(base.replace("%", "%%"), tmpl)}
        if ff:
            opts["ffmpeg_location"] = ff
            if audio:
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                           "preferredquality": "192"}]
        if self.replace: opts["overwrites"] = True
        if Limiter.rate > 0: opts["ratelimit"] = Limiter.rate
        if Job.proxy_url: opts["proxy"] = Job.proxy_url
        if Job.browser: opts["cookiesfrombrowser"] = (Job.browser,)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                if self.mode == "auto":
                    try:
                        pre = ydl.extract_info(self.url, download=False, process=False)
                    except yt_dlp.utils.DownloadError as e:
                        if "Unsupported URL" in str(e):
                            return False
                        raise
                    if (pre or {}).get("direct"):
                        return False
                    self.mode = "site"
                info = ydl.extract_info(self.url, download=True)
                if info.get("entries"):
                    info = next(iter(info["entries"]))
                rd = (info.get("requested_downloads") or [{}])[0]
                path = rd.get("filepath") or ydl.prepare_filename(info)
                if not os.path.exists(path):
                    base = os.path.splitext(path)[0]
                    for ext in (".mp3", ".mp4", ".mkv", ".webm", ".m4a"):
                        if os.path.exists(base + ext):
                            path = base + ext
                            break
        except Exception as e:
            if self.state == "downloading":
                self._fail(re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", ""))
            return True
        if self.state != "downloading":
            return True
        self.path, self.name = path, os.path.basename(path)
        self.size = os.path.getsize(path) if os.path.exists(path) else self.dl_total
        self.dl_done = self.dl_total = self.size
        self.state = "done"
        return True

    def _worker(self, s):
        tries, srcs = 0, self.srcs
        idx = next((i for i, x in enumerate(self.segs) if x is s), 0)
        while self.state == "downloading":
            if s[1] >= 0 and s[2] > s[1]:
                return
            before, url, stop = s[2], self.url, s[1]
            try:
                if not self.ranged:
                    s[2] = before = 0                     # no range support: every attempt starts from byte 0
                if srcs is not None:                      # several sources: pick the fastest one for this slice
                    url, measuring = srcs.pick(idx)
                    stop = min(s[1], s[2] + (PROBE_SLICE if measuring else SLICE) - 1)
                bad = MirrorError if srcs is not None else IOError
                extra = {"Range": f"bytes={s[2]}-{stop}"} if self.ranged else None
                t_open, spent, got = time.time(), 0.0, 0
                req = urllib.request.Request(ensure_http(url, base=self.url) if url != self.url else ensure_http(url),
                                             headers=self.hdr(extra, url=url))
                with netsec.urlopen(req, timeout=20) as r, open(self.path + ".part", "r+b") as f:
                    spent = time.time() - t_open          # connecting + waiting for the first answer
                    if self.ranged:
                        if r.status != 206:
                            raise bad("Server ignored range request")
                        cr = r.headers.get("Content-Range", "") or ""
                        m = re.match(r"bytes\s+(\d+)-", cr)
                        if not m or int(m[1]) != s[2]:
                            raise bad("Server returned the wrong byte range")
                        tot = re.search(r"/(\d+)\s*$", cr)
                        if srcs is not None and tot and self.size and int(tot[1]) != self.size:
                            raise bad("Source has a different file size")
                    elif s[1] < 0:
                        f.truncate(0)
                    f.seek(s[2])
                    while self.state == "downloading":
                        want = self._chunk()
                        if stop >= 0:
                            want = min(want, stop - s[2] + 1)   # never write past the end of this segment / slice
                        if want <= 0:
                            break
                        t_read = time.time()
                        b = r.read(want)
                        spent += time.time() - t_read
                        if not b:
                            break
                        f.write(b)
                        f.flush()                               # progress counter never runs ahead of the disk
                        s[2] += len(b)
                        got += len(b)
                        Counter.add(len(b))
                        Limiter.wait(len(b))
                        self.throttle.wait(len(b))
                if srcs is not None and got:
                    srcs.report(url, got, spent)
                if s[1] < 0 and self.state == "downloading":
                    s[1] = s[2] - 1                             # unknown size: stream finished
                    return
                if s[2] > before:
                    tries = 0
                    continue
                raise IOError("Server sent no data")
            except Exception as e:
                tries += 1
                disk = isinstance(e, OSError) and getattr(e, "errno", None) in (errno.ENOSPC, errno.EACCES)
                fatal = (isinstance(e, ValueError)
                         or (isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404, 410))
                         or disk)
                if srcs is not None and not disk:
                    srcs.fail(url, fatal or isinstance(e, MirrorError), e)
                    if srcs.alive():                            # another source takes over, nothing is lost
                        tries -= 1
                        continue
                if fatal or tries > 5:
                    self._fail(e)
                    return
                time.sleep(self.RETRY_DELAY)

    # ---- HLS (.m3u8): plain and fMP4 streams natively; AES-128 / live / byte-range go to yt-dlp ----
    def _text(self, url):
        req = urllib.request.Request(ensure_http(url, base=self.url), headers=self.hdr(url=url))
        with netsec.urlopen(req, timeout=20) as r:
            return r.read(5 << 20).decode("utf-8", "replace")

    def _hls_prepare(self):
        base = self.url
        lines = [l.strip() for l in self._text(base).splitlines() if l.strip()]
        if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):  # master: pick best quality
            best, bw = None, -1
            for i, l in enumerate(lines):
                if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                    m = re.search(r"BANDWIDTH=(\d+)", l)
                    b = int(m.group(1)) if m else 0
                    if b > bw:
                        best, bw = lines[i + 1], b
            base = urllib.parse.urljoin(base, best)
            lines = [l.strip() for l in self._text(base).splitlines() if l.strip()]
        self.hls_map = None
        for l in lines:
            if l.startswith("#EXT-X-KEY") and "METHOD=NONE" not in l:
                raise NeedEngine("encrypted")
            if l.startswith("#EXT-X-BYTERANGE") or (l.startswith("#EXT-X-MAP") and "BYTERANGE" in l):
                raise NeedEngine("byte-range")
            if l.startswith("#EXT-X-MAP"):
                m = re.search(r'URI="([^"]+)"', l)
                if m:
                    self.hls_map = urllib.parse.urljoin(base, m[1])
        if not any(l.startswith("#EXT-X-ENDLIST") for l in lines):
            raise NeedEngine("live")
        self.hls = [urllib.parse.urljoin(base, l) for l in lines if not l.startswith("#")]
        if not self.hls:
            raise IOError("Empty playlist")
        self.hls_total = len(self.hls)

    def _seg(self, url, out, i):
        for _ in range(3):
            if self.state != "downloading":
                return
            try:
                req = urllib.request.Request(ensure_http(url, base=self.url), headers=self.hdr(url=url))
                with netsec.urlopen(req, timeout=20) as r:
                    data = r.read()
                Counter.add(len(data))
                Limiter.wait(len(data))
                self.throttle.wait(len(data))
                out[i] = data
                return
            except ValueError as e:                  # blocked link: do not retry
                out[i] = None
                log.warning("HLS segment blocked: %s", e)
                return
            except Exception as e:
                log.debug("HLS segment retry: %s", short_err(e))
                time.sleep(self.RETRY_DELAY)

    def _run_hls(self):
        if not self.hls:
            self._hls_prepare()
        if not self.path:
            stem = os.path.splitext(os.path.basename(urllib.parse.urlparse(self.url).path))[0]
            b = clean(self.fname or self.title or stem or "video")
            if b.lower().endswith(".m3u8"):
                b = b[:-5]
            default = ".mp4" if self.hls_map else ".ts"
            self._assign_path(b + ("" if os.path.splitext(b)[1].lower() in MEDIA_EXT else default))
        part = self.path + ".part"
        if self.hls_pos and not os.path.exists(part):
            self.hls_pos = self.hls_bytes = 0
        with open(part, "ab" if self.hls_pos else "wb") as f:
            if self.hls_map and not self.hls_pos:      # fMP4: the init segment must come first
                tmp = [None]
                self._seg(self.hls_map, tmp, 0)
                if tmp[0] is None:
                    raise IOError("Could not download the stream header")
                f.write(tmp[0])
                self.hls_bytes += len(tmp[0])
            while self.hls_pos < len(self.hls) and self.state == "downloading":
                batch = self.hls[self.hls_pos:self.hls_pos + max(1, self.conns)]
                out = [None] * len(batch)
                ts = [threading.Thread(target=self._seg, args=(u, out, i), daemon=True)
                      for i, u in enumerate(batch)]
                for t in ts: t.start()
                for t in ts: t.join()
                if self.state != "downloading":
                    break
                if any(o is None for o in out):
                    raise IOError("Segment download failed")
                for o in out:
                    f.write(o)
                    self.hls_bytes += len(o)
                f.flush()
                self.hls_pos += len(batch)
        if self.state == "downloading" and self.hls_pos >= len(self.hls):
            os.replace(part, self.path)
            self.size = self.hls_bytes
            self.state = "done"
