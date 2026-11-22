"""Tests for the engine, network safety and local API. Standard library only:  python -m unittest discover -s tests -t ."""
import hashlib, http.client, json, os, queue, shutil, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from ikd import common, netsec
from ikd.api import Api, Pairing, Server, make_handler
from ikd.common import (clean, read_json, safe_folder, seal_headers, unseal_headers, version_tuple, write_json)
from ikd.engine import Job, NeedEngine

Job.RETRY_DELAY = 0.01


# ------------------------------------------------------------------ a tiny configurable test web server
class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, ranged=True, slow=0.0):
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.files, self.ctypes, self.redirects, self.seen = {}, {}, {}, []
        self.ranged, self.slow, self.cuts, self.empty = ranged, slow, 0, False
        self.lock = threading.Lock()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def base(self): return f"http://127.0.0.1:{self.server_address[1]}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            s = self.server
            with s.lock: s.seen.append((self.path, dict(self.headers)))
            try:
                if self.path in s.redirects:
                    self.send_response(302); self.send_header("Location", s.redirects[self.path])
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                body = s.files.get(self.path)
                if body is None:
                    self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
                ct = s.ctypes.get(self.path, "application/octet-stream")
                total, start, end, code = len(body), 0, len(body) - 1, 200
                rng = self.headers.get("Range", "")
                if s.ranged and rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    start, end, code = int(a), (int(b) if b else total - 1), 206
                chunk = body[start:end + 1]
                self.send_response(code); self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(chunk)))
                if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.end_headers()
                if s.empty: return
                cut = False
                with s.lock:
                    if s.cuts > 0 and len(chunk) > 1_000_000: s.cuts -= 1; cut = True
                if cut: chunk = chunk[:len(chunk) // 2]            # connection dies half way
                for i in range(0, len(chunk), 32768):
                    self.wfile.write(chunk[i:i + 32768])
                    if s.slow: time.sleep(s.slow)
            except (BrokenPipeError, ConnectionResetError):
                pass


def run_job(job, timeout=30):
    job.start()
    t0 = time.time()
    while job.state == "downloading" and time.time() - t0 < timeout:
        time.sleep(0.02)
    return job


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        netsec.set_proxy("")

    def web(self, **kw):
        w = WebServer(**kw)
        self.addCleanup(w.shutdown); self.addCleanup(w.server_close)
        return w

    def job(self, url, name="out.bin", headers=None, conns=4):
        j = Job(url, self.tmp, conns, headers or {})
        j.exact, j.fname, j.keepname = True, name, True
        return j


DATA = os.urandom(3_500_000)
SHA = hashlib.sha256(DATA).hexdigest()


def sha(path):
    with open(path, "rb") as f: return hashlib.sha256(f.read()).hexdigest()


def slurp(path):
    with open(path, "rb") as f: return f.read()


# ------------------------------------------------------------------ downloads
class DownloadTests(Base):
    def test_multi_connection_download_is_byte_exact(self):
        w = self.web(); w.files["/f.bin"] = DATA
        j = run_job(self.job(w.base + "/f.bin"))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)
        self.assertFalse(os.path.exists(j.path + ".part"))
        self.assertGreater(len(j.segs), 1)

    def test_server_without_range_support(self):
        w = self.web(ranged=False); w.files["/f.bin"] = DATA
        j = run_job(self.job(w.base + "/f.bin"))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)

    def test_connection_cut_mid_transfer_recovers_without_corruption(self):
        w = self.web(); w.files["/f.bin"] = DATA; w.cuts = 2
        j = run_job(self.job(w.base + "/f.bin", conns=2))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)

    def test_cut_without_range_restarts_cleanly(self):
        w = self.web(ranged=False); w.files["/f.bin"] = DATA; w.cuts = 1
        j = run_job(self.job(w.base + "/f.bin"))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)

    def test_empty_body_fails_instead_of_looping_forever(self):
        w = self.web(ranged=False); w.files["/f.bin"] = DATA; w.empty = True
        j = run_job(self.job(w.base + "/f.bin"), timeout=15)
        self.assertEqual(j.state, "error")

    def test_404_is_fatal_immediately(self):
        w = self.web()
        j = run_job(self.job(w.base + "/missing.bin"))
        self.assertEqual(j.state, "error")

    def test_pause_then_resume_after_restart(self):
        w = self.web(slow=0.01); w.files["/f.bin"] = DATA
        j = self.job(w.base + "/f.bin"); j.start()
        t0 = time.time()
        while j.done_bytes < 400_000 and time.time() - t0 < 20: time.sleep(0.02)
        j.pause(); time.sleep(0.5)
        self.assertEqual(j.state, "paused")
        self.assertLess(j.done_bytes, len(DATA))
        saved = json.loads(json.dumps(j.to_dict()))                 # what the app writes to disk
        w.slow = 0
        j2 = Job(saved["url"], saved["folder"], 4, saved["headers"], "", saved)
        self.assertEqual(j2.state, "paused")
        run_job(j2)
        self.assertEqual(j2.state, "done", j2.msg)
        self.assertEqual(sha(j2.path), SHA)

    def test_two_downloads_never_share_a_file_name(self):
        w = self.web(); w.files["/f.bin"] = b"x" * 2000
        a, b = self.job(w.base + "/f.bin"), self.job(w.base + "/f.bin")
        a.probe(); b.probe()
        self.assertNotEqual(a.path, b.path)

    def test_not_enough_disk_space_is_refused_up_front(self):
        w = self.web(); w.files["/f.bin"] = DATA
        j = self.job(w.base + "/f.bin")
        fake = shutil._ntuple_diskusage(100, 99, 1)
        with mock.patch("shutil.disk_usage", return_value=fake):
            with self.assertRaises(IOError): j.probe()
        self.assertEqual([f for f in os.listdir(self.tmp)], [])     # placeholder removed


