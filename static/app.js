const $ = (id) => document.getElementById(id);
let currentJob = null, pollTimer = null;

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) { const t = await r.text(); throw new Error(t || r.statusText); }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

$("start-btn").addEventListener("click", async () => {
  const errEl = $("form-error");
  errEl.classList.add("hidden");
  const link = $("link").value.trim();
  const file = $("file").files[0];
  if (!link && !file) { errEl.textContent = "Paste a video link or choose a video file first."; errEl.classList.remove("hidden"); return; }
  $("start-btn").disabled = true;
  try {
    const fd = new FormData();
    if (link) fd.append("link", link);
    if (file) fd.append("upload", file);
    const { id } = await api("/api/jobs", { method: "POST", body: fd });
    currentJob = id;
    $("progress-card").classList.remove("hidden");
    $("results").innerHTML = "";
    $("progress-title").textContent = "Clipping…";
    $("progress-bar").style.width = "0%";
    $("progress-log").textContent = "";
    poll();
  } catch (e) {
    errEl.textContent = e.message || "Could not start. Please try again.";
    errEl.classList.remove("hidden");
  }
  $("start-btn").disabled = false;
});

$("cancel-btn").addEventListener("click", async () => {
  if (currentJob) await api(`/api/jobs/${currentJob}/cancel`, { method: "POST" });
});

async function poll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!currentJob) return clearInterval(pollTimer);
    try {
      const j = await api(`/api/jobs/${currentJob}`);
      $("progress-bar").style.width = j.progress + "%";
      $("progress-stage").textContent = j.stage + (j.progress ? ` — ${j.progress}%` : "");
      $("progress-log").textContent = (j.log || []).join("\n");
      $("progress-log").scrollTop = 1e9;
      if (j.status === "done") { clearInterval(pollTimer); showResults(j); loadHistory(); }
      else if (j.status === "failed" || j.status === "cancelled") {
        clearInterval(pollTimer);
        $("progress-title").textContent = j.status === "cancelled" ? "Cancelled." : "Something went wrong.";
        $("progress-stage").textContent = j.error || "";
        loadHistory();
      }
    } catch { /* keep polling */ }
  }, 3000);
}

function clipCard(c, i, jid) {
  const first = c.files[0];
  const div = document.createElement("div");
  div.className = "clip";
  div.innerHTML = `
    <h3>Clip ${i + 1}: ${esc(c.title || c.type || "Moment")}</h3>
    <p class="hook">${esc(c.hook || "")}</p>
    <div class="badges">
      <span class="badge">⭐ ${c.score || "?"} / 100</span>
      <span class="badge">⏱ ${c.seconds || "?"} sec</span>
      <span class="badge">${esc(c.type || "")}</span>
    </div>
    ${first ? `<video controls preload="metadata" playsinline src="${first.url}"></video>` : ""}
    <div class="actions">
      ${c.files.map(f => `<a class="btn primary" href="${f.url}?download=1" download="${f.file}">⬇ Download</a>`).join("")}
      <button class="btn" data-cap="${i}" data-jid="${jid}">📋 Copy caption</button>
    </div>`;
  div.querySelectorAll("[data-cap]").forEach(b => b.addEventListener("click", async () => {
    const job = await api(`/api/jobs/${b.dataset.jid}`);
    const clip = job.clips[+b.dataset.cap];
    const text = (clip.caption || "") + "\n\n" + (clip.hashtags || []).join(" ");
    navigator.clipboard.writeText(text).then(() => { b.textContent = "✓ Copied"; setTimeout(() => b.textContent = "📋 Copy caption", 2000); });
  }));
  return div;
}

function showResults(j) {
  $("progress-title").textContent = `Done — ${j.clips.length} clips ready`;
  $("progress-bar").style.width = "100%";
  $("progress-stage").textContent = "Watch them below, then download the ones you like.";
  const host = $("results");
  host.innerHTML = `<section class="card"><h2>Your new clips</h2><div id="clip-list"></div></section>`;
  const list = $("clip-list");
  j.clips.forEach((c, i) => list.appendChild(clipCard(c, i, j.id)));
  host.scrollIntoView({ behavior: "smooth" });
}

async function loadHistory() {
  const host = $("history");
  try {
    const { jobs } = await api("/api/jobs");
    const done = jobs.filter(j => j.status === "done" && j.clips && j.clips.length);
    if (!done.length) { host.innerHTML = `<p class="hint">No clips yet. Paste a link above and press Start Clipping.</p>`; return; }
    host.innerHTML = "";
    done.forEach(j => {
      const sec = document.createElement("div");
      sec.className = "history-job";
      const when = new Date(j.created_at).toLocaleString();
      sec.innerHTML = `<h3>${esc(j.params.source_name || j.params.url || "Uploaded video")}</h3><p class="hint">${when} — ${j.clips.length} clips</p>`;
      j.clips.forEach((c, i) => sec.appendChild(clipCard(c, i, j.id)));
      host.appendChild(sec);
    });
  } catch {
    host.innerHTML = `<p class="hint">Could not load saved clips. Try refreshing the page.</p>`;
  }
}

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

loadHistory();
