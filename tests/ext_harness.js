// Loads extension/background.js with a mocked `chrome` API and drives it against the real Python local API.
// usage: node tests/ext_harness.js <port>
const fs = require("fs"), path = require("path");
const port = process.argv[2];
let src = fs.readFileSync(path.join(__dirname, "..", "extension", "background.js"), "utf8");
if (!src.includes("http://127.0.0.1:18080")) throw new Error("APP constant not found");
src = src.replace("http://127.0.0.1:18080", `http://127.0.0.1:${port}`);

const ID = "abcdefghijklmnopabcdefghijklmnop";
const store = { local: {}, session: {} };
const area = (o) => ({
  get: async (k) => (typeof k === "string" ? (k in o ? { [k]: o[k] } : {}) : { ...o }),
  set: async (obj) => { Object.assign(o, obj); },
  remove: async (k) => { [].concat(k).forEach((x) => delete o[x]); },
});
const L = {};
let cancelCalls = 0;
const chrome = {
  runtime: { id: ID, getURL: (p) => `chrome-extension://${ID}/${p}`, onMessage: { addListener: (f) => (L.msg = f) },
             onInstalled: { addListener() {} }, getPlatformInfo: (cb) => cb && cb({}) },
  storage: { local: area(store.local), session: area(store.session) },
  webRequest: { onHeadersReceived: { addListener: (f) => (L.hdr = f) } },
  tabs: { onUpdated: { addListener: (f) => (L.upd = f) }, onRemoved: { addListener: (f) => (L.rem = f) } },
  action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
  contextMenus: { create() {}, onClicked: { addListener: (f) => (L.ctx = f) } },
  cookies: { getAll: async () => [{ name: "sid", value: "abc" }] },
  downloads: { onCreated: { addListener: (f) => (L.dl = f) }, search: async () => [], cancel: () => { cancelCalls++; }, erase() {} },
};
// Chrome always adds "Origin: chrome-extension://<id>" to an extension's POST requests; Node's fetch does not, so add it.
const originFetch = (u, o = {}) => fetch(u, { ...o, headers: { ...(o.headers || {}), Origin: `chrome-extension://${ID}` } });
new Function("chrome", "fetch", "navigator", src)(chrome, originFetch, { userAgent: "test" });

const send = (m, sender) => new Promise((res) => L.msg(m, sender, res));
const panel = (tabId) => ({ id: ID, url: `chrome-extension://${ID}/panel.html?x=1`, tab: { id: tabId, url: "https://site.example/v", title: "T" } });
const content = (tabId) => ({ id: ID, url: "https://site.example/v", tab: { id: tabId, url: "https://site.example/v", title: "T" } });
const popup = { id: ID, url: `chrome-extension://${ID}/popup.html` };
const results = [];
const check = (name, ok, extra) => { results.push({ name, ok: !!ok, extra }); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const nonce = "0123456789abcdef-nonce-xyz";
  // 1. a web page that frames panel.html itself (no registered nonce) is refused
  let r = await send({ type: "prepare", src: "https://x/v.mp4", nonce: "guess" }, panel(5));
  check("framed panel without nonce is refused", !r.ok && /not authorised/i.test(r.error));
  // 2. other senders / other kinds
  r = await send({ type: "start", url: "https://x/y", kind: "direct" }, content(5));
  check("content script cannot start downloads", !r.ok);
  r = await send({ type: "panelOpen", nonce }, { id: "someotherextension", url: "https://x", tab: { id: 5 } });
  check("foreign extension is refused", !r.ok);
  r = await send({ type: "panelOpen", nonce: "short" }, content(5));
  check("weak nonce refused", !r.ok);
  // 3. proper handshake, then a wrong nonce is still refused
  r = await send({ type: "panelOpen", nonce }, content(5));
  check("content script registers nonce", r.ok);
  r = await send({ type: "prepare", src: "https://x/v.mp4", nonce: "wrong" }, panel(5));
  check("wrong nonce refused", !r.ok);
  r = await send({ type: "prepare", src: "https://x/v.mp4", nonce }, panel(6));
  check("nonce is bound to its own tab", !r.ok);
  // 4. real prepare: triggers pairing against the Python server (auto-approved there)
  check("no token before first use", !store.local.ikToken);
  r = await send({ type: "prepare", src: "https://x/v.mp4", nonce }, panel(5));
  check("authorised panel can prepare (pairs on first use)", r.ok && r.data && r.data.kind === "direct", JSON.stringify(r));
  check("token stored after pairing", typeof store.local.ikToken === "string" && store.local.ikToken.length > 20);
  r = await send({ type: "status" }, popup);
  check("popup sees paired status", r.ok && r.paired === true);
  // 5. start sends the download (cookies included) and the app accepts it
  r = await send({ type: "start", url: "https://x/v.mp4", kind: "direct", filename: "v", folder: "", nonce }, panel(5));
  check("start is accepted by the app", r.ok === true, JSON.stringify(r));
  r = await send({ type: "start", url: "file:///etc/passwd", kind: "direct", nonce }, panel(5));
  check("non-http link refused", !r.ok);
  // 6. stale/revoked token: extension recovers by pairing again
  store.local.ikToken = "bogus-token";
  r = await send({ type: "prepare", src: "https://x/v.mp4", nonce }, panel(5));
  check("revoked token triggers re-pairing", r.ok && store.local.ikToken !== "bogus-token", JSON.stringify(r));
  // 7. automatic capture never opens a question when not paired
  delete store.local.ikToken;
  await L.dl({ id: 1, url: "https://files.example/a.zip", referrer: "", byExtensionId: undefined });
  await sleep(400);
  check("capture without token lets the browser download", cancelCalls === 0 && !store.local.ikToken);
  // 8. stream list: parallel requests must not overwrite each other
  const mk = (i) => ({ tabId: 7, url: `https://cdn.example/v${i}.mp4`,
                       responseHeaders: [{ name: "content-type", value: "video/mp4" }, { name: "content-length", value: "5000000" }] });
  for (let i = 0; i < 15; i++) L.hdr(mk(i));
  await sleep(300);
  r = await send({ type: "list", tabId: 7 }, popup);
  check("15 parallel detections are all kept", r.ok && r.list.length === 15, r.list && r.list.length);
  L.upd(7, { url: "https://other.example/" }); await sleep(100);
  r = await send({ type: "list", tabId: 7 }, popup);
  check("list cleared on navigation", r.ok && r.list.length === 0);

  console.log(JSON.stringify(results));
  process.exit(results.every((x) => x.ok) ? 0 : 1);
})().catch((e) => { console.error("HARNESS ERROR", e); process.exit(2); });
