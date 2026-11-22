"""Shared constants and small helpers (no network, no GUI). Imported by every other module."""
import os, re, sys, json, shutil, base64, logging, logging.handlers, threading, subprocess, urllib.parse
from typing import Any, Optional


APP, VERSION, PORT = "IK DOWNLOADER", "2.6.0", 18080
REPO = "YOUR-GITHUB-USER/IK-DOWNLOADER"   # set automatically by the release workflow; edit by hand only for local builds
HOME = os.path.join(os.path.expanduser("~"), ".ik_downloader")
STATE, CFG = os.path.join(HOME, "downloads.json"), os.path.join(HOME, "settings.json")
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "IK DOWNLOADER")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) IKDownloader/2.0"
CATS = {
    "Video": ".mp4 .mkv .avi .mov .wmv .flv .webm .ts .m4v",
    "Music": ".mp3 .flac .wav .aac .m4a .ogg",
    "Compressed": ".zip .rar .7z .tar .gz .bz2 .xz",
    "Documents": ".pdf .doc .docx .xls .xlsx .ppt .pptx .txt .epub",
    "Programs": ".exe .msi .dmg .apk .deb .iso",
}
MEDIA_EXT = (CATS["Video"] + " " + CATS["Music"]).split()
ALL_EXT = set(sum((e.split() for e in CATS.values()), []))
KNOWN_EXT = ALL_EXT | set((".html .htm .jpg .jpeg .png .gif .webp .svg .bmp .ico .tif .tiff .psd .csv .json .xml "
                           ".torrent .srt .vtt .ttf .otf .woff .jar .bin .img .cab .msu .pkg .rpm .whl .xapk "
                           ".dll .m3u8 .apks .appx .msix .crx .vsix .lz .zst .tgz .odt .ods .rtf .mpg .mpeg .3gp").split())
MIME_EXT = {"application/zip": ".zip", "application/x-zip-compressed": ".zip", "application/x-rar-compressed": ".rar",
            "application/vnd.rar": ".rar", "application/x-7z-compressed": ".7z", "application/gzip": ".gz",
            "application/x-gzip": ".gz", "application/x-tar": ".tar", "application/x-msdownload": ".exe",
            "application/vnd.microsoft.portable-executable": ".exe", "application/x-msi": ".msi",
            "application/x-apple-diskimage": ".dmg", "application/vnd.android.package-archive": ".apk",
            "application/x-iso9660-image": ".iso", "application/pdf": ".pdf", "application/epub+zip": ".epub",
            "application/msword": ".doc", "image/jpeg": ".jpg", "audio/mpeg": ".mp3", "video/mp4": ".mp4",
            "video/webm": ".webm", "video/x-matroska": ".mkv", "video/quicktime": ".mov", "text/plain": ".txt",
            "text/html": ".html", "application/x-bittorrent": ".torrent"}
QUALITIES = {"Best": "best", "1080p": "1080p", "720p": "720p", "480p": "480p", "360p": "360p",
             "Audio only (MP3)": "audio"}
SITE_HOSTS = ("youtube.com youtu.be tiktok.com instagram.com facebook.com fb.watch twitter.com x.com "
              "vimeo.com dailymotion.com reddit.com redd.it twitch.tv soundcloud.com bilibili.com "
              "pinterest.com linkedin.com streamable.com rumble.com ok.ru vk.com bsky.app threads.net "
              "snapchat.com").split()

# Files bundled inside the .exe (deno for YouTube) are found through PATH.
# Project root: the PyInstaller bundle folder, or (running from source) the folder that contains the `ikd` package.
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PATH"] = BUNDLE + os.pathsep + os.environ.get("PATH", "")


def asset(name):
    p = os.path.join(BUNDLE, "assets", name)
    return p if os.path.exists(p) else None


def ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:                       # package missing or no bundled binary: try the system one
        log.debug("imageio-ffmpeg unavailable: %s", e)
        return shutil.which("ffmpeg")


def guess_mode(url):
    """site = known video site, direct = plain file / m3u8, auto = try video engine, else direct."""
    u = urllib.parse.urlparse(url)
    host = u.netloc.lower().split(":")[0]
    if any(host == h or host.endswith("." + h) for h in SITE_HOSTS):
        return "site"
    ext = os.path.splitext(u.path.lower())[1]
    return "direct" if ext in (".m3u8", ".torrent") or ext in ALL_EXT else "auto"


