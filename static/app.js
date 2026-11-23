const $ = (id) => document.getElementById(id);
let currentJob = null, pollTimer = null;
// All jobs from the server, shared by Home / Dashboard / Library / History.
let allJobs = [];
let libDetailId = null;

// Format seconds as H:MM:SS (for entire-stream clip timestamps).
function fmtHMS(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
               : `${m}:${String(s).padStart(2, "0")}`;
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function fmtDate(ts) { try { return new Date(ts).toLocaleString(); } catch { return ""; } }
function jobTitle(j) { return (j.params && (j.params.source_name || j.params.url)) || "Uploaded video"; }
function jobUrl(j) { return (j.params && j.params.url) || ""; }
function clipCount(j) { return (j.clips || []).length; }
function isActive(j) { return j.status === "queued" || j.status === "running"; }
function statusText(j) {
  return { queued: "Queued", running: "Processing", done: "Done",
           failed: "Failed", cancelled: "Cancelled" }[j.status] || j.status;
}
// YouTube thumbnail for a job (real image from YouTube for that video).
function ytThumb(j) {
  const id = ytVideoId(jobUrl(j));
  return id ? `https://i.ytimg.com/vi/${id}/hqdefault.jpg` : null;
}

// ---- Browser-side YouTube fetch (the visitor's home IP) ----
// YouTube blocks downloads from this server's datacenter IP (its
// player API answers LOGIN_REQUIRED there), but it does NOT block a
// normal home connection. So for a YouTube link we first fetch the
// video right here in the visitor's browser — the request comes from
// their home IP — then upload the downloaded file to the server for
// clipping. If the browser cannot fetch it (e.g. the browser blocks
// the request), we return null and the caller falls back to the
// server-side fetch. No login, no cookies, no API key of ours: the
// key below is YouTube's own public web-client key, the same one its
// own pages use.
const YT_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8";
function ytVideoId(url) {
  const m = String(url || "").match(/(?:youtube\.com\/(?:watch\?[^#]*v=|live\/|shorts\/|embed\/|v\/)|youtu\.be\/)([A-Za-z0-9_-]{11})/);
  return m ? m[1] : null;
}
async function fetchYoutubeInBrowser(url, onNote) {
  const vid = ytVideoId(url);
  if (!vid) return null;
  onNote("Fetching the video in your browser (your home connection)...");
  let data;
  try {
    const r = await fetch(`https://www.youtube.com/youtubei/v1/player?key=${YT_API_KEY}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        context: { client: { clientName: "ANDROID", clientVersion: "20.10.38", androidSdkVersion: 30, hl: "en", gl: "US" } },
        videoId: vid,
        playbackContext: { contentPlaybackContext: { vis: 0, splay: false, lact: [] } },
      }),
    });
    if (!r.ok) return null;
    data = await r.json();
  } catch { return null; }
  if ((data.playabilityStatus || {}).status !== "OK") return null;
  const sd = data.streamingData || {};
  const formats = [...(sd.formats || []), ...(sd.adaptiveFormats || [])];
  const prog = formats.filter(f => f.mimeType && f.mimeType.includes("video/mp4") && f.url && f.height && f.height <= 720);
  prog.sort((a, b) => (b.height || 0) - (a.height || 0));
  if (!prog.length) return null;
  onNote(`Found a ${prog[0].height}p stream, downloading it in your browser...`);
  let blob;
  try {
    const r = await fetch(prog[0].url);
    if (!r.ok) return null;
    blob = await r.blob();
  } catch { return null; }
  if (!blob || blob.size < 100000) return null;
  onNote(`Downloaded ${(blob.size / 1024 / 1024).toFixed(1)} MB in your browser, uploading it for clipping...`);
  return new File([blob], `youtube-${vid}.mp4`, { type: "video/mp4" });
}

// ---- Helper extension (installed in the visitor's browser) ----
// If the free "Simple Clipping Helper" extension is installed, it can
// fetch a YouTube video directly (an extension is allowed to contact
// youtube.com; a plain web page is not), upload it to the server, and
// hand us the job id. The page talks to the extension through
// window.postMessage; the extension signals it is present by setting
// data-sc-ext="ready" on <html>.
function extensionReady() {
  return document.documentElement.getAttribute("data-sc-ext") === "ready";
}
function fetchYoutubeViaExtension(url, onNote) {
  return new Promise((resolve) => {
    const requestId = "sc" + Date.now() + Math.random().toString(36).slice(2, 8);
    const timer = setTimeout(() => { window.removeEventListener("message", onMsg); resolve(null); }, 600000);
    function onMsg(event) {
      if (event.source !== window) return;
      const d = event.data || {};
      if (d.requestId !== requestId) return;
      if (d.type === "SC_FETCH_YOUTUBE_NOTE") { onNote(d.note); return; }
      if (d.type !== "SC_FETCH_YOUTUBE_RESULT") return;
      clearTimeout(timer);
      window.removeEventListener("message", onMsg);
      if (d.ok && d.jobId) resolve(d.jobId);
      else { if (d.error) onNote("Extension: " + d.error); resolve(null); }
    }
    window.addEventListener("message", onMsg);
    window.postMessage({ type: "SC_FETCH_YOUTUBE", url, requestId }, "*");
  });
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) { const t = await r.text(); throw new Error(t || r.statusText); }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

// ---- Views (Home / Dashboard / Library / History) ----
function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
  const el = $("view-" + name);
  if (el) el.classList.remove("hidden");
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === name));
  if (name === "dashboard") renderDashboard();
  if (name === "library") renderLibrary();
  if (name === "history") renderHistory();
  window.scrollTo({ top: 0 });
}
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => showView(t.dataset.view)));
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) showView(g.dataset.goto);
});

// ---- Shared job data (GET /api/jobs drives every view) ----
async function refreshJobs() {
  try {
    const { jobs } = await api("/api/jobs");
    allJobs = (jobs || []).slice().sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    return true;
  } catch { return false; }
}

// ---- Dashboard ----
function renderDashboard() {
  const totalClips = allJobs.reduce((n, j) => n + clipCount(j), 0);
  const processing = allJobs.filter(isActive).length;
  const done = allJobs.filter(j => j.status === "done").length;
  const failed = allJobs.filter(j => j.status === "failed" || j.status === "cancelled").length;
  $("stat-clips").textContent = totalClips;
  $("stat-projects").textContent = allJobs.length;
  $("stat-processing").textContent = processing;
  $("stat-done").textContent = done;
  $("stat-failed").textContent = failed;
  // Recent activity (latest 6 jobs, any status).
  const act = $("dash-activity");
  if (!allJobs.length) { act.innerHTML = `<p class="hint">No jobs yet. Start your first clip from Home.</p>`; }
  else {
    act.innerHTML = "";
    allJobs.slice(0, 6).forEach(j => {
      const row = document.createElement("div");
      row.className = "activity-row";
      row.innerHTML = `<span class="act-dot ${j.status}"></span>
        <div class="act-main"><div class="act-title">${esc(jobTitle(j))}</div>
        <div class="act-meta">${fmtDate(j.created_at)} · ${clipCount(j)} clips</div></div>
        <span class="pill ${j.status}">${statusText(j)}</span>`;
      row.addEventListener("click", () => openProject(j.id));
      act.appendChild(row);
    });
  }
  // Recently processed streams (latest 4 finished jobs).
  const rec = $("dash-recent");
  const finished = allJobs.filter(j => j.status === "done" && clipCount(j));
  if (!finished.length) { rec.innerHTML = `<p class="hint">No finished projects yet.</p>`; }
  else {
    rec.innerHTML = "";
    finished.slice(0, 4).forEach(j => {
      const row = document.createElement("div");
      row.className = "activity-row";
      row.innerHTML = `<span class="act-dot done"></span>
        <div class="act-main"><div class="act-title">${esc(jobTitle(j))}</div>
        <div class="act-meta">${fmtDate(j.created_at)} · ${clipCount(j)} clips</div></div>
        <span class="pill done">${clipCount(j)} clips</span>`;
      row.addEventListener("click", () => openProject(j.id));
      rec.appendChild(row);
    });
  }
}

// ---- Library ----
function libFiltered() {
  const q = ($("lib-search").value || "").trim().toLowerCase();
  const f = $("lib-filter").value;
  return allJobs.filter(j => {
    if (f === "done" && j.status !== "done") return false;
    if (f === "processing" && !isActive(j)) return false;
    if (f === "failed" && j.status !== "failed" && j.status !== "cancelled") return false;
    if (q && !(jobTitle(j).toLowerCase().includes(q) || jobUrl(j).toLowerCase().includes(q))) return false;
    return true;
  });
}
function renderLibrary() {
  const grid = $("lib-grid");
  const detail = $("lib-detail");
  if (libDetailId) { renderProjectDetail(); return; }
  detail.classList.add("hidden");
  const list = libFiltered();
  if (!list.length) {
    grid.innerHTML = `<p class="hint">${allJobs.length ? "Nothing matches your search." : "No projects yet. Start your first clip from Home."}</p>`;
    return;
  }
  grid.innerHTML = "";
  list.forEach(j => {
    const thumb = ytThumb(j);
    const card = document.createElement("button");
    card.className = "project";
    card.innerHTML = `${thumb ? `<img class="thumb" src="${thumb}" alt="" loading="lazy" onerror="this.outerHTML='<div class=&quot;thumb-fallback&quot;>✂️</div>'">` : `<div class="thumb-fallback">✂️</div>`}
      <div class="project-body">
        <h3>${esc(jobTitle(j))}</h3>
        ${jobUrl(j) ? `<p class="project-url">${esc(jobUrl(j))}</p>` : ""}
        <div class="project-meta">
          <span class="pill ${j.status}">${statusText(j)}</span>
          <span class="badge">🎬 ${clipCount(j)} clips</span>
          <span class="badge">${fmtDate(j.created_at)}</span>
          ${j.params && j.params.entire_stream ? `<span class="badge">Entire stream</span>` : ""}
        </div>
      </div>`;
    card.addEventListener("click", () => openProject(j.id));
    grid.appendChild(card);
  });
}
function openProject(id) {
  libDetailId = id;
  showView("library");
  renderProjectDetail();
}
function renderProjectDetail() {
  const j = allJobs.find(x => x.id === libDetailId);
  const detail = $("lib-detail");
  const grid = $("lib-grid");
  if (!j) { libDetailId = null; detail.classList.add("hidden"); renderLibrary(); return; }
  grid.innerHTML = "";
  detail.classList.remove("hidden");
  const thumb = ytThumb(j);
  detail.innerHTML = `
    <button class="btn ghost back-btn" id="lib-back">← Back to Library</button>
    <div class="detail-head">
      <h2>${esc(jobTitle(j))}</h2>
      ${jobUrl(j) ? `<p class="src"><a href="${esc(jobUrl(j))}" target="_blank" rel="noopener">${esc(jobUrl(j))}</a></p>` : ""}
      <div class="project-meta">
        <span class="pill ${j.status}">${statusText(j)}</span>
        <span class="badge">🎬 ${clipCount(j)} clips</span>
        <span class="badge">${fmtDate(j.created_at)}</span>
        ${j.params && j.params.entire_stream ? `<span class="badge">Entire stream</span>` : ""}
        ${j.params && (j.params.start_sec || j.params.clip_sec) ? `<span class="badge">Segment ${fmtHMS(j.params.start_sec || 0)} · ${j.params.clip_sec ? Math.round(j.params.clip_sec / 60) + " min" : ""}</span>` : ""}
      </div>
      ${j.status === "failed" && j.error ? `<p class="error">${esc(j.error)}</p>` : ""}
    </div>
    <div id="detail-clips" class="clip-grid"></div>`;
  $("lib-back").addEventListener("click", () => { libDetailId = null; renderLibrary(); });
  const host = $("detail-clips");
  if (!clipCount(j)) { host.innerHTML = `<p class="hint">No clips in this project yet${isActive(j) ? " — it is still processing." : "."}</p>`; return; }
  j.clips.forEach((c, i) => host.appendChild(clipCard(c, i, j.id)));
}
$("lib-search").addEventListener("input", () => { libDetailId = null; renderLibrary(); });
$("lib-filter").addEventListener("change", () => { libDetailId = null; renderLibrary(); });

// ---- History ----
function renderHistory() {
  const host = $("history-list");
  if (!allJobs.length) { host.innerHTML = `<p class="hint">No jobs yet. Start your first clip from Home.</p>`; return; }
  host.innerHTML = "";
  allJobs.forEach(j => {
    const row = document.createElement("div");
    row.className = "hist-row";
    row.innerHTML = `<span class="act-dot ${j.status}"></span>
      <div class="act-main"><div class="act-title">${esc(jobTitle(j))}</div>
      <div class="act-meta">${fmtDate(j.created_at)} · ${clipCount(j)} clips${j.params && j.params.entire_stream ? " · Entire stream" : ""}</div></div>
      <span class="pill ${j.status}">${statusText(j)}</span>`;
    row.addEventListener("click", () => openProject(j.id));
    host.appendChild(row);
  });
}

// ---- Clipping (unchanged flow) ----
$("start-btn").addEventListener("click", async () => {
  const errEl = $("form-error");
  errEl.classList.add("hidden");
  const link = $("link").value.trim();
  let file = $("file").files[0];
  if (!link && !file) { errEl.textContent = "Paste a video link or choose a video file first."; errEl.classList.remove("hidden"); return; }
  // Long video / livestream time range: only this segment is fetched
  // and clipped (max 30 min per job). When a range is set, skip the
  // browser-side fetch (it would download the whole video) and let
  // the server fetch just the segment.
  const startVal = ($("start") ? $("start").value : "").trim();
  const minutesVal = ($("minutes") ? $("minutes").value : "").trim();
  const hasRange = startVal !== "" || (minutesVal !== "" && +minutesVal !== 30);
  // Entire-stream mode: the server splits the whole video into
  // 30-min parts and clips each part in turn. Skip the browser-side
  // fetch (it would download the whole video) — the server fetches
  // only one part at a time.
  const entireStream = $("entire") ? $("entire").checked : false;
  $("start-btn").disabled = true;
  try {
    // YouTube links: fetch the video in THIS browser first (the
    // visitor's home IP is not blocked by YouTube). If that works,
    // upload the downloaded file; the server then clips it exactly
    // like an uploaded video. If the browser cannot fetch it, fall
    // back to the server-side link fetch. Skipped when a time range
    // or Entire Stream is set (the server fetches only the segment
    // / part instead).
    if (link && !file && ytVideoId(link) && !hasRange && !entireStream) {
      $("progress-card").classList.remove("hidden");
      $("progress-title").textContent = "Fetching the YouTube video…";
      $("progress-bar").style.width = "5%";
      $("progress-stage").textContent = "Downloading it in your browser first (your home connection is not blocked by YouTube).";
      $("progress-log").textContent = "";
      const note = (s) => { $("progress-log").textContent += s + "\n"; $("progress-log").scrollTop = 1e9; };
      // 1) Helper extension (if installed): it fetches the video with
      // your home IP and uploads it, returning a job id. If it works,
      // the job already exists — go straight to polling it.
      if (extensionReady()) {
        note("Helper extension found — fetching the video through it...");
        const jobId = await fetchYoutubeViaExtension(link, note);
        if (jobId) {
          currentJob = jobId;
          $("progress-title").textContent = "Clipping…";
          $("progress-bar").style.width = "0%";
          $("progress-stage").textContent = "The video was fetched in your browser — clipping now.";
          $("results").innerHTML = "";
          poll();
          $("start-btn").disabled = false;
          return;
        }
        note("Extension could not fetch it — trying the browser directly.");
      }
      // 2) Direct browser fetch (works only if YouTube allows it).
      const fetched = await fetchYoutubeInBrowser(link, note);
      if (fetched) {
        file = fetched;
        note("Browser fetch worked — uploading the video for clipping.");
      } else {
        note("Browser fetch did not work here — trying the server instead.");
      }
    }
    const fd = new FormData();
    if (file) fd.append("upload", file);
    else if (link) fd.append("link", link);
    if (hasRange) {
      if (startVal) fd.append("start", startVal);
      if (minutesVal) fd.append("minutes", minutesVal);
    }
    if (entireStream) fd.append("entire", "1");
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
      if (j.status === "done") { clearInterval(pollTimer); showResults(j); refreshAll(); }
      else if (j.status === "failed" || j.status === "cancelled") {
        clearInterval(pollTimer);
        $("progress-title").textContent = j.status === "cancelled" ? "Cancelled." : "Something went wrong.";
        $("progress-stage").textContent = j.error || "";
        refreshAll();
      }
    } catch { /* keep polling */ }
  }, 3000);
}

function clipCard(c, i, jid) {
  const first = c.files[0];
  const div = document.createElement("div");
  div.className = "clip";
  // Entire-stream clips carry their part number + absolute time in
  // the video (e.g. Part 3 · 1:05:00) so the user can find the moment.
  const partBadge = c.part ? `<span class="badge">Part ${c.part}</span>` : "";
  const atBadge = (c.abs_start != null) ? `<span class="badge">@ ${fmtHMS(c.abs_start)}</span>` : "";
  div.innerHTML = `
    <h3>Clip ${i + 1}: ${esc(c.title || c.type || "Moment")}</h3>
    <p class="hook">${esc(c.hook || "")}</p>
    <div class="badges">
      <span class="badge">⭐ ${c.score || "?"} / 100</span>
      <span class="badge">⏱ ${c.seconds || "?"} sec</span>
      <span class="badge">${esc(c.type || "")}</span>
      ${partBadge}${atBadge}
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

// Home "Recent clips": the 3 newest finished jobs (full list lives in Library).
async function loadHistory() {
  const host = $("history");
  const done = allJobs.filter(j => j.status === "done" && clipCount(j)).slice(0, 3);
  if (!done.length) { host.innerHTML = `<p class="hint">No clips yet. Paste a link above and press Start Clipping.</p>`; return; }
  host.innerHTML = "";
  done.forEach(j => {
    const sec = document.createElement("div");
    sec.className = "history-job";
    sec.innerHTML = `<h3>${esc(jobTitle(j))}</h3><p class="hint">${fmtDate(j.created_at)} — ${j.clips.length} clips</p>`;
    j.clips.forEach((c, i) => sec.appendChild(clipCard(c, i, j.id)));
    host.appendChild(sec);
  });
}

// Refresh every view's data after a job finishes, and re-render the open view.
async function refreshAll() {
  await refreshJobs();
  loadHistory();
  const open = document.querySelector(".tab.active");
  const name = open ? open.dataset.view : "home";
  if (name === "dashboard") renderDashboard();
  if (name === "library") renderLibrary();
  if (name === "history") renderHistory();
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

// First load: fetch jobs once, then paint Home's recent clips.
refreshJobs().then(() => loadHistory());
