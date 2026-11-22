"""Tests for torrent/magnet hand-off, mirrors (multi-source), statistics + history search and duplicate detection."""
import os, tempfile, time, unittest
from unittest import mock

from ikd import dupes, torrent
from ikd.engine import Counter, Job, Sources
from ikd.history import History, Stats
from tests.test_core import DATA, SHA, Base, run_job, sha

Job.RETRY_DELAY = 0.01


def put(path, data=b""):
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def get(path):
    with open(path, "rb") as f:
        return f.read()


BTIH = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"


# ------------------------------------------------------------------ torrents / magnets
class TorrentTests(unittest.TestCase):
    def test_detects_magnet_and_torrent_links(self):
        self.assertTrue(torrent.is_magnet("magnet:?xt=urn:btih:" + BTIH))
        self.assertTrue(torrent.is_magnet("  MAGNET:?xt=urn:btih:" + BTIH))
        self.assertFalse(torrent.is_magnet("https://example.com/magnet:?x"))
        self.assertTrue(torrent.is_torrent_url("https://x.org/a/b.TORRENT?token=1"))
        self.assertTrue(torrent.is_torrent_url("https://x.org/get?id=3", "application/x-bittorrent; charset=x"))
        self.assertFalse(torrent.is_torrent_url("https://x.org/a.zip"))

    def test_parse_magnet_hex_and_base32(self):
        p = torrent.parse_magnet(f"magnet:?xt=urn:btih:{BTIH.upper()}&dn=Ubuntu%2024.04&tr=udp%3A%2F%2Ft.org%3A1&xl=123")
        self.assertEqual((p["btih"], p["name"], p["size"]), (BTIH, "Ubuntu 24.04", 123))
        self.assertEqual(p["trackers"], ["udp://t.org:1"])
        import base64
        b32 = base64.b32encode(bytes.fromhex(BTIH)).decode()
        self.assertEqual(torrent.parse_magnet("magnet:?xt=urn:btih:" + b32)["btih"], BTIH)
        self.assertEqual(torrent.parse_magnet("magnet:?dn=x")["btih"], "")
        self.assertEqual(torrent.display_name("magnet:?xt=urn:btih:" + BTIH), "magnet " + BTIH[:12])

    def test_malformed_magnets_are_refused_before_reaching_another_program(self):
        self.assertFalse(torrent.valid_magnet("magnet:?xt=urn:btih:abc\n--evil"))
        self.assertFalse(torrent.valid_magnet("magnet:?xt=urn:btih:abc --evil"))
        self.assertFalse(torrent.valid_magnet("magnet:?" + "a" * 5000))
        with self.assertRaises(torrent.HandoffError):
            torrent.send("magnet:?xt=urn:btih:abc --evil")
        with self.assertRaises(torrent.HandoffError):
            torrent.send("/etc/passwd")

    def test_hands_magnet_to_configured_client_as_an_argument_list(self):
        tmp = tempfile.mkdtemp()
        exe = os.path.join(tmp, "qbittorrent")
        put(exe)
        uri = "magnet:?xt=urn:btih:" + BTIH
        with mock.patch("ikd.torrent.subprocess.Popen") as po:
            name = torrent.send(uri, configured=exe)
        self.assertEqual(name, "qbittorrent")
        self.assertEqual(po.call_args[0][0], [exe, uri])                 # list, never a shell string
        self.assertNotIn("shell", po.call_args[1])

    def test_aria2_gets_download_folder_and_torrent_file(self):
        tmp = tempfile.mkdtemp()
        exe, tf = os.path.join(tmp, "aria2c"), os.path.join(tmp, "a.torrent")
        put(exe); put(tf)
        with mock.patch("ikd.torrent.subprocess.Popen") as po:
            torrent.send(tf, configured=exe, save_dir=tmp)
        cmd = po.call_args[0][0]
        self.assertEqual(cmd[0], exe)
        self.assertIn("--dir", cmd)
        self.assertEqual(cmd[-1], tf)

    def test_falls_back_to_system_handler_and_reports_when_nothing_works(self):
        uri = "magnet:?xt=urn:btih:" + BTIH
        with mock.patch("ikd.torrent.find_client", return_value=None), \
                mock.patch("ikd.torrent._os_default") as od:
            self.assertEqual(torrent.send(uri), "default torrent program")
            od.assert_called_once_with(uri)
        with mock.patch("ikd.torrent.find_client", return_value=None), \
                mock.patch("ikd.torrent.subprocess.Popen", side_effect=OSError("none")), \
                mock.patch("ikd.torrent.sys.platform", "linux"):
            with self.assertRaises(torrent.HandoffError):
                torrent.send(uri)

    def test_broken_client_falls_back_instead_of_crashing(self):
        tmp = tempfile.mkdtemp()
        exe = os.path.join(tmp, "deluge")
        put(exe)
        calls = []

        def popen(cmd, **k):
            calls.append(cmd)
            if cmd[0] == exe:
                raise OSError("cannot execute")
        with mock.patch("ikd.torrent.subprocess.Popen", side_effect=popen), mock.patch("ikd.torrent.sys.platform", "linux"):
            self.assertEqual(torrent.send("magnet:?xt=urn:btih:" + BTIH, configured=exe), "default torrent program")
        self.assertEqual(calls[-1][0], "xdg-open")

    def test_torrent_files_are_regular_downloads(self):
        from ikd.common import guess_mode
        self.assertEqual(guess_mode("https://x.org/file.torrent"), "direct")


