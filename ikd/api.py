"""Local API for the browser extension (127.0.0.1 only).

Security model
  * The server only listens on loopback and (on Windows) takes the port exclusively.
  * Every request must carry the right Host header (blocks DNS rebinding) and X-IK-Client: 1
    (a custom header makes browsers run a CORS preflight, which this server never answers).
  * The first time, the extension must PAIR: the app shows a confirmation dialog, and on "Yes" hands out a
    random token. Only the SHA-256 of the token is stored here; the token is bound to the extension's origin.
  * /add, /info and /browse require that token. /ping is public and reveals only the app name and version.
"""
import hashlib, hmac, json, os, secrets, socket, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .common import APP, HOME, PORT, VERSION, guess_mode, log, read_json, short_err, write_json
from .engine import fetch_info
from .netsec import ensure_http

PAIR_FILE = os.path.join(HOME, "pairing.json")
EXT_SCHEMES = ("chrome-extension://", "moz-extension://")


class Busy(Exception):
    pass


class Pairing:
    """Remembers which browser extensions the user approved."""

    def __init__(self, path: str = PAIR_FILE, ask: Optional[Callable[[str], bool]] = None):
        self.path, self.ask = path, ask
        self.lock, self.pending = threading.Lock(), threading.Lock()
        data = read_json(path, {})
        self.tokens: dict = data if isinstance(data, dict) else {}

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def verify(self, token: str, origin: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self.lock:
            rec = self.tokens.get(self._h(token))
        if not rec:
            return False
        return not origin or hmac.compare_digest(str(rec.get("origin", "")), origin)

    def pair(self, origin: str) -> Optional[str]:
        """Ask the user. Returns a new token, or None if they said no. Raises Busy if a question is already open."""
        if not self.pending.acquire(blocking=False):
            raise Busy()
        try:
            if not (self.ask and self.ask(origin)):
                log.info("pairing refused for %s", origin)
                return None
            token = secrets.token_urlsafe(32)
            with self.lock:
                self.tokens[self._h(token)] = {"origin": origin, "created": int(time.time())}
                write_json(self.path, self.tokens)
            log.info("paired browser extension %s", origin)
            return token
        finally:
            self.pending.release()

    def revoke_all(self) -> int:
        with self.lock:
            n = len(self.tokens)
            self.tokens = {}
            write_json(self.path, self.tokens)
        log.info("all browser pairings revoked (%d)", n)
        return n

    def count(self) -> int:
        with self.lock:
            return len(self.tokens)


class Api:
    """Calls from the browser extension that need the app (video info, folder picker)."""

    def __init__(self, app):
        self.app = app

    def info(self, d: dict) -> dict:
        out = {"ok": True, "folder": self.app.pub_folder, "ffmpeg": self.app.pub_ffmpeg}
        if d.get("extract"):
            url = str(d.get("url", ""))
            try:
                ensure_http(url)
            except ValueError:
                return {"ok": False, "error": "Invalid link"}
            if guess_mode(url) != "site":          # the video engine is only used for known video sites here
                return {"ok": False, "error": "Not a supported video site"}
            try:
                out.update(fetch_info(url))
            except ImportError:
                return {"ok": False, "error": "Video engine missing (pip install yt-dlp)"}
            except Exception as e:
                return {"ok": False, "error": short_err(e)}
        return out

    def browse(self, d: dict) -> dict:
        ev, box = threading.Event(), {}
        self.app.cmds.put(("browse", str(d.get("initial", "")), ev, box))
        ev.wait(180)
        return {"ok": True, "path": box.get("path")}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False      # on Windows SO_REUSEADDR would let another program share (hijack) the port

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_handler(inbox, api: Optional[Api] = None, pairing: Optional[Pairing] = None):
    slots = threading.BoundedSemaphore(4)        # at most 4 slow calls (/info, /browse) at the same time

    class H(BaseHTTPRequestHandler):
        timeout = 30                              # drop clients that stall while sending

        def log_message(self, *a):
            pass

        def _send(self, code: int, obj: dict):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host", "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {})
            self._send(200, {"app": APP, "version": VERSION}) if self.path == "/ping" else self._send(404, {})

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"ok": False})
            origin = self.headers.get("Origin", "")
            ext_origin = origin.startswith(EXT_SCHEMES)
            if (self.path not in ("/add", "/info", "/browse", "/pair") or self.headers.get("X-IK-Client") != "1"
                    or (origin and not ext_origin)):
                return self._send(403, {"ok": False})          # blocks random websites
            if self.path == "/pair":
                if not ext_origin or pairing is None:
                    return self._send(403, {"ok": False})
                try:
                    token = pairing.pair(origin)
                except Busy:
                    return self._send(429, {"ok": False, "error": "A pairing question is already open in the app"})
                return self._send(200, {"ok": True, "token": token}) if token else self._send(403, {"ok": False, "error": "denied"})
            if pairing is None or not pairing.verify(self.headers.get("X-IK-Token", ""), origin):
                return self._send(401, {"ok": False, "error": "not paired"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n < 0 or n > 65536:
                    return self._send(413, {"ok": False})
                data = json.loads(self.rfile.read(n))
                if not isinstance(data, dict):
                    raise ValueError
            except Exception:
                return self._send(400, {"ok": False})
            if self.path == "/add":
                if inbox.qsize() > 200:
                    return self._send(429, {"ok": False})
                inbox.put(data)
                return self._send(200, {"ok": True})
            if api is None:
                return self._send(404, {"ok": False})
            if not slots.acquire(blocking=False):
                return self._send(429, {"ok": False, "error": "Busy, try again"})
            try:
                self._send(200, api.info(data) if self.path == "/info" else api.browse(data))
            finally:
                slots.release()
    return H


def start_server(inbox, api: Optional[Api], pairing: Pairing, port: int = PORT) -> Server:
    srv = Server(("127.0.0.1", port), make_handler(inbox, api, pairing))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
