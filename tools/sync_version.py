"""Keeps every version number in step with VERSION in ikd/common.py.

  python tools/sync_version.py          update installer, extension manifest and README
  python tools/sync_version.py --check  only report (exit 1 if something is out of date) - used by CI
  python tools/sync_version.py --print  print the version
"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION = re.search(r'APP, VERSION, PORT = "[^"]+", "([^"]+)"', (ROOT / "ikd" / "common.py").read_text(encoding="utf-8"))[1]

TARGETS = [  # (file, regex with 2 groups around the version, what it is)
    ("installer/ik_downloader.iss", r'(#define AppVersion ")[^"]*(")', "installer"),
    ("extension/manifest.json", r'("version":\s*")[^"]*(")', "extension manifest"),
    ("README.md", r"(git tag v)[\d.]+()", "README release example"),
]


def main(argv) -> int:
    if "--print" in argv:
        print(VERSION)
        return 0
    check, bad = "--check" in argv, 0
    for rel, rx, what in TARGETS:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(rx, lambda m: m[1] + VERSION + m[2], text, count=1)
        if n != 1:
            print(f"!! {what}: pattern not found in {rel}")
            bad += 1
        elif new != text:
            if check:
                print(f"!! {what} ({rel}) is not {VERSION}")
                bad += 1
            else:
                path.write_text(new, encoding="utf-8")
                print(f"updated {what} -> {VERSION}")
    print(f"version {VERSION}: " + ("OUT OF SYNC" if bad else "all files agree"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