class TorrentDownloadTests(Base):
    def test_torrent_file_downloads_and_is_recognised_by_content_type(self):
        w = self.web(); w.files["/get"] = b"d8:announce3:abce"; w.ctypes["/get"] = "application/x-bittorrent"
        j = Job(w.base + "/get", self.tmp, 2, {})
        j.exact = True
        j = run_job(j)
        self.assertEqual(j.state, "done", j.msg)
        self.assertTrue(j.name.endswith(".torrent"), j.name)


# ------------------------------------------------------------------ mirrors / multi-source
class MirrorTests(Base):
    def multi(self, urls, conns=4):
        j = self.job(urls[0], conns=conns)
        j.set_mirrors(urls[1:])
        return j

    def test_same_file_from_two_sources_is_byte_exact_and_uses_both(self):
        a, b = self.web(), self.web()
        a.files["/f.bin"] = b.files["/f.bin"] = DATA
        j = run_job(self.multi([a.base + "/f.bin", b.base + "/f.bin"]))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)
        self.assertIsNotNone(j.srcs)
        used = [r for r in j.source_rows() if r["bytes"] > 0]
        self.assertEqual(len(used), 2, j.source_rows())                  # both were measured and used
        self.assertEqual(sum(r["bytes"] for r in j.source_rows()), len(DATA))

    def test_the_fastest_source_serves_most_of_the_file(self):
        slow, fast = self.web(slow=0.02), self.web()
        slow.files["/f.bin"] = fast.files["/f.bin"] = DATA
        big = DATA * 3                                                   # ~10 MB so that several slices are needed
        slow.files["/f.bin"] = fast.files["/f.bin"] = big
        j = run_job(self.multi([slow.base + "/f.bin", fast.base + "/f.bin"], conns=2), timeout=60)
        self.assertEqual(j.state, "done", j.msg)
        got = {r["host"] + str(i): r["bytes"] for i, r in enumerate(j.source_rows())}
        rows = j.source_rows()
        self.assertGreater(rows[1]["bytes"], rows[0]["bytes"] * 2, got)  # fast mirror did the bulk of the work
        self.assertEqual(sha(j.path), __import__("hashlib").sha256(big).hexdigest())

    def test_dead_mirror_is_dropped_and_download_still_succeeds(self):
        a, b = self.web(), self.web()
        a.files["/f.bin"] = DATA                                         # b has no such file: 404
        j = run_job(self.multi([a.base + "/f.bin", b.base + "/f.bin"]))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)
        self.assertIsNone(j.srcs)                                        # refused at the start: plain download
        self.assertEqual(len(j.dropped), 1)

    def test_mirror_with_other_size_is_refused(self):
        a, b = self.web(), self.web()
        a.files["/f.bin"], b.files["/f.bin"] = DATA, DATA[:-5]
        j = run_job(self.multi([a.base + "/f.bin", b.base + "/f.bin"]))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)
        self.assertIn("different size", j.dropped[0][1])

    def test_mirror_without_range_support_is_refused(self):
        a, b = self.web(), self.web(ranged=False)
        a.files["/f.bin"] = b.files["/f.bin"] = DATA
        j = run_job(self.multi([a.base + "/f.bin", b.base + "/f.bin"]))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)
        self.assertEqual(len(j.dropped), 1)

    def test_main_link_dying_mid_download_fails_over_to_the_mirror(self):
        a, b = self.web(), self.web()
        a.files["/f.bin"] = b.files["/f.bin"] = DATA
        j = self.multi([a.base + "/f.bin", b.base + "/f.bin"], conns=2)
        j.start()
        t0 = time.time()
        while time.time() - t0 < 10 and not (j.srcs and sum(r["bytes"] for r in j.source_rows()) > 0):
            time.sleep(0.01)
        a.files.pop("/f.bin", None)                                      # the main server now answers 404
        while j.state == "downloading" and time.time() - t0 < 30:
            time.sleep(0.02)
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)

    def test_all_sources_dead_gives_an_error_not_a_hang(self):
        a, b = self.web(), self.web()
        a.files["/f.bin"] = b.files["/f.bin"] = DATA
        j = self.multi([a.base + "/f.bin", b.base + "/f.bin"], conns=2)
        j.start()
        t0 = time.time()
        while time.time() - t0 < 10 and not (j.srcs and sum(r["bytes"] for r in j.source_rows()) > 0):
            time.sleep(0.01)
        a.files.clear(); b.files.clear()
        while j.state == "downloading" and time.time() - t0 < 30:
            time.sleep(0.02)
        self.assertIn(j.state, ("error", "done"))

    def test_mirrors_are_saved_and_restored(self):
        j = self.job("http://a.example/f.bin")
        j.set_mirrors(["http://b.example/f.bin", "http://b.example/f.bin", "ftp://bad/x", "http://a.example/f.bin"])
        self.assertEqual(j.mirrors, ["http://b.example/f.bin"])
        d = j.to_dict()
        j2 = Job(d["url"], d["folder"], 4, d["headers"], "", d)
        self.assertEqual(j2.mirrors, ["http://b.example/f.bin"])
        d["mirrors"] = "garbage"
        self.assertEqual(Job(d["url"], d["folder"], 4, {}, "", d).mirrors, [])

    def test_cookies_are_not_sent_to_a_mirror_on_another_site(self):
        a, b = self.web(), self.web()
        a.files["/f.bin"] = b.files["/f.bin"] = DATA
        # same loopback host name for both, so exercise the rule directly
        j = self.job("http://one.example/f.bin", headers={"Cookie": "sid=SECRET"})
        self.assertIn("Cookie", j.hdr(url="http://one.example/f.bin"))
        self.assertNotIn("Cookie", j.hdr(url="http://other.test/f.bin"))

    def test_mirrors_ignored_when_main_link_cannot_resume(self):
        a, b = self.web(ranged=False), self.web()
        a.files["/f.bin"] = b.files["/f.bin"] = DATA
        j = run_job(self.multi([a.base + "/f.bin", b.base + "/f.bin"]))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(sha(j.path), SHA)
        self.assertEqual(len(j.dropped), 1)

    def test_resume_after_restart_keeps_using_mirrors(self):
        a, b = self.web(slow=0.002), self.web(slow=0.002)
        a.files["/f.bin"] = b.files["/f.bin"] = DATA
        j = self.multi([a.base + "/f.bin", b.base + "/f.bin"], conns=2)
        j.start()
        t0 = time.time()
        while time.time() - t0 < 10 and j.done_bytes < len(DATA) // 4:
            time.sleep(0.01)
        j.pause()
        time.sleep(0.3)
        d = j.to_dict()
        j2 = Job(d["url"], d["folder"], 2, {}, "", d)
        self.assertEqual(len(j2.mirrors), 1)
        j2 = run_job(j2)
        self.assertEqual(j2.state, "done", j2.msg)
        self.assertEqual(sha(j2.path), SHA)

    def test_sources_scheduler_unit(self):
        s = Sources(["a", "b", "c"])
        first = {s.pick(i)[0] for i in range(3)}
        self.assertEqual(first, {"a", "b", "c"})                         # unmeasured sources get spread over segments
        s.report("a", 1000, 1.0); s.report("b", 5000, 1.0); s.report("c", 3000, 1.0)
        self.assertEqual(s.pick(0), ("b", False))                        # best speed wins
        s.fail("b", hard=False)
        self.assertEqual(s.pick(0)[0], "c")                              # cooling source is skipped
        s.fail("c", hard=True)
        self.assertEqual(s.pick(0)[0], "a")
        self.assertEqual(s.alive(), 1)
        s.fail("a", hard=True)
        self.assertEqual(s.alive(), 0)
        self.assertIn(s.pick(0)[0], ("a", "b", "c"))                     # never crashes with everything failed


