const APP = "http://127.0.0.1:18080";
const EXT = chrome.runtime.getURL("");
const SITE_RE = /^https?:\/\/([^/]+\.)?(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|facebook\.com|fb\.watch|twitter\.com|x\.com|vimeo\.com|dailymotion\.com|reddit\.com|twitch\.tv|soundcloud\.com|bilibili\.com|pinterest\.com|streamable\.com|rumble\.com|threads\.net)(\/|$)/i;
const MEDIA_RE = /\.(mp4|webm|mkv|m4v|mov|flv|m3u8|mp3|m4a|aac|ogg)(\?|#|$)/i;
const isHttp = (u) => typeof u === "string" && /^https?:\/\//i.test(u);

// ---------- per-tab list of detected streams (updates are serialised so parallel requests can't overwrite each other)
const chains = {};
function serial(key, fn) {
  const run = (chains[key] || Promise.resolve()).then(fn);
  chains[key] = run.catch(() => {});
  return run;
}
async function getList(tabId) {
  const k = "t" + tabId;
  return (await chrome.storage.session.get(k))[k] || [];
}
function badge(tabId, list) {
  chrome.action.setBadgeText({ tabId, text: list.length ? String(list.length) : "" }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#e11d48" }).catch(() => {});
}
async function setList(tabId, list) {
  await chrome.storage.session.set({ ["t" + tabId]: list });
  badge(tabId, list);
}

// Detect media streams the page loads
chrome.webRequest.onHeadersReceived.addListener((d) => {
  if (d.tabId < 0) return;
  const h = {};
  for (const x of d.responseHeaders || []) h[x.name.toLowerCase()] = x.value;
  const type = (h["content-type"] || "").toLowerCase();
  if (type.includes("mp2t")) return; // skip HLS segments
  const isHls = /mpegurl/.test(type) || /\.m3u8(\?|#|$)/i.test(d.url);
  const isMedia = /^(video|audio)\//.test(type) || MEDIA_RE.test(d.url);
  if (!isHls && !isMedia) return;
  let size = parseInt(h["content-length"] || "0") || 0;
  const cr = h["content-range"];
  if (cr && cr.includes("/")) size = parseInt(cr.split("/")[1]) || size;
  if (!isHls && size && size < 300000) return;
  const url = d.url.split("#")[0];
  serial("t" + d.tabId, async () => {
    const list = await getList(d.tabId);
    if (list.some((i) => i.url === url)) return;
    list.push({ url, type: isHls ? "hls" : type || "media", size });
    await setList(d.tabId, list.slice(-20));
  }).catch(() => {});
}, { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] }, ["responseHeaders"]);

chrome.tabs.onUpdated.addListener((id, info) => {
  if (info.url) serial("t" + id, () => setList(id, [])).catch(() => {}); // new page: clear list
});
chrome.tabs.onRemoved.addListener((id) => {
  delete chains["t" + id];
  chrome.storage.session.remove(["t" + id, "n" + id]);
});

// ---------- talking to the desktop app (token pairing) ----------
const getToken = async () => (await chrome.storage.local.get("ikToken")).ikToken || "";
let pairing = null;

async function post(path, body, token) {
  const headers = { "Content-Type": "application/json", "X-IK-Client": "1" };
  if (token) headers["X-IK-Token"] = token;
  try {
    return await fetch(APP + path, { method: "POST", headers, body: JSON.stringify(body || {}) });
  } catch (e) {
    throw new Error("Open IK DOWNLOADER first, then try again.");
  }
}

// Asks the user (a dialog inside the desktop app) to approve this extension, once.
function pair() {
  if (!pairing) {
    pairing = (async () => {
      const r = await post("/pair", {});
      if (r.status === 429) throw new Error("Answer the question that is already open in IK DOWNLOADER.");
      if (!r.ok) throw new Error("The connection was not approved in IK DOWNLOADER.");
      const d = await r.json();
      await chrome.storage.local.set({ ikToken: d.token });
      return d.token;
    })().finally(() => { pairing = null; });
  }
  return pairing;
}

// interactive=false (automatic download capture): never pops up a question, just lets the browser download normally
async function callApp(path, body, interactive = true) {
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(() => {}), 20000); // long calls (dialogs in the app)
  try {
    let token = await getToken();
    if (!token) {
      if (!interactive) throw new Error("Not connected yet");
      token = await pair();
    }
    let r = await post(path, body, token);
    if (r.status === 401) {                         // the app forgot us (user pressed "Disconnect all browsers")
      await chrome.storage.local.remove("ikToken");
      if (!interactive) throw new Error("Not connected");
      token = await pair();
      r = await post(path, body, token);
    }
    if (!r.ok) throw new Error("App refused the request (HTTP " + r.status + ")");
    return await r.json();
  } finally {
    clearInterval(keepAlive);
  }
}

async function sendToApp({ url, referer, title, site, quality, filename, folder, ask, size, mime }, interactive = true) {
  if (!isHttp(url)) throw new Error("Not a web link");
  let cookie = "";
  try {
    const cs = await chrome.cookies.getAll({ url });
    cookie = cs.map((c) => c.name + "=" + c.value).join("; ");
  } catch (e) { /* no cookies for this site */ }
  return callApp("/add", {
    url, referer, title, site: !!site, quality, filename, folder, cookie, ua: navigator.userAgent,
    ask: !!ask, size, mime,
  }, interactive);
}

// Show a failure where the user can see it (badge on the toolbar icon)
function flashError(tabId, text) {
  if (tabId == null || tabId < 0) return;
  chrome.action.setBadgeText({ tabId, text: "!" }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#f59e0b" }).catch(() => {});
  chrome.action.setTitle({ tabId, title: "IK DOWNLOADER: " + text }).catch(() => {});
  setTimeout(async () => {
    chrome.action.setTitle({ tabId, title: "IK DOWNLOADER" }).catch(() => {});
    badge(tabId, await getList(tabId).catch(() => []));
  }, 6000);
}

// ---------- who is allowed to talk to the background page ----------
// "content" = our content script (a normal web page's tab), "panel"/"popup" = our own pages, everything else is refused.
function senderKind(s) {
  if (!s || s.id !== chrome.runtime.id || !s.url) return "other";
  if (s.url.startsWith(EXT)) {
    const p = new URL(s.url).pathname;
    return p === "/popup.html" ? "popup" : p === "/panel.html" ? "panel" : "other";
  }
  return s.tab ? "content" : "other";
}
// A web page can put panel.html in its own iframe. Only a panel opened by our content script knows the secret nonce.
async function panelOk(m, s) {
  if (!s.tab) return false;
  const k = "n" + s.tab.id;
  const n = (await chrome.storage.session.get(k))[k];
  return !!n && typeof m.nonce === "string" && m.nonce === n;
}

// Everything the quality picker needs (formats, sizes, default folder)
async function prepare(m, sender) {
  const page = isHttp(m.pageUrl) ? m.pageUrl : sender.tab.url;
  if (SITE_RE.test(page)) {
    const r = await callApp("/info", { url: page, extract: true });
    if (!r.ok) throw new Error(r.error || "Could not read video info");
    return { kind: "site", url: page, title: r.title, folder: r.folder, ffmpeg: r.ffmpeg, formats: r.formats };
  }
  const list = await getList(sender.tab.id);
  const items = [];
  if (isHttp(m.src)) items.push({ url: m.src, type: "video", size: 0 });
  for (const i of [...list].reverse()) if (!items.some((x) => x.url === i.url)) items.push(i);
  if (!items.length) throw new Error("No stream found yet. Play the video a few seconds and retry.");
  const d = await callApp("/info", { url: items[0].url, extract: false });
  return {
    kind: "direct", url: items[0].url, title: sender.tab.title || "video", folder: d.folder, ffmpeg: d.ffmpeg,
    formats: items.map((i) => ({
      url: i.url, size: i.size,
      label: i.type === "hls" ? "HLS stream" : ((i.type.split("/")[1] || "video").toUpperCase() + " file"),
    })),
  };
}

chrome.runtime.onMessage.addListener((m, sender, reply) => {
  (async () => {
    try {
      const who = senderKind(sender);
      if (who === "content" && m.type === "panelOpen") {
        if (typeof m.nonce !== "string" || m.nonce.length < 16) throw new Error("bad nonce");
        await chrome.storage.session.set({ ["n" + sender.tab.id]: m.nonce });
        return reply({ ok: true });
      }
      if (who === "panel") {
        if (!(await panelOk(m, sender))) throw new Error("Panel not authorised");
        if (m.type === "prepare") {
          reply({ ok: true, data: await prepare(m, sender) });
        } else if (m.type === "start") {
          if (!isHttp(m.url)) throw new Error("Not a web link");
          const direct = m.kind === "direct";
          const r = await sendToApp({
            url: m.url, referer: sender.tab && sender.tab.url, title: m.filename, site: !direct,
            quality: m.quality, filename: m.filename, folder: m.folder,
          });
          reply({ ok: !!(r && r.ok), error: "App did not accept the download" });
        } else if (m.type === "browse") {
          const r = await callApp("/browse", { initial: String(m.initial || "") });
          reply({ ok: true, path: r.path });
        }
        return;
      }
      if (who === "popup") {
        if (m.type === "send") {                    // popup quick-send of a detected stream
          await sendToApp(m);
          reply({ ok: true });
        } else if (m.type === "list") {
          reply({ ok: true, list: await getList(m.tabId) });
        } else if (m.type === "status") {           // connected + approved?
          try { await callApp("/info", { extract: false }, false); reply({ ok: true, paired: true }); }
          catch (e) { reply({ ok: true, paired: false }); }
        } else if (m.type === "pair") {
          await pair();
          reply({ ok: true });
        }
        return;
      }
      reply({ ok: false, error: "Not allowed" });
    } catch (e) {
      reply({ ok: false, error: String(e.message || e) });
    }
  })();
  return true;
});

// ---------- right-click menu ----------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "ikd", title: "Download with IK DOWNLOADER", contexts: ["link", "video", "audio", "page"],
  });
});
chrome.contextMenus.onClicked.addListener((info, tab) => {
  let url = info.linkUrl || info.srcUrl, site = false;
  if (!isHttp(url)) { url = info.pageUrl; site = true; }
  if (isHttp(url)) sendToApp({ url, referer: tab && tab.url, site }).catch((e) => flashError(tab && tab.id, e.message));
});

// ---------- take over every browser download (zip, exe, pdf, ...) ----------
// The app detects the file first and shows an IDM-style "Download File Info" box (Start / Later / Cancel).
// Turn it off in the popup. If the app is not running or the extension is not approved yet, the browser
// simply downloads the file itself.
chrome.downloads.onCreated.addListener(async (item) => {
  const { capture } = await chrome.storage.local.get("capture");
  if (capture === false || item.byExtensionId) return;
  const url = isHttp(item.url) ? item.url : item.finalUrl;
  if (!isHttp(url) || /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) return;
  try {
    await new Promise((r) => setTimeout(r, 150)); // let Chrome work out name / size / type first
    const [cur] = await chrome.downloads.search({ id: item.id });
    const it = cur || item;
    const r = await sendToApp({
      url, referer: it.referrer || item.referrer, ask: true,
      filename: (it.filename || "").split(/[\\/]/).pop(),
      size: it.totalBytes > 0 ? it.totalBytes : it.fileSize > 0 ? it.fileSize : 0, mime: it.mime,
    }, false);
    if (r && r.ok) {
      chrome.downloads.cancel(item.id, () => chrome.downloads.erase({ id: item.id }));
    }
  } catch (e) { /* app not running / not approved: let the browser download it */ }
});