# ------------------------------------------------------------------ HLS
class HlsTests(Base):
    def playlist(self, w, segs, extra="", end=True):
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4", extra]
        for name in segs: lines += ["#EXTINF:4.0,", name]
        if end: lines.append("#EXT-X-ENDLIST")
        w.files["/p.m3u8"] = "\n".join(l for l in lines if l).encode()

    def test_plain_hls_is_concatenated_in_order(self):
        w = self.web(); parts = [bytes([i]) * 5000 for i in range(7)]
        for i, p in enumerate(parts): w.files[f"/s{i}.ts"] = p
        self.playlist(w, [f"s{i}.ts" for i in range(7)])
        j = run_job(self.job(w.base + "/p.m3u8", "clip"))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(slurp(j.path), b"".join(parts))

    def test_fmp4_hls_writes_init_segment_first(self):
        w = self.web(); w.files["/init.mp4"] = b"INIT"
        for i in range(3): w.files[f"/s{i}.m4s"] = f"[seg{i}]".encode()
        self.playlist(w, [f"s{i}.m4s" for i in range(3)], extra='#EXT-X-MAP:URI="init.mp4"')
        j = run_job(self.job(w.base + "/p.m3u8", "clip"))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(slurp(j.path), b"INIT[seg0][seg1][seg2]")
        self.assertTrue(j.path.endswith(".mp4"))

    def test_encrypted_live_and_byterange_streams_need_the_engine(self):
        w = self.web()
        for extra, end in (('#EXT-X-KEY:METHOD=AES-128,URI="k"', True), ("", False), ("#EXT-X-BYTERANGE:100@0", True)):
            self.playlist(w, ["s0.ts"], extra=extra, end=end)
            j = self.job(w.base + "/p.m3u8")
            with self.assertRaises(NeedEngine): j._hls_prepare()

    def test_file_url_in_playlist_is_blocked(self):
        w = self.web(); w.files["/ok.ts"] = b"ok"
        secret = os.path.join(self.tmp, "secret.txt")
        with open(secret, "w") as f: f.write("TOPSECRET")
        self.playlist(w, ["ok.ts", "file://" + secret])
        j = run_job(self.job(w.base + "/p.m3u8", "clip"))
        self.assertEqual(j.state, "error")
        part = j.path + ".part"
        self.assertNotIn(b"TOPSECRET", slurp(part) if os.path.exists(part) else b"")

    def test_cookie_goes_to_same_site_segments_only(self):
        a, b = self.web(), self.web()                 # 127.0.0.1 (playlist) and localhost (other site)
        a.files["/s0.ts"] = b"a"
        b.files["/s1.ts"] = b"b"
        other = f"http://localhost:{b.server_address[1]}/s1.ts"
        self.playlist(a, ["s0.ts", other])
        j = run_job(self.job(a.base + "/p.m3u8", "clip", headers={"Cookie": "sid=SECRET", "Referer": "http://x/"}))
        self.assertEqual(j.state, "done", j.msg)
        sent_a = [h for p, h in a.seen if p == "/s0.ts"][0]
        sent_b = [h for p, h in b.seen if p == "/s1.ts"][0]
        self.assertEqual(sent_a.get("Cookie"), "sid=SECRET")
        self.assertNotIn("Cookie", sent_b)
        self.assertEqual(sent_b.get("Referer"), "http://x/")