# ------------------------------------------------------------------ statistics
class StatsTests(Base):
    def test_global_counter_counts_every_received_byte(self):
        w = self.web(); w.files["/f.bin"] = DATA
        before = Counter.total
        j = run_job(self.job(w.base + "/f.bin"))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(Counter.total - before, len(DATA))

    def test_job_tracks_active_time_across_runs(self):
        w = self.web(slow=0.002); w.files["/f.bin"] = DATA
        j = run_job(self.job(w.base + "/f.bin"))
        self.assertGreater(j.active, 0.0)
        self.assertEqual(Job(j.url, j.folder, 1, {}, "", j.to_dict()).active, j.active)

    def test_stats_average_speed_ignores_idle_time(self):
        st = Stats(os.path.join(tempfile.mkdtemp(), "stats.json"))
        for _ in range(10):
            st.tick(1.0, 2_000_000, True)           # 10 s at 2 MB/s
        for _ in range(100):
            st.tick(1.0, 0, False)                  # a long idle spell
        s = st.summary()
        self.assertEqual(s["session"]["bytes"], 20_000_000)
        self.assertAlmostEqual(s["session"]["avg"], 2_000_000, delta=1)
        self.assertGreater(s["session"]["peak"], 1_000_000)

    def test_lifetime_stats_persist_but_session_resets(self):
        path = os.path.join(tempfile.mkdtemp(), "stats.json")
        st = Stats(path)
        st.tick(1.0, 5000, True); st.file_done(); st.save()
        st2 = Stats(path)
        s = st2.summary()
        self.assertEqual(s["life"]["bytes"], 5000)
        self.assertEqual(s["life"]["files"], 1)
        self.assertEqual(s["session"]["bytes"], 0)

    def test_damaged_stats_file_is_set_aside(self):
        path = os.path.join(tempfile.mkdtemp(), "stats.json")
        put(path, "{not json")
        self.assertEqual(Stats(path).summary()["life"]["bytes"], 0)
        self.assertTrue(os.path.exists(path + ".bad"))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "history.json")
        self.h = History(self.path)
        self.h.add("Ubuntu 24.04.iso", "https://releases.ubuntu.com/u.iso", size=5_000_000, secs=10, path="/dl/Programs/u.iso")
        self.h.add("holiday.mp4", "https://cdn.example.org/v/holiday.mp4", size=900_000, secs=3, path="/dl/Video/holiday.mp4")
        self.h.add("broken.zip", "https://bad.example.net/broken.zip", status="error", note="HTTP 404")
        self.h.add("Debian", "magnet:?xt=urn:btih:" + BTIH, status="torrent", btih=BTIH)

    def test_search_matches_every_word_in_name_url_folder_or_note(self):
        self.assertEqual([r["name"] for r in self.h.search("ubuntu")], ["Ubuntu 24.04.iso"])
        self.assertEqual([r["name"] for r in self.h.search("cdn holiday")], ["holiday.mp4"])
        self.assertEqual([r["name"] for r in self.h.search("/dl/video")], ["holiday.mp4"])
        self.assertEqual([r["name"] for r in self.h.search("404")], ["broken.zip"])
        self.assertEqual(self.h.search("ubuntu holiday"), [])             # all words must match
        self.assertEqual(len(self.h.search("")), 4)

    def test_search_is_newest_first_and_filters_by_status(self):
        self.assertEqual(self.h.search("")[0]["name"], "Debian")
        self.assertEqual([r["name"] for r in self.h.search("", "done")], ["holiday.mp4", "Ubuntu 24.04.iso"])
        self.assertEqual([r["name"] for r in self.h.search("", "error")], ["broken.zip"])
        self.assertEqual(len(self.h.search("", limit=2)), 2)

    def test_search_by_date(self):
        today = time.strftime("%Y-%m-%d")
        self.assertEqual(len(self.h.search(today)), 4)
        self.assertEqual(self.h.search("1999-01-01"), [])

    def test_average_speed_is_stored_per_record(self):
        self.assertEqual(self.h.search("ubuntu")[0]["avg"], 500_000)

    def test_persists_remove_and_clear(self):
        self.h.save()
        h2 = History(self.path)
        self.assertEqual(len(h2.rows), 4)
        h2.remove(h2.search("broken"))
        h2.save()
        self.assertEqual(len(History(self.path).rows), 3)
        h2.clear(); h2.save()
        self.assertEqual(History(self.path).rows, [])

    def test_lookups_for_duplicates_and_stats(self):
        self.assertEqual(self.h.last_for_url("https://releases.ubuntu.com/u.iso/")["name"], "Ubuntu 24.04.iso")
        self.assertIsNone(self.h.last_for_url("https://bad.example.net/broken.zip"))      # failed ones do not count
        self.assertEqual(self.h.last_for_btih(BTIH)["name"], "Debian")
        pc = self.h.per_category()
        self.assertEqual(pc["Programs"], {"files": 1, "bytes": 5_000_000})
        self.assertEqual(self.h.top_hosts(1)[0][0], "releases.ubuntu.com")

    def test_history_is_capped(self):
        from ikd import history
        with mock.patch.object(history, "MAX_HISTORY", 3):
            self.h.add("x", "https://x.org/x")
        self.assertEqual(len(self.h.rows), 3)

    def test_damaged_history_is_set_aside_and_new_file_is_flagged_fresh(self):
        p = os.path.join(tempfile.mkdtemp(), "h.json")
        self.assertTrue(History(p).fresh)
        put(p, "[1, 2")
        self.assertEqual(History(p).rows, [])
        self.assertTrue(os.path.exists(p + ".bad"))


