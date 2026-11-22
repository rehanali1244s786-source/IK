(() => {
  if (window.__ikd) return;
  window.__ikd = true;
  const LABEL = "⬇ Download with IK DOWNLOADER";
  const host = document.createElement("div");
  host.style.cssText = "all:initial;position:fixed;z-index:2147483647;top:0;left:0;width:0;height:0";
  const root = host.attachShadow({ mode: "closed" });
  root.innerHTML = `<style>
    .bar{position:fixed;display:none;align-items:center;gap:8px;background:#111c;color:#fff;
      font:600 13px system-ui,sans-serif;padding:7px 12px;border-radius:999px;cursor:pointer;
      box-shadow:0 2px 10px #0008;border:1px solid #fff3;user-select:none;white-space:nowrap}
    .bar:hover{background:#e11d48}
    .x{opacity:.7;padding:0 2px}.x:hover{opacity:1}
    iframe{position:fixed;width:370px;height:180px;border:0;border-radius:12px;box-shadow:0 8px 30px #000a;color-scheme:normal}
  </style><div class="bar" id="bar"><span id="t"></span><span class="x" id="x">✕</span></div>`;
  const bar = root.getElementById("bar"), label = root.getElementById("t"), x = root.getElementById("x");
  label.textContent = LABEL;
  const dismissed = new WeakSet();
  let current = null, frame = null;

  const SEL = 'a[href*="/reel/"],a[href*="/reels/"],a[href*="/p/"],a[href*="/video/"],a[href*="/status/"],a[href*="watch?v="],a[href*="/shorts/"]';
  // Find the page URL of the video (needed for feeds on Instagram / TikTok / X)
  function pageUrlFor(v) {
    if (/youtube\.com\/watch|\/shorts\/|\/reels?\/|\/p\/[^/]+|\/video\/\d|\/status\/\d|vimeo\.com\/\d/.test(location.href)) return location.href;
    let el = v;
    for (let i = 0; el && i < 8; i++, el = el.parentElement) {
      if (el.querySelectorAll && el.querySelectorAll("video").length !== 1) break;
      const a = el.matches && el.matches(SEL) ? el : el.querySelector && el.querySelector(SEL);
      if (a && a.href) return a.href;
    }
    return location.href;
  }

  function pick() {
    let best = null, area = 0;
    for (const v of document.querySelectorAll("video")) {
      const r = v.getBoundingClientRect();
      if (r.width < 200 || r.height < 120 || dismissed.has(v)) continue;
      if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
      if (v.paused && v.currentTime === 0) continue; // show once playback started
      if (r.width * r.height > area) { area = r.width * r.height; best = v; }
    }
    return best;
  }
  function update() {
    if (document.hidden || !document.querySelector("video")) {   // nothing to do: no video on this page / tab not visible
      current = null; bar.style.display = "none"; return;
    }
    current = pick();
    if (!current) { bar.style.display = "none"; return; }
    const r = current.getBoundingClientRect();
    bar.style.display = "flex";
    bar.style.top = Math.max(8, r.top + 8) + "px";
    bar.style.left = Math.max(8, Math.min(innerWidth - bar.offsetWidth - 8, r.right - bar.offsetWidth - 8)) + "px";
  }
  setInterval(update, 700);
  addEventListener("scroll", update, true);
  addEventListener("resize", update);

  // ---- quality picker / download dialog (lives in an extension iframe) ----
  function closePanel() { if (frame) { frame.remove(); frame = null; } }
  // The panel only works with a one-time secret (nonce) that this script registers with the extension first.
  // A web page that frames panel.html on its own does not know the nonce, so the extension ignores it.
  function openPanel(src, pageUrl) {
    closePanel();
    const nonce = crypto.randomUUID();
    const br = bar.style.display === "flex" ? bar.getBoundingClientRect() : null;
    try {
      chrome.runtime.sendMessage({ type: "panelOpen", nonce }, (r) => {
        if (chrome.runtime.lastError || !r || !r.ok) return;           // extension was reloaded: page needs a refresh
        closePanel();
        frame = document.createElement("iframe");
        frame.src = chrome.runtime.getURL("panel.html") + "?src=" + encodeURIComponent(src || "") +
          "&page=" + encodeURIComponent(pageUrl || location.href) + "&n=" + encodeURIComponent(nonce);
        const left = br ? Math.min(Math.max(8, br.right - 370), innerWidth - 378) : Math.max(8, (innerWidth - 370) / 2);
        frame.style.top = (br ? br.bottom + 8 : 60) + "px";
        frame.style.left = left + "px";
        root.appendChild(frame);
      });
    } catch (e) { /* extension context invalidated */ }
  }
  addEventListener("message", (e) => {
    if (!frame || e.source !== frame.contentWindow || !e.data) return;
    if (e.data.ikd === "resize") frame.style.height = Math.min(+e.data.h || 180, innerHeight - 20) + "px";
    if (e.data.ikd === "close") closePanel();
  });
  addEventListener("mousedown", (e) => { if (frame && e.target !== host) closePanel(); }, true);
  addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); }, true);
  chrome.runtime.onMessage.addListener((m, s, reply) => {
    if (m.type === "openPanel" && window === top) { openPanel("", location.href); reply(true); }
  });

  bar.addEventListener("click", (e) => {
    if (e.target === x) { if (current) dismissed.add(current); closePanel(); update(); return; }
    if (!current) return;
    if (frame) return closePanel();
    openPanel(current.currentSrc || current.src, pageUrlFor(current));
  });
  document.documentElement.appendChild(host);
})();
