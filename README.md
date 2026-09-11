# STT Worker

Coolify-deployable Speech-to-Text worker: downloads YouTube audio with **yt-dlp**, transcribes with **faster-whisper** (CPU / int8), exposes a small FastAPI API.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | none | Liveness + quick POT probe (`potProviderUrl`, `potProviderReachable`, `potProviderError`, `potProviderLatencyMs`, …). Pass `?deep=1` for a longer probe. |
| `GET` | `/health/deep` | none | Always live-probes POT provider (GET `{POT_PROVIDER_URL}/ping`). |
| `POST` | `/v1/transcribe` | Bearer `STT_WORKER_SECRET` | Transcribe a YouTube video |

**Request body** (JSON): `{ "videoId": "..." }` and/or `{ "youtubeUrl": "..." }`.

**Success**: `{ "transcript": "...", "videoId": "...", "source": "whisper" }`

**Errors**: `{ "error": "message", "code"?: "…" }` with 4xx/5xx (401 auth, 400 bad input / too long, 429 busy, 503 YouTube bot check, 504 timeout).

When YouTube returns “Sign in to confirm you’re not a bot”, the worker responds **503** with:

```json
{
  "error": "…",
  "code": "YOUTUBE_BOT_CHECK",
  "potConfigured": true,
  "potProviderUrl": "http://bgutil-pot:4416",
  "potProviderReachable": false,
  "hint": "STT cannot reach POT provider; put both on same Docker network"
}
```

`hint` is `STT cannot reach POT provider; put both on same Docker network` when the probe fails, or `POT reachable but YouTube still blocked; try residential proxy or cookies` when the sidecar answers but YouTube still blocks. Prefer **android player client + bgutil PO Token sidecar** (no cookies). Cookies are optional last resort only.

## YouTube download (no cookies)

1. **android-first** `player_client` (`android`, `android_vr`, `ios`, `tv`, `web`) — often enough on residential IPs.
2. **bgutil PO Token sidecar** — required on datacenter IPs (e.g. Contabo). This image installs the PyPI plugin `bgutil-ytdlp-pot-provider` and points yt-dlp at `POT_PROVIDER_URL` via extractor arg `youtubepot-bgutilhttp:base_url=…`.
3. **Cookies** — optional fallback only (`YTDLP_COOKIES_FILE` / `YTDLP_COOKIES`). **Not required** and not used in the Contabo no-cookie path.

## Environment

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `STT_WORKER_SECRET` | **yes** | — | Bearer token for `/v1/transcribe` |
| `POT_PROVIDER_URL` | no (recommended on Contabo) | `http://bgutil-pot:4416` | HTTP base URL of the **bgutil** POT sidecar. Wired as yt-dlp `--extractor-args "youtubepot-bgutilhttp:base_url=<URL>"`. Set to `off` / `false` / `0` / empty to disable. Hostname must resolve on the Coolify shared network (compose service name `bgutil-pot` below). |
| `WHISPER_MODEL` | no | `small` | `tiny` / `base` / `small` |
| `DOWNLOAD_TIMEOUT` | no | `120` | seconds |
| `TRANSCRIBE_TIMEOUT` | no | `600` | seconds |
| `METADATA_TIMEOUT` | no | `30` | seconds |
| `YTDLP_COOKIES_FILE` | no | `/data/youtube.cookies.txt` if that file exists | Optional cookie fallback only. |
| `YTDLP_COOKIES` | no | — | Optional alternate cookie contents. Prefer file mount. **Never commit real cookies.** |

### Env names Senior must set on Contabo STT service

```text
STT_WORKER_SECRET=<same secret the app uses>
POT_PROVIDER_URL=http://bgutil-pot:4416
WHISPER_MODEL=small
```

Do **not** set cookie envs for the no-cookie Contabo path.

## Coolify compose sketch (STT + bgutil sidecar)

