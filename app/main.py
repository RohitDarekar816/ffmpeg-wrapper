"""
FFmpeg API — a small wrapper service for n8n.

n8n sends an audio URL, this service downloads it, converts it to a real MP3
(libmp3lame, 128 kbps by default) and returns a usable public MP3 URL
(or the raw file). Interactive Swagger UI is served at /docs.
"""
import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Literal, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AliasChoices, BaseModel, Field, HttpUrl

# --- Configuration (all overridable via environment variables) --------------
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
# Public base used to build the returned MP3 URL. On a shared Docker network
# set this to e.g. http://ffmpeg-api:8000 so n8n can fetch the file back.
# If left empty, the URL is derived from the incoming request.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_BITRATE = os.getenv("DEFAULT_BITRATE", "128k")
# Max download size guard (bytes). Default 500 MB.
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(500 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = float(os.getenv("DOWNLOAD_TIMEOUT", "120"))

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="FFmpeg Audio → MP3 API",
    description=(
        "Send an audio URL, get back a real MP3 (libmp3lame, 128 kbps). "
        "Built for n8n. Try **POST /convert** below."
    ),
    version="1.0.0",
)

_BITRATE_RE = re.compile(r"^\d{1,4}k$")


class ConvertRequest(BaseModel):
    # Accept both "audio_url" (what n8n sends) and "url" as an alias.
    audio_url: HttpUrl = Field(
        ...,
        validation_alias=AliasChoices("audio_url", "url"),
        description="Public URL of the source audio file.",
    )
    bitrate: str = Field(
        DEFAULT_BITRATE,
        description="MP3 bitrate, e.g. '128k', '192k', '320k'.",
        examples=["128k"],
    )
    return_type: Literal["url", "file"] = Field(
        "url",
        description="'url' → JSON with a public MP3 URL. 'file' → raw MP3 bytes.",
    )

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "audio_url": "https://download.samplelib.com/wav/sample-3s.wav",
                "bitrate": "128k",
                "return_type": "url",
            }
        },
    }


class ConvertResponse(BaseModel):
    id: str
    filename: str
    mp3_url: str
    size_bytes: int
    bitrate: str


def _build_url(request: Request, filename: str) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/files/{filename}"


async def _download(url: str, dest: Path) -> None:
    total = 0
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=DOWNLOAD_TIMEOUT
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Source returned HTTP {resp.status_code}",
                    )
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail="Source file exceeds size limit.",
                            )
                        fh.write(chunk)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Download failed: {exc}") from exc


async def _convert(src: Path, dst: Path, bitrate: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",  # drop any video/cover stream
        "-acodec",
        "libmp3lame",
        "-b:a",
        bitrate,
        str(dst),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(
            status_code=422,
            detail=f"FFmpeg conversion failed: {stderr.decode(errors='replace')[:500]}",
        )


@app.get("/health", tags=["meta"], summary="Health check")
async def health():
    return {"status": "ok"}


async def _produce_mp3(src: Path, bitrate: str, return_type: str, request: Request):
    """Shared core: convert an already-staged source file to MP3 and respond."""
    if not _BITRATE_RE.match(bitrate):
        raise HTTPException(
            status_code=400, detail="bitrate must look like '128k', '192k', '320k'."
        )

    job_id = uuid.uuid4().hex
    out_name = f"{job_id}.mp3"
    out = DATA_DIR / out_name

    try:
        await _convert(src, out, bitrate)
    finally:
        src.unlink(missing_ok=True)

    if return_type == "file":
        return FileResponse(out, media_type="audio/mpeg", filename=out_name)

    return ConvertResponse(
        id=job_id,
        filename=out_name,
        mp3_url=_build_url(request, out_name),
        size_bytes=out.stat().st_size,
        bitrate=bitrate,
    )


@app.post(
    "/convert",
    response_model=ConvertResponse,
    tags=["convert"],
    summary="Convert an audio URL to MP3 (JSON)",
    description="Variant 1 — n8n sends a JSON body with `audio_url` (or `url`).",
    responses={200: {"content": {"audio/mpeg": {}}}},
)
async def convert_url(req: ConvertRequest, request: Request):
    job_id = uuid.uuid4().hex
    src = DATA_DIR / f"{job_id}.src"
    try:
        await _download(str(req.audio_url), src)
    except Exception:
        src.unlink(missing_ok=True)
        raise
    return await _produce_mp3(src, req.bitrate, req.return_type, request)


@app.post(
    "/convert/upload",
    response_model=ConvertResponse,
    tags=["convert"],
    summary="Convert an uploaded audio file to MP3 (multipart)",
    description=(
        "Variant 2 — n8n uploads the audio file directly as multipart/form-data "
        "in the `file` field."
    ),
    responses={200: {"content": {"audio/mpeg": {}}}},
)
async def convert_upload(
    request: Request,
    file: UploadFile = File(..., description="Audio file to convert."),
    bitrate: str = Form(DEFAULT_BITRATE, description="MP3 bitrate, e.g. '128k'."),
    return_type: Literal["url", "file"] = Form(
        "url", description="'url' → JSON with public MP3 URL. 'file' → raw MP3 bytes."
    ),
):
    job_id = uuid.uuid4().hex
    src = DATA_DIR / f"{job_id}.src"
    total = 0
    try:
        with src.open("wb") as fh:
            while chunk := await file.read(64 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise HTTPException(
                        status_code=413, detail="Uploaded file exceeds size limit."
                    )
                fh.write(chunk)
    except Exception:
        src.unlink(missing_ok=True)
        raise
    if total == 0:
        src.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Empty upload.")
    return await _produce_mp3(src, bitrate, return_type, request)


@app.get(
    "/files/{filename}",
    tags=["files"],
    summary="Download a converted MP3",
)
async def get_file(filename: str):
    # Prevent path traversal: only allow our generated names.
    if not re.fullmatch(r"[0-9a-f]{32}\.mp3", filename):
        raise HTTPException(status_code=404, detail="Not found")
    path = DATA_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="audio/mpeg", filename=filename)


@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse({"service": "ffmpeg-api", "docs": "/docs", "health": "/health"})
