---
title: Simple Clipping
emoji: ✂️
colorFrom: red
colorTo: black
sdk: docker
app_port: 7860
pinned: false
license: other
---

# Simple Clipping

Private-by-link web app: paste a stream link, press Start Clipping, get 6
vertical 9:16 clips. No login, no email, no password, no registration — open
the link and clip. Anyone with the link can use it, so share the link only
with people you trust.

## How it works

The app is a small FastAPI server (`server.py`, my own code) that runs the
**unmodified** [BeyondGTAClips](https://github.com/beyondtahir/beyondgtaclips)
engine (free tool by Beyond Tahir) as a subprocess. The engine is fetched from
its official GitHub repo at image build time — no engine code is copied into
this repository, nothing is rebranded or sold.

- Paste a YouTube / Twitch / Kick link, press Start Clipping
- The engine listens (Whisper), finds loud/chat/on-screen spikes, picks up to
  6 moments, renders 1080×1920 clips with word-by-word captions and hooks
- Preview in the app, download the MP4s (works on mobile too), copy the
  posting caption
- Finished clips stay saved under "Your clips" for 7 days, so you can watch
  and download them later

## Notes

- Moments are picked by the engine's signal picker (loudness + chat + screen
  activity + Whisper transcript) — no cloud AI and no API keys on this server.
- One job runs at a time; long streams take a while on free CPU.
- Finished clips are kept for 7 days (or until 2 GB of clips is stored, oldest
  first out).
- Only clip creators who allow it, and always credit the streamer.
