"""
StepAudio Voice Studio — 带用户认证和积分系统
"""
from __future__ import annotations
import uuid
import base64
import sqlite3
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Optional
import requests
from passlib.context import CryptContext
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

# === 数据库 ===
import tempfile
DB_PATH = Path(tempfile.gettempdir()) / "users.db"

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            credits INTEGER DEFAULT 100,
            is_admin BOOLEAN DEFAULT 0,
            last_login_date TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invite_codes (
            code TEXT PRIMARY KEY,
            created_by TEXT NOT NULL,
            used_by TEXT,
            is_used BOOLEAN DEFAULT 0,
            created_at TEXT
        )
    """)
    # 创建默认管理员账号
    admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    if not admin:
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        admin_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, username, password_hash, credits, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, "admin", pwd_context.hash("admin123"), 999999, 1, datetime.utcnow().isoformat())
        )
    conn.commit()
    conn.close()

# === 密码和 JWT ===
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="无效的token")
    except JWTError:
        raise HTTPException(status_code=401, detail="token已过期")

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return dict(user)

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

    conn = get_db()

    # 检查用户名是否已存在
    if conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone():
        conn.close()
        raise HTTPException(400, "用户名已存在")

    # 验证邀请码
    code_row = conn.execute("SELECT * FROM invite_codes WHERE code = ? AND is_used = 0", (invite_code,)).fetchone()
    if not code_row:
        conn.close()
        raise HTTPException(400, "邀请码无效或已使用")

    # 创建用户
    user_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, credits, is_admin, last_login_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, get_password_hash(password), INITIAL_CREDITS, 0, date.today().isoformat(), now)
    )

    # 标记邀请码已使用
    conn.execute("UPDATE invite_codes SET is_used = 1, used_by = ? WHERE code = ?", (user_id, invite_code))

    conn.commit()
    conn.close()

    token = create_access_token({"sub": user_id})
    return {"token": token, "user_id": user_id, "username": username, "credits": INITIAL_CREDITS}

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if not user or not verify_password(password, user["password_hash"]):
        conn.close()
        raise HTTPException(401, "用户名或密码错误")

    # 检查每日登录奖励
    today = date.today().isoformat()
    if user["last_login_date"] != today:
        conn.execute("UPDATE users SET credits = credits + ?, last_login_date = ? WHERE id = ?",
                     (DAILY_BONUS, today, user["id"]))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()

    conn.close()

    token = create_access_token({"sub": user["id"]})
    return {"token": token, "user_id": user["id"], "username": user["username"], "credits": user["credits"]}

@app.get("/api/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {"user_id": current_user["id"], "username": current_user["username"],
            "credits": current_user["credits"], "is_admin": current_user["is_admin"]}

# === 管理员 API ===
@app.post("/api/admin/generate-code")
async def generate_invite_code(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")

    code = str(uuid.uuid4())[:8].upper()
    conn = get_db()
    conn.execute(
        "INSERT INTO invite_codes (code, created_by, is_used, created_at) VALUES (?, ?, ?, ?)",
        (code, current_user["id"], 0, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    return {"code": code}

@app.get("/api/admin/users")
async def list_users(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")

    conn = get_db()
    users = conn.execute("SELECT id, username, credits, is_admin, last_login_date, created_at FROM users").fetchall()
    conn.close()
    return [dict(u) for u in users]

@app.get("/api/admin/codes")
async def list_invite_codes(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")

    conn = get_db()
    codes = conn.execute("SELECT * FROM invite_codes ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(c) for c in codes]

# === 克隆和 TTS API（需要认证和积分）===
@app.post("/api/clone")
async def clone_voice(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    # 检查积分
    if current_user["credits"] < COST_PER_USE:
        raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分，当前 {current_user['credits']} 积分")

    # 扣除积分
    conn = get_db()
    conn.execute("UPDATE users SET credits = credits - ? WHERE id = ?", (COST_PER_USE, current_user["id"]))
    conn.commit()
    conn.close()

    content = await file.read()
    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"status": "processing"}
    content_type = file.content_type or "audio/wav"
    thread = threading.Thread(target=_do_clone, args=(task_id, content, file.filename or "audio.wav", content_type))
    thread.start()
    return JSONResponse({"task_id": task_id})

@app.post("/api/tts")
async def create_tts(request: Request, current_user: dict = Depends(get_current_user)):
    # 检查积分
    if current_user["credits"] < COST_PER_USE:
        raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分，当前 {current_user['credits']} 积分")

    body = await request.json()
    step_voice_id = body.get("step_voice_id")
    text = body.get("text", "")
    instruction = body.get("instruction", "用情绪高昂、积极向上、充满活力的语气")
    if not step_voice_id or not text:
        raise HTTPException(400, "缺少参数")

    # 扣除积分
    conn = get_db()
    conn.execute("UPDATE users SET credits = credits - ? WHERE id = ?", (COST_PER_USE, current_user["id"]))
    conn.commit()
    conn.close()

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
