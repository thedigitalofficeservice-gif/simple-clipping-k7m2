#!/usr/bin/env python3
"""Fetch a YouTube video to a local file, from a datacenter IP.

YouTube serves a sign-in/bot wall to yt-dlp on Railway's datacenter IP
("Sign in to confirm you're not a bot") for EVERY video, so the engine's
own yt-dlp cannot fetch a YouTube URL there. The YouTube Data API cannot
fix this: an API key only returns metadata (title, duration) — it never
returns the video file or a stream URL. And "caching" cannot fix it
either: caching only helps once a video has been downloaded, and the
download itself is what is blocked.

So this helper fetches the video through services that already do the
YouTube download on their own servers, then hands the engine a LOCAL file
(the same path the upload flow uses, which already works on Railway).
No login, no cookies, no API key, no payment. Methods are tried in order
and the first one that produces a real file wins; every attempt is logged
so a failed job shows exactly what was tried.

  1. cobalt (https://co.api) — a free downloader API: POST the YouTube
     URL, get back a direct download link, fetch the file. Fallback
     instances are tried if the main one is unreachable.
  2. Invidious API — GET /api/v1/videos/<id> from a list of public
     instances, take a formatStreams URL (a direct googlevideo.com link)
     and fetch the file. Instances are often behind bot-checks, so this
     is a fallback, not the primary route.
  3. yt-dlp with the TV player client — the engine's own downloader with
     player_client=tv (smart-TV clients are sometimes treated
     differently from the web client). Kept as a last resort; the web /
     android / ios clients are already known to be walled on Railway.
  4. yt-dlp with the default (web) client — the current behaviour.

The downloaded file is capped at MAX_FETCH_MB (default 500 MB, the same
as the upload limit) and is requested at 720p or below, because the
engine renders 720p clips anyway. Very long videos (multi-hour
livestream recordings) may exceed the cap; the job then fails with a
clear message instead of filling the disk.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

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


def _ytdlp(url, dest, note, client):
    """Method 3/4: the engine's own yt-dlp, with a chosen player client."""
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
    note(f"trying yt-dlp (player client {client or 'default'}) ...")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600, env=env)
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    # yt-dlp may still append an extension (e.g. source.mp4.webm) if the
    # merge format flag is ignored; accept any file it actually wrote.
    actual = dest if dest.exists() else None
    if actual is None:
        for cand in sorted(dest.parent.glob(dest.name + ".*")):
            if cand.is_file() and cand.stat().st_size >= 100_000:
                actual = cand
                break
    if proc.returncode != 0 or actual is None or actual.stat().st_size < 100_000:
        raise FetchError("yt-dlp " + (client or "default") + " failed: "
                         + " | ".join(tail)[:300])
    if actual != dest:
        actual.replace(dest)
    note(f"yt-dlp ({client or 'default'}) downloaded "
         f"{dest.stat().st_size / 1024 / 1024:.1f} MB")
    return ""


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
    for name, fn in (("cobalt", lambda: _cobalt(url, dest, note)),
                     ("invidious", lambda: _invidious(vid, dest, note)),
                     ("yt-dlp tv", lambda: _ytdlp(url, dest, note, "tv")),
                     ("yt-dlp", lambda: _ytdlp(url, dest, note, None))):
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
