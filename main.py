"""STT Worker — YouTube audio transcription via yt-dlp + faster-whisper."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
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

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# ---------------------------------------------------------------------------
# Shared state — max 1 concurrent transcription job
# ---------------------------------------------------------------------------

_job_lock = asyncio.Lock()
_whisper_model = None
_model_lock = asyncio.Lock()


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
    logger.info(
        "STT worker starting (model=%s, max_duration=%ss)",
        WHISPER_MODEL,
        MAX_DURATION_SECONDS,
    )
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


@app.get("/health")
async def health():
    return {"ok": True}


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
