#!/usr/bin/env python3
"""Phase 1 of the two-phase clip (see server.py).

Runs the full BeyondGTAClips pipeline — download, listen (Whisper),
pick, prepare — but SKIPS the real video render. produce.render is
replaced by a stub that just touches an empty placeholder file, so
produce.make() records the clip and pipeline.process() completes
normally (writes clips.json + POSTING.md, exit 0).

Phase 2 (the engine's own `remake` command, run as a FRESH process)
then re-renders the clips for real, after the heavy Whisper/OpenCV
memory from Phase 1 has been freed.

Why: on a 1 GB server the single-process run peaks at ~777 MB and the
render's ffmpeg is OOM-killed. Splitting it lets each phase fit in
1 GB. Nothing about the clips changes: same moments, same templates,
same 720x1280 canvas, same captions, same audio (loudness off).

Usage: phase1.py clip <source> --total N --styles auto --length short
       --brain none --topic Gaming
(identical args to the engine's `clip` command; ENGINE_DIR env var
points at the engine checkout, default /opt/beyondgtaclips.)
"""
import json
import os
import sys
from pathlib import Path

ENGINE_DIR = Path(os.environ.get("ENGINE_DIR", "/opt/beyondgtaclips")).resolve()
sys.path.insert(0, str(ENGINE_DIR / "scripts"))

# Same system-python package setup the engine itself uses
# (~/.beyondgtaclips/system-python.json, written by setup_env.py
# --system-python at image build time).
try:
    conf = json.loads(
        (Path.home() / ".beyondgtaclips" / "system-python.json")
        .read_text(encoding="utf-8"))
    pkgs = Path(conf["packages"])
    if pkgs.exists():
        sys.path.insert(0, str(pkgs))
        # The engine spawns yt-dlp as `sys.executable -m yt_dlp` (see
        # bgt/common.py run()), i.e. a NEW Python process that does not
        # inherit this process's sys.path — it only sees env vars. So the
        # packages dir must be on PYTHONPATH (and its bin/ on PATH) for
        # those subprocesses, exactly as beyondgtaclips.py sets it up.
        os.environ["PYTHONPATH"] = (str(pkgs) + os.pathsep
                                    + os.environ.get("PYTHONPATH", ""))
        os.environ["PATH"] = (str(pkgs / "bin") + os.pathsep
                              + os.environ.get("PATH", ""))
except Exception:
    pass

from bgt import cli, listen, produce  # noqa: E402
import gc  # noqa: E402

try:
    import ctypes  # noqa: E402
    _libc = ctypes.CDLL("libc.so.6")

    def _trim():
        """Release freed heap memory back to the OS.

        The engine's per-phase work (Whisper transcribe, OpenCV face
        detection, sampled frames) frees its objects after each phase,
        but CPython's allocator + glibc hold the freed pages in arenas,
        so RSS grows phase after phase (the 744 MB Phase-1 peak). A
        gc.collect() + malloc_trim(0) at each phase boundary returns the
        fully-free pages, keeping the peak near one phase's live use.
        Harmless if it trims nothing.
        """
        try:
            gc.collect()
            _libc.malloc_trim(0)
        except Exception:
            pass
except Exception:
    def _trim():
        gc.collect()


# Trim right after the listen phase (Whisper scan) finishes, so its
# retained model memory is released BEFORE the per-clip prepare
# (which loads its own Whisper + OpenCV + frames). Without this the
# first clip's prepare peaks with listen + prepare memory combined.
_orig_scan = listen.scan


def _scan_trimmed(*args, **kwargs):
    result = _orig_scan(*args, **kwargs)
    _trim()
    return result


listen.scan = _scan_trimmed


def _fake_render(name, job, cfg, dest):
    """Stand in for produce.render: create an empty placeholder so
    produce.make() records the clip and pipeline.process() finishes
    (writes clips.json + POSTING.md). Phase 2 re-renders it for real,
    overwriting this placeholder."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.touch()
    return dest


produce.render = _fake_render

_orig_make = produce.make


def _make_trimmed(index, pick, ctx, cfg, memory):
    """produce.make + a heap trim after each clip, so retained memory
    from one clip's prepare does not pile up into the next."""
    result = _orig_make(index, pick, ctx, cfg, memory)
    _trim()
    return result


produce.make = _make_trimmed

sys.exit(cli.main(sys.argv[1:]) or 0)