# ------------------------------------------------------------------ network safety
class NetSafetyTests(Base):
    def test_only_http_and_https(self):
        for bad in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "http:///nohost"):
            with self.assertRaises(ValueError): netsec.ensure_http(bad)
        self.assertEqual(netsec.ensure_http("https://example.com/a"), "https://example.com/a")

    def test_public_to_private_hop_is_blocked(self):
        with mock.patch.object(netsec, "is_private_host", side_effect=lambda h: h in ("127.0.0.1", "10.0.0.5")):
            with self.assertRaises(ValueError): netsec.ensure_http("http://10.0.0.5/x", base="https://example.com/p.m3u8")
            netsec.ensure_http("http://10.0.0.5/x", base="http://127.0.0.1/p")          # LAN -> LAN is fine
            netsec.ensure_http("https://cdn.example.org/x", base="https://example.com/p")

    def test_same_site_rules(self):
        self.assertTrue(netsec.same_site("https://a.example.com/x", "https://example.com/y"))
        self.assertTrue(netsec.same_site("https://example.com", "https://example.com:8080"))
        self.assertFalse(netsec.same_site("https://evil-example.com", "https://example.com"))
        self.assertFalse(netsec.same_site("http://127.0.0.1", "http://localhost"))

    def test_redirect_to_file_is_blocked_and_cookies_are_not_forwarded(self):
        a, b = self.web(), self.web()
        a.redirects["/go"] = "file:///etc/hostname"
        with self.assertRaises((ValueError, urllib.error.HTTPError)): netsec.urlopen(urllib.request.Request(a.base + "/go"))
        a.redirects["/ftp"] = "ftp://example.com/x"                      # urllib would follow this one by default
        with self.assertRaises(ValueError): netsec.urlopen(urllib.request.Request(a.base + "/ftp"))
        b.files["/x"] = b"ok"
        a.redirects["/hop"] = f"http://localhost:{b.server_address[1]}/x"
        netsec.urlopen(urllib.request.Request(a.base + "/hop", headers={"Cookie": "sid=1", "Authorization": "Bearer t"})).read()
        got = [h for p, h in b.seen if p == "/x"][0]
        self.assertNotIn("Cookie", got); self.assertNotIn("Authorization", got)


