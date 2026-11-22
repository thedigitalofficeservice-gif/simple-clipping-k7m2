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

So the fix is NOT another server. It is: use the least-used player
client first (visionos — a new client, far less hammered than web/tv),
and RETRY WITH BACKOFF when the API answers LOGIN_REQUIRED, giving
the quota time to recover. One client, a few spaced retries, no
rapid-fire attempts (each failed attempt spends quota).

The YouTube Data API cannot fix this: an API key only returns
metadata (title, duration) — it never returns the video file or a
stream URL. And "caching" cannot fix it either: caching only helps
once a video has been downloaded, and the download itself is what is
blocked. No API-key / Data-API / video-cache solution exists anywhere
in this project's code or history (verified by full search) — it was
an idea discussed, never implemented. The only caches in the engine
are intermediate results (meta.json, storyboard.npz, chat.json) that
only help AFTER a download.

No login, no cookies, no API key, no payment. The downloaded file is
capped at MAX_FETCH_MB (default 500 MB) and requested at 720p or
below, because the engine renders 720p clips anyway.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

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


def _ytdlp(url, dest, note, client, retries=True):
    """The engine's own yt-dlp, with a chosen player client.

    Retries with backoff when YouTube answers with the bot wall
    (LOGIN_REQUIRED / "Sign in to confirm you're not a bot"): that is
    the per-client rate limit, and it recovers after a short cooldown.
    A retry right away would just spend quota again, so we wait.
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


def fetch_youtube(url, dest_dir, note):
    """Download a YouTube URL to dest_dir/source.mp4. Returns (path, title).

    Raises FetchError with a human-readable reason if every method fails.
    `note` is a callable taking one string (appended to the job log).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "source.mp4"
    vid = video_id(url)
    title = ""
    errors = []
    # Order matters: visionos first (newest, least-used client — the
    # one that actually got through on 22 Nov), then tv, then android.
    # cobalt / Invidious are NOT tried: their public instances are
    # unreachable (DNS fail / HTTP 403) and the self-hosted cobalt hits
    # the same YouTube rate limit, so they only waste time and quota.
    # Each client gets spaced retries inside _ytdlp (backoff) so the
    # per-client quota can recover instead of failing at once.
    for name, fn in (("yt-dlp visionos",
                      lambda: _ytdlp(url, dest, note, "visionos")),
                     ("yt-dlp tv",
                      lambda: _ytdlp(url, dest, note, "tv",
                                     retries=False)),
                     ("yt-dlp android",
                      lambda: _ytdlp(url, dest, note, "android",
                                     retries=False))):
        if dest.exists():
            dest.unlink()
        try:
            title = fn() or title
            if dest.exists() and dest.stat().st_size >= 100_000:
                note(f"YouTube video fetched via {name} ({title or 'untitled'})")
                return dest, title
        except FetchError as e:
            errors.append(f"{name}: {e}")
            note(f"{name} failed: {e}")
        except Exception as e:  # noqa: BLE001 - keep trying the next method
            errors.append(f"{name}: {type(e).__name__}: {e}")
            note(f"{name} failed: {type(e).__name__}: {e}")
    raise FetchError("YouTube could not be fetched from this server. "
                     "Tried: " + " | ".join(errors)[:800])
