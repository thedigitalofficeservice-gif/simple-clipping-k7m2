# Simple Clipping — deploy image.
# The BeyondGTAClips engine is fetched from its official GitHub repo at build
# time and used on this private server only. It is not copied into this
# repository, not rebranded and not sold, per its LICENSE. TWO minimal changes
# are applied at build time (see steps 2b and 2c): (a) the render canvas is
# scaled from 1080x1920 to 720x1280 (exact 2/3 proportions, same look)
# because the 1080x1920 render peaks at ~1.14 GB and OOMs on Railway's 1 GB
# free trial; the 720p render peaks at ~0.84 GB. (b) the frame analysis
# grayscale means are computed in float32 instead of float64, halving two
# ~133 MB temporaries with identical motion scores (verified locally).
# The engine's pipeline, moment picker, captions and SFX are untouched.
# The app runs the engine in TWO phases (server.py): Phase 1 (phase1.py)
# does download/listen/pick/prepare with the render stubbed out, then Phase 2
# (the engine's own `remake` command) re-renders in a fresh process — so the
# heavy Whisper/OpenCV memory is freed before the render. Phase 1 peaks at
# ~611 MB, Phase 2 at ~328 MB; both fit in Railway's 1 GB.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    ENGINE_DIR=/opt/beyondgtaclips \
    DATA_DIR=/data \
    PORT=7860

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git nodejs ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# 1. Engine (unmodified, official source)
RUN git clone --depth 1 https://github.com/beyondtahir/beyondgtaclips.git /opt/beyondgtaclips

# 2. Web app (my own code)
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY server.py /app/server.py
COPY phase1.py /app/phase1.py
COPY yt_fetch.py /app/yt_fetch.py
COPY static /app/static

# 2b. 720p canvas patch (see header). Applied to the cloned engine at build
#     time; the script fails the build if the engine's templates.py ever
#     changes shape, so a silent no-op is impossible.
COPY apply-720p-canvas.py /app/apply-720p-canvas.py
RUN python3 /app/apply-720p-canvas.py /opt/beyondgtaclips/scripts/bgt/templates.py

# 2c. float32 frame-analysis patch (see header + apply-frame-float32.py).
#     Same fail-loud rule as 2b.
COPY apply-frame-float32.py /app/apply-frame-float32.py
RUN python3 /app/apply-frame-float32.py /opt/beyondgtaclips/scripts/bgt/frame.py

# 3. Engine setup: system-python mode (packages into ~/.beyondgtaclips/packages,
#    Whisper tiny+base ~220 MB + YuNet face model). Needs ffmpeg + a JS runtime first.
RUN python3 /opt/beyondgtaclips/scripts/setup_env.py --system-python

RUN mkdir -p /data && chmod 777 /data

# 4. yt-dlp config: YouTube shows a sign-in/bot wall to the default web
#    player client from datacenter IPs. The Android/iOS player clients do
#    not hit that wall for public videos. Downloader setting only — the
#    engine itself is untouched.
RUN mkdir -p /root/.config/yt-dlp && printf '%s\n' '--extractor-args youtube:player_client=android,ios,web' > /root/.config/yt-dlp/config

EXPOSE 7860
# No login, no secrets needed: the app is open to anyone with the link.
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
