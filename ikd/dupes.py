"""Duplicate detection: is this URL or file name already in the list, or is the file already on disk?
Pure logic (no GUI); the UI decides what to ask. Choices are: skip, rename, replace (= re-download)."""
import os, time, urllib.parse
from typing import Iterable, List, NamedTuple, Optional

from .common import category, fmt

SKIP, RENAME, REPLACE = "skip", "rename", "replace"
POLICIES = ("ask", SKIP, RENAME, REPLACE)


class Dup(NamedTuple):
    kind: str                     # "url" | "name" | "disk"
    text: str                     # one line for the person
    job: object = None            # the list entry involved (None for disk)
    path: str = ""                # the file involved


def norm_url(u: str) -> str:
    """Compare links the way a person would: scheme/host case, default ports, '#fragment' and '/' at the end ignored."""
    try:
        p = urllib.parse.urlsplit((u or "").strip())
        host = (p.hostname or "").lower()
        port = p.port if p.port not in (None, 80 if p.scheme.lower() == "http" else 443) else None
    except ValueError:                                   # malformed link / port: compare it as written
        return (u or "").strip()
    netloc = host + (f":{port}" if port else "")
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme.lower()}://{netloc}{path}" + (f"?{p.query}" if p.query else "")


def target_path(folder: str, name: str, exact: bool) -> str:
    """Where a file called `name` ends up: the folder itself when the person chose it, else a type sub-folder."""
    return os.path.join(folder, name) if exact else os.path.join(folder, category(name), name)


def job_target(j) -> str:
    """The file a list entry writes to (or will write to)."""
    if getattr(j, "path", ""):
        return j.path
    name = getattr(j, "fname", "") or getattr(j, "name", "")
    return target_path(j.folder, name, bool(getattr(j, "exact", False))) if name else ""


def _same(a: str, b: str) -> bool:
    return bool(a and b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _when(path: str) -> str:
    try:
        st = os.stat(path)
        return f"{fmt(st.st_size)}, {time.strftime('%Y-%m-%d', time.localtime(st.st_mtime))}"
    except OSError:
        return ""


def find_dupes(url: str, name: str, folder: str, exact: bool, jobs: Iterable, ignore=None) -> List[Dup]:
    """All the ways `url` / `name` collide with the list or the disk. `name` may be "" when it is not known yet
    (video sites): then only the link is compared. `ignore` is a list entry to leave out (e.g. the one being edited)."""
    out: List[Dup] = []
    nu = norm_url(url) if url else ""
    tgt = target_path(folder, name, exact) if name else ""
    for j in jobs:
        if j is ignore:
            continue
        state = {"done": "already downloaded", "downloading": "downloading now", "queued": "waiting in the queue",
                 "paused": "in the list (paused)", "error": "in the list (failed)"}.get(j.state, "in the list")
        if nu and norm_url(j.url) == nu:
            out.append(Dup("url", f"This link is {state}: {j.name or j.url}", j, job_target(j)))
        elif tgt and _same(job_target(j), tgt):
            out.append(Dup("name", f"A download with this file name is {state}: {j.name}", j, job_target(j)))
    if tgt and os.path.isfile(tgt) and not any(_same(d.path, tgt) for d in out):
        out.append(Dup("disk", f"A file with this name already exists in that folder ({_when(tgt)}): {tgt}", None, tgt))
    return out


def unique_name(name: str, folder: str, exact: bool, jobs: Iterable) -> str:
    """name, or 'name (1).ext', 'name (2).ext' ... that is free on disk and in the list."""
    base, ext = os.path.splitext(name)
    taken = [job_target(j) for j in jobs]
    cand, i = name, 1
    while True:
        p = target_path(folder, cand, exact)
        if not os.path.exists(p) and not os.path.exists(p + ".part") and not any(_same(t, p) for t in taken):
            return cand
        cand = f"{base} ({i}){ext}"
        i += 1


def summary(dups: List[Dup], limit: int = 6) -> str:
    lines = [d.text for d in dups[:limit]]
    if len(dups) > limit:
        lines.append(f"... and {len(dups) - limit} more")
    return "\n".join(lines)


def choose(policy: str, answer: Optional[str]) -> str:
    """The policy from Options decides, unless it is 'ask' (then the person's answer does; no answer = skip)."""
    return policy if policy in (SKIP, RENAME, REPLACE) else (answer if answer in (SKIP, RENAME, REPLACE) else SKIP)
