"""Torrent and magnet links: detect them and hand them to a torrent client (no GUI code, no BitTorrent engine).

IK DOWNLOADER does not speak BitTorrent itself. Instead a magnet link or .torrent file is handed to a client:
  1. the client configured in Options > Torrents,
  2. a client bundled next to the program (a `torrent` folder, or aria2c next to the .exe),
  3. a well-known client that is installed (qBittorrent, Transmission, Deluge, uTorrent, ...),
  4. the operating system's default handler for magnet links / .torrent files.
If none of these works the caller is told so (it never crashes) and can offer to copy the link instead.
"""
import base64, os, re, shutil, subprocess, sys, urllib.parse
from typing import List, NamedTuple, Optional

from .common import BUNDLE, log

MAX_MAGNET = 4096
TORRENT_TYPES = ("application/x-bittorrent",)


class Client(NamedTuple):
    name: str
    path: str
    kind: str = "gui"          # "gui" (opens its own window), "aria2" (console downloader) or "app" (macOS bundle)


def is_magnet(s) -> bool:
    return isinstance(s, str) and s.strip()[:8].lower() == "magnet:?"


def is_torrent_url(url: str, ctype: str = "") -> bool:
    """A link to a .torrent file (by extension, or because the server said so)."""
    if (ctype or "").split(";")[0].strip().lower() in TORRENT_TYPES:
        return True
    try:
        return urllib.parse.urlparse(url).path.lower().endswith(".torrent")
    except ValueError:
        return False


def valid_magnet(uri: str) -> bool:
    """Reject anything that is not a single-line magnet URI of sane length (it goes to another program)."""
    if not is_magnet(uri):
        return False
    uri = uri.strip()
    return len(uri) <= MAX_MAGNET and not re.search(r"[\x00-\x1f\x7f\s]", uri)


def parse_magnet(uri: str) -> dict:
    """{'btih': hex info-hash or '', 'name': display name, 'trackers': [...], 'size': exact length or 0}."""
    out = {"btih": "", "name": "", "trackers": [], "size": 0}
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(uri.strip()).query, keep_blank_values=True)
    except ValueError:
        return out
    for xt in q.get("xt", []):
        m = re.fullmatch(r"urn:btih:([0-9A-Za-z]+)", xt.strip())
        if not m:
            continue
        h = m[1]
        if re.fullmatch(r"[0-9a-fA-F]{40}", h):
            out["btih"] = h.lower()
        elif re.fullmatch(r"[A-Za-z2-7]{32}", h):                    # 32-character base32 form
            try:
                out["btih"] = base64.b32decode(h.upper()).hex()
            except ValueError:
                pass
        if out["btih"]:
            break
    out["name"] = (q.get("dn") or [""])[0].strip()[:200]
    out["trackers"] = [t for t in q.get("tr", []) if t][:20]
    xl = (q.get("xl") or ["0"])[0]
    out["size"] = int(xl) if xl.isdigit() else 0
    return out


def display_name(uri: str) -> str:
    p = parse_magnet(uri)
    return p["name"] or (("magnet " + p["btih"][:12]) if p["btih"] else "magnet link")


# ---------------------------------------------------------------------------------------------
# finding a client
# ---------------------------------------------------------------------------------------------
def _known_clients() -> List[Client]:
    e, j = os.environ.get, os.path.join
    found: List[Client] = []

    def add(name, path, kind="gui"):
        if path and os.path.exists(path):
            found.append(Client(name, path, kind))

    if sys.platform.startswith("win"):
        for base in filter(None, (e("ProgramFiles"), e("ProgramFiles(x86)"), e("ProgramW6432"))):
            add("qBittorrent", j(base, "qBittorrent", "qbittorrent.exe"))
            add("Transmission", j(base, "Transmission", "transmission-qt.exe"))
            add("Deluge", j(base, "Deluge", "deluge.exe"))
            add("Tixati", j(base, "Tixati", "tixati.exe"))
        for base in filter(None, (e("APPDATA"), e("LOCALAPPDATA"))):
            add("uTorrent", j(base, "uTorrent", "uTorrent.exe"))
            add("BitTorrent", j(base, "BitTorrent", "BitTorrent.exe"))
    elif sys.platform == "darwin":
        for app in ("qBittorrent", "Transmission", "Deluge", "Folx", "WebTorrent"):
            add(app, j("/Applications", app + ".app"), "app")
    for exe, label in (("qbittorrent", "qBittorrent"), ("transmission-qt", "Transmission"),
                       ("transmission-gtk", "Transmission"), ("deluge-gtk", "Deluge"), ("deluge", "Deluge"),
                       ("ktorrent", "KTorrent"), ("fragments", "Fragments"), ("tixati", "Tixati")):
        p = shutil.which(exe)
        if p:
            found.append(Client(label, p))
    return found


