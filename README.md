# STT Worker

Coolify-deployable Speech-to-Text worker: downloads YouTube audio with **yt-dlp**, transcribes with **faster-whisper** (CPU / int8), exposes a small FastAPI API.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | none | `{"ok": true}` |
| `POST` | `/v1/transcribe` | Bearer `STT_WORKER_SECRET` | Transcribe a YouTube video |

**Request body** (JSON): `{ "videoId": "..." }` and/or `{ "youtubeUrl": "..." }`.

**Success**: `{ "transcript": "...", "videoId": "...", "source": "whisper" }`

**Errors**: `{ "error": "message" }` with 4xx/5xx (401 auth, 400 bad input / too long, 429 busy, 504 timeout).

## Environment

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `STT_WORKER_SECRET` | **yes** | — | Bearer token for `/v1/transcribe` |
| `WHISPER_MODEL` | no | `small` | `tiny` / `base` / `small` |
| `DOWNLOAD_TIMEOUT` | no | `120` | seconds |
| `TRANSCRIBE_TIMEOUT` | no | `600` | seconds |
| `METADATA_TIMEOUT` | no | `30` | seconds |

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
