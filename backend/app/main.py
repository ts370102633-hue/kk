from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pathlib import Path
from .config import get_settings
from .db import init_db
from .schemas import HealthOut
from .storage import ensure_bucket
from .routers import voices, tts, jobs

settings = get_settings()
app = FastAPI(title="StepAudio Voice Studio", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def on_startup():
    init_db()
    ensure_bucket()


@app.get("/health", response_model=HealthOut)
def health():
    return HealthOut(ok=True, env=settings.app_env, openvoice_mode="stepaudio")


app.include_router(voices.router)
app.include_router(tts.router)
app.include_router(jobs.router)


# 文件服务（支持 Range 请求）
storage_path = Path(settings.local_storage_dir)
storage_path.mkdir(parents=True, exist_ok=True)

@app.get("/files/{file_path:path}")
async def serve_file(file_path: str, request: Request):
    full_path = storage_path / file_path
    if not full_path.exists() or not full_path.is_file():
        return HTMLResponse("Not Found", status_code=404)
    file_size = full_path.stat().st_size
    suffix = full_path.suffix.lower()
    ct_map = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}
    content_type = ct_map.get(suffix, "application/octet-stream")
    range_header = request.headers.get("range")
    if range_header:
        start, end = 0, file_size - 1
        parts = range_header.replace("bytes=", "").split("-")
        if parts[0]: start = int(parts[0])
        if len(parts) > 1 and parts[1]: end = int(parts[1])
        content_length = end - start + 1
        def iter_file():
            with open(full_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(8192, remaining))
                    if not chunk: break
                    remaining -= len(chunk)
                    yield chunk
        return StreamingResponse(iter_file(), status_code=206, media_type=content_type,
            headers={"Content-Range": f"bytes {start}-{end}/{file_size}", "Accept-Ranges": "bytes", "Content-Length": str(content_length)})
    return FileResponse(str(full_path), media_type=content_type)


# 前端
static_dir = Path(__file__).parent / "static"
index_path = static_dir / "index.html"

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_index():
    return FileResponse(str(index_path))
