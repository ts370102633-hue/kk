"""
StepAudio Voice Studio — Vercel 异步模式
API 调用超时长，用轮询方式避免 Vercel 10 秒超时
"""
from __future__ import annotations
import uuid
import base64
import threading
from datetime import datetime
from pathlib import Path
import requests

API_BASE = "https://api.stepfun.com/step_plan/v1"
API_KEY = "3TK66wvoMBlQUYt953nHgtKoNC8SAJyRCIUjzNSz3ZtatHm0fehfODRkhYbYcfsyb"
MODEL = "stepaudio-2.5-tts"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# 内存任务存储
_tasks = {}


def _do_clone(task_id: str, file_bytes: bytes, filename: str):
    try:
        # 上传
        resp = requests.post("https://api.stepfun.com/v1/files", headers=HEADERS,
            files={"file": (filename, file_bytes, "audio/wav")}, data={"purpose": "storage"}, timeout=60)
        resp.raise_for_status()
        file_id = resp.json()["id"]
        # 克隆
        resp = requests.post(f"{API_BASE}/audio/voices", headers=HEADERS,
            json={"file_id": file_id, "model": MODEL}, timeout=60)
        resp.raise_for_status()
        step_voice_id = resp.json()["id"]
        # 生成示例
        audio_bytes = _tts_sync("声音克隆成功，以后可以直接选择这个声音朗读文案。", step_voice_id,
                                "用情绪高昂、积极向上、充满活力的语气")
        _tasks[task_id] = {"status": "done", "step_voice_id": step_voice_id,
                           "sample_audio": base64.b64encode(audio_bytes).decode()}
    except Exception as e:
        _tasks[task_id] = {"status": "error", "error": str(e)}


def _do_tts(task_id: str, step_voice_id: str, text: str, instruction: str):
    try:
        audio_bytes = _tts_sync(text, step_voice_id, instruction)
        _tasks[task_id] = {"status": "done", "audio": base64.b64encode(audio_bytes).decode()}
    except Exception as e:
        _tasks[task_id] = {"status": "error", "error": str(e)}


def _tts_sync(text: str, voice_id: str, instruction: str = "") -> bytes:
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
    content = await file.read()
    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"status": "processing"}
    thread = threading.Thread(target=_do_clone, args=(task_id, content, file.filename or "audio.wav"))
    thread.start()
    return JSONResponse({"task_id": task_id})


@app.post("/api/tts")
async def create_tts(request: Request):
    body = await request.json()
    step_voice_id = body.get("step_voice_id")
    text = body.get("text", "")
    instruction = body.get("instruction", "用情绪高昂、积极向上、充满活力的语气")
    if not step_voice_id or not text:
        raise HTTPException(400, "缺少参数")
    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"status": "processing"}
    thread = threading.Thread(target=_do_tts, args=(task_id, step_voice_id, text, instruction))
    thread.start()
    return JSONResponse({"task_id": task_id})


@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404)
    return JSONResponse(task)
