"""STT Worker — YouTube audio transcription via yt-dlp + faster-whisper."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("stt_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STT_WORKER_SECRET = os.environ.get("STT_WORKER_SECRET", "")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small").strip().lower()
ALLOWED_MODELS = {"tiny", "base", "small"}
MAX_DURATION_SECONDS = 20 * 60  # 20 minutes
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "120"))
TRANSCRIBE_TIMEOUT = int(os.environ.get("TRANSCRIBE_TIMEOUT", "600"))
METADATA_TIMEOUT = int(os.environ.get("METADATA_TIMEOUT", "30"))
DEFAULT_COOKIES_PATH = "/data/youtube.cookies.txt"
# bgutil HTTP POT provider (sidecar). Empty / "0" / "off" / "false" disables wiring.
POT_PROVIDER_URL = (os.environ.get("POT_PROVIDER_URL") or "http://bgutil-pot:4416").strip()
# Short timeout for /health and bot-check diagnostics (seconds).
POT_PROBE_TIMEOUT = float(os.environ.get("POT_PROBE_TIMEOUT", "1.5"))
POT_PROBE_DEEP_TIMEOUT = float(os.environ.get("POT_PROBE_DEEP_TIMEOUT", "3.0"))

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# ---------------------------------------------------------------------------
# Shared state — max 1 concurrent transcription job
# ---------------------------------------------------------------------------

_job_lock = asyncio.Lock()
_whisper_model = None
_model_lock = asyncio.Lock()
_COOKIE_TMP: Optional[str] = None
# Last successful/failed POT probe (updated by /health and bot-check).
_pot_probe_last: dict[str, Any] = {
    "potProviderReachable": None,
    "potProviderError": None,
    "potProviderLatencyMs": None,
}


def _require_secret() -> None:
    if not STT_WORKER_SECRET:
        raise RuntimeError(
            "STT_WORKER_SECRET environment variable is required but not set"
        )


def _validate_model_name(name: str) -> str:
    if name not in ALLOWED_MODELS:
        raise RuntimeError(
            f"WHISPER_MODEL must be one of {sorted(ALLOWED_MODELS)}, got {name!r}"
        )
    return name


async def _get_whisper_model():
    """Lazily load faster-whisper model (CPU / int8)."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    async with _model_lock:
        if _whisper_model is not None:
            return _whisper_model
        model_name = _validate_model_name(WHISPER_MODEL)
        logger.info("Loading Whisper model %r (cpu/int8) …", model_name)

        def _load():
            from faster_whisper import WhisperModel

            return WhisperModel(model_name, device="cpu", compute_type="int8")

        _whisper_model = await asyncio.to_thread(_load)
        logger.info("Whisper model %r ready", model_name)
        return _whisper_model


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _require_secret()
    _validate_model_name(WHISPER_MODEL)
    pot = _pot_provider_url()
    extractor = _ytdlp_extractor_args()
    pot_applied = bool(
        pot and "youtubepot-bgutilhttp" in extractor.get("extractor_args", {})
    )
    logger.info(
        "STT worker starting (model=%s, max_duration=%ss, pot_provider=%s)",
        WHISPER_MODEL,
        MAX_DURATION_SECONDS,
        pot or "disabled",
    )
    logger.info(
        "yt-dlp pot plugin / extractor_args: applied=%s pot_provider_url=%s extractor_args=%s",
        pot_applied,
        pot or "disabled",
        extractor.get("extractor_args"),
    )
    if pot:
        probe = await asyncio.to_thread(_probe_pot_provider, pot, POT_PROBE_TIMEOUT)
        logger.info(
            "POT provider startup probe: reachable=%s latencyMs=%s error=%s",
            probe.get("potProviderReachable"),
            probe.get("potProviderLatencyMs"),
            probe.get("potProviderError"),
        )
    else:
        logger.info("POT provider disabled — skipping reachability probe")
    yield
    logger.info("STT worker shutting down")


app = FastAPI(title="STT Worker", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TranscribeRequest(BaseModel):
    videoId: Optional[str] = Field(default=None, description="YouTube video ID")
    youtubeUrl: Optional[str] = Field(default=None, description="Full YouTube URL")

    @model_validator(mode="after")
    def require_one(self) -> "TranscribeRequest":
        if not self.videoId and not self.youtubeUrl:
            raise ValueError("Provide videoId or youtubeUrl")
        return self


class TranscribeResponse(BaseModel):
    transcript: str
    videoId: str
    source: str = "whisper"


class ErrorResponse(BaseModel):
    error: str
    code: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_video_id(video_id: Optional[str], youtube_url: Optional[str]) -> str:
    if video_id:
        vid = video_id.strip()
        if not VIDEO_ID_RE.match(vid):
            raise HTTPException(
                status_code=400,
                detail={"error": f"Invalid videoId format: {vid!r}"},
            )
        return vid

    assert youtube_url is not None
    url = youtube_url.strip()
    patterns = [
        r"(?:youtube\.com/watch\?.*?v=|youtube\.com/embed/|youtube\.com/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    raise HTTPException(
        status_code=400,
        detail={"error": f"Could not extract videoId from youtubeUrl: {url!r}"},
    )


def _youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _is_bot_check_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "sign in to confirm" in msg
        or "not a bot" in msg
        or ("confirm you" in msg and "bot" in msg)
    )


