# Simple Clipping — deploy image.
# The BeyondGTAClips engine is fetched UNMODIFIED from its official GitHub repo
# at build time and used on this private server only. It is not copied into
# this repository, not rebranded and not sold, per its LICENSE.
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
COPY static /app/static

# 3. Engine setup: system-python mode (packages into ~/.beyondgtaclips/packages,
#    Whisper tiny+base ~220 MB + YuNet face model). Needs ffmpeg + a JS runtime first.
RUN python3 /opt/beyondgtaclips/scripts/setup_env.py --system-python

RUN mkdir -p /data && chmod 777 /data
EXPOSE 7860
# No login, no secrets needed: the app is open to anyone with the link.
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
