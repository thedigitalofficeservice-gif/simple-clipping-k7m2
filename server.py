#!/usr/bin/env python3
"""Simple Clipping — private-by-link stream clipper (my own code).

Paste a video link, press Start Clipping, get 6 vertical clips.
No login, no email, no password, no registration. Anyone with the link can
open the app, so share the link only with people you trust.

Finished clips are saved on the server for 7 days and listed under
"Your clips", so you can watch and download them later, including on mobile
(the Download button saves the MP4 to the phone).

The clipping itself is done by the BeyondGTAClips engine (official repo,
fetched at image build time), called as a subprocess. No engine code is
copied, rebranded or sold. Moments are picked by the engine's signal picker
(loudness + chat + screen activity + Whisper transcript) — no cloud AI, no
API keys. Two minimal build-time patches keep it inside Railway's 1 GB
(see Dockerfile + apply-720p-canvas.py + apply-frame-float32.py): a 720p
render canvas and float32 frame-analysis means; the pipeline, picker,
captions and SFX are untouched. The engine runs in TWO phases (phase1.py
does download/listen/pick/prepare with the render stubbed out, then the
engine's own `remake` re-renders in a fresh process) because the
single-process run peaks at ~777 MB and the render's ffmpeg is OOM-killed
on 1 GB; Phase 1 peaks at ~611 MB, Phase 2 at ~328 MB.

YouTube URLs are fetched to a local file FIRST (yt_fetch.py) and then
handed to the engine like an upload, because YouTube serves a sign-in/bot
wall to the engine's yt-dlp on Railway's datacenter IP. The YouTube Data
API cannot fix that — an API key only returns metadata, never the video
file. yt_fetch tries cobalt, Invidious, then yt-dlp's TV client, and the
first one that produces a real file wins. Non-YouTube URLs are still
passed to the engine directly, unchanged.

Routes:
  GET  /                        -> simple frontend
  GET  /healthz                 -> liveness
  POST /api/jobs                -> start a clipping job (form: link or file upload)
  GET  /api/jobs                -> list of jobs (newest first)
  GET  /api/jobs/{id}           -> job status + progress + log tail
  POST /api/jobs/{id}/cancel
  GET  /api/jobs/{id}/file/{name} -> preview/download a finished clip

Jobs run one at a time. Job files are deleted after JOB_TTL_HOURS.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

try:
    import yt_fetch
except ImportError:  # pragma: no cover - yt_fetch.py ships with the app
    yt_fetch = None

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
ENGINE_DIR = Path(os.environ.get("ENGINE_DIR", "/opt/beyondgtaclips")).resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
ENGINE_HOME = DATA_DIR / "engine-home"          # BEYONDGTACLIPS_HOME for the engine
ENGINE_HOME.mkdir(parents=True, exist_ok=True)
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

JOB_TTL_HOURS = float(os.environ.get("JOB_TTL_HOURS", "168"))   # 7 days
MAX_JOBS_BYTES = int(os.environ.get("MAX_JOBS_BYTES", str(2 * 1024 ** 3)))  # 2 GB
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
ALLOWED_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg", ".avi"}

# One fixed recipe: 6 short clips, auto style, clean-ish captions.
RECIPE = {"clips": 6, "length": "short", "style": "auto",
          "captions": "slurs", "topic": "Gaming", "focus": ""}

# Long-video time range: max 30 minutes per job (the free 1 GB server
# cannot process multi-hour livestreams whole — it runs out of memory).
MAX_CLIP_MINUTES = 30


def _parse_time(s):
    """Parse a start time into seconds. Accepts HH:MM:SS, MM:SS or plain
    seconds (e.g. "1:00:00", "90", "1:30"). Returns 0 on empty/invalid."""
    s = (s or "").strip()
    if not s:
        return 0
    try:
        parts = s.split(":")
        if len(parts) == 1:
            return max(0, int(float(parts[0])))
        if len(parts) == 2:
            return max(0, int(parts[0]) * 60 + int(float(parts[1])))
        if len(parts) == 3:
            return max(0, int(parts[0]) * 3600 + int(parts[1]) * 60
                       + int(float(parts[2])))
    except (ValueError, IndexError):
        return 0
    return 0

app = FastAPI(title="Simple Clipping")
jobs = {}              # id -> job dict
jobs_lock = threading.Lock()
queue = []             # job ids waiting for the worker
queue_cond = threading.Condition()
current_proc = {}      # job id -> subprocess.Popen (for cancel)

STAGE_MAP = [
    ("look up", ("Finding the video", 5)),
    ("downloading the audio", ("Downloading audio", 15)),
    ("download audio", ("Downloading audio", 15)),
    ("looked at the whole stream", ("Watching the stream", 25)),
    ("measuring loudness", ("Listening for loud moments", 35)),
    ("listening to", ("Transcribing the hot parts", 45)),
    ("heard ", ("Transcribing the hot parts", 45)),
    ("picked ", ("Picking the best moments", 60)),
    ("clip 1:", ("Cutting and editing clips", 65)),
    ("clip 2:", ("Cutting and editing clips", 72)),
    ("clip 3:", ("Cutting and editing clips", 79)),
    ("clip 4:", ("Cutting and editing clips", 86)),
    ("clip 5:", ("Cutting and editing clips", 92)),
    ("clip 6:", ("Cutting and editing clips", 95)),
    ("saved ", ("Finishing clips", 96)),
    ("done:", ("Done", 100)),
]


def _save_manifest(job):
    """Persist the job to disk so saved clips survive a server restart."""
    try:
        (JOBS_DIR / job["id"] / "job.json").write_text(
            json.dumps(_job_public(job), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_manifests():
    """Reload saved jobs from disk on startup (for the Your clips list)."""
    for d in JOBS_DIR.iterdir():
        if not d.is_dir():
            continue
        mf = d / "job.json"
        if not mf.exists():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            if data.get("status") in ("queued", "running"):
                data["status"] = "failed"
                data["stage"] = "Failed"
                data["error"] = "The server restarted while this job was running."
            data["created_ts"] = d.stat().st_mtime
            data.setdefault("log", [])
            jobs[data["id"]] = data
        except Exception:
            continue


def _job_public(j):
    return {
        "id": j["id"], "status": j["status"], "stage": j["stage"],
        "progress": j["progress"], "created_at": j["created_at"],
        "params": j["params"], "error": j.get("error"),
        "clips": j.get("clips"), "posting": j.get("posting"),
        "log": j["log"][-60:],
    }


def _note(job, line, cap=None):
    line = line.rstrip("\n")
    if not line.strip():
        return
    job["log"].append(line[-500:])
    low = line.lower()
    for key, (stage, pct) in STAGE_MAP:
        if key in low:
            if cap is not None and pct > cap:
                # Phase 1 must never show "Done"/100% — the render
                # (Phase 2) is still to come.
                pct = cap
                if key == "done:":
                    stage = "Moments picked — rendering clips"
            job["stage"] = stage
            job["progress"] = max(job["progress"], pct)
            break


def _run_cmd(job, cmd, env, cwd, cap=None):
    """Run one engine subprocess, streaming its output to the job log.
    Returns the exit code, or None if the user cancelled the job."""
    jid = job["id"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            cwd=str(cwd), env=env, start_new_session=True)
    current_proc[jid] = proc
    try:
        for line in proc.stdout:
            _note(job, line, cap=cap)
            if job.get("cancel_requested"):
                break
        if job.get("cancel_requested"):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            proc.wait(timeout=30)
            job["status"] = "cancelled"
            job["stage"] = "Cancelled"
            return None
        return proc.wait()
    finally:
        current_proc.pop(jid, None)


def _job_size(jid):
    total = 0
    for p in (JOBS_DIR / jid).rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def _cleanup_old_jobs():
    cutoff = time.time() - JOB_TTL_HOURS * 3600
    with jobs_lock:
        for jid, j in list(jobs.items()):
            if j["created_ts"] < cutoff and j["status"] in ("done", "failed", "cancelled"):
                shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
                jobs.pop(jid, None)
        # Disk guard: keep the newest finished jobs, drop the oldest first.
        finished = sorted(
            (j for j in jobs.values() if j["status"] in ("done", "failed", "cancelled")),
            key=lambda j: j["created_ts"])
        total = sum(_job_size(j["id"]) for j in finished)
        for j in finished:
            if total <= MAX_JOBS_BYTES:
                break
            total -= _job_size(j["id"])
            shutil.rmtree(JOBS_DIR / j["id"], ignore_errors=True)
            jobs.pop(j["id"], None)
    try:
        env = dict(os.environ, BEYONDGTACLIPS_HOME=str(ENGINE_HOME))
        subprocess.run([sys.executable, str(ENGINE_DIR / "scripts" / "beyondgtaclips.py"),
                        "prune", "--days", "7"], capture_output=True, timeout=120, env=env)
    except Exception:
        pass
    # Orphaned job folders (no manifest, e.g. from an older version): drop
    # them once they are older than the TTL.
    cutoff = time.time() - JOB_TTL_HOURS * 3600
    for d in JOBS_DIR.iterdir():
        if d.is_dir() and d.name not in jobs and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _sweeper():
    while True:
        time.sleep(3600)
        try:
            _cleanup_old_jobs()
        except Exception:
            pass


def _find_output(start_ts, topic):
    """Newest engine output folder (with clips.json) created after the job started."""
    day = datetime.now().date().isoformat()
    base = ENGINE_HOME / day / re.sub(r"[^a-z0-9-]+", "-", topic.lower().strip())[:30]
    best = None
    if base.exists():
        for d in base.iterdir():
            cj = d / "clips.json"
            if d.is_dir() and cj.exists() and cj.stat().st_mtime >= start_ts - 5:
                if best is None or cj.stat().st_mtime > (best / "clips.json").stat().st_mtime:
                    best = d
    return best


def _run_job(job):
    jid = job["id"]
    job_dir = JOBS_DIR / jid
    p = job["params"]
    start_ts = time.time()
    local_server = None
    try:
        job["status"] = "running"
        job["stage"] = "Starting"
        job["progress"] = 2
        # Caption + render settings live in the engine's settings.json
        # (per-job, jobs run one at a time). Low-memory settings for the
        # free 1GB Railway server: Whisper "tiny" for captions (instead of
        # "base"), 720p source slices, a faster x264 preset and no
        # punch-in zooms. The render canvas itself is 1080x1920 by the
        # engine's design (scaled to 720x1280 by the build-time patch).
        # These are the engine's own documented settings.json knobs.
        settings_path = ENGINE_HOME / "settings.json"
        settings_path.write_text(json.dumps({"clean_captions": p["captions"],
                                             "caption_model": "tiny",
                                             "video_height": 720,
                                             "width": 720,
                                             "height": 1280,
                                             "loudness": None,
                                             "x264_preset": "veryfast",
                                             "punch_ins": False}), encoding="utf-8")
        if p.get("source_type") == "upload":
            # Serve the uploaded file on localhost and hand the engine an
            # http(s) URL, because yt-dlp (the engine's downloader) fetches by URL.
            from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
            from functools import partial
            handler = partial(SimpleHTTPRequestHandler, directory=str(job_dir))
            local_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=local_server.serve_forever, daemon=True).start()
            port = local_server.server_address[1]
            source = f"http://127.0.0.1:{port}/{p['source_file']}"
        elif yt_fetch is not None and yt_fetch.is_youtube_url(p.get("url", "")):
            # YouTube is walled to the engine's yt-dlp on Railway's IP, so
            # fetch the video to a local file first (yt_fetch.py) and hand
            # the engine that file, exactly like an upload. The engine,
            # the recipe and the two-phase split are untouched.
            _note(job, "Fetching the YouTube video (YouTube blocks this "
                       "server's direct download, so fetching via a "
                       "downloader service first)...")
            try:
                _fpath, _title = yt_fetch.fetch_youtube(
                    p["url"], job_dir, lambda line: _note(job, line),
                    p.get("start_sec", 0), p.get("clip_sec"))
            except yt_fetch.FetchError as exc:
                job["status"] = "failed"
                job["stage"] = "Failed"
                job["error"] = str(exc)
                return
            from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
            from functools import partial
            handler = partial(SimpleHTTPRequestHandler, directory=str(job_dir))
            local_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=local_server.serve_forever, daemon=True).start()
            port = local_server.server_address[1]
            source = f"http://127.0.0.1:{port}/source.mp4"
        else:
            source = p["url"]
        # TWO PHASES (see phase1.py + Dockerfile header): the single-process
        # run peaks at ~777 MB and the render's ffmpeg is OOM-killed on
        # Railway's 1 GB. Phase 1 does download/listen/pick/prepare with the
        # render stubbed out (peak ~611 MB); Phase 2 (the engine's own
        # `remake`) re-renders in a fresh process (peak ~328 MB). Same
        # moments, same templates, same 720p clips — just split in two.
        env = dict(os.environ, BEYONDGTACLIPS_HOME=str(ENGINE_HOME),
                   ENGINE_DIR=str(ENGINE_DIR), PYTHONIOENCODING="utf-8")
        clip_args = ["clip", source,
                     "--total", str(p["clips"]),
                     "--styles", p["style"],
                     "--length", p["length"],
                     "--brain", "none",
                     "--topic", p["topic"]]
        _note(job, "Running the clipping engine (phase 1: finding moments)...")
        code1 = _run_cmd(job, [sys.executable, str(BASE / "phase1.py"), *clip_args],
                         env, ENGINE_DIR, cap=60)
        if code1 is None:  # cancelled
            return
        if code1 != 0:
            tail = "\n".join(job["log"][-12:])
            job["status"] = "failed"
            job["stage"] = "Failed"
            job["error"] = ("Phase 1 (finding moments) stopped without finished clips "
                            f"(exit {code1}). Last output:\n{tail}")
            return
        outdir = _find_output(start_ts, p["topic"])
        if outdir is None:
            tail = "\n".join(job["log"][-12:])
            job["status"] = "failed"
            job["stage"] = "Failed"
            job["error"] = ("Phase 1 finished but no clip folder was found. "
                            f"Last output:\n{tail}")
            return
        _note(job, "Moments picked — rendering clips (phase 2)...")
        code2 = _run_cmd(job, [sys.executable,
                               str(ENGINE_DIR / "scripts" / "beyondgtaclips.py"),
                               "remake", str(outdir)],
                         env, ENGINE_DIR)
        if code2 is None:  # cancelled
            return
        if code2 != 0:
            tail = "\n".join(job["log"][-12:])
            job["status"] = "failed"
            job["stage"] = "Failed"
            job["error"] = ("Phase 2 (rendering clips) stopped without finished clips "
                            f"(exit {code2}). Last output:\n{tail}")
            return
        data = json.loads((outdir / "clips.json").read_text(encoding="utf-8"))
        clips = []
        for c in data.get("clips", []):
            files = []
            for f in c.get("files", []):
                src = outdir / f["file"]
                if src.exists():
                    dst = job_dir / f["file"]
                    shutil.copy2(src, dst)
                    files.append({"file": f["file"], "template": f["template"],
                                  "url": f"/api/jobs/{jid}/file/{f['file']}",
                                  "bytes": src.stat().st_size})
            clips.append({"title": c.get("title"), "hook": c.get("hook"),
                          "score": c.get("score"), "seconds": c.get("seconds"),
                          "type": c.get("type"), "style": c.get("style"),
                          "caption": c.get("caption"), "hashtags": c.get("hashtags"),
                          "files": files})
        posting = (outdir / "POSTING.md").read_text(encoding="utf-8", errors="replace") \
            if (outdir / "POSTING.md").exists() else ""
        job["clips"] = clips
        job["posting"] = posting
        job["status"] = "done" if clips else "failed"
        job["stage"] = "Done" if clips else "Failed"
        job["progress"] = 100 if clips else job["progress"]
        if not clips:
            job["error"] = "The engine finished but produced no clips."
    except Exception as exc:  # noqa: BLE001 - surface as a failed job, never crash the server
        job["status"] = "failed"
        job["stage"] = "Failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if local_server:
            local_server.shutdown()
        _save_manifest(job)
        current_proc.pop(jid, None)


def _worker():
    while True:
        with queue_cond:
            while not queue:
                queue_cond.wait()
            jid = queue.pop(0)
        job = jobs.get(jid)
        if job:
            _run_job(job)


@app.on_event("startup")
def _startup():
    _load_manifests()
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_sweeper, daemon=True).start()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/jobs")
async def create_job(link: str = Form(""), upload: UploadFile = File(None),
                     start: str = Form(""), minutes: str = Form("")):
    jid = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    params = dict(RECIPE)
    # Optional time range (for long videos/livestreams): only this
    # segment is fetched and clipped. `start` is HH:MM:SS, MM:SS or
    # seconds; `minutes` is how many minutes to clip (1-30, default 30).
    # Only stored when the user set a start time or a non-default
    # length, so short videos keep the current behaviour.
    start_sec = _parse_time(start)
    try:
        clip_min = int(float((minutes or "").strip())) if (minutes or "").strip() else 30
    except ValueError:
        clip_min = 30
    clip_min = max(1, min(MAX_CLIP_MINUTES, clip_min))
    ranged = bool((start or "").strip()) or (minutes or "").strip() != ""
    if ranged:
        params.update(start_sec=start_sec, clip_sec=clip_min * 60)
    if upload is not None and upload.filename:
        ext = Path(upload.filename).suffix.lower()
        if ext not in ALLOWED_EXTS:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(400, f"unsupported file type {ext or '(none)'}")
        fname = f"source{ext}"
        dest = job_dir / fname
        size = 0
        with dest.open("wb") as fh:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    dest.unlink(missing_ok=True)
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise HTTPException(400, f"file over {MAX_UPLOAD_MB} MB")
                fh.write(chunk)
        # The engine refuses sources under ~1 minute. Fail fast with a
        # clear message instead of running the engine for minutes.
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", str(dest)],
                               capture_output=True, text=True, timeout=60)
        try:
            dur = float(probe.stdout.strip())
        except ValueError:
            dur = 0
        if dur and dur < 65:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                400, f"video is only {dur:.0f} seconds long — it must be at least "
                     "1 minute long for the engine to find clips")
        params.update(source_type="upload", source_file=fname,
                      source_name=upload.filename)
    elif (link or "").strip():
        link = link.strip()
        if not re.match(r"^https?://", link):
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(400, "paste a full http(s) video link")
        params.update(source_type="url", url=link)
    else:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "paste a video link or upload a video file")
    job = {"id": jid, "status": "queued", "stage": "Queued", "progress": 0,
           "created_at": datetime.now().isoformat(timespec="seconds"),
           "created_ts": time.time(), "params": params, "log": [], "clips": None,
           "posting": None}
    with jobs_lock:
        jobs[jid] = job
    _save_manifest(job)
    with queue_cond:
        queue.append(jid)
        queue_cond.notify()
    return {"id": jid, "status": "queued"}


@app.get("/api/jobs")
async def list_jobs():
    with jobs_lock:
        js = sorted(jobs.values(), key=lambda j: j["created_ts"], reverse=True)
    return {"jobs": [_job_public(j) for j in js]}


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "no such job")
    return _job_public(job)


@app.post("/api/jobs/{jid}/cancel")
async def cancel_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "no such job")
    if job["status"] in ("done", "failed", "cancelled"):
        return {"status": job["status"]}
    job["cancel_requested"] = True
    proc = current_proc.get(jid)
    if proc:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass
    return {"status": "cancelling"}


@app.get("/api/jobs/{jid}/file/{name}")
async def job_file(jid: str, name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad file name")
    path = JOBS_DIR / jid / name
    if not path.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(path, filename=name)


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
