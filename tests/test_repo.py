"""Repository-level checks: versions, manifest, Python 3.9 syntax, asset lookup, --selftest."""
import ast, glob, json, os, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RepoTests(unittest.TestCase):
    def test_versions_are_in_sync(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "sync_version.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_extension_manifest_is_hardened(self):
        with open(os.path.join(ROOT, "extension", "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m["manifest_version"], 3)
        res = [x for w in m["web_accessible_resources"] for x in w["resources"]]
        self.assertEqual(res, ["panel.html"])                      # scripts are not exposed to web pages

    def test_sources_parse_as_python_3_9(self):
        for p in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
            with open(p, encoding="utf-8") as f:
                ast.parse(f.read(), filename=p, feature_version=(3, 9))

    def test_assets_are_found_from_source_tree(self):
        from ikd.common import asset
        for n in ("logo.png", "icon.ico", "splash.png"):
            self.assertTrue(asset(n), n)

    def test_selftest_runs_without_a_window(self):
        out = os.path.join(tempfile.mkdtemp(), "r.json")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "ik_downloader.py"), "--selftest", out],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out, encoding="utf-8") as f:
            res = json.load(f)
        self.assertTrue(res["assets"]); self.assertIn("version", res)


if __name__ == "__main__":
    unittest.main()
