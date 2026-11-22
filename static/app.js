const $ = (id) => document.getElementById(id);
let currentJob = null, pollTimer = null;

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

$("start-btn").addEventListener("click", async () => {
  const errEl = $("form-error");
  errEl.classList.add("hidden");
  const link = $("link").value.trim();
  let file = $("file").files[0];
  if (!link && !file) { errEl.textContent = "Paste a video link or choose a video file first."; errEl.classList.remove("hidden"); return; }
  $("start-btn").disabled = true;
  try {
    // YouTube links: fetch the video in THIS browser first (the
    // visitor's home IP is not blocked by YouTube). If that works,
    // upload the downloaded file; the server then clips it exactly
    // like an uploaded video. If the browser cannot fetch it, fall
    // back to the server-side link fetch.
    if (link && !file && ytVideoId(link)) {
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