Deploy as a **Docker Compose** application (or two services on the same Coolify network). The POT provider is a **sidecar** — it is **not** baked into the STT image.

```yaml
services:
  stt:
    build: .   # this repo / workers/stt Dockerfile
    # Or: image: <your Coolify-built STT image>
    ports:
      - "8000:8000"
    environment:
      STT_WORKER_SECRET: ${STT_WORKER_SECRET}
      WHISPER_MODEL: small
      # Exact env name — plugin uses this as youtubepot-bgutilhttp:base_url
      POT_PROVIDER_URL: http://bgutil-pot:4416
    depends_on:
      - bgutil-pot
    networks:
      - stt-net
    restart: unless-stopped

  # Official HTTP POT provider (Node by default). Listens on 4416 inside the network.
  # https://github.com/Brainicism/bgutil-ytdlp-pot-provider
  # https://hub.docker.com/r/brainicism/bgutil-ytdlp-pot-provider
  bgutil-pot:
    image: brainicism/bgutil-ytdlp-pot-provider:latest
    # Do NOT publish 4416 publicly; only the STT service needs it on the internal network.
    expose:
      - "4416"
    networks:
      - stt-net
    restart: unless-stopped

networks:
  stt-net:
    driver: bridge
```

### Same Coolify / Compose network (required)

**`bgutil-pot` and STT must share one Docker/Coolify network** (Compose `networks:` as in the sketch above, or Coolify “Connect to predefined network”). Hostname `bgutil-pot` only resolves across that shared network — a sidecar “running on 4416” on a *different* network still yields `potProviderReachable: false` and `YOUTUBE_BOT_CHECK`.

Diagnose with:

```bash
curl -s https://<stt-host>/health
# expect potProviderReachable: true when networks are correct
curl -s https://<stt-host>/health/deep
```

### Coolify steps (exact)

1. **Redeploy STT** from `mathew36c/youtube2quizai-stt` **main** after this commit (or sync folder `youtube2quizai/workers/stt`). Confirm image build installs `bgutil-ytdlp-pot-provider` from PyPI.
2. Add sidecar service **`bgutil-pot`** with image `brainicism/bgutil-ytdlp-pot-provider:latest` on the **same Docker network** as STT (Compose file or Coolify shared network). Do not expose port 4416 to the public internet.
3. On the STT service, set env:
   - `POT_PROVIDER_URL=http://bgutil-pot:4416`
   - (keep existing) `STT_WORKER_SECRET=…`
4. Restart/redeploy both. Hit `GET /health` — expect `"potProviderUrl": "http://bgutil-pot:4416"` and `"potProviderReachable": true`.
5. Retest `POST /v1/transcribe` with `videoId=rA91cjP1vEg` (previous Contabo `YOUTUBE_BOT_CHECK`).
6. If still 503: read `potProviderReachable` + `hint` in the error body. If unreachable → fix Coolify/Compose network. If reachable → residential proxy or optional cookies. Also check sidecar logs (`bgutil-pot`) and that yt-dlp would show `PO Token Providers: bgutil:http-…`. Optional last resort only: cookies (Matt refuses export — skip).

Plugin docs: [Brainicism/bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) · [yt-dlp PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide).

> Note: JS/Deno for BotGuard runs **inside the sidecar** image. The STT image only needs the Python plugin. Optional future: install Deno in the STT image if yt-dlp EJS challenge solving is needed separately from POT.

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
# Optional local POT server: docker run --rm -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
export STT_WORKER_SECRET=dev-secret
export POT_PROVIDER_URL=http://127.0.0.1:4416
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
  -e POT_PROVIDER_URL=http://host.docker.internal:4416 \
  stt-worker
```

**Coolify**: build from this Dockerfile, map port **8000**, set `STT_WORKER_SECRET` + `POT_PROVIDER_URL`, run sidecar on shared network. Do not bake secrets into the image. Do not require cookies for Contabo.
