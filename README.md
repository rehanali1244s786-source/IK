# IK DOWNLOADER

Fast multi-connection download manager with a browser video bar. Windows is the main target (installer, autostart); the app itself also runs from source on macOS and Linux.

## Features
- **IDM-style main window:** menu bar (Tasks / File / Downloads / View / Help), toolbar (Add URL, Resume, Stop, Stop All, Delete, Delete Completed, Options, Scheduler, Start Queue, Stop Queue), category tree (All Downloads, Compressed, Documents, Music, Programs, Video, Unfinished, Finished, Queues), Options dialog, About, startup splash screen
- Up to 32 connections per file, pause / resume, resume after restart (the queue is saved every 15 seconds and on exit, so even a crash loses nothing)
- Queue, simultaneous-download limit, speed limiter, ETA
- Scheduler (start queue at HH:MM), proxy support, dark mode, shut down / power off when finished (Windows, macOS, Linux)
- **Video sites: YouTube, TikTok, Instagram, Facebook, X/Twitter, Vimeo, Reddit and 1000+ more** (via yt-dlp), with quality choice (up to best / 1080p / 720p ... / MP3 audio)
- HLS (.m3u8) streams: plain and fMP4 natively; encrypted (AES-128), live and byte-range streams are handed to yt-dlp automatically
- Auto-sorting into Video / Music / Compressed / Documents / Programs
- Batch add, clipboard watcher, edit expired links, checksum verify, shut down when done
- **IDM-style picker in the browser:** click the bar on a video -> choose resolution (with sizes) -> dialog with file name, save folder (Browse button), **Start Video** (video + audio), **Start Audio** (MP3 only) or Cancel
- **IDM-style progress window** for every download: green progress bar, per-connection strip, speed, time left, resume info, **per-download speed limiter** (change it live), Pause / Resume / Cancel, Open / Open folder when finished
- **Catches every browser download** (zip, exe, iso, pdf, ... any file): the app detects the file first and shows a **Download File Info** box (file name, type, size, resume support, save folder) with **Start Download / Download Later / Cancel**
- The video bar appears only on playing videos; normal files get the Download File Info box instead
- **Mirrors / multi-source downloads:** give the same file as several URLs (a box in *Download File Info*, a second link on a batch line, or right-click > *Add mirror links*). Every mirror is checked first (same size, resume supported); then each part of the file is fetched from the **fastest source at that moment** (speeds are measured continuously, slow sources are re-sampled now and then). A dead or failing mirror is dropped and the others take over; if the main link dies the mirrors finish the file. The progress window lists every source with its speed and share. Logins/cookies are only sent to the site they belong to. Needs a main link that supports resume.
- **Torrents and magnet links:** IK DOWNLOADER does not download torrents itself; it detects them and hands them over instead of failing. `magnet:` links (pasted, from the clipboard watcher, a batch or the browser link) and `.torrent` files (downloaded normally, or *Tasks > Add torrent file...*) go to the client set in *Options > Torrents*, else a bundled one (`torrent` folder or `aria2c` next to the program), else an installed qBittorrent / Transmission / Deluge / uTorrent ..., else the system's default program. Finished `.torrent` downloads open in the client automatically (switchable). If nothing is available you get a message and the magnet link is copied to the clipboard.
- **Statistics:** *Tasks > Statistics* shows total downloaded, time spent downloading, **average and peak speed** for this session and for all time (average = data / time something was actually downloading, so idle time does not distort it), plus totals by file type and the biggest sources.
- **History search:** *Tasks > Download history* keeps every finished / failed download and torrent hand-off (last 5000). Search by any words of the name, link, folder, site, file type, date or error (`pdf 2026-11`, `youtube failed` ...), filter Completed / Failed / Torrents, then *Download again*, open the file or folder, copy the link or remove entries. Downloads that were already finished before this feature are imported on the first start.
- **Duplicate detection:** before a download is added the link and the file name are compared with the list (in any state) and with the destination folder on disk. You get a box to **Skip**, **Keep both (rename)** or **Re-download (replace)**; *Options > General > When a duplicate is found* can make one of these automatic. A batch shows one question for all duplicates. Re-download replaces the old file atomically (only when the new one is complete).
- Browser extension: "Download" bar on videos, right-click menu, capture of all downloads (on by default, switch off in the popup; if the app is not running the browser downloads normally)

