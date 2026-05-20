"""
StepAudio Voice Studio — Vercel 无服务器版本
内存数据库，不依赖文件系统
"""
from __future__ import annotations
import io
import uuid
import base64
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
import requests
import soundfile as sf
import numpy as np


# === 内存数据库 ===
_voices = {}
_jobs = {}


# === StepAudio 引擎 ===
API_BASE = "https://api.stepfun.com/step_plan/v1"
API_KEY = "3TK66wvoMBlQUYt953nHgtKoNC8SAJyRCIUjzNSz3ZtatHm0fehfODRkhYbYcfsyb"
MODEL = "stepaudio-2.5-tts"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def _upload_file(file_bytes: bytes, filename: str) -> str:
    resp = requests.post(
        "https://api.stepfun.com/v1/files",
        headers=HEADERS,
        files={"file": (filename, file_bytes, "audio/wav")},
        data={"purpose": "storage"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _clone_voice(file_bytes: bytes, filename: str) -> str:
    file_id = _upload_file(file_bytes, filename)
    resp = requests.post(
        f"{API_BASE}/audio/voices",
        headers=HEADERS,
        json={"file_id": file_id, "model": MODEL},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _tts(text: str, voice_id: str, instruction: str = "") -> bytes:
    payload = {"model": MODEL, "voice": voice_id, "input": text}
    if instruction:
        payload["instruction"] = instruction
    resp = requests.post(f"{API_BASE}/audio/speech", headers=HEADERS, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.content


# === FastAPI ===
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

app = FastAPI(title="StepAudio Voice Studio")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def _voice_out(v):
    return {
        "id": v["id"], "name": v["name"], "speaker_name": v.get("speaker_name"),
        "department": v.get("department"), "language": v.get("language", "ZH"),
        "style_tags": v.get("style_tags"), "status": v["status"],
        "error_message": v.get("error_message"),
        "sample_audio_url": f"/api/voices/{v['id']}/audio" if v.get("audio_b64") else None,
        "created_at": v.get("created_at"), "updated_at": v.get("updated_at"),
    }


def _job_out(j):
    return {
        "id": j["id"], "title": j.get("title"), "voice_id": j.get("voice_id"),
        "voice_name": j.get("voice_name"), "text": j.get("text"),
        "language": j.get("language"), "emotion": j.get("emotion"),
        "speed": j.get("speed"), "status": j["status"],
        "output_audio_url": f"/api/jobs/{j['id']}/audio" if j.get("audio_b64") else None,
        "duration_seconds": j.get("duration_seconds"),
        "error_message": j.get("error_message"),
        "created_at": j.get("created_at"), "completed_at": j.get("completed_at"),
    }


@app.get("/health")
def health():
    return {"ok": True, "engine": "stepaudio"}


@app.get("/api/voices")
def list_voices():
    return [_voice_out(v) for v in sorted(_voices.values(), key=lambda x: x.get("created_at", ""), reverse=True)]


@app.get("/api/voices/{voice_id}")
def get_voice(voice_id: str):
    v = _voices.get(voice_id)
    if not v:
        raise HTTPException(404)
    return _voice_out(v)


@app.get("/api/voices/{voice_id}/audio")
def get_voice_audio(voice_id: str):
    v = _voices.get(voice_id)
    if not v or not v.get("audio_b64"):
        raise HTTPException(404)
    return Response(content=base64.b64decode(v["audio_b64"]), media_type="audio/mpeg")


@app.post("/api/voices")
async def create_voice(
    name: str = Form(...), speaker_name: str = Form(""), department: str = Form(""),
    language: str = Form("ZH"), style_tags: str = Form(""), file: UploadFile = File(...),
):
    vid = str(uuid.uuid4())
    content = await file.read()
    _voices[vid] = {
        "id": vid, "name": name, "speaker_name": speaker_name, "department": department,
        "language": language, "style_tags": style_tags, "status": "processing",
        "created_at": datetime.utcnow().isoformat(), "updated_at": datetime.utcnow().isoformat(),
    }
    # 同步克隆（Vercel 有 10s 超时，实际可能需要异步）
    try:
        step_voice_id = _clone_voice(content, file.filename or "audio.wav")
        # 生成示例音频
        audio_bytes = _tts("声音克隆成功，以后可以直接选择这个声音朗读文案。", step_voice_id)
        _voices[vid].update({
            "status": "active", "step_voice_id": step_voice_id,
            "audio_b64": base64.b64encode(audio_bytes).decode(),
            "updated_at": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        _voices[vid].update({
            "status": "failed", "error_message": str(e),
            "updated_at": datetime.utcnow().isoformat(),
        })
    return _voice_out(_voices[vid])


@app.delete("/api/voices/{voice_id}")
def delete_voice(voice_id: str):
    _voices.pop(voice_id, None)
    return {"ok": True}


@app.get("/api/jobs")
def list_jobs():
    return [_job_out(j) for j in sorted(_jobs.values(), key=lambda x: x.get("created_at", ""), reverse=True)]


@app.post("/api/tts")
async def create_tts(request: Request):
    body = await request.json()
    voice_id = body.get("voice_id")
    text = body.get("text", "")
    title = body.get("title", "")
    emotion = body.get("emotion", "natural")

    v = _voices.get(voice_id)
    if not v or v.get("status") != "active":
        raise HTTPException(400, "声音未就绪")

    jid = str(uuid.uuid4())
    _jobs[jid] = {
        "id": jid, "title": title, "voice_id": voice_id, "voice_name": v.get("name"),
        "text": text, "language": body.get("language", "ZH"), "emotion": emotion,
        "speed": body.get("speed", 1.0), "status": "processing",
        "created_at": datetime.utcnow().isoformat(),
    }

    try:
        instruction = ""
        emotion_map = {"happy": "开心愉快", "sad": "低沉悲伤", "angry": "严肃有力", "gentle": "温柔轻柔"}
        if emotion in emotion_map:
            instruction = f"用{emotion_map[emotion]}的语气"
        audio_bytes = _tts(text, v["step_voice_id"], instruction=instruction)
        _jobs[jid].update({
            "status": "completed", "audio_b64": base64.b64encode(audio_bytes).decode(),
            "completed_at": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        _jobs[jid].update({"status": "failed", "error_message": str(e)})

    return _job_out(_jobs[jid])


@app.get("/api/jobs/{job_id}/audio")
def get_job_audio(job_id: str):
    j = _jobs.get(job_id)
    if not j or not j.get("audio_b64"):
        raise HTTPException(404)
    return Response(content=base64.b64decode(j["audio_b64"]), media_type="audio/mpeg")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    _jobs.pop(job_id, None)
    return {"ok": True}


# === 前端 ===
_static_dir = Path(__file__).parent / "static"

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return (_static_dir / "index.html").read_text(encoding="utf-8")
