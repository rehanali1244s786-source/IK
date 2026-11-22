import os, subprocess, sys, unittest


class UiSmokeTest(unittest.TestCase):
    def test_app_logic_with_stub_tkinter(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_smoke_script.py")
        r = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("UI SMOKE OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