DRM-protected streams (Netflix, Disney+ ...) are not supported. Only download content you have the right to, and follow each site's terms. Private / login-only videos (e.g. some Instagram posts) need the "Use login cookies from" option (Firefox works best).

## Install (Windows)
1. Download **`IK-DOWNLOADER-Setup.exe`** from **Releases** and run it. It installs the app and copies the browser extension to the install folder. Check the download against `SHA256SUMS.txt` on the release page if you like.
2. Open `chrome://extensions`, enable Developer mode, **Load unpacked**, choose `<install folder>\browser-extension` (Help > Install the browser extension opens it).
3. Start IK DOWNLOADER, then send your first download from the browser. **The app asks once whether to trust the extension - click Yes.** (Or use the extension's popup > Connect.)
4. Keep the app open while browsing.

Portable alternative: download `IK-DOWNLOADER.exe` (no installer) and `IK-DOWNLOADER-browser-extension.zip`. The weekly **Latest build** (a pre-release) is rebuilt with the newest video engine so YouTube keeps working.

> **Windows SmartScreen / antivirus:** builds are only code-signed if the maintainer configured a certificate (see SECURITY.md). Unsigned PyInstaller programs often trigger a "Windows protected your PC" warning; compare the SHA-256 with `SHA256SUMS.txt` before running "More info > Run anyway".

## Security and privacy (short version)
- The browser link listens on `127.0.0.1` only, answers only requests with the right `Host` header and a secret token, and the token is only handed out after you click **Yes** in the app. Options > Browser > *Disconnect all browsers* revokes everything.
- Only `http` and `https` links are ever fetched. Redirects or playlist entries that point to `file://`, `ftp://` or from the internet into your private network are blocked. Login cookies are only sent to the site they belong to.
- Login cookies kept for resuming are encrypted with your Windows account (DPAPI); on other systems they are not stored at all.
- The browser cannot make the app save into system or auto-start folders.
- Data lives in `~/.ik_downloader` (Help > Open data and log folder): download list, settings, `history.json`, `stats.json`, `pairing.json` (hashes only), `ik.log`.

Details and how to report a problem: [SECURITY.md](SECURITY.md).

## Build from source
```
python ik_downloader.py            # Python 3.9+; `pip install -r requirements.txt` for video sites
build_exe.bat                      # dist\IK-DOWNLOADER.exe  (also self-tests the result)
build_installer.bat                # + dist\IK-DOWNLOADER-Setup.exe (needs Inno Setup 6)
python tools/make_assets.py        # regenerates logo / icon / splash / installer pictures (needs pillow)
python tools/sync_version.py       # after changing VERSION in ikd/common.py
```
Code layout: `ikd/common.py` (constants, helpers, secrets), `ikd/netsec.py` (network safety), `ikd/engine.py` (downloads, mirrors, HLS, yt-dlp), `ikd/torrent.py` (magnet / .torrent hand-off), `ikd/dupes.py` (duplicate detection), `ikd/history.py` (history search + statistics), `ikd/api.py` (local browser link + pairing), `ikd/ui.py` (Tkinter), `extension/` (Chrome MV3).

## Tests
```
pip install -r requirements-dev.txt
python -m pyflakes ikd tests tools ik_downloader.py
python -m unittest discover -s tests -t . -v
```
The suite starts real local web servers and checks byte-exact multi-connection downloads, resume, dropped connections, HLS/fMP4, blocked links, cookie scoping, the pairing API, the extension's logic (needs Node.js) and the app's queue/save/restore logic against a stubbed Tkinter, mirror speed selection and failover, magnet/torrent hand-off (client launch is mocked), history search, statistics and duplicate handling. GitHub Actions runs it on Windows and Linux for Python 3.9 and 3.12 before every build.

## Publish a release (automatic .exe)
Set your GitHub repo's secrets only if you want signing (`WINDOWS_CERT_PFX_BASE64`, `WINDOWS_CERT_PASSWORD`). Then:
```
python tools/sync_version.py
git tag v2.6.0
git push origin v2.5.0
```
GitHub Actions tests, builds, self-tests and signs (if configured) the .exe and installer, writes `SHA256SUMS.txt` and attaches everything to a new Release. The workflow fills in `REPO` automatically, so Help > Check for updates works without editing any file.