def _resolve_cookiefile() -> Optional[str]:
    """Return a Netscape cookies.txt path for yt-dlp, or None.

    - YTDLP_COOKIES_FILE: path to cookies file
    - else default /data/youtube.cookies.txt if present
    - else YTDLP_COOKIES: Netscape cookies.txt contents → temp file
    Never log cookie contents.
    """
    global _COOKIE_TMP

    explicit = (os.environ.get("YTDLP_COOKIES_FILE") or "").strip()
    if explicit:
        if os.path.isfile(explicit) and os.path.getsize(explicit) > 0:
            return explicit
        logger.warning("YTDLP_COOKIES_FILE set but file missing/empty: %s", explicit)

    if os.path.isfile(DEFAULT_COOKIES_PATH) and os.path.getsize(DEFAULT_COOKIES_PATH) > 0:
        return DEFAULT_COOKIES_PATH

    raw = os.environ.get("YTDLP_COOKIES")
    if raw and raw.strip():
        # If someone mistakenly put a path in YTDLP_COOKIES, treat as file path
        candidate = raw.strip()
        if (
            "\n" not in candidate
            and len(candidate) < 512
            and os.path.isfile(candidate)
            and os.path.getsize(candidate) > 0
        ):
            return candidate

        if _COOKIE_TMP and os.path.isfile(_COOKIE_TMP):
            return _COOKIE_TMP
        fd, path = tempfile.mkstemp(prefix="ytdlp_cookies_", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw)
                if not raw.endswith("\n"):
                    f.write("\n")
            os.chmod(path, 0o600)
            _COOKIE_TMP = path
            logger.info("Wrote yt-dlp cookies from YTDLP_COOKIES env to temp file")
            return path
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

    return None


def _pot_provider_url() -> Optional[str]:
    """Return configured bgutil POT HTTP base URL, or None if disabled."""
    raw = POT_PROVIDER_URL
    if not raw or raw.lower() in {"0", "false", "off", "none", "disabled"}:
        return None
    return raw.rstrip("/")


