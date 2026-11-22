# Changelog

## 2.6.0 - mirrors, torrents, statistics, duplicates
### New
- **Mirror / multi-source downloads:** several URLs for one file; mirrors are validated (same size, ranges), each slice of every part is fetched from the currently fastest source, failing mirrors are dropped and the rest continue (also if the main link dies). Sources shown in the progress window. Add mirrors in *Download File Info*, in batch lines (`url mirror1 mirror2`) or via right-click > *Add mirror links*.
- **Torrents / magnet links:** detected everywhere links are accepted (Add URL, batch, clipboard watcher, browser link, `.torrent` files and `application/x-bittorrent` responses) and handed to the configured, bundled or installed client, or the system default - never fed to the HTTP engine. New *Options > Torrents*, *Tasks > Add torrent file...*, auto-open of finished `.torrent` downloads, repeat-send warning.
- **Statistics:** total downloaded, active time, average and peak speed (session and all time), by file type and by site; shown in the status bar too.
- **Download history with search** (`history.json`, newest 5000): completed, failed and torrent hand-offs; word search over name / link / folder / site / type / date / error; Download again, open, copy, remove, clear. Existing finished downloads are imported once.
- **Duplicate detection:** same link or same file name already in the list, or file already on disk -> Skip / Keep both (rename) / Re-download (replace), optionally remembered (*Options > General*). Batch adds ask once. *Download File Info* shows a duplicate warning.

### Details
- Re-download replaces the old file with `os.replace` only after the new one is complete; it never overwrites a partial file that belongs to another download.
- Magnet links are validated (single line, `magnet:?`, max 4 KB) and clients are started with an argument list, never through a shell.
- A download that finishes within a tick of quitting is still recorded in history and statistics.
- Tests: 92 (new: mirror selection / failover / resume, torrent hand-off, history search, statistics, duplicates, UI logic).

## 2.5.0 - hardening and reliability release
### Security
- Browser link now requires **pairing**: user-approved token, bound to the extension, only a hash stored; Options > Browser > *Disconnect all browsers*.
- `Host` header check (DNS rebinding), exclusive port binding on Windows, request limits and timeouts, at most 4 slow calls at once.
- Login cookies are **encrypted at rest** (Windows DPAPI) or not stored; old plaintext cookies are dropped on load.
- Cookies/Authorization never sent to other hosts - including HLS segments (previously leaked).
- Only http(s): `file://` / `ftp://` blocked in redirects and playlists; public-to-private-network hops blocked.
- Browser-supplied save folders can no longer point at Startup/system folders; `/info` only extracts known video sites.
- Extension panel protected by a per-tab nonce (web pages can no longer drive it by framing it); `panel.js` no longer web-accessible; sender checks in the background page.

### Reliability
- Download list and settings saved **every 15 s and atomically**; corrupt files are set aside instead of crashing.
- Range responses validated (`206` + `Content-Range`), writes capped to the segment, flush on each chunk, size verified before finishing, free disk space checked up front, retry loop can no longer spin forever on empty replies, non-range servers restart cleanly after a drop (fixed a corruption bug).
- File names reserved atomically (no two downloads share a name); Windows device names (CON, NUL...) are renamed.
- HLS: fMP4 (`EXT-X-MAP`) supported natively; AES-128, live and byte-range streams fall back to yt-dlp; playlist size capped.
- Extension: stream list updates serialised, errors shown on the toolbar icon, background keep-alive during dialogs, lighter polling.

### Maintenance
- Code split into the `ikd` package (common, netsec, engine, api, ui); 45 tests (real local servers, extension logic via Node, UI logic via stub Tk); logging to `ik.log`.
- One version source (`tools/sync_version.py`), `REPO` filled in by CI, update check compares versions, rolling build is a pre-release.
- Shut down when finished works on Windows, macOS and Linux.
- CI: lint + tests on Windows/Linux, Deno pinned + SHA-256 verified, built exe self-tested, optional code signing, `SHA256SUMS.txt`.