def _bundled() -> Optional[Client]:
    """A client shipped with the program: any .exe/binary in a `torrent` folder, or aria2c beside the program."""
    d = os.path.join(BUNDLE, "torrent")
    if os.path.isdir(d):
        try:
            for n in sorted(os.listdir(d)):
                p = os.path.join(d, n)
                if os.path.isfile(p) and (n.lower().endswith(".exe") or os.access(p, os.X_OK)):
                    kind = "aria2" if n.lower().startswith("aria2c") else "gui"
                    return Client("bundled " + os.path.splitext(n)[0], p, kind)
        except OSError:
            pass
    for n in ("aria2c.exe", "aria2c"):
        p = os.path.join(BUNDLE, n)
        if os.path.isfile(p):
            return Client("bundled aria2", p, "aria2")
    return None


def _kind_of(path: str) -> str:
    n = os.path.basename(path).lower()
    if path.lower().endswith(".app"):
        return "app"
    return "aria2" if n.startswith("aria2c") else "gui"


def find_client(configured: str = "") -> Optional[Client]:
    """Best available client, or None (the OS default handler may still work: see send())."""
    configured = (configured or "").strip().strip('"')
    if configured:
        if os.path.exists(configured):
            return Client(os.path.splitext(os.path.basename(configured))[0] or "client", configured, _kind_of(configured))
        log.info("configured torrent client not found: %s", configured)
    b = _bundled()
    if b:
        return b
    k = _known_clients()
    return k[0] if k else None


def describe_client(configured: str = "") -> str:
    c = find_client(configured)
    if c:
        return f"{c.name}  ({c.path})"
    return "no torrent client found - the system default for magnet links will be tried"


# ---------------------------------------------------------------------------------------------
# hand-off
# ---------------------------------------------------------------------------------------------
class HandoffError(Exception):
    pass


def _os_default(target: str) -> None:
    """Let the operating system pick the program registered for magnet: links / .torrent files."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)                                              # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except (OSError, ValueError) as e:
        raise HandoffError("no program is registered to open torrent links on this computer") from e


def send(target: str, configured: str = "", save_dir: str = "") -> str:
    """Hand a magnet URI or a .torrent file path to a client. Returns the client's name; raises HandoffError.
    Arguments are always passed as a list (never through a shell)."""
    if is_magnet(target):
        target = target.strip()
        if not valid_magnet(target):
            raise HandoffError("the magnet link is malformed")
    elif os.path.isfile(target) and target.lower().endswith(".torrent"):
        target = os.path.abspath(target)
    else:
        raise HandoffError("not a magnet link or .torrent file")
    c = find_client(configured)
    if c:
        try:
            if c.kind == "app":
                subprocess.Popen(["open", "-a", c.path, target])
            elif c.kind == "aria2":
                cmd = [c.path, "--seed-time=0", "--bt-save-metadata=true"]
                if save_dir and os.path.isdir(save_dir):
                    cmd += ["--dir", save_dir]
                kw = {"creationflags": subprocess.CREATE_NEW_CONSOLE} if sys.platform.startswith("win") else {}
                subprocess.Popen(cmd + [target], **kw)
            else:
                subprocess.Popen([c.path, target])
            log.info("torrent handed to %s", c.name)
            return c.name
        except OSError as e:
            log.warning("could not start %s: %s", c.path, e)       # fall through to the system default
    _os_default(target)
    log.info("torrent handed to the system default handler")
    return "default torrent program"