# ------------------------------------------------------------------ duplicates
class FakeJob:
    def __init__(self, url, folder, name="", state="done", path="", exact=True, fname=""):
        self.url, self.folder, self.name, self.state = url, folder, name, state
        self.path, self.exact, self.fname = path, exact, fname


class DupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_urls_are_compared_like_a_person_would(self):
        n = dupes.norm_url
        self.assertEqual(n("HTTP://Example.COM:80/a/b/#frag"), n("http://example.com/a/b"))
        self.assertEqual(n("https://e.com:443/x?y=1"), n("https://e.com/x?y=1"))
        self.assertNotEqual(n("https://e.com/x?y=1"), n("https://e.com/x?y=2"))
        self.assertNotEqual(n("https://e.com/a"), n("https://e.com/A"))      # paths are case-sensitive
        self.assertEqual(n("http://bad:port/x"), "http://bad:port/x")        # malformed: compared as written

    def test_same_url_in_list_is_found_in_any_state(self):
        for st in ("done", "downloading", "queued", "paused", "error"):
            jobs = [FakeJob("https://e.com/a.zip", self.tmp, "a.zip", st)]
            d = dupes.find_dupes("https://E.com/a.zip#x", "a.zip", self.tmp, True, jobs)
            self.assertEqual([x.kind for x in d], ["url"], st)

    def test_same_name_in_same_destination_is_found_but_other_folder_is_not(self):
        other = os.path.join(self.tmp, "other")
        jobs = [FakeJob("https://e.com/1", self.tmp, "a.zip", "queued", fname="a.zip")]
        d = dupes.find_dupes("https://f.com/2", "A.ZIP" if os.name == "nt" else "a.zip", self.tmp, True, jobs)
        self.assertEqual([x.kind for x in d], ["name"])
        self.assertEqual(dupes.find_dupes("https://f.com/2", "a.zip", other, True, jobs), [])

    def test_sorted_sub_folder_destination_is_used_when_not_exact(self):
        jobs = [FakeJob("https://e.com/1", self.tmp, "a.zip", "queued", exact=False, fname="a.zip")]
        d = dupes.find_dupes("https://f.com/2", "a.zip", self.tmp, False, jobs)
        self.assertEqual([x.kind for x in d], ["name"])

    def test_existing_file_on_disk_is_found_once(self):
        p = os.path.join(self.tmp, "a.zip")
        put(p, b"x" * 10)
        d = dupes.find_dupes("https://f.com/2", "a.zip", self.tmp, True, [])
        self.assertEqual([x.kind for x in d], ["disk"])
        self.assertIn("a.zip", d[0].text)
        jobs = [FakeJob("https://f.com/2", self.tmp, "a.zip", "done", path=p)]
        d = dupes.find_dupes("https://f.com/2", "a.zip", self.tmp, True, jobs)
        self.assertEqual([x.kind for x in d], ["url"])                       # list entry already explains the file

    def test_unknown_name_compares_only_the_link(self):
        put(os.path.join(self.tmp, "x.mp4"))
        self.assertEqual(dupes.find_dupes("https://youtu.be/abc", "", self.tmp, True, []), [])

    def test_nothing_found_for_a_new_download_and_the_edited_entry_is_ignored(self):
        j = FakeJob("https://e.com/a.zip", self.tmp, "a.zip", "paused")
        self.assertEqual(dupes.find_dupes("https://e.com/new.zip", "new.zip", self.tmp, True, [j]), [])
        self.assertEqual(dupes.find_dupes("https://e.com/a.zip", "a.zip", self.tmp, True, [j], ignore=j), [])

    def test_unique_name_skips_disk_list_and_partial_files(self):
        put(os.path.join(self.tmp, "a.zip"))
        put(os.path.join(self.tmp, "a (1).zip.part"))
        jobs = [FakeJob("u", self.tmp, "a (2).zip", "queued", fname="a (2).zip")]
        self.assertEqual(dupes.unique_name("a.zip", self.tmp, True, jobs), "a (3).zip")
        self.assertEqual(dupes.unique_name("fresh.zip", self.tmp, True, jobs), "fresh.zip")

    def test_policy_overrides_the_question_and_no_answer_means_skip(self):
        self.assertEqual(dupes.choose("rename", "skip"), "rename")
        self.assertEqual(dupes.choose("ask", "replace"), "replace")
        self.assertEqual(dupes.choose("ask", None), "skip")
        self.assertEqual(dupes.choose("garbage", "bogus"), "skip")

    def test_summary_is_short(self):
        ds = [dupes.Dup("url", f"line {i}") for i in range(9)]
        self.assertEqual(dupes.summary(ds, 3).count("\n"), 3)
        self.assertIn("6 more", dupes.summary(ds, 3))