# ------------------------------------------------------------------ files, secrets, folders
class StorageTests(Base):
    def test_cookies_are_never_written_in_plaintext(self):
        j = Job("http://x/y", self.tmp, 4, {"Cookie": "sid=SECRET", "Referer": "http://r/"})
        blob = json.dumps(j.to_dict())
        self.assertNotIn("SECRET", blob)
        self.assertIn("http://r/", blob)

    def test_legacy_plaintext_cookie_in_old_state_file_is_dropped(self):
        d = Job("http://x/y", self.tmp, 4, {"Referer": "r"}).to_dict()
        d["headers"] = {"Cookie": "sid=OLD", "Referer": "r"}
        self.assertEqual(Job("http://x/y", self.tmp, 4, None, "", d).headers, {"Referer": "r"})

    @unittest.skipUnless(sys.platform.startswith("win"), "DPAPI exists only on Windows")
    def test_dpapi_round_trip(self):
        h = {"Cookie": "sid=SECRET", "X": "1"}
        sealed = seal_headers(h)
        self.assertTrue(sealed["Cookie"].startswith("dpapi:")); self.assertNotIn("SECRET", sealed["Cookie"])
        self.assertEqual(unseal_headers(sealed), h)

    def test_write_json_is_atomic_and_corrupt_files_are_set_aside(self):
        p = os.path.join(self.tmp, "s.json")
        write_json(p, {"a": 1}); write_json(p, {"a": 2})
        self.assertEqual(read_json(p), {"a": 2}); self.assertFalse(os.path.exists(p + ".tmp"))
        with open(p, "w") as f: f.write("{broken")
        self.assertEqual(read_json(p, "fallback"), "fallback"); self.assertTrue(os.path.exists(p + ".bad"))

    def test_safe_folder(self):
        self.assertIsNone(safe_folder("relative/dir")); self.assertIsNone(safe_folder(""))
        self.assertIsNone(safe_folder(None)); self.assertIsNone(safe_folder("/etc/cron.d" if os.name == "posix" else "C:\\Windows\\System32"))
        ok = os.path.join(self.tmp, "dl")
        self.assertEqual(safe_folder(ok), os.path.normpath(ok))
        if os.name == "posix":
            self.assertIsNone(safe_folder(os.path.expanduser("~/.ssh")))
            self.assertIsNone(safe_folder(os.path.expanduser("~/.config/autostart/x")))

    def test_clean_file_names(self):
        self.assertEqual(clean("a/b\\c:d"), "a b c d")
        self.assertEqual(clean(".."), "download"); self.assertEqual(clean("CON.txt"), "_CON.txt")
        self.assertEqual(clean("x" * 500)[:150], "x" * 150)

    def test_version_compare(self):
        self.assertGreater(version_tuple("v2.10.0"), version_tuple("2.9.9"))
        self.assertEqual(version_tuple("v2.5.0"), (2, 5, 0))

    def test_shutdown_helpers_do_not_crash(self):
        with mock.patch("subprocess.Popen") as po:
            common.shutdown_pc(60)
            if not sys.platform.startswith("win"):
                self.assertIsNotNone(common._off_timer)
            common.cancel_shutdown()
        if sys.platform.startswith("win"):
            self.assertEqual(po.call_count, 2)
        else:
            self.assertIsNone(common._off_timer); po.assert_not_called()    # cancelled before it fired


