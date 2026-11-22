const app = document.getElementById("app");
const qs = new URLSearchParams(location.search);
const SRC = qs.get("src") || "", PAGE = qs.get("page") || "", NONCE = qs.get("n") || "";
let data = null;

const fmt = (n) => (n > 0 ? (n >= 1073741824 ? (n / 1073741824).toFixed(2) + " GB" : (n / 1048576).toFixed(1) + " MB") : "");
const el = (tag, props = {}, ...kids) => { const e = document.createElement(tag); Object.assign(e, props); e.append(...kids); return e; };
const resize = () => parent.postMessage({ ikd: "resize", h: document.documentElement.scrollHeight + 2 }, "*");
const close = () => parent.postMessage({ ikd: "close" }, "*");
const bg = (m) => new Promise((res) => chrome.runtime.sendMessage({ ...m, nonce: NONCE }, (r) =>
  res(r || { ok: false, error: (chrome.runtime.lastError && chrome.runtime.lastError.message) || "No response" })));

function show(...nodes) {
  const x = el("span", { className: "x", textContent: "✕", title: "Close" });
  x.onclick = close;
  app.replaceChildren(el("header", {}, el("b", { textContent: "⬇" }), el("span", { textContent: "IK DOWNLOADER" }), x), ...nodes);
  requestAnimationFrame(resize);
}
const msg = (t) => show(el("div", { className: "msg", textContent: t }));
function errorView(t) {
  const b = el("button", { textContent: "Close", onclick: close });
  show(el("div", { className: "msg", textContent: "⚠ " + t }), el("div", { className: "btns" }, b));
}

// Step 1: choose a resolution
function listView() {
  const rows = data.formats.map((f) => {
    const r = el("div", { className: "row" }, el("span", { textContent: f.label }),
      el("small", { textContent: fmt(f.size) ? (data.kind === "site" ? "~" : "") + fmt(f.size) : "" }));
    r.onclick = () => dialogView(f);
    return r;
  });
  show(el("div", { className: "title", textContent: data.title }),
       el("div", { className: "hint", textContent: "Select video quality:" }), ...rows);
}

// Step 2: "Download File Info" dialog (name, path, Start / Cancel)
function dialogView(f) {
  const isSite = data.kind === "site";
  const name = el("input", { value: (data.title || "video").replace(/[\\/:*?"<>|]+/g, " ").trim().slice(0, 120) });
  const ext = el("span", { className: "ext", textContent: isSite ? ".mp4 / .mp3" : "" });
  const folder = el("input", { value: data.folder || "" });
  const browse = el("button", { textContent: "Browse…" });
  browse.onclick = async () => {
    browse.textContent = "Pick in app…";
    const r = await bg({ type: "browse", initial: folder.value });
    if (r.ok && r.path) folder.value = r.path;
    browse.textContent = "Browse…";
  };
  const url = el("input", { value: f.url || data.url, readOnly: true });
  const vb = el("button", { className: "v", textContent: isSite ? "Start Video" : "Start" });
  const ab = el("button", { className: "a", textContent: "Start Audio (MP3)" });
  const cb = el("button", { textContent: "Cancel", onclick: close });
  const back = el("button", { textContent: "←", title: "Back", onclick: listView });
  if (!isSite) { ab.disabled = true; ab.title = "Audio-only works for video sites (YouTube, TikTok, ...)"; }
  else if (!data.ffmpeg) { ab.title = "Saved in its original audio format (MP3 needs ffmpeg)"; }

  async function start(audio) {
    [vb, ab, cb, back, browse].forEach((b) => (b.disabled = true));
    const r = await bg({
      type: "start", kind: data.kind, url: f.url || data.url, quality: audio ? "audio" : (f.quality || "best"),
      filename: name.value.trim(), folder: folder.value.trim(),
    });
    if (r.ok) { msg("✔ Sent to IK DOWNLOADER"); setTimeout(close, 1100); }
    else errorView(r.error || "Failed");
  }
  vb.onclick = () => start(false);
  ab.onclick = () => start(true);

  show(
    el("div", { className: "info", textContent: `Quality: ${f.label}${fmt(f.size) ? "  •  " + (isSite ? "~" : "") + fmt(f.size) : ""}` }),
    el("label", { textContent: "URL" }), el("div", { className: "line" }, url),
    el("label", { textContent: "File name" }), el("div", { className: "line" }, name, ext),
    el("label", { textContent: "Save to" }), el("div", { className: "line" }, folder, browse),
    el("div", { className: "btns" }, back, vb, ab, cb),
  );
  name.focus(); name.select();
  name.onkeydown = folder.onkeydown = (e) => { if (e.key === "Enter") start(false); };
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
msg("Reading video info…");
bg({ type: "prepare", src: SRC, pageUrl: PAGE }).then((r) => {
  if (!r.ok) return errorView(r.error || "Failed");
  data = r.data;
  listView();
});