def _probe_pot_provider(
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Probe bgutil POT HTTP provider reachability.

    Tries GET /ping (official bgutil health), then GET / as fallback.
    Any HTTP response counts as reachable (network path OK); connection /
    DNS / timeout failures mean unreachable — the Coolify network suspect.
    """
    global _pot_probe_last
    url = base_url if base_url is not None else _pot_provider_url()
    t_out = POT_PROBE_TIMEOUT if timeout is None else timeout
    if not url:
        result: dict[str, Any] = {
            "potProviderUrl": None,
            "potProviderReachable": None,
            "potProviderError": None,
            "potProviderLatencyMs": None,
        }
        return result

    candidates = [f"{url}/ping", f"{url}/"]
    last_error: Optional[str] = None
    t0 = time.monotonic()
    for candidate in candidates:
        try:
            req = urllib.request.Request(
                candidate,
                method="GET",
                headers={"Accept": "application/json,*/*", "User-Agent": "stt-worker-pot-probe"},
            )
            with urllib.request.urlopen(req, timeout=t_out) as resp:
                code = int(resp.getcode())
                latency_ms = int((time.monotonic() - t0) * 1000)
                err = None if 200 <= code < 300 else f"HTTP {code} from {candidate}"
                result = {
                    "potProviderUrl": url,
                    "potProviderReachable": True,
                    "potProviderError": err,
                    "potProviderLatencyMs": latency_ms,
                }
                _pot_probe_last = {
                    "potProviderReachable": True,
                    "potProviderError": err,
                    "potProviderLatencyMs": latency_ms,
                }
                return result
        except urllib.error.HTTPError as e:
            # Got an HTTP response → host is reachable on the network.
            latency_ms = int((time.monotonic() - t0) * 1000)
            err = f"HTTP {e.code} from {candidate}"
            result = {
                "potProviderUrl": url,
                "potProviderReachable": True,
                "potProviderError": err,
                "potProviderLatencyMs": latency_ms,
            }
            _pot_probe_last = {
                "potProviderReachable": True,
                "potProviderError": err,
                "potProviderLatencyMs": latency_ms,
            }
            return result
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            continue

    latency_ms = int((time.monotonic() - t0) * 1000)
    result = {
        "potProviderUrl": url,
        "potProviderReachable": False,
        "potProviderError": last_error or "unreachable",
        "potProviderLatencyMs": latency_ms,
    }
    _pot_probe_last = {
        "potProviderReachable": False,
        "potProviderError": result["potProviderError"],
        "potProviderLatencyMs": latency_ms,
    }
    return result


def _health_pot_fields(probe: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    pot = _pot_provider_url()
    if probe is None:
        return {
            "potProviderUrl": pot,
            "potProviderReachable": _pot_probe_last.get("potProviderReachable"),
            "potProviderError": _pot_probe_last.get("potProviderError"),
            "potProviderLatencyMs": _pot_probe_last.get("potProviderLatencyMs"),
        }
    return {
        "potProviderUrl": pot,
        "potProviderReachable": probe.get("potProviderReachable"),
        "potProviderError": probe.get("potProviderError"),
        "potProviderLatencyMs": probe.get("potProviderLatencyMs"),
    }


def _ytdlp_extractor_args() -> dict:
    """Prefer android clients; wire bgutil PO Token HTTP provider when configured.

    player_client=android first (no cookies). PO tokens come from the
    bgutil-ytdlp-pot-provider sidecar via extractor arg
    youtubepot-bgutilhttp:base_url=<POT_PROVIDER_URL>.
    Cookies remain an optional last-resort fallback (see _ytdlp_cookie_opts).
    """
    extractor_args: dict = {
        "youtube": {
            "player_client": ["android", "android_vr", "ios", "tv", "web"],
        }
    }
    pot_url = _pot_provider_url()
    if pot_url:
        # Plugin key from bgutil-ytdlp-pot-provider (PyPI); see README.
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [pot_url]}
        logger.info("yt-dlp PO token provider base_url=%s", pot_url)
    return {"extractor_args": extractor_args}


def _ytdlp_cookie_opts() -> dict:
    cookiefile = _resolve_cookiefile()
    if cookiefile:
        logger.info("yt-dlp using cookiefile=%s", cookiefile)
        return {"cookiefile": cookiefile}
    return {}


def _bot_check_http_exception() -> HTTPException:
    pot = _pot_provider_url()
    pot_configured = bool(pot)
    probe = (
        _probe_pot_provider(pot, POT_PROBE_TIMEOUT)
        if pot
        else {
            "potProviderReachable": None,
            "potProviderError": None,
            "potProviderLatencyMs": None,
        }
    )
    reachable = probe.get("potProviderReachable")
    if not pot_configured:
        hint = (
            "Set POT_PROVIDER_URL and deploy bgutil-pot sidecar on the same "
            "Coolify/Compose network as STT"
        )
    elif reachable is False:
        hint = "STT cannot reach POT provider; put both on same Docker network"
    elif reachable is True:
        hint = (
            "POT reachable but YouTube still blocked; try residential proxy or cookies"
        )
    else:
        hint = (
            "PO Token provider configured but reachability unknown; "
            "confirm Coolify shared network with bgutil-pot"
        )
    return HTTPException(
        status_code=503,
        detail={
            "error": (
                "YouTube bot check blocked audio download even with android "
                f"player client. {hint}"
            ),
            "code": "YOUTUBE_BOT_CHECK",
            "potConfigured": pot_configured,
            "potProviderUrl": pot,
            "potProviderReachable": reachable,
            "hint": hint,
        },
    )


def _check_auth(authorization: Optional[str]) -> None:
    if not authorization:
        raise HTTPException(
            status_code=401, detail={"error": "Missing Authorization header"}
        )
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != STT_WORKER_SECRET:
        raise HTTPException(
            status_code=401, detail={"error": "Invalid or missing bearer token"}
        )


def _fetch_duration(url: str) -> Optional[float]:
    """Fetch video duration via yt-dlp metadata (no download)."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": METADATA_TIMEOUT,
        **_ytdlp_extractor_args(),
        **_ytdlp_cookie_opts(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return None
    duration = info.get("duration")
    return float(duration) if duration is not None else None


def _download_audio(url: str, out_dir: str) -> str:
    """Download audio-only track into out_dir; return path to file."""
    import yt_dlp

    outtmpl = os.path.join(out_dir, "%(id)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
        **_ytdlp_extractor_args(),
        **_ytdlp_cookie_opts(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("yt-dlp returned no info")
        base = ydl.prepare_filename(info)
        root, _ = os.path.splitext(base)
        candidates = [root + ".mp3", base]
        vid = info.get("id") or ""
        for name in os.listdir(out_dir):
            if vid and name.startswith(vid):
                candidates.append(os.path.join(out_dir, name))
        for path in candidates:
            if os.path.isfile(path):
                return path
        raise RuntimeError("Downloaded audio file not found")


def _transcribe_file(audio_path: str) -> str:
    global _whisper_model
    model = _whisper_model
    if model is None:
        raise RuntimeError("Whisper model not loaded")
    segments, _info = model.transcribe(audio_path, beam_size=1)
    texts = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
    return " ".join(texts)


# ---------------------------------------------------------------------------
# Routes & error handlers
# ---------------------------------------------------------------------------


async def _health_payload(*, deep: bool = False) -> dict[str, Any]:
    global _pot_probe_last
    pot = _pot_provider_url()
    probe: Optional[dict[str, Any]] = None
    if pot:
        timeout = POT_PROBE_DEEP_TIMEOUT if deep else POT_PROBE_TIMEOUT
        try:
            probe = await asyncio.wait_for(
                asyncio.to_thread(_probe_pot_provider, pot, timeout),
                timeout=timeout + 0.5,
            )
        except asyncio.TimeoutError:
            probe = {
                "potProviderUrl": pot,
                "potProviderReachable": False,
                "potProviderError": f"probe timed out after {timeout}s",
                "potProviderLatencyMs": int(timeout * 1000),
            }
            _pot_probe_last = {
                "potProviderReachable": False,
                "potProviderError": probe["potProviderError"],
                "potProviderLatencyMs": probe["potProviderLatencyMs"],
            }
    payload = {
        "ok": True,
        "cookiesConfigured": bool(_resolve_cookiefile()),
        "model": WHISPER_MODEL,
        **_health_pot_fields(probe),
    }
    if deep:
        payload["deep"] = True
    return payload


@app.get("/health")
async def health(deep: Optional[int] = Query(default=None)):
    """Liveness + quick POT reachability probe (1–2s timeout).

    Pass ?deep=1 for the longer probe (same as GET /health/deep).
    """
    return await _health_payload(deep=bool(deep))


@app.get("/health/deep")
async def health_deep():
    """Always live-probe the POT provider with a slightly longer timeout."""
    return await _health_payload(deep=True)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code, content={"error": str(exc.detail)}
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
):
    msgs = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
        msg = err.get("msg", "invalid")
        msgs.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(
        status_code=400,
        content={"error": "; ".join(msgs) or "Invalid request"},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


@app.post(
    "/v1/transcribe",
    response_model=TranscribeResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
async def transcribe(
    body: TranscribeRequest,
    authorization: Optional[str] = Header(default=None),
):
    _check_auth(authorization)

    video_id = _extract_video_id(body.videoId, body.youtubeUrl)
    url = _youtube_url(video_id)

    # Non-blocking: reject if another job is in progress
    if _job_lock.locked():
        raise HTTPException(
            status_code=429,
            detail={"error": "Worker busy — max 1 concurrent job"},
        )

    try:
        await asyncio.wait_for(_job_lock.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail={"error": "Worker busy — max 1 concurrent job"},
        )

    tmp_dir: Optional[str] = None
    try:
        # Duration check (metadata only — before full download)
        try:
            duration = await asyncio.wait_for(
                asyncio.to_thread(_fetch_duration, url),
                timeout=METADATA_TIMEOUT + 5,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail={"error": "Timed out fetching video metadata"},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Metadata fetch failed for %s: %s", video_id, e)
            if _is_bot_check_error(e):
                raise _bot_check_http_exception() from e
            raise HTTPException(
                status_code=400,
                detail={"error": f"Could not fetch video metadata: {e}"},
            )

        if duration is not None and duration > MAX_DURATION_SECONDS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        f"Video too long ({int(duration)}s); "
                        f"max {MAX_DURATION_SECONDS}s (20 minutes)"
                    )
                },
            )

        try:
            await asyncio.wait_for(_get_whisper_model(), timeout=300)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail={"error": "Timed out loading Whisper model"},
            )

        tmp_dir = tempfile.mkdtemp(prefix="stt_")
        try:
            audio_path = await asyncio.wait_for(
                asyncio.to_thread(_download_audio, url, tmp_dir),
                timeout=DOWNLOAD_TIMEOUT + 30,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail={"error": "Timed out downloading audio"},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Download failed for %s: %s", video_id, e)
            if _is_bot_check_error(e):
                raise _bot_check_http_exception() from e
            raise HTTPException(
                status_code=400,
                detail={"error": f"Audio download failed: {e}"},
            )

        try:
            transcript = await asyncio.wait_for(
                asyncio.to_thread(_transcribe_file, audio_path),
                timeout=TRANSCRIBE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail={"error": "Timed out during transcription"},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Transcription failed for %s", video_id)
            raise HTTPException(
                status_code=500,
                detail={"error": f"Transcription failed: {e}"},
            )

        if not transcript.strip():
            raise HTTPException(
                status_code=500,
                detail={"error": "Empty transcript produced"},
            )

        return TranscribeResponse(
            transcript=transcript,
            videoId=video_id,
            source="whisper",
        )
    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if _job_lock.locked():
            _job_lock.release()
