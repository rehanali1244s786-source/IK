const $ = (id) => document.getElementById(id);
const fmt = (n) => (n ? (n / 1048576).toFixed(1) + " MB" : "");
(async () => {
  let running = true;
  try { await fetch("http://127.0.0.1:18080/ping"); $("st").textContent = "● app running"; }
  catch (e) { $("st").textContent = "○ start the app"; running = false; }
  const refresh = () => chrome.runtime.sendMessage({ type: "status" }, (r) => {
    const ok = !!(r && r.paired);
    $("st").textContent = ok ? "● connected" : "● app running - not approved";
    $("connect").style.display = ok ? "none" : "block";
  });
  if (running) refresh();
  $("pairbtn").onclick = () => {
    $("pairbtn").textContent = "Waiting for your answer in the app...";
    chrome.runtime.sendMessage({ type: "pair" }, (r) => {
      $("pairbtn").textContent = r && r.ok ? "Connected ✔" : (r && r.error) || "Not approved";
      refresh();
    });
  };
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  $("page").onclick = () => chrome.tabs.sendMessage(tab.id, { type: "openPanel" }, { frameId: 0 })
    .then(() => window.close())
    .catch(() => ($("page").textContent = "Reload the page once, then try again"));
  chrome.runtime.sendMessage({ type: "list", tabId: tab.id }, (res) => {
    const list = (res && res.list) || [];
    $("empty").style.display = list.length ? "none" : "block";
    for (const it of [...list].reverse()) {
      const row = document.createElement("div"); row.className = "row";
      const name = decodeURIComponent(it.url.split("?")[0].split("/").pop() || it.url);
      const sp = document.createElement("span");
      sp.textContent = name;
      const sm = document.createElement("small");
      sm.textContent = (it.type === "hls" ? "HLS stream" : it.type) + " " + fmt(it.size);
      sp.appendChild(sm);
      const b = document.createElement("button"); b.textContent = "Download";
      b.onclick = () => chrome.runtime.sendMessage(
        { type: "send", url: it.url, referer: tab.url, title: tab.title },
        (r) => (b.textContent = r && r.ok ? "Sent ✔" : "Failed"));
      row.append(sp, b); $("list").appendChild(row);
    }
  });
  const { capture } = await chrome.storage.local.get("capture");
  $("cap").checked = capture !== false; // on by default
  $("cap").onchange = (e) => chrome.storage.local.set({ capture: e.target.checked });
})();
