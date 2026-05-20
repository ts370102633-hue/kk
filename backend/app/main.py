"""
StepAudio Voice Studio — Vercel 无服务器版本
数据存在浏览器 localStorage，不会因重新部署丢失
"""
from __future__ import annotations
import uuid
import base64
from datetime import datetime
from pathlib import Path
import requests

API_BASE = "https://api.stepfun.com/step_plan/v1"
API_KEY = "3TK66wvoMBlQUYt953nHgtKoNC8SAJyRCIUjzNSz3ZtatHm0fehfODRkhYbYcfsyb"
MODEL = "stepaudio-2.5-tts"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def _upload_file(file_bytes: bytes, filename: str) -> str:
    resp = requests.post("https://api.stepfun.com/v1/files", headers=HEADERS,
        files={"file": (filename, file_bytes, "audio/wav")}, data={"purpose": "storage"}, timeout=60)
    resp.raise_for_status()
    return resp.json()["id"]


def _clone_voice(file_bytes: bytes, filename: str) -> dict:
    file_id = _upload_file(file_bytes, filename)
    resp = requests.post(f"{API_BASE}/audio/voices", headers=HEADERS,
        json={"file_id": file_id, "model": MODEL}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _tts(text: str, voice_id: str, instruction: str = "") -> bytes:
    payload = {"model": MODEL, "voice": voice_id, "input": text}
    if instruction:
        payload["instruction"] = instruction
    resp = requests.post(f"{API_BASE}/audio/speech", headers=HEADERS, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.content


from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

app = FastAPI(title="StepAudio Voice Studio")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

_static_dir = Path(__file__).parent / "static"


@app.get("/health")
def health():
    return {"ok": True, "engine": "stepaudio"}


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return (_static_dir / "index.html").read_text(encoding="utf-8")


@app.post("/api/clone")
async def clone_voice(file: UploadFile = File(...)):
    """上传音频 → 克隆 → 返回 step_voice_id 和示例音频"""
    content = await file.read()
    result = _clone_voice(content, file.filename or "audio.wav")
    step_voice_id = result["id"]
    # 生成示例音频
    audio_bytes = _tts("声音克隆成功，以后可以直接选择这个声音朗读文案。", step_voice_id,
                        instruction="用情绪高昂、积极向上、充满活力的语气")
    return JSONResponse({
        "step_voice_id": step_voice_id,
        "sample_audio": base64.b64encode(audio_bytes).decode(),
    })


@app.post("/api/tts")
async def create_tts(request: Request):
    """生成语音 → 返回 base64 音频"""
    body = await request.json()
    step_voice_id = body.get("step_voice_id")
    text = body.get("text", "")
    if not step_voice_id or not text:
        raise HTTPException(400, "缺少参数")
    instruction = "用情绪高昂、积极向上、充满活力的语气"
    audio_bytes = _tts(text, step_voice_id, instruction=instruction)
    return JSONResponse({
        "audio": base64.b64encode(audio_bytes).decode(),
        "duration": len(audio_bytes) / 16000,  # 估算时长
    })