def fmt(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def hms(s):
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def category(name):
    ext = os.path.splitext(name)[1].lower()
    for c, e in CATS.items():
        if ext in e.split():
            return c
    return "Other"


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def clean(n: str) -> str:
    """Make a safe file name: no path separators, control characters, trailing dots or Windows device names."""
    n = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", n).strip(" .")[:150].rstrip(" .") or "download"
    if os.path.splitext(n)[0].upper() in _RESERVED:
        n = "_" + n
    return n


def short_err(e):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")[:160]



# --------------------------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------------------------
log = logging.getLogger("ikd")


log.addHandler(logging.NullHandler())
_log_ready = False


def setup_logging() -> None:
    """Rotating log file in ~/.ik_downloader/ik.log (never contains cookies or tokens)."""
    global _log_ready
    if _log_ready:
        return
    _log_ready = True
    log.setLevel(logging.INFO)
    try:
        os.makedirs(HOME, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(os.path.join(HOME, "ik.log"), maxBytes=512 * 1024,
                                                 backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    except OSError:
        pass


# --------------------------------------------------------------------------------------------
# files: atomic JSON, private permissions
# --------------------------------------------------------------------------------------------
def write_json(path: str, obj: Any, private: bool = True) -> None:
    """Write JSON atomically (temp file + replace) so a crash can never leave a half-written file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    if private and os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("could not read %s: %s", path, e)
        try:
            os.replace(path, path + ".bad")      # keep the broken file for inspection, start fresh
        except OSError:
            pass
        return default


# --------------------------------------------------------------------------------------------
# secrets at rest: login cookies are encrypted with the Windows user account (DPAPI)
# --------------------------------------------------------------------------------------------
SENSITIVE_HEADERS = ("Cookie", "Authorization")


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def seal(text: str) -> str:
    """Encrypt a secret for storage. Returns "" where no safe storage exists (then it is simply not saved)."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")
    except Exception as e:           # ctypes / OS errors: never fall back to plaintext
        log.warning("could not encrypt a secret: %s", e)
        return ""


def unseal(blob: str) -> str:
    """Inverse of seal(). Anything that is not a valid sealed value (old plaintext, other PC/user) gives ""."""
    if not (isinstance(blob, str) and blob.startswith("dpapi:") and sys.platform.startswith("win")):
        return ""
    try:
        return _dpapi(base64.b64decode(blob[6:]), False).decode("utf-8")
    except Exception as e:
        log.info("stored secret could not be decrypted (%s); it will be dropped", e)
        return ""


def seal_headers(h: dict) -> dict:
    out = {}
    for k, v in (h or {}).items():
        if k in SENSITIVE_HEADERS:
            s = seal(v) if isinstance(v, str) and v else ""
            if s:
                out[k] = s
        else:
            out[k] = v
    return out


def unseal_headers(h: dict) -> dict:
    out = {}
    for k, v in (h or {}).items():
        if k in SENSITIVE_HEADERS:
            u = unseal(v)
            if u:
                out[k] = u
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------------------------
# folders the browser link may NOT write into (autostart / system locations)
# --------------------------------------------------------------------------------------------
def _blocked_dirs() -> list:
    e, j = os.environ.get, os.path.join
    home = os.path.expanduser("~")
    d = []
    if sys.platform.startswith("win"):
        for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
            if e(v):
                d.append(e(v))
        for base in filter(None, (e("APPDATA"), e("ProgramData"))):
            d.append(j(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup"))
    else:
        d += ["/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/System", "/Library", "/var/lib",
              j(home, ".ssh"), j(home, ".config", "autostart"), j(home, "Library", "LaunchAgents")]
    return [os.path.normcase(os.path.normpath(x)) for x in d]


def safe_folder(p: Any) -> Optional[str]:
    """Validate a save-folder that came from outside (browser extension). None = not acceptable."""
    if not isinstance(p, str) or not p.strip() or "\x00" in p or not os.path.isabs(p):
        return None
    if p.startswith(("\\\\?\\", "\\\\.\\")):          # raw device paths
        return None
    n = os.path.normcase(os.path.normpath(p))
    for b in _blocked_dirs():
        if n == b or n.startswith(b + os.sep):
            log.warning("blocked save folder from browser link: %s", p)
            return None
    return os.path.normpath(p)


def version_tuple(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3])


# --------------------------------------------------------------------------------------------
# shut the PC down when everything is finished (Windows, macOS, Linux)
# --------------------------------------------------------------------------------------------
_off_timer: Optional[threading.Timer] = None


def _power_off() -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", 'tell application "System Events" to shut down'])
        else:
            subprocess.Popen(["systemctl", "poweroff"])
    except OSError as e:
        log.warning("shutdown failed: %s", e)


def shutdown_pc(delay: int = 60) -> None:
    global _off_timer
    if sys.platform.startswith("win"):
        subprocess.Popen(["shutdown", "/s", "/t", str(delay)])
    else:
        cancel_shutdown()
        _off_timer = threading.Timer(delay, _power_off)
        _off_timer.daemon = True
        _off_timer.start()


def cancel_shutdown() -> None:
    global _off_timer
    if sys.platform.startswith("win"):
        subprocess.Popen(["shutdown", "/a"])
    elif _off_timer is not None:
        _off_timer.cancel()
        _off_timer = None