# ------------------------------------------------------------------ local API
class ApiTests(Base):
    ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"

    def setUp(self):
        super().setUp()
        self.answers, self.gate = [True], None
        def ask(origin):
            if self.gate: self.gate.wait(5)
            return self.answers.pop(0) if self.answers else False
        self.pairing = Pairing(os.path.join(self.tmp, "pair.json"), ask)
        self.inbox = queue.Queue()
        class FakeApi:
            pub_folder, pub_ffmpeg, cmds = self.tmp, False, queue.Queue()
        self.srv = Server(("127.0.0.1", 0), make_handler(self.inbox, Api(FakeApi()), self.pairing))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown); self.addCleanup(self.srv.server_close)
        self.port = self.srv.server_address[1]

    def call(self, method, path, body=None, **headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        raw = json.dumps(body).encode() if body is not None else b""
        h = {"X-IK-Client": "1", "Content-Type": "application/json"}; h.update(headers)
        c.request(method, path, raw, h)
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, (json.loads(data) if data else {})

    def pair(self, origin=None):
        st, d = self.call("POST", "/pair", {}, Origin=origin or self.ORIGIN)
        return st, d.get("token")

    def test_ping_is_public_but_minimal(self):
        st, d = self.call("GET", "/ping"); self.assertEqual(st, 200); self.assertEqual(set(d), {"app", "version"})

    def test_wrong_host_header_is_rejected_dns_rebinding(self):
        self.assertEqual(self.call("GET", "/ping", Host="evil.example.com")[0], 403)
        self.assertEqual(self.call("POST", "/add", {"url": "http://x/y"}, Host="evil.example.com")[0], 403)

    def test_requests_without_token_are_refused(self):
        self.assertEqual(self.call("POST", "/add", {"url": "http://x/y"}, Origin=self.ORIGIN)[0], 401)
        self.assertEqual(self.call("POST", "/add", {"url": "http://x/y"})[0], 401)
        self.assertTrue(self.inbox.empty())

    def test_websites_and_missing_header_are_refused(self):
        self.assertEqual(self.call("POST", "/add", {}, Origin="https://evil.com")[0], 403)
        self.assertEqual(self.call("POST", "/pair", {}, Origin="https://evil.com")[0], 403)
        self.assertEqual(self.call("POST", "/pair", {})[0], 403)                      # no origin = not a browser extension
        self.assertEqual(self.call("POST", "/add", {}, **{"X-IK-Client": "0"})[0], 403)

    def test_pairing_flow_and_token_use(self):
        st, tok = self.pair(); self.assertEqual(st, 200); self.assertTrue(tok)
        st, d = self.call("POST", "/add", {"url": "http://x/y"}, Origin=self.ORIGIN, **{"X-IK-Token": tok})
        self.assertEqual((st, d["ok"]), (200, True)); self.assertEqual(self.inbox.get_nowait()["url"], "http://x/y")
        st, d = self.call("POST", "/info", {"extract": False}, Origin=self.ORIGIN, **{"X-IK-Token": tok})
        self.assertEqual(st, 200); self.assertTrue(d["ok"])
        with open(self.pairing.path) as f: self.assertNotIn(tok, f.read())           # only a hash is stored

    def test_user_can_deny_pairing(self):
        self.answers = [False]
        self.assertEqual(self.pair()[0], 403); self.assertEqual(self.pairing.count(), 0)

    def test_token_is_bound_to_the_extension_that_was_paired(self):
        _, tok = self.pair()
        other = "chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba"
        self.assertEqual(self.call("POST", "/add", {"url": "http://x/y"}, Origin=other, **{"X-IK-Token": tok})[0], 401)

    def test_revoke_all_logs_everyone_out(self):
        _, tok = self.pair(); self.pairing.revoke_all()
        self.assertEqual(self.call("POST", "/add", {"url": "http://x/y"}, Origin=self.ORIGIN, **{"X-IK-Token": tok})[0], 401)
        self.assertEqual(Pairing(self.pairing.path).count(), 0)

    def test_pairing_survives_an_app_restart(self):
        _, tok = self.pair()
        self.assertTrue(Pairing(self.pairing.path).verify(tok, self.ORIGIN))

    def test_second_pairing_while_one_is_open_gets_429(self):
        self.gate = threading.Event(); res = []
        t = threading.Thread(target=lambda: res.append(self.pair())); t.start(); time.sleep(0.3)
        self.assertEqual(self.pair()[0], 429)
        self.gate.set(); t.join(10)

    def test_oversized_and_malformed_bodies(self):
        _, tok = self.pair(); h = dict(Origin=self.ORIGIN, **{"X-IK-Token": tok})
        self.assertEqual(self.call("POST", "/add", {"u": "x" * 70000}, **h)[0], 413)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/add", b"not json", {"X-IK-Client": "1", "Origin": self.ORIGIN, "X-IK-Token": tok})
        r = c.getresponse(); r.read(); c.close()
        self.assertEqual(r.status, 400)

    def test_info_refuses_non_video_sites(self):
        _, tok = self.pair()
        st, d = self.call("POST", "/info", {"extract": True, "url": "http://127.0.0.1:1/secret"}, Origin=self.ORIGIN, **{"X-IK-Token": tok})
        self.assertFalse(d["ok"])
        st, d = self.call("POST", "/info", {"extract": True, "url": "file:///etc/passwd"}, Origin=self.ORIGIN, **{"X-IK-Token": tok})
        self.assertFalse(d["ok"])


if __name__ == "__main__":
    unittest.main()
