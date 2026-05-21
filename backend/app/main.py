"""
StepAudio Voice Studio — 带用户认证和积分系统
"""
from __future__ import annotations
import uuid
import base64
import hashlib
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Optional
import requests
from jose import JWTError, jwt
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

# === 配置 ===
API_BASE = "https://api.stepfun.com/step_plan/v1"
API_KEY = "3TK66wvoMBlQUYt953nHgtKoNC8SAJyRCIUjzNSz3ZtatHm0fehfODRkhYbYcfsyb"
MODEL = "stepaudio-2.5-tts"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}
SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"
INITIAL_CREDITS = 100
DAILY_BONUS = 10
COST_PER_USE = 1

# === 密码和 JWT ===
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return hash_password(plain_password) == hashed_password

def create_access_token(data: dict):
    to_encode = data.copy()
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# === 内存数据库 ===
_users = {}
_invite_codes = {}

def init_db():
    # 创建默认管理员账号
    if "admin" not in _users:
        _users["admin"] = {
            "id": str(uuid.uuid4()),
            "username": "admin",
            "password_hash": hash_password("admin123"),
            "credits": 999999,
            "is_admin": True,
            "last_login_date": date.today().isoformat(),
            "created_at": datetime.utcnow().isoformat()
        }

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username or username not in _users:
            raise HTTPException(status_code=401, detail="无效的token")
    except JWTError:
        raise HTTPException(status_code=401, detail="token已过期")
    return _users[username]

# === 内存任务存储 ===
_tasks = {}

# === StepAudio API ===
def _upload_file(file_bytes: bytes, filename: str, content_type: str = "audio/wav") -> str:
    resp = requests.post("https://api.stepfun.com/v1/files", headers=HEADERS,
        files={"file": (filename, file_bytes, content_type)}, data={"purpose": "storage"}, timeout=60)
    resp.raise_for_status()
    return resp.json()["id"]

def _clone_voice(file_bytes: bytes, filename: str, content_type: str = "audio/wav") -> dict:
    file_id = _upload_file(file_bytes, filename, content_type)
    resp = requests.post(f"{API_BASE}/audio/voices", headers=HEADERS,
        json={"file_id": file_id, "model": MODEL}, timeout=60)
    resp.raise_for_status()
    return resp.json()

def _tts_sync(text: str, voice_id: str, instruction: str = "") -> bytes:
    payload = {"model": MODEL, "voice": voice_id, "input": text}
    if instruction:
        payload["instruction"] = instruction
    resp = requests.post(f"{API_BASE}/audio/speech", headers=HEADERS, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.content

def _do_clone(task_id: str, file_bytes: bytes, filename: str, content_type: str = "audio/wav"):
    try:
        result = _clone_voice(file_bytes, filename, content_type)
        step_voice_id = result["id"]
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

# === FastAPI ===
app = FastAPI(title="StepAudio Voice Studio")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

_static_dir = Path(__file__).parent / "static"

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    return {"ok": True, "engine": "stepaudio"}

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return (_static_dir / "index.html").read_text(encoding="utf-8")

# === 用户认证 API ===
@app.post("/api/register")
async def register(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    invite_code = body.get("invite_code", "").strip()

    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    if len(password) < 6:
        raise HTTPException(400, "密码至少6位")

    # 检查用户名是否已存在
    if username in _users:
        raise HTTPException(400, "用户名已存在")

    # 验证邀请码
    if invite_code not in _invite_codes or _invite_codes[invite_code]["is_used"]:
        raise HTTPException(400, "邀请码无效或已使用")

    # 创建用户
    _users[username] = {
        "id": str(uuid.uuid4()),
        "username": username,
        "password_hash": hash_password(password),
        "credits": INITIAL_CREDITS,
        "is_admin": False,
        "last_login_date": date.today().isoformat(),
        "created_at": datetime.utcnow().isoformat()
    }

    # 标记邀请码已使用
    _invite_codes[invite_code]["is_used"] = True
    _invite_codes[invite_code]["used_by"] = username

    token = create_access_token({"sub": username})
    return {"token": token, "username": username, "credits": INITIAL_CREDITS}

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if username not in _users:
        raise HTTPException(401, "用户名或密码错误")

    user = _users[username]
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")

    # 检查每日登录奖励
    today = date.today().isoformat()
    if user["last_login_date"] != today:
        user["credits"] += DAILY_BONUS
        user["last_login_date"] = today

    token = create_access_token({"sub": username})
    return {"token": token, "username": username, "credits": user["credits"]}

@app.get("/api/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {"username": current_user["username"], "credits": current_user["credits"],
            "is_admin": current_user["is_admin"]}

# === 管理员 API ===
@app.post("/api/admin/generate-code")
async def generate_invite_code(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")

    code = str(uuid.uuid4())[:8].upper()
    _invite_codes[code] = {
        "code": code,
        "created_by": current_user["username"],
        "is_used": False,
        "used_by": None,
        "created_at": datetime.utcnow().isoformat()
    }
    return {"code": code}

@app.get("/api/admin/users")
async def list_users(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    return [{"username": u["username"], "credits": u["credits"], "is_admin": u["is_admin"],
             "created_at": u["created_at"]} for u in _users.values()]

@app.get("/api/admin/codes")
async def list_invite_codes(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    return list(_invite_codes.values())

# === 克隆和 TTS API（需要认证和积分）===
@app.post("/api/clone")
async def clone_voice(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    if current_user["credits"] < COST_PER_USE:
        raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分，当前 {current_user['credits']} 积分")

    current_user["credits"] -= COST_PER_USE

    content = await file.read()
    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"status": "processing"}
    content_type = file.content_type or "audio/wav"
    thread = threading.Thread(target=_do_clone, args=(task_id, content, file.filename or "audio.wav", content_type))
    thread.start()
    return JSONResponse({"task_id": task_id})

@app.post("/api/tts")
async def create_tts(request: Request, current_user: dict = Depends(get_current_user)):
    if current_user["credits"] < COST_PER_USE:
        raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分，当前 {current_user['credits']} 积分")

    body = await request.json()
    step_voice_id = body.get("step_voice_id")
    text = body.get("text", "")
    instruction = body.get("instruction", "用情绪高昂、积极向上、充满活力的语气")
    if not step_voice_id or not text:
        raise HTTPException(400, "缺少参数")

    current_user["credits"] -= COST_PER_USE

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
