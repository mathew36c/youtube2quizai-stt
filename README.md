# STT Worker

Coolify-deployable Speech-to-Text worker: downloads YouTube audio with **yt-dlp**, transcribes with **faster-whisper** (CPU / int8), exposes a small FastAPI API.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | none | `{"ok": true}` |
| `POST` | `/v1/transcribe` | Bearer `STT_WORKER_SECRET` | Transcribe a YouTube video |

**Request body** (JSON): `{ "videoId": "..." }` and/or `{ "youtubeUrl": "..." }`.

**Success**: `{ "transcript": "...", "videoId": "...", "source": "whisper" }`

**Errors**: `{ "error": "message", "code"?: "…" }` with 4xx/5xx (401 auth, 400 bad input / too long, 429 busy, 503 YouTube bot check, 504 timeout).

When YouTube returns “Sign in to confirm you’re not a bot”, the worker responds **503** with `"code": "YOUTUBE_BOT_CHECK"`. Prefer fixing via player client / PO token / proxy first; cookies are optional (see below).

## YouTube download (no cookies when possible)

yt-dlp is configured with `extractor_args` preferring the **android** player client first (`android`, `android_vr`, `ios`, `tv`, `web`). On many networks this downloads audio **without cookies** (avoids the web client bot-check). Cookies remain an **optional** fallback when `YTDLP_COOKIES_FILE` / `/data/youtube.cookies.txt` / `YTDLP_COOKIES` is present — they are **not required**.

If android still fails on a datacenter IP (e.g. Contabo):

1. **PO Token provider** — run [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) (Docker) and point yt-dlp at it (future worker env wiring).
2. **Residential proxy** — set a proxy env later so yt-dlp egresses via residential IP.
3. **Cookies** — still supported as last-resort fallback (see below).

## Environment

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `STT_WORKER_SECRET` | **yes** | — | Bearer token for `/v1/transcribe` |
| `WHISPER_MODEL` | no | `small` | `tiny` / `base` / `small` |
| `DOWNLOAD_TIMEOUT` | no | `120` | seconds |
| `TRANSCRIBE_TIMEOUT` | no | `600` | seconds |
| `METADATA_TIMEOUT` | no | `30` | seconds |
| `YTDLP_COOKIES_FILE` | no | `/data/youtube.cookies.txt` if that file exists | Optional cookie fallback: path to Netscape `cookies.txt` (mount a volume in Coolify). Not required when android client works. |
| `YTDLP_COOKIES` | no | — | Optional alternate: paste full Netscape `cookies.txt` contents; worker writes a temp file. Prefer file mount when using cookies. **Never commit real cookies to git.** |

## RAM note

The Whisper model loads **lazily on first `/v1/transcribe` request** (CPU, `int8`). Approximate RSS:

| Model | Rough RAM |
|-------|-----------|
| `tiny` | ~1 GB |
| `base` | ~1–1.5 GB |
| `small` | ~2–3 GB |

Give Coolify at least **2–4 GB RAM** for `small`. First request is slow while the model downloads/loads; later requests reuse it. Max concurrent jobs: **1** (429 if busy). Videos longer than **20 minutes** are rejected after a metadata check.

## Local run (no Docker)

```bash
cd workers/stt
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# ffmpeg must be on PATH
export STT_WORKER_SECRET=dev-secret
export WHISPER_MODEL=tiny   # faster for local smoke tests
uvicorn main:app --host 0.0.0.0 --port 8000
```

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/v1/transcribe \
  -H "Authorization: Bearer dev-secret" \
  -H "Content-Type: application/json" \
  -d '{"videoId":"dQw4w9WgXcQ"}'
```

## Docker / Coolify

```bash
docker build -t stt-worker .
docker run --rm -p 8000:8000 \
  -e STT_WORKER_SECRET=changeme \
  -e WHISPER_MODEL=small \
  stt-worker
```

**Coolify**: point the service at this folder (Dockerfile), map port **8000**, set `STT_WORKER_SECRET` (and optionally `WHISPER_MODEL`) in the service env. Do not bake secrets into the image.

### YouTube bot check / cookies (optional fallback)

The worker prefers the **android** player client so cookies are usually unnecessary. If you still hit bot-check on a datacenter IP, try PO Token / residential proxy (above), or supply cookies as a last resort:

1. Mount a volume at `/data` and place `youtube.cookies.txt` there (auto-used), or set `YTDLP_COOKIES_FILE`.
2. Or set Coolify env `YTDLP_COOKIES` to full Netscape file contents.

Do **not** commit cookie files or paste real cookies into git / Dockerfiles. Cookies are optional — do not require them for normal deploys.