class ReplaceTests(Base):
    def test_replace_overwrites_the_existing_file_atomically(self):
        w = self.web(); w.files["/f.bin"] = DATA
        old = os.path.join(self.tmp, "out.bin")
        put(old, b"old content")
        j = self.job(w.base + "/f.bin"); j.replace = True
        j = run_job(j)
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(j.path, old)
        self.assertEqual(sha(old), SHA)
        self.assertFalse(os.path.exists(old + ".part"))

    def test_without_replace_the_existing_file_is_kept_and_new_one_is_numbered(self):
        w = self.web(); w.files["/f.bin"] = DATA
        old = os.path.join(self.tmp, "out.bin")
        put(old, b"old content")
        j = run_job(self.job(w.base + "/f.bin"))
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(os.path.basename(j.path), "out (1).bin")
        self.assertEqual(get(old), b"old content")

    def test_replace_does_not_clobber_a_partial_file_of_another_download(self):
        w = self.web(); w.files["/f.bin"] = DATA
        put(os.path.join(self.tmp, "out.bin"), b"old")
        put(os.path.join(self.tmp, "out.bin.part"), b"someone else is downloading this")
        j = self.job(w.base + "/f.bin"); j.replace = True
        j = run_job(j)
        self.assertEqual(j.state, "done", j.msg)
        self.assertEqual(os.path.basename(j.path), "out (1).bin")
        self.assertEqual(get(os.path.join(self.tmp, "out.bin")), b"old")


if __name__ == "__main__":
    unittest.main()
