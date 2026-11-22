import json, os, queue, shutil, subprocess, tempfile, threading, unittest

from ikd.api import Api, Pairing, Server, make_handler


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class ExtensionTest(unittest.TestCase):
    def test_extension_logic_against_real_api(self):
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        asked = []
        pairing = Pairing(os.path.join(tmp, "p.json"), lambda origin: asked.append(origin) or True)
        inbox = queue.Queue()
        class FakeApp:
            pub_folder, pub_ffmpeg, cmds = tmp, False, queue.Queue()
        srv = Server(("127.0.0.1", 0), make_handler(inbox, Api(FakeApp()), pairing))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown); self.addCleanup(srv.server_close)
        harness = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext_harness.js")
        r = subprocess.run(["node", harness, str(srv.server_address[1])], capture_output=True, text=True, timeout=60)
        out = r.stdout.strip().splitlines()
        results = json.loads(out[-1]) if out else []
        failed = [x for x in results if not x["ok"]]
        self.assertEqual(r.returncode, 0, f"failed: {failed}\n{r.stderr}")
        self.assertGreaterEqual(len(results), 15)
        self.assertEqual(len(asked), 2)                                   # first use + after the token was revoked
        self.assertTrue(all(o.startswith("chrome-extension://") for o in asked))
        item = inbox.get_nowait()
        self.assertEqual(item["url"], "https://x/v.mp4"); self.assertEqual(item["cookie"], "sid=abc")


if __name__ == "__main__":
    unittest.main()
