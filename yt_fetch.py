#!/usr/bin/env python3
"""Fetch a YouTube video to a local file, from a datacenter IP.

DIAGNOSIS (22 Nov 2026, evidence in the job logs + live tests):
YouTube does NOT permanently block the server's IP. It rate-limits the
InnerTube player API (youtubei/v1/player) per IP + per player client.
When the quota is exhausted the API returns a 200 with
playabilityStatus LOGIN_REQUIRED ("Sign in to confirm you're not a
bot") and NO stream URLs — not an HTTP 403 or 429, not a CAPTCHA, and
the watch page itself still loads (HTTP 200) but its embedded player
response is the same LOGIN_REQUIRED stub with zero formats. The quota
recovers after a short cooldown (~2-3 min): the same video, tool and
client failed at 12:32 PKT and succeeded at 12:35 PKT on this VM, then
failed again immediately after the success (quota spent), proving it
is a tight per-client rate limit, not a permanent ban. Railway's shared
egress IPs are used by thousands of apps, so their quota is almost
always exhausted — that is why yt-dlp fails consistently there, while
the less-shared VM IP works intermittently.

So the fix is NOT another server, browser, or extension. It is:
use MANY player clients, because the limit is per IP + per client —
a rarely-used client may still have quota when the popular ones are
exhausted. The fetch tries each client once (a failed attempt answers
in seconds, so the whole pass takes ~1-2 min); if none works, the
least-used client (visionos) is retried with backoff so its quota can
recover. No rapid-fire attempts on one client (each failed attempt
spends quota).

The YouTube Data API cannot fix this: an API key only returns
metadata (title, duration) — it never returns the video file or a
stream URL. And "caching" cannot fix it either: caching only helps
once a video has been downloaded, and the download itself is what is
blocked. No API-key / Data-API / video-cache solution exists anywhere
in this project's code or history (verified by full search) — it was
an idea discussed, never implemented. The only caches in the engine
are intermediate results (meta.json, storyboard.npz, chat.json) that
only help AFTER a download.

UPDATE (22 Nov 2026): authenticated requests bypass the wall —
YouTube trusts a logged-in session, so the rate limit no longer
applies. A SEPARATE throwaway YouTube account ("Simple Clipper",
created only for this, never the user's main account) is used, under
the user's 8 conditions: cookies only for fetching + clipping, never
in the frontend / repo / logs, backend-only, no other activity, delete
after use. Its cookies.txt (Netscape format, exported by the user) is
stored base64-encoded in the YTDLP_COOKIES_B64 env var on the server
only — never in the repo or chat. At runtime it is decoded to a 0600
temp file and passed to yt-dlp with --cookies. No API key, no payment.
The downloaded file is capped at MAX_FETCH_MB (default 500 MB) and
requested at 720p or below, because the engine renders 720p clips anyway.
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Player clients to try, least-used first. YouTube rate-limits the
# player API per IP + per player client, so a rarely-used client may
# still have quota when the popular ones are exhausted. Each client is
# tried once (a failed attempt answers in seconds); if none works,
# visionos is retried with backoff so its quota can recover.
YT_CLIENTS = ["visionos", "tv_embedded", "web_embedded", "web_safari",
              "tv", "ios", "android", "web"]

# Retry delays (seconds) after a bot-wall / LOGIN_REQUIRED answer.
# The quota recovers after ~2-3 min; spaced retries let it come back.
# Longer processing wait is acceptable (user's own terms).
RETRY_DELAYS = [60, 90, 120]
BOT_WALL_MARKERS = ("Sign in to confirm", "LOGIN_REQUIRED", "not a bot")

MAX_FETCH_MB = int(os.environ.get("MAX_FETCH_MB", "500"))
MAX_BYTES = MAX_FETCH_MB * 1024 * 1024

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"}

# cobalt instances (public, free, no key). Main first, then fallbacks.
COBALT_INSTANCES = [
    "https://co.api/json",
    "https://co.wukko.fun/api/json",
]

# Public Invidious instances. Several are often down or bot-walled;
# the loop tries each one until one answers with stream URLs.
INVIDIOUS_INSTANCES = [
    "https://yewtu.be",
    "https://inv.nadeko.net",
    "https://invidious.privacyredirect.com",
    "https://iv.datura.network",
    "https://invidious.0011.lt",
]

YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|live/|shorts/|embed/|v/)|"
    r"youtu\.be/)([A-Za-z0-9_-]{11})")


def is_youtube_url(url):
    return bool(YOUTUBE_RE.search(url or ""))


def video_id(url):
    m = YOUTUBE_RE.search(url or "")
    return m.group(1) if m else None


def _cookies_file():
    """Decode YTDLP_COOKIES_B64 (base64 Netscape cookies.txt) to a 0600
    temp file and return its path, or None if not configured / invalid.

    The env var holds the separate throwaway account's cookies only.
    The decoded file lives in /tmp (never in the repo) and is only
    passed to yt-dlp via --cookies. Returns None on any problem so
    the caller falls back to the unauthenticated multi-client loop.
    """
    b64 = os.environ.get("YTDLP_COOKIES_B64", "").strip()
    if not b64:
        return None
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if b"# Netscape HTTP Cookie File" not in data[:200]:
        return None
    path = Path("/tmp") / "yt-cookies.txt"
    try:
        path.write_bytes(data)
        os.chmod(path, 0o600)
    except Exception:
        return None
    return path


class FetchError(Exception):
    pass


def _http_json(method, url, payload=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        **UA, "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise FetchError(f"{url} -> HTTP {e.code}")
    except Exception as e:  # noqa: BLE001 - report as a fetch failure
        raise FetchError(f"{url} -> {type(e).__name__}: {e}")


def _fmt_hms(sec):
    """Format seconds as HH:MM:SS (for yt-dlp --download-sections)."""
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _download_segment(url, dest, start_sec, duration_sec, note):
    """Extract one time segment from a stream URL with ffmpeg.

    Uses -ss before -i so ffmpeg seeks (HTTP Range) instead of
    downloading the whole file — essential for multi-hour livestreams.
    -c copy keeps it fast; the output may start at the previous
    keyframe (a few seconds early), which is fine for clipping.
    Raises FetchError on failure so the caller falls back to yt-dlp.
    """
    import subprocess
    tmp = dest.with_suffix(".seg.mp4")
    if tmp.exists():
        tmp.unlink()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-ss", str(int(start_sec)), "-i", url,
           "-t", str(int(duration_sec)), "-c", "copy",
           "-movflags", "+faststart", "-y", str(tmp)]
    note(f"fetching {duration_sec // 60} min segment from "
         f"{_fmt_hms(start_sec)} ...")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:  # noqa: BLE001 - report as a fetch failure
        raise FetchError(f"segment extract failed: {type(e).__name__}: {e}")
    if proc.returncode != 0 or not tmp.exists() \
            or tmp.stat().st_size < 100_000:
        if tmp.exists():
            tmp.unlink()
        err = (proc.stderr or "").strip().splitlines()[-1:] 
        raise FetchError("segment extract failed: "
                         + (" | ".join(err)[:200] if err else "ffmpeg error"))
    tmp.replace(dest)
    note(f"segment downloaded {dest.stat().st_size / 1024 / 1024:.1f} MB")
    return dest.stat().st_size


def _download(url, dest, note):
    """Stream a URL to dest, aborting over MAX_BYTES. Returns bytes written."""
    req = urllib.request.Request(url, headers=UA)
    written = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise FetchError(
                            f"video is over {MAX_FETCH_MB} MB — too large for "
                            "the free server (try a shorter video)")
                    fh.write(chunk)
    except FetchError:
        raise
    except Exception as e:  # noqa: BLE001 - report as a fetch failure
        raise FetchError(f"download failed: {type(e).__name__}: {e}")
    if written < 100_000:
        raise FetchError(f"downloaded only {written} bytes — not a real video")
    note(f"downloaded {written / 1024 / 1024:.1f} MB")
    return written


def _cobalt(url, dest, note):
    """Method 1: cobalt downloader API."""
    for api in COBALT_INSTANCES:
        try:
            note(f"trying cobalt ({api}) ...")
            resp = _http_json("POST", api,
                              {"url": url, "videoQuality": "720",
                               "downloadMode": "auto"}, timeout=45)
        except FetchError as e:
            note(f"cobalt {api} failed: {e}")
            continue
        status = resp.get("status")
        if status == "error":
            err = resp.get("error", {})
            note(f"cobalt {api} error: {err.get('code') or err}")
            continue
        if status == "picker":
            items = [p for p in resp.get("picker", []) if p.get("type") == "video"]
            if not items:
                note(f"cobalt {api}: picker had no video item")
                continue
            dl = items[0].get("url")
        else:
            dl = resp.get("url")
        if not dl:
            note(f"cobalt {api}: no download URL in response ({status})")
            continue
        note(f"cobalt {api} gave a download link, fetching ...")
        _download(dl, dest, note)
        return resp.get("filename") or ""
    raise FetchError("cobalt: all instances failed")


def _invidious(vid, dest, note):
    """Method 2: Invidious API — direct googlevideo.com stream URLs."""
    for base in INVIDIOUS_INSTANCES:
        try:
            note(f"trying Invidious ({base}) ...")
            d = _http_json("GET", f"{base}/api/v1/videos/{vid}", timeout=30)
        except FetchError as e:
            note(f"Invidious {base} failed: {e}")
            continue
        streams = [f for f in (d.get("formatStreams") or [])
                   if f.get("url") and f.get("container") == "mp4"]
        if not streams:
            note(f"Invidious {base}: no mp4 streams returned")
            continue
        # Best mp4 at 720p or below (the engine renders 720p anyway).
        def _h(f):
            try:
                return int((f.get("qualityLabel") or "0p").rstrip("p"))
            except ValueError:
                return 0
        streams.sort(key=_h, reverse=True)
        pick = next((f for f in streams if _h(f) <= 720), streams[-1])
        note(f"Invidious {base} gave a {pick.get('qualityLabel')} stream, fetching ...")
        _download(pick["url"], dest, note)
        return d.get("title") or ""
    raise FetchError("Invidious: all instances failed")


def _ytdlp(url, dest, note, client, retries=True, cookies=None, section=None):
    """The engine's own yt-dlp, with a chosen player client.

    Retries with backoff when YouTube answers with the bot wall
    (LOGIN_REQUIRED / "Sign in to confirm you're not a bot"): that is
    the per-client rate limit, and it recovers after a short cooldown.
    A retry right away would just spend quota again, so we wait.
    When `cookies` (a Netscape cookies.txt path) is given, it is
    passed via --cookies: an authenticated session bypasses the wall.
    When `section` is set (e.g. "*00:00:00-00:30:00"), only that time
    range is downloaded — for multi-hour livestreams.
    """
    packages = Path.home() / ".beyondgtaclips" / "packages"
    env = dict(os.environ)
    if packages.exists():
        env["PYTHONPATH"] = str(packages) + os.pathsep + env.get("PYTHONPATH", "")
    # --merge-output-format mp4 forces the merged file to be exactly
    # <dest> (without it, a webm audio track makes yt-dlp write
    # <dest>.webm instead, and the caller would think the fetch failed).
    args = [sys.executable, "-m", "yt_dlp", "--no-check-certificates",
            "--no-playlist", "--no-warnings",
            "-f", "bv[height<=720]+ba/b[height<=720]/b",
            "--merge-output-format", "mp4",
            "-o", str(dest), url]
    if client:
        args[4:4] = ["--extractor-args", f"youtube:player_client={client}"]
    if section:
        # Insert --download-sections just before the URL (last element).
        args[-1:-1] = ["--download-sections", section]
    if cookies:
        # Insert --cookies just before the URL (last element).
        args[-1:-1] = ["--cookies", str(cookies)]
    label = f"yt-dlp ({client or 'default'})"
    delays = list(RETRY_DELAYS) if retries else []
    attempt = 0
    while True:
        attempt += 1
        note(f"trying {label} (attempt {attempt}) ...")
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=600, env=env)
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        # yt-dlp may still append an extension (e.g. source.mp4.webm) if the
        # merge format flag is ignored; accept any file it actually wrote.
        actual = dest if dest.exists() else None
        if actual is None:
            for cand in sorted(dest.parent.glob(dest.name + ".*")):
                if cand.is_file() and cand.stat().st_size >= 100_000:
                    actual = cand
                    break
        if proc.returncode == 0 and actual is not None \
                and actual.stat().st_size >= 100_000:
            if actual != dest:
                actual.replace(dest)
            note(f"{label} downloaded "
                 f"{dest.stat().st_size / 1024 / 1024:.1f} MB")
            return ""
        err = " | ".join(tail)[:300]
        is_wall = any(m in err for m in BOT_WALL_MARKERS)
        if is_wall and delays:
            wait = delays.pop(0)
            note(f"{label} hit YouTube's rate limit (bot wall) — "
                 f"waiting {wait}s for the quota to recover, then retrying ...")
            time.sleep(wait)
            if dest.exists():
                dest.unlink()
            continue
        raise FetchError(f"{label} failed: {err}")


def _worker_relay(vid, dest, note, start_sec=0, clip_sec=None):
    """Ask the Cloudflare Worker for stream URLs, download from googlevideo.com.

    Returns the video title. Raises FetchError if the Worker is walled
    or the download fails. Prefers a single-file (progressive) mp4 at
    720p or below so the engine gets a ready-to-clip mp4.
    """
    worker_url = os.environ.get("YT_RELAY_URL", "").strip()
    secret = os.environ.get("YT_RELAY_SECRET", "").strip()
    if not worker_url:
        raise FetchError("relay: YT_RELAY_URL not set")
    note(f"trying Cloudflare relay ({worker_url}) ...")
    payload = json.dumps({"videoId": vid}).encode("utf-8")
    headers = {**UA, "Content-Type": "application/json",
               "Accept": "application/json"}
    if secret:
        headers["x-relay-secret"] = secret
    req = urllib.request.Request(worker_url, data=payload,
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise FetchError(f"relay: HTTP {e.code}")
    except Exception as e:
        raise FetchError(f"relay: {type(e).__name__}: {e}")
    if not data.get("ok"):
        raise FetchError(f"relay: {data.get('error')} "
                         f"{data.get('reason', '')}".strip())
    # Prefer a single-file mp4 at 720p or below (progressive formats).
    cands = [f for f in (data.get("formats") or [])
             if f.get("url") and "mp4" in (f.get("mimeType") or "")]
    def _h(f):
        try:
            return int((f.get("qualityLabel") or "0p").rstrip("p"))
        except ValueError:
            return 0
    cands.sort(key=_h, reverse=True)
    pick = next((f for f in cands if _h(f) <= 720), cands[0] if cands else None)
    if pick is None:
        raise FetchError("relay: no mp4 stream returned")
    note(f"relay gave a {pick.get('qualityLabel')} stream, fetching ...")
    if clip_sec:
        _download_segment(pick["url"], dest, start_sec, clip_sec, note)
    else:
        _download(pick["url"], dest, note)
    return data.get("title") or ""


def fetch_youtube(url, dest_dir, note, start_sec=0, clip_sec=None):
    """Download a YouTube URL to dest_dir/source.mp4. Returns (path, title).

    Raises FetchError with a human-readable reason if every method fails.
    `note` is a callable taking one string (appended to the job log).
    When `clip_sec` is set, only the segment starting at `start_sec`
    (for `clip_sec` seconds) is fetched — for multi-hour livestreams
    that the free server cannot process whole.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "source.mp4"
    vid = video_id(url)
    title = ""
    errors = []
    # Time range for long videos: yt-dlp downloads only this section;
    # the relay extracts the same segment with ffmpeg.
    section = None
    if clip_sec:
        section = (f"*{_fmt_hms(start_sec)}-"
                   f"{_fmt_hms(start_sec + clip_sec)}")
        note(f"clipping {clip_sec // 60} min from {_fmt_hms(start_sec)}")
    # Authenticated session (separate throwaway account) bypasses the
    # per-IP rate limit. Decoded once per fetch from YTDLP_COOKIES_B64.
    cookies = _cookies_file()
    if cookies:
        note("using saved YouTube login (separate account) for the fetch")
    # Cloudflare Worker relay first (if configured): its edge IPs may
    # still have player-API quota while this server's IP is exhausted.
    # Falls back to the yt-dlp loop below if the relay fails.
    if os.environ.get("YT_RELAY_URL"):
        try:
            title = _worker_relay(vid, dest, note, start_sec, clip_sec)
            if dest.exists() and dest.stat().st_size >= 100_000:
                note(f"YouTube video fetched via Cloudflare relay ({title or 'untitled'})")
                return dest, title
        except FetchError as e:
            errors.append(f"relay: {e}")
            note(f"relay failed: {e}")
    # Pass 1: every player client once, no waits. A failed attempt
    # answers in seconds, so this whole pass takes ~1-2 min and
    # succeeds as soon as ANY client still has quota. cobalt /
    # Invidious are NOT tried: their public instances are unreachable
    # (DNS fail / HTTP 403) and the self-hosted cobalt hits the same
    # YouTube rate limit, so they only waste time and quota.
    for client in YT_CLIENTS:
        if dest.exists():
            dest.unlink()
        try:
            title = _ytdlp(url, dest, note, client, retries=False,
                           cookies=cookies, section=section) or title
            if dest.exists() and dest.stat().st_size >= 100_000:
                note(f"YouTube video fetched via yt-dlp {client} ({title or 'untitled'})")
                return dest, title
        except FetchError as e:
            errors.append(f"yt-dlp {client}: {e}")
            note(f"yt-dlp {client} failed: {e}")
        except Exception as e:  # noqa: BLE001 - keep trying the next method
            errors.append(f"yt-dlp {client}: {type(e).__name__}: {e}")
            note(f"yt-dlp {client} failed: {type(e).__name__}: {e}")
    # Pass 2: visionos (least-used client) with spaced retries, so its
    # quota can recover.
    if dest.exists():
        dest.unlink()
    try:
        title = _ytdlp(url, dest, note, "visionos", retries=True,
                       cookies=cookies, section=section) or title
        if dest.exists() and dest.stat().st_size >= 100_000:
            note(f"YouTube video fetched via yt-dlp visionos (retry) ({title or 'untitled'})")
            return dest, title
    except FetchError as e:
        errors.append(f"yt-dlp visionos (retry): {e}")
        note(f"yt-dlp visionos (retry) failed: {e}")
    except Exception as e:  # noqa: BLE001 - report the failure
        errors.append(f"yt-dlp visionos (retry): {type(e).__name__}: {e}")
        note(f"yt-dlp visionos (retry) failed: {type(e).__name__}: {e}")
    raise FetchError("YouTube could not be fetched from this server. "
                     "Tried: " + " | ".join(errors)[:800])
