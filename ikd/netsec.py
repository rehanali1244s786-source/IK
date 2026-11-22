"""Network safety: only http(s), no hops from the public internet into the private network,
login cookies only to the site they belong to, one shared opener (proxy + safe redirects)."""
import ipaddress, socket, threading, time, urllib.request, urllib.parse
from .common import log

_dns_cache: dict = {}
_dns_lock = threading.Lock()


def host_of(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def is_private_host(host: str) -> bool:
    """True if the name resolves to loopback / LAN / link-local / otherwise non-public addresses."""
    if not host:
        return False
    now = time.time()
    with _dns_lock:
        hit = _dns_cache.get(host)
        if hit and now - hit[0] < 60:
            return hit[1]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False                       # unresolvable: the real request will fail on its own
    private = False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            continue
        if not ip.is_global or ip.is_multicast:
            private = True
    with _dns_lock:
        if len(_dns_cache) > 512:
            _dns_cache.clear()
        _dns_cache[host] = (now, private)
    return private


def ensure_http(url: str, base: str = None) -> str:
    """Return url if it is a plain http(s) link; raise ValueError otherwise.
    With `base` (the page/playlist that referred to it) also refuse jumps from a public site to a private address."""
    u = urllib.parse.urlparse(url)
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        raise ValueError("Blocked link type (only http and https are allowed)")
    if base and is_private_host(u.hostname) and not is_private_host(host_of(base)):
        raise ValueError("Blocked: a public site pointed to an address inside your private network")
    return url


def same_site(a: str, b: str) -> bool:
    """Same host, or one is a sub-domain of the other. Used to decide where cookies may go."""
    ha, hb = host_of(a), host_of(b)
    return bool(ha) and (ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to http(s) (no ftp:// or file://) and never into the private network from the
    internet; never forward login cookies / credentials to another site."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ensure_http(urllib.parse.urljoin(req.full_url, newurl), base=req.full_url)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not same_site(newurl, req.full_url):
            for k in ("Cookie", "Authorization"):
                new.headers.pop(k, None)
                new.unredirected_hdrs.pop(k, None)
        return new


_opener = None
_lock = threading.Lock()


def set_proxy(px: str = "") -> None:
    global _opener
    px = (px or "").strip()
    handlers = [urllib.request.ProxyHandler({"http": px, "https": px}) if px else urllib.request.ProxyHandler(),
                SafeRedirect()]
    with _lock:
        _opener = urllib.request.build_opener(*handlers)
    log.info("network opener ready (proxy: %s)", "yes" if px else "no")


def urlopen(req, timeout: float = 20):
    with _lock:
        op = _opener
    if op is None:
        set_proxy("")
        op = _opener
    return op.open(req, timeout=timeout)
