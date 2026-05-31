"""
StepAudio Voice Studio — 带用户认证和积分系统
"""
from __future__ import annotations
import uuid
import base64
import hashlib
import hmac
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, date
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote
import numpy as np
import requests
import soundfile as sf
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from .config import get_settings
from . import persistent_store as store

# === 配置 ===
settings = get_settings()
API_BASE = settings.step_api_base.rstrip("/")
FILE_API_BASE = settings.step_file_api_base.rstrip("/")
MODEL = settings.step_tts_model
ADMIN_USERNAME = settings.admin_username.strip() or "admin"
ADMIN_PASSWORD = settings.admin_password
DEFAULT_ADMIN_PASSWORD = "admin123"
INITIAL_CREDITS = 100
DAILY_BONUS = 10
COST_PER_USE = 1
CLONE_MIN_SECONDS = 5.0
CLONE_TARGET_SECONDS = 10.0
CLONE_MAX_SECONDS = 10.0
CLONE_ANALYSIS_MAX_SECONDS = 180.0
CLONE_MIN_VOICE_SECONDS = 2.5
TTS_SEGMENT_MAX_CHARS = 220
TTS_RETRY_ATTEMPTS = 2
VIDEO_HISTORY_LIMIT = 30
VIDEO_GLOBAL_CONCURRENCY = 2
VIDEO_MAX_RETRIES = 2
VIDEO_SINGLE_ATTEMPT_MODES = {"original", "tikhub"}
VIDEO_ACTIVE_STATUSES = {"queued", "processing", "retrying"}
VIDEO_COOKIE_KEYS = {
    "xhs": "XHS_COOKIE",
    "douyin": "DOUYIN_COOKIE",
}

_video_download_queue: queue.Queue[str] = queue.Queue()
_video_workers_started = False
_video_workers_lock = threading.Lock()
_video_submission_lock = threading.Lock()

# === 密码 ===
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return hmac.compare_digest(hash_password(plain_password), hashed_password)

def get_stepaudio_headers() -> dict:
    api_key = get_settings().step_api_key.strip()
    if not api_key:
        raise RuntimeError("STEP_API_KEY 未配置，请在 .env 或云服务器环境变量中设置")
    return {"Authorization": f"Bearer {api_key}"}

def init_db():
    if settings.app_env != "local" and ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD:
        raise RuntimeError("生产环境必须配置 ADMIN_PASSWORD，不能使用默认管理员密码")
    store.init_db()
    store.ensure_admin(
        username=ADMIN_USERNAME,
        password_hash=hash_password(ADMIN_PASSWORD),
        credits=999999,
        last_login_date=date.today().isoformat(),
    )

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.split(" ")[1]
    username = store.get_token_username(token)
    if not username:
        raise HTTPException(status_code=401, detail="token已过期")
    user = store.get_user(username)
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


def _safe_video_filename(title: str | None) -> str:
    name = (title or "video").strip()
    name = re.sub(r"[\r\n\t]+", " ", name)
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "video")[:80] + ".mp4"


def _safe_audio_filename(title: str | None) -> str:
    name = (title or "sample").strip()
    name = re.sub(r"[\r\n\t]+", " ", name)
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "sample")[:80] + ".mp3"


def _delete_video_files(task: dict) -> None:
    file_path = task.get("file_path")
    if not file_path:
        return
    storage_root = Path(get_settings().local_storage_dir).resolve()
    path = Path(file_path)
    try:
        resolved = path.resolve()
        resolved.relative_to(storage_root)
    except Exception:
        return
    try:
        if resolved.exists():
            resolved.unlink()
        parent = resolved.parent
        if parent.name == task.get("id") and parent.exists():
            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def _cleanup_old_video_tasks(user_id: str) -> None:
    for old_task in store.prune_video_tasks(user_id, keep=VIDEO_HISTORY_LIMIT):
        _delete_video_files(old_task)


def _has_active_video_task(user_id: str) -> dict | None:
    for task in store.list_video_tasks(user_id, limit=None):
        if task.get("status") in VIDEO_ACTIVE_STATUSES:
            return task
    return None


def _format_video_failures(failures: list[dict]) -> str:
    if not failures:
        return "下载失败"
    return "\n".join(
        f"第 {item.get('attempt', idx + 1)} 次失败：{item.get('error', '未知错误')}"
        for idx, item in enumerate(failures)
    )


def _video_max_retries_for_mode(download_mode: str) -> int:
    if (download_mode or "").strip().lower() in VIDEO_SINGLE_ATTEMPT_MODES:
        return 0
    return VIDEO_MAX_RETRIES


def _is_non_retryable_video_error(error: str) -> bool:
    text = (error or "").lower()
    quota_markers = [
        "第三方原画接口限额",
        "429",
        "too many requests",
        "rate limit",
        "quota",
        "今日额度",
        "高清额度",
    ]
    return any(marker in text for marker in quota_markers)


def _compact_video_error(error: str) -> str:
    if _is_non_retryable_video_error(error):
        if "今日" in (error or "") or "每日" in (error or "") or "每天" in (error or "") or "daily" in (error or "").lower():
            return "第三方原画接口今日高清额度已用完，严格原画模式已取消低清下载；请明天额度恢复后再试"
        return "第三方原画接口请求失败，严格原画模式已取消低清下载；请稍后再试"
    cleaned = re.sub(r"https?://\S+", "[已省略链接]", error or "下载失败")
    return cleaned[:500]


def _refund_video_credit_once(task_id: str) -> None:
    task = store.get_video_task(task_id) or {}
    if task.get("credits_refunded"):
        return
    username = task.get("username", "")
    if username:
        store.add_credits(username, COST_PER_USE)
    store.update_video_task(task_id, {"credits_refunded": True})


def _video_cookie_setting_key(env_name: str) -> str:
    return f"video_cookie_{env_name.lower()}"


def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:6]}...{value[-6:]}"


def _normalize_cookie_input(raw: str, platform: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    domain_key = "douyin.com" if platform == "douyin" else "xiaohongshu.com"
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not any("\t" in line for line in lines):
        return re.sub(r"^\s*Cookie:\s*", "", value, flags=re.I).strip()

    pairs: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, name, cookie_value = parts[0].strip(), parts[5].strip(), parts[6].strip()
        if domain_key not in domain.lower() or not name or not cookie_value:
            continue
        if name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={cookie_value}")
    return "; ".join(pairs)


def _video_cookie_status(platform: str) -> dict:
    env_name = VIDEO_COOKIE_KEYS[platform]
    stored = store.get_app_setting(_video_cookie_setting_key(env_name))
    stored_value = (stored or {}).get("value", "") if stored else ""
    env_value = os.getenv(env_name, "").strip()
    value = stored_value.strip() or env_value
    source = "admin" if stored_value.strip() else "env" if env_value else ""
    return {
        "platform": platform,
        "configured": bool(value),
        "source": source,
        "length": len(value),
        "preview": _mask_secret(value),
        "updated_at": (stored or {}).get("updated_at", "") if stored else "",
        "updated_by": (stored or {}).get("updated_by", "") if stored else "",
    }


def _run_video_download_task(task_id: str) -> None:
    task = store.get_video_task(task_id)
    if not task:
        return
    url = task.get("url", "")
    platform = task.get("platform", "")
    download_mode = task.get("download_mode", "auto")
    max_retries = task.get("max_retries")
    if not isinstance(max_retries, int):
        max_retries = _video_max_retries_for_mode(download_mode)
    failures: list[dict] = list(task.get("failure_reasons") or [])

    for attempt in range(1, max_retries + 2):
        status = "processing" if attempt == 1 else "retrying"
        update_fields = {
            "status": status,
            "attempt": attempt,
            "max_retries": max_retries,
            "started_at": datetime.utcnow().isoformat(),
        }
        if attempt == 1:
            update_fields["last_error"] = ""
        store.update_video_task(task_id, update_fields)
        try:
            from .video_downloader import download_video as dl_video
            output_dir = Path(get_settings().local_storage_dir) / "videos" / task_id
            output_dir.mkdir(parents=True, exist_ok=True)
            result = dl_video(url, output_dir, mode=download_mode)
            store.update_video_task(task_id, {
                "status": "done",
                "attempt": attempt,
                "title": result.get("title", ""),
                "description": result.get("description", ""),
                "file_path": result.get("file_path", ""),
                "file_size": result.get("file_size", 0),
                "platform": result.get("platform", ""),
                "download_mode": result.get("download_mode", download_mode),
                "quality": result.get("quality", ""),
                "width": result.get("width", 0),
                "height": result.get("height", 0),
                "bitrate": result.get("bitrate", 0),
                "codec": result.get("codec", ""),
                "quality_source": result.get("quality_source", ""),
                "shortcut_quota": result.get("shortcut_quota"),
                "shortcut_fallback_reason": result.get("shortcut_fallback_reason", ""),
                "candidate_count": result.get("candidate_count", 0),
                "candidate_diagnostics": result.get("candidate_diagnostics", []),
                "platform_cookie": result.get("platform_cookie", {}),
                "platform_cookie_configured": result.get("platform_cookie_configured", False),
                "completed_at": datetime.utcnow().isoformat(),
                "last_error": "",
                "error": "",
                "failure_reasons": failures,
            })
            return
        except Exception as e:
            raw_error = str(e)
            error = _compact_video_error(raw_error)
            non_retryable = _is_non_retryable_video_error(raw_error)
            failures.append({
                "attempt": attempt,
                "error": error,
                "time": datetime.utcnow().isoformat(),
            })
            if attempt <= max_retries and not non_retryable:
                store.update_video_task(task_id, {
                    "status": "retrying",
                    "attempt": attempt,
                    "last_error": error,
                    "failure_reasons": failures,
                    "next_retry_in_seconds": 3 if attempt == 1 else 10,
                })
                time.sleep(3 if attempt == 1 else 10)
                continue
            _refund_video_credit_once(task_id)
            final_error = error if non_retryable else _format_video_failures(failures)
            store.update_video_task(task_id, {
                "status": "error",
                "error": final_error,
                "last_error": error,
                "failure_reasons": failures,
                "failed_at": datetime.utcnow().isoformat(),
            })
            return


def _video_download_worker() -> None:
    while True:
        task_id = _video_download_queue.get()
        try:
            _run_video_download_task(task_id)
        finally:
            _video_download_queue.task_done()


def _start_video_download_workers() -> None:
    global _video_workers_started
    with _video_workers_lock:
        if _video_workers_started:
            return
        for idx in range(VIDEO_GLOBAL_CONCURRENCY):
            threading.Thread(
                target=_video_download_worker,
                name=f"video-download-worker-{idx + 1}",
                daemon=True,
            ).start()
        _video_workers_started = True


def _mark_interrupted_video_tasks() -> None:
    for task in store.list_video_tasks(limit=None):
        if task.get("status") in VIDEO_ACTIVE_STATUSES:
            _refund_video_credit_once(task["id"])
            store.update_video_task(task["id"], {
                "status": "error",
                "error": "服务重启，下载任务已中断，请重新提交",
                "last_error": "服务重启，下载任务已中断",
                "interrupted_at": datetime.utcnow().isoformat(),
            })

# === StepAudio API ===
def _upload_file(file_bytes: bytes, filename: str, content_type: str = "audio/wav") -> str:
    resp = requests.post(f"{FILE_API_BASE}/files", headers=get_stepaudio_headers(),
        files={"file": (filename, file_bytes, content_type)}, data={"purpose": "storage"}, timeout=60)
    if resp.status_code == 402:
        raise RuntimeError("StepAudio 文件上传失败：当前 API Key 的文件上传额度不足或未开通计费，请检查 StepFun 的 /v1/files 额度")
    if resp.status_code >= 400:
        raise RuntimeError(f"StepAudio 文件上传失败：HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()["id"]


def _probe_media_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("无法识别音频时长，请上传可播放的 mp3/wav 或带音轨的视频")
    try:
        return float((result.stdout or "0").strip())
    except Exception:
        raise RuntimeError("无法识别音频时长，请上传可播放的 mp3/wav 或带音轨的视频")


@dataclass
class CloneAudioPrep:
    path: Path
    filename: str
    content_type: str
    duration: float
    report: dict


def _dbfs(value: float) -> float:
    if value <= 1e-9:
        return -120.0
    return 20.0 * math.log10(value)


def _frame_rms_db(audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray, float]:
    frame_size = max(int(sample_rate * 0.2), 1)
    hop_size = max(int(sample_rate * 0.1), 1)
    if len(audio) < frame_size:
        rms = float(np.sqrt(np.mean(np.square(audio))) if len(audio) else 0.0)
        return np.array([0.0]), np.array([_dbfs(rms)]), -45.0

    starts = np.arange(0, len(audio) - frame_size + 1, hop_size)
    db_values: list[float] = []
    for start in starts:
        frame = audio[start:start + frame_size]
        rms = float(np.sqrt(np.mean(np.square(frame))))
        db_values.append(_dbfs(rms))
    frame_db = np.array(db_values)
    noise_floor = float(np.percentile(frame_db, 20)) if len(frame_db) else -60.0
    threshold = max(-45.0, min(-25.0, noise_floor + 10.0))
    return starts / sample_rate, frame_db, threshold


def _choose_clone_window(audio: np.ndarray, sample_rate: int) -> dict:
    duration = len(audio) / sample_rate if sample_rate else 0.0
    frame_times, frame_db, voice_threshold = _frame_rms_db(audio, sample_rate)
    voiced = frame_db >= voice_threshold

    if duration <= CLONE_TARGET_SECONDS:
        start, end = 0.0, duration
    else:
        step = 0.5
        best = (-1.0, 0.0, CLONE_TARGET_SECONDS)
        max_start = max(0.0, duration - CLONE_TARGET_SECONDS)
        scan_count = int(max_start / step) + 1
        for idx in range(scan_count + 1):
            candidate_start = min(idx * step, max_start)
            candidate_end = candidate_start + CLONE_TARGET_SECONDS
            mask = (frame_times >= candidate_start) & (frame_times < candidate_end)
            if not np.any(mask):
                continue
            window_db = frame_db[mask]
            window_voiced = voiced[mask]
            voice_ratio = float(np.mean(window_voiced))
            mean_voice_db = float(np.mean(window_db[window_voiced])) if np.any(window_voiced) else float(np.mean(window_db))
            peak = float(np.max(np.abs(audio[int(candidate_start * sample_rate):int(candidate_end * sample_rate)])))
            clipped_penalty = 25.0 if peak > 0.98 else 0.0
            score = voice_ratio * 100.0 + max(0.0, mean_voice_db + 45.0) - clipped_penalty
            if score > best[0]:
                best = (score, candidate_start, candidate_end)
        start, end = best[1], min(best[2], duration)

    start_sample = max(0, int(start * sample_rate))
    end_sample = min(len(audio), int(end * sample_rate))
    selected = audio[start_sample:end_sample]
    if len(selected) == 0:
        selected = audio[: int(min(duration, CLONE_TARGET_SECONDS) * sample_rate)]
        start, end = 0.0, len(selected) / sample_rate

    sel_times, sel_db, sel_threshold = _frame_rms_db(selected, sample_rate)
    sel_voiced = sel_db >= sel_threshold
    if np.any(sel_voiced):
        first = float(sel_times[np.argmax(sel_voiced)])
        last = float(sel_times[len(sel_voiced) - 1 - np.argmax(sel_voiced[::-1])]) + 0.2
        padded_start = max(0.0, first - 0.2)
        padded_end = min(len(selected) / sample_rate, last + 0.2)
        trimmed = selected[int(padded_start * sample_rate):int(padded_end * sample_rate)]
        if len(trimmed) / sample_rate >= CLONE_MIN_SECONDS:
            selected = trimmed
            start += padded_start
            end = start + len(selected) / sample_rate

    duration_selected = len(selected) / sample_rate if sample_rate else 0.0
    peak = float(np.max(np.abs(selected))) if len(selected) else 0.0
    rms = float(np.sqrt(np.mean(np.square(selected)))) if len(selected) else 0.0
    _, selected_db, selected_threshold = _frame_rms_db(selected, sample_rate)
    selected_voiced = selected_db >= selected_threshold
    voice_ratio = float(np.mean(selected_voiced)) if len(selected_voiced) else 0.0
    effective_voice_seconds = voice_ratio * duration_selected
    clipped_percent = float(np.mean(np.abs(selected) > 0.98)) if len(selected) else 0.0

    issues: list[str] = []
    if duration_selected < CLONE_MIN_SECONDS:
        issues.append("有效人声不足 5 秒")
    if effective_voice_seconds < CLONE_MIN_VOICE_SECONDS:
        issues.append("可识别人声偏少")
    if voice_ratio < 0.35:
        issues.append("背景声或空白较多")
    if _dbfs(rms) < -35:
        issues.append("音量偏低")
    if clipped_percent > 0.01:
        issues.append("音频有爆音风险")

    score = int(max(0, min(100, 30 + voice_ratio * 45 + max(0.0, _dbfs(rms) + 35.0) * 0.8 - clipped_percent * 1500)))
    if score >= 80 and not issues:
        level, level_text = "good", "优秀"
    elif score >= 60 or len(issues) <= 1:
        level, level_text = "ok", "可用"
    else:
        level, level_text = "poor", "偏弱"

    return {
        "audio": selected,
        "duration": round(duration_selected, 2),
        "selected_start": round(start, 2),
        "selected_end": round(end, 2),
        "voice_ratio": round(voice_ratio, 2),
        "effective_voice_seconds": round(effective_voice_seconds, 2),
        "rms_dbfs": round(_dbfs(rms), 1),
        "peak_dbfs": round(_dbfs(peak), 1),
        "clipped_percent": round(clipped_percent * 100, 2),
        "score": score,
        "level": level,
        "level_text": level_text,
        "issues": issues,
        "voice_threshold_dbfs": round(selected_threshold, 1),
    }


def _prepare_clone_audio(file_bytes: bytes, filename: str, workdir: Path) -> CloneAudioPrep:
    suffix = Path(filename or "audio").suffix.lower() or ".bin"
    src = workdir / f"source{suffix}"
    full_wav = workdir / "source_16k.wav"
    selected_raw = workdir / "selected_raw.wav"
    out = workdir / "clone_reference.wav"
    src.write_bytes(file_bytes)

    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i", str(src),
            "-vn",
            "-t", str(CLONE_ANALYSIS_MAX_SECONDS),
            "-ac", "1",
            "-ar", "16000",
            "-sample_fmt", "s16",
            str(full_wav),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not full_wav.exists() or full_wav.stat().st_size < 1000:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"音频预处理失败，请上传清晰人声 mp3/wav 或带音轨的视频：{detail[0][:160]}")

    try:
        audio, sample_rate = sf.read(str(full_wav), dtype="float32", always_2d=False)
    except Exception:
        raise RuntimeError("音频读取失败，请上传可播放的 mp3/wav 或带音轨的视频")
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)
    duration = len(audio) / sample_rate if sample_rate else 0.0
    if duration < CLONE_MIN_SECONDS:
        raise RuntimeError(
            f"音频有效时长 {duration:.1f} 秒，StepAudio 音色复刻要求至少 {CLONE_MIN_SECONDS:.0f} 秒；"
            "请上传至少 5 秒的清晰人声"
        )

    selection = _choose_clone_window(audio, int(sample_rate))
    selected_audio = np.asarray(selection.pop("audio"), dtype=np.float32)
    if selection["duration"] < CLONE_MIN_SECONDS or selection["effective_voice_seconds"] < CLONE_MIN_VOICE_SECONDS:
        raise RuntimeError(
            f"这段素材可识别人声只有 {selection['effective_voice_seconds']:.1f} 秒，建议换一段单人清晰讲话素材"
        )

    sf.write(str(selected_raw), selected_audio, int(sample_rate), subtype="PCM_16")
    filter_chain = "highpass=f=80,lowpass=f=7600,loudnorm=I=-18:TP=-1.5:LRA=11"
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i", str(selected_raw),
            "-af", filter_chain,
            "-ac", "1",
            "-ar", "16000",
            "-sample_fmt", "s16",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"参考音频标准化失败：{detail[0][:160]}")

    processed_duration = _probe_media_duration(out)
    report = {
        "original_duration": round(duration, 2),
        "processed_duration": round(processed_duration, 2),
        "selected_start": selection["selected_start"],
        "selected_end": selection["selected_end"],
        "voice_ratio": selection["voice_ratio"],
        "effective_voice_seconds": selection["effective_voice_seconds"],
        "rms_dbfs": selection["rms_dbfs"],
        "peak_dbfs": selection["peak_dbfs"],
        "clipped_percent": selection["clipped_percent"],
        "quality_score": selection["score"],
        "quality_level": selection["level"],
        "quality_level_text": selection["level_text"],
        "issues": selection["issues"],
        "preprocess": "自动选段、去静音、基础降噪、音量归一",
        "summary": (
            f"已选 {processed_duration:.1f} 秒参考片段，人声占比 {int(selection['voice_ratio'] * 100)}%，"
            f"质量：{selection['level_text']}"
        ),
    }
    return CloneAudioPrep(path=out, filename="clone_reference.wav", content_type="audio/wav", duration=processed_duration, report=report)


def _transcribe_clone_reference(audio_path: Path) -> tuple[str, str]:
    try:
        from .video_downloader import _transcribe_with_stepaudio
        text = _transcribe_with_stepaudio(str(audio_path)).strip()
        return text, ""
    except Exception as exc:
        return "", str(exc)


def _create_step_voice(file_id: str, reference_text: str, duration: float, warnings: list[str]) -> dict:
    payload = {"file_id": file_id, "model": MODEL}
    if reference_text.strip():
        payload["text"] = reference_text.strip()

    resp = requests.post(f"{API_BASE}/audio/voices", headers=get_stepaudio_headers(), json=payload, timeout=60)
    if resp.status_code < 400:
        return resp.json()

    if reference_text.strip() and resp.status_code == 400:
        warnings.append("参考文稿被接口拒绝，已自动退回普通克隆")
        fallback = requests.post(
            f"{API_BASE}/audio/voices",
            headers=get_stepaudio_headers(),
            json={"file_id": file_id, "model": MODEL},
            timeout=60,
        )
        if fallback.status_code < 400:
            return fallback.json()
        resp = fallback

    raise RuntimeError(
        f"StepAudio 音色复刻失败：HTTP {resp.status_code} {resp.text[:300]}；"
        f"已自动处理为 {duration:.1f} 秒 WAV，模型 {MODEL}"
    )


def _clone_voice(
    file_bytes: bytes,
    filename: str,
    content_type: str = "audio/wav",
    sample_text: str = "",
    clone_mode: str = "fast",
    auto_transcribe: bool = True,
) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        prep = _prepare_clone_audio(file_bytes, filename, Path(tmp))
        warnings = list(prep.report.get("issues") or [])
        manual_text = sample_text.strip()
        reference_text = manual_text
        reference_text_source = "manual" if manual_text else ""
        asr_text = ""
        asr_error = ""
        if auto_transcribe or clone_mode == "hifi":
            asr_text, asr_error = _transcribe_clone_reference(prep.path)
            if not reference_text and asr_text:
                reference_text = asr_text
                reference_text_source = "asr"
            elif asr_error:
                warnings.append("参考文稿自动识别失败，已继续克隆")

        file_id = _upload_file(prep.path.read_bytes(), prep.filename, prep.content_type)
        result = _create_step_voice(file_id, reference_text, prep.duration, warnings)
        result.update({
            "clone_mode": "hifi" if clone_mode == "hifi" else "fast",
            "reference_text": reference_text,
            "reference_text_source": reference_text_source,
            "asr_reference_text": asr_text,
            "asr_error": asr_error,
            "clone_quality": prep.report,
            "processed_reference_audio": base64.b64encode(prep.path.read_bytes()).decode(),
            "warnings": warnings,
        })
        return result


def _probe_audio_bytes_duration(audio_bytes: bytes, suffix: str = ".mp3") -> float:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"audio{suffix}"
        path.write_bytes(audio_bytes)
        return _probe_media_duration(path)


def _tts_once(text: str, voice_id: str, instruction: str = "") -> bytes:
    payload = {"model": MODEL, "voice": voice_id, "input": text}
    if instruction:
        payload["instruction"] = instruction
    resp = requests.post(f"{API_BASE}/audio/speech", headers=get_stepaudio_headers(), json=payload, timeout=120)
    if resp.status_code == 402:
        raise RuntimeError("StepAudio 语音生成失败：当前 API Key 额度不足或未开通计费")
    if resp.status_code >= 400:
        raise RuntimeError(f"StepAudio 语音生成失败：HTTP {resp.status_code} {resp.text[:200]}")
    return resp.content


def _tts_sync_with_meta(text: str, voice_id: str, instruction: str = "") -> tuple[bytes, float]:
    last_error = ""
    for attempt in range(1, TTS_RETRY_ATTEMPTS + 2):
        try:
            audio_bytes = _tts_once(text, voice_id, instruction)
            if len(audio_bytes) < 1000:
                raise RuntimeError("StepAudio 返回的音频文件为空")
            duration = _probe_audio_bytes_duration(audio_bytes)
            if duration < 0.5:
                raise RuntimeError("StepAudio 返回的音频时长异常")
            return audio_bytes, duration
        except Exception as exc:
            last_error = str(exc)
            if attempt <= TTS_RETRY_ATTEMPTS:
                time.sleep(0.8 * attempt)
    raise RuntimeError(last_error or "StepAudio 语音生成失败")


def _tts_sync(text: str, voice_id: str, instruction: str = "") -> bytes:
    audio_bytes, _ = _tts_sync_with_meta(text, voice_id, instruction)
    return audio_bytes


def _split_tts_text(text: str, max_chars: int = TTS_SEGMENT_MAX_CHARS) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not text:
        return []
    pieces: list[str] = []
    for paragraph in [p.strip() for p in re.split(r"\n+", text) if p.strip()]:
        sentences = re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", paragraph)
        if not sentences:
            sentences = [paragraph]
        current = ""
        for sentence in [s.strip() for s in sentences if s.strip()]:
            if len(sentence) > max_chars:
                if current:
                    pieces.append(current)
                    current = ""
                for idx in range(0, len(sentence), max_chars):
                    chunk = sentence[idx:idx + max_chars].strip()
                    if chunk:
                        pieces.append(chunk)
                continue
            if current and len(current) + len(sentence) > max_chars:
                pieces.append(current)
                current = sentence
            else:
                current = (current + sentence).strip()
        if current:
            pieces.append(current)
    return pieces or [text[:max_chars]]


def _concat_audio_segments(segments: list[bytes]) -> bytes:
    if len(segments) == 1:
        return segments[0]
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        list_path = tmpdir / "segments.txt"
        out_path = tmpdir / "merged.mp3"
        lines = []
        for idx, audio in enumerate(segments):
            path = tmpdir / f"seg_{idx}.mp3"
            path.write_bytes(audio)
            lines.append(f"file '{path.as_posix()}'")
        list_path.write_text("\n".join(lines), encoding="utf-8")
        attempts = [
            ["-c", "copy"],
            ["-c:a", "libmp3lame", "-b:a", "192k"],
        ]
        last_detail = ""
        for codec_args in attempts:
            result = subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), *codec_args, str(out_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000:
                return out_path.read_bytes()
            last_detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"分段音频合并失败：{last_detail[0][:160] if last_detail else ''}")


def _tts_generate_audio(text: str, voice_id: str, instruction: str = "") -> tuple[bytes, dict]:
    segments = _split_tts_text(text)
    if not segments:
        raise RuntimeError("文案内容为空")
    audio_segments: list[bytes] = []
    durations: list[float] = []
    for idx, segment in enumerate(segments, start=1):
        seg_audio, seg_duration = _tts_sync_with_meta(segment, voice_id, instruction)
        audio_segments.append(seg_audio)
        durations.append(round(seg_duration, 2))
        if idx < len(segments):
            time.sleep(0.2)
    merged = _concat_audio_segments(audio_segments)
    total_duration = _probe_audio_bytes_duration(merged)
    return merged, {
        "segment_count": len(segments),
        "segment_chars": [len(s) for s in segments],
        "segment_durations": durations,
        "duration_seconds": round(total_duration, 2),
        "split_enabled": len(segments) > 1,
        "quality_checked": True,
    }

def _refund_task_credit(task_id: str) -> None:
    task = store.get_task(task_id)
    username = task.get("username") if task else ""
    if username:
        store.add_credits(username, COST_PER_USE)

def _do_clone(
    task_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str = "audio/wav",
    sample_text: str = "",
    clone_mode: str = "fast",
    auto_transcribe: bool = True,
):
    try:
        result = _clone_voice(file_bytes, filename, content_type, sample_text, clone_mode, auto_transcribe)
        step_voice_id = result["id"]
        audio_bytes = _tts_sync("声音克隆成功，以后可以直接选择这个声音朗读文案。", step_voice_id,
                                "用情绪高昂、积极向上、充满活力的语气")
        store.update_task(task_id, {
            "status": "done",
            "step_voice_id": step_voice_id,
            "sample_audio": base64.b64encode(audio_bytes).decode(),
            "clone_mode": result.get("clone_mode", clone_mode),
            "reference_text": result.get("reference_text", ""),
            "reference_text_source": result.get("reference_text_source", ""),
            "asr_reference_text": result.get("asr_reference_text", ""),
            "asr_error": result.get("asr_error", ""),
            "clone_quality": result.get("clone_quality", {}),
            "processed_reference_audio": result.get("processed_reference_audio", ""),
            "clone_warnings": result.get("warnings", []),
        })
    except Exception as e:
        _refund_task_credit(task_id)
        store.update_task(task_id, {"status": "error", "error": str(e)})

def _do_tts(task_id: str, step_voice_id: str, text: str, instruction: str):
    try:
        audio_bytes, tts_meta = _tts_generate_audio(text, step_voice_id, instruction)
        store.update_task(task_id, {
            "status": "done",
            "audio": base64.b64encode(audio_bytes).decode(),
            "tts_meta": tts_meta,
            "segment_count": tts_meta.get("segment_count", 1),
            "duration_seconds": tts_meta.get("duration_seconds"),
        })
    except Exception as e:
        _refund_task_credit(task_id)
        store.update_task(task_id, {"status": "error", "error": str(e)})

# === FastAPI ===
app = FastAPI(title="StepAudio Voice Studio")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

_static_dir = Path(__file__).parent / "static"

@app.on_event("startup")
def startup():
    init_db()
    _mark_interrupted_video_tasks()
    _start_video_download_workers()

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
    if store.get_user(username):
        raise HTTPException(400, "用户名已存在")
    invite = store.get_invite_code(invite_code)
    if not invite or invite["is_used"]:
        raise HTTPException(400, "邀请码无效或已使用")

    store.create_user(
        username=username,
        password_hash=hash_password(password),
        credits=INITIAL_CREDITS,
        is_admin=False,
        last_login_date=date.today().isoformat(),
    )
    store.mark_invite_used(invite_code, username)

    token = store.create_token(username)
    return {"token": token, "username": username, "credits": INITIAL_CREDITS}

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    user = store.get_user(username)
    if not user:
        raise HTTPException(401, "用户名或密码错误")
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")

    today = date.today().isoformat()
    if user["last_login_date"] != today:
        user = store.update_user(username, credits=user["credits"] + DAILY_BONUS, last_login_date=today)

    token = store.create_token(username)
    return {"token": token, "username": username, "credits": user["credits"]}

@app.get("/api/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {"username": current_user["username"], "credits": current_user["credits"],
            "is_admin": current_user["is_admin"]}

@app.post("/api/admin/change-password")
async def change_admin_password(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    body = await request.json()
    current_password = body.get("current_password", "")
    new_password = body.get("new_password", "")
    if not current_password or not new_password:
        raise HTTPException(400, "当前密码和新密码不能为空")
    if len(new_password) < 8:
        raise HTTPException(400, "新密码至少8位")
    if not verify_password(current_password, current_user["password_hash"]):
        raise HTTPException(400, "当前密码错误")
    if verify_password(new_password, current_user["password_hash"]):
        raise HTTPException(400, "新密码不能和当前密码相同")

    username = current_user["username"]
    store.update_user(username, password_hash=hash_password(new_password))
    store.delete_tokens_for_user(username)
    return {"ok": True, "message": "密码已修改，请用新密码重新登录"}

# === 管理员 API ===
@app.post("/api/admin/generate-code")
async def generate_invite_code(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    code = str(uuid.uuid4())[:8].upper()
    store.create_invite_code(code, current_user["username"])
    return {"code": code}

@app.get("/api/admin/users")
async def list_users(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    return [{"username": u["username"], "credits": u["credits"], "is_admin": u["is_admin"],
             "created_at": u["created_at"]} for u in store.list_users()]

@app.get("/api/admin/codes")
async def list_invite_codes(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    return store.list_invite_codes()

@app.post("/api/admin/update-credits")
async def update_user_credits(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    body = await request.json()
    username = body.get("username", "").strip()
    credits = body.get("credits", 0)
    if not store.get_user(username):
        raise HTTPException(400, "用户不存在")
    store.update_user(username, credits=int(credits))
    return {"username": username, "credits": credits}

@app.get("/api/admin/video-cookies")
async def get_video_cookies(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    return {platform: _video_cookie_status(platform) for platform in VIDEO_COOKIE_KEYS}

@app.post("/api/admin/video-cookies")
async def save_video_cookies(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    body = await request.json()
    for platform, env_name in VIDEO_COOKIE_KEYS.items():
        field = f"{platform}_cookie"
        if field not in body:
            continue
        value = _normalize_cookie_input(str(body.get(field) or ""), platform)
        store.set_app_setting(_video_cookie_setting_key(env_name), value, current_user["username"])
    return {platform: _video_cookie_status(platform) for platform in VIDEO_COOKIE_KEYS}

@app.post("/api/admin/video-cookies/test")
async def test_video_cookie(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    body = await request.json()
    platform = str(body.get("platform", "")).strip().lower()
    if platform not in VIDEO_COOKIE_KEYS:
        raise HTTPException(400, "平台必须是 xhs 或 douyin")
    from .video_downloader import _cookie_header
    env_name = VIDEO_COOKIE_KEYS[platform]
    cookie = _cookie_header(env_name)
    if not cookie:
        return {"platform": platform, "configured": False, "ok": False, "message": "未配置 Cookie"}

    target = "https://www.xiaohongshu.com/explore" if platform == "xhs" else "https://www.douyin.com/"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": cookie,
    }
    try:
        resp = requests.get(target, headers=headers, timeout=12, allow_redirects=True)
    except Exception as e:
        return {"platform": platform, "configured": True, "ok": False, "message": f"访问失败：{e}"}
    text = resp.text[:2000].lower()
    login_hint = any(mark in text for mark in ["login", "登录", "验证码", "captcha", "verify"])
    return {
        "platform": platform,
        "configured": True,
        "ok": resp.status_code < 400,
        "status_code": resp.status_code,
        "login_hint": login_hint,
        "message": "服务器已带 Cookie 访问平台页面；这只能验证可访问性，是否能拿原画还要看具体链接返回的 stream 列表",
    }


@app.post("/api/admin/video/xhs4k-probe")
async def probe_xhs_4k_endpoint(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    body = await request.json()
    url = str(body.get("url") or "").strip()
    provider = str(body.get("provider") or "").strip()
    if not url:
        raise HTTPException(400, "请粘贴小红书链接")
    from .video_downloader import extract_url, get_platform, probe_xhs_4k_endpoint as run_probe
    video_url = extract_url(url)
    if not video_url or get_platform(video_url) != "xhs":
        raise HTTPException(400, "只支持小红书链接")

    output_dir = Path(get_settings().local_storage_dir) / "video_probe" / str(uuid.uuid4())
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_probe(video_url, provider, str(output_dir))
        result.pop("file_path", None)
        return JSONResponse(result)
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


# === 克隆和 TTS API ===
@app.post("/api/clone")
async def clone_voice(
    file: UploadFile = File(...),
    name: str = Form(""),
    speaker: str = Form(""),
    department: str = Form(""),
    sample_text: str = Form(""),
    clone_mode: str = Form("fast"),
    auto_transcribe: str = Form("true"),
    current_user: dict = Depends(get_current_user),
):
    if current_user["credits"] < COST_PER_USE:
        raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分，当前 {current_user['credits']} 积分")
    current_user = store.deduct_credits(current_user["username"], COST_PER_USE)
    if not current_user:
        raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分")
    content = await file.read()
    task_id = str(uuid.uuid4())
    clone_mode = "hifi" if clone_mode == "hifi" else "fast"
    auto_transcribe_enabled = str(auto_transcribe).lower() not in {"0", "false", "no", "off"}
    store.create_task(task_id, {"status": "processing", "user_id": current_user["id"], "username": current_user["username"],
                       "type": "clone", "voice_name": name.strip(), "speaker": speaker.strip(),
                       "department": department.strip(), "sample_text": sample_text.strip(),
                       "clone_mode": clone_mode, "auto_transcribe": auto_transcribe_enabled,
                       "created_at": datetime.utcnow().isoformat()})
    content_type = file.content_type or "audio/wav"
    thread = threading.Thread(
        target=_do_clone,
        args=(task_id, content, file.filename or "audio.wav", content_type, sample_text, clone_mode, auto_transcribe_enabled),
    )
    thread.start()
    return JSONResponse({"task_id": task_id})

@app.get("/api/voices")
def list_persisted_voices(current_user: dict = Depends(get_current_user)):
    tasks = store.list_tasks(None if current_user["is_admin"] else current_user["id"])
    voices = []
    for task in tasks:
        if task.get("type") != "clone" or task.get("status") != "done" or not task.get("step_voice_id"):
            continue
        voices.append({
            "id": task.get("id"),
            "name": task.get("voice_name") or "未命名声音",
            "speaker": task.get("speaker") or "",
            "department": task.get("department") or "",
            "language": "ZH",
            "step_voice_id": task.get("step_voice_id"),
            "has_sample": bool(task.get("sample_audio")),
            "has_reference_audio": bool(task.get("processed_reference_audio")),
            "clone_mode": task.get("clone_mode", "fast"),
            "reference_text": task.get("reference_text", ""),
            "reference_text_source": task.get("reference_text_source", ""),
            "clone_quality": task.get("clone_quality") or {},
            "clone_warnings": task.get("clone_warnings") or [],
            "created_at": task.get("created_at"),
            "username": task.get("username"),
        })
    return JSONResponse(voices)


@app.get("/api/voices/{voice_id}/sample")
def download_voice_sample(voice_id: str, current_user: dict = Depends(get_current_user)):
    task = store.get_task(voice_id)
    if not task:
        raise HTTPException(404, "声音不存在")
    if task.get("user_id") != current_user["id"] and not current_user["is_admin"]:
        raise HTTPException(403, "无权访问")
    if task.get("type") != "clone" or task.get("status") != "done" or not task.get("sample_audio"):
        raise HTTPException(404, "暂无可下载样音")
    try:
        audio_bytes = base64.b64decode(task["sample_audio"])
    except Exception:
        raise HTTPException(500, "样音数据损坏")
    filename = _safe_audio_filename(task.get("voice_name") or "voice_sample")
    return Response(
        content=audio_bytes,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f"attachment; filename=\"voice_sample.mp3\"; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/voices/{voice_id}/reference")
def download_voice_reference(voice_id: str, current_user: dict = Depends(get_current_user)):
    task = store.get_task(voice_id)
    if not task:
        raise HTTPException(404, "声音不存在")
    if task.get("user_id") != current_user["id"] and not current_user["is_admin"]:
        raise HTTPException(403, "无权访问")
    if task.get("type") != "clone" or task.get("status") != "done" or not task.get("processed_reference_audio"):
        raise HTTPException(404, "暂无可下载参考片段")
    try:
        audio_bytes = base64.b64decode(task["processed_reference_audio"])
    except Exception:
        raise HTTPException(500, "参考音频数据损坏")
    filename = _safe_audio_filename((task.get("voice_name") or "voice") + "_参考片段").replace(".mp3", ".wav")
    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={"Content-Disposition": f"attachment; filename=\"voice_reference.wav\"; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/api/tts")
async def create_tts(request: Request, current_user: dict = Depends(get_current_user)):
    if current_user["credits"] < COST_PER_USE:
        raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分，当前 {current_user['credits']} 积分")
    body = await request.json()
    step_voice_id = body.get("step_voice_id")
    text = body.get("text", "")
    instruction = body.get("instruction", "用激情澎湃、充满感染力、像抖音主播一样有节奏感和爆发力的语气")
    if not step_voice_id or not text:
        raise HTTPException(400, "缺少参数")
    current_user = store.deduct_credits(current_user["username"], COST_PER_USE)
    if not current_user:
        raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分")
    task_id = str(uuid.uuid4())
    store.create_task(task_id, {"status": "processing", "user_id": current_user["id"], "username": current_user["username"],
                       "type": "tts", "text": text[:50], "created_at": datetime.utcnow().isoformat()})
    thread = threading.Thread(target=_do_tts, args=(task_id, step_voice_id, text, instruction))
    thread.start()
    return JSONResponse({"task_id": task_id})


@app.get("/api/task/{task_id}/audio")
def download_task_audio(task_id: str, current_user: dict = Depends(get_current_user)):
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.get("user_id") != current_user["id"] and not current_user["is_admin"]:
        raise HTTPException(403, "无权访问")
    if task.get("type") != "tts" or task.get("status") != "done" or not task.get("audio"):
        raise HTTPException(404, "暂无可下载音频")
    try:
        audio_bytes = base64.b64decode(task["audio"])
    except Exception:
        raise HTTPException(500, "音频数据损坏")
    filename = _safe_audio_filename(task.get("text") or "tts_audio")
    return Response(
        content=audio_bytes,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f"attachment; filename=\"tts_audio.mp3\"; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/task/{task_id}")
def get_task(task_id: str, current_user: dict = Depends(get_current_user)):
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404)
    if task.get("user_id") != current_user["id"] and not current_user["is_admin"]:
        raise HTTPException(403, "无权访问")
    return JSONResponse(task)

@app.get("/api/my-tasks")
def get_my_tasks(current_user: dict = Depends(get_current_user)):
    return JSONResponse(store.list_tasks(current_user["id"]))

@app.get("/api/admin/all-tasks")
def get_all_tasks(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    return JSONResponse(store.list_tasks())

# === 视频下载 API ===
@app.post("/api/video/download")
async def download_video(request: Request, current_user: dict = Depends(get_current_user)):
    if current_user["credits"] < COST_PER_USE:
        raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分，当前 {current_user['credits']} 积分")

    body = await request.json()
    url = body.get("url", "").strip()
    download_mode = (body.get("download_mode") or body.get("mode") or "auto").strip().lower()
    if download_mode not in {"auto", "original", "builtin", "tikhub"}:
        raise HTTPException(400, "不支持的下载方式")
    if not url:
        raise HTTPException(400, "请提供视频链接")

    from .video_downloader import extract_url, get_platform
    video_url = extract_url(url)
    if not video_url:
        raise HTTPException(400, "未找到有效链接，请粘贴包含抖音、小红书或B站链接的文本")

    platform = get_platform(video_url)
    if not platform:
        raise HTTPException(400, "不支持的链接，请提供抖音、小红书或B站链接")

    with _video_submission_lock:
        active_task = _has_active_video_task(current_user["id"])
        if active_task:
            status_text = "下载队列中" if active_task.get("status") == "queued" else "正在处理"
            raise HTTPException(429, f"你已有视频任务{status_text}，完成后再提交新的下载")

        current_user = store.deduct_credits(current_user["username"], COST_PER_USE)
        if not current_user:
            raise HTTPException(400, f"积分不足，需要 {COST_PER_USE} 积分")

        task_id = str(uuid.uuid4())
        max_retries = _video_max_retries_for_mode(download_mode)
        store.create_video_task(task_id, {
            "status": "queued",
            "user_id": current_user["id"],
            "username": current_user["username"],
            "url": video_url,
            "platform": platform,
            "download_mode": download_mode,
            "attempt": 0,
            "max_retries": max_retries,
            "failure_reasons": [],
            "queued_at": datetime.utcnow().isoformat(),
            "created_at": datetime.utcnow().isoformat()
        })
        _cleanup_old_video_tasks(current_user["id"])
        _video_download_queue.put(task_id)
    return JSONResponse({"task_id": task_id})

@app.get("/api/video/task/{task_id}")
def get_video_task(task_id: str, current_user: dict = Depends(get_current_user)):
    task = store.get_video_task(task_id)
    if not task:
        raise HTTPException(404)
    # 只能看自己的任务（管理员可以看所有）
    if task.get("user_id") != current_user["id"] and not current_user["is_admin"]:
        raise HTTPException(403, "无权访问")
    return JSONResponse(task)

@app.get("/api/video/my-tasks")
def get_my_video_tasks(current_user: dict = Depends(get_current_user)):
    return JSONResponse(store.list_video_tasks(current_user["id"], limit=VIDEO_HISTORY_LIMIT))

@app.get("/api/video/admin/all-tasks")
def get_all_video_tasks(current_user: dict = Depends(get_current_user)):
    if not current_user["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    return JSONResponse(store.list_video_tasks(limit=VIDEO_HISTORY_LIMIT))

@app.get("/api/video/download-file/{task_id}")
def download_video_file(task_id: str, current_user: dict = Depends(get_current_user)):
    task = store.get_video_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    # 只能下载自己的任务（管理员可以下载所有）
    if task.get("user_id") != current_user["id"] and not current_user["is_admin"]:
        raise HTTPException(403, "无权下载")
    if task.get("status") != "done":
        raise HTTPException(400, "视频尚未下载完成")
    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(404, "视频文件已过期或被清理")
    from fastapi.responses import FileResponse
    return FileResponse(file_path, media_type="video/mp4", filename=_safe_video_filename(task.get("title")))

@app.delete("/api/video/task/{task_id}")
def delete_video_task(task_id: str, current_user: dict = Depends(get_current_user)):
    task = store.get_video_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.get("user_id") != current_user["id"] and not current_user["is_admin"]:
        raise HTTPException(403, "无权删除")
    if task.get("status") in VIDEO_ACTIVE_STATUSES:
        raise HTTPException(400, "视频还在队列中或下载中，完成后再删除")
    _delete_video_files(task)
    store.delete_video_task(task_id)
    return {"ok": True}

@app.post("/api/video/transcribe/{task_id}")
def transcribe_video(task_id: str, current_user: dict = Depends(get_current_user)):
    task = store.get_video_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.get("user_id") != current_user["id"] and not current_user["is_admin"]:
        raise HTTPException(403, "无权操作")
    if task.get("status") != "done":
        raise HTTPException(400, "视频尚未下载完成")
    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(404, "视频文件已过期或被清理")

    from .video_downloader import _transcribe_with_stepaudio
    store.update_video_task(task_id, {"transcription_status": "processing", "transcription_error": ""})
    try:
        text = _transcribe_with_stepaudio(file_path)
    except RuntimeError as e:
        store.update_video_task(task_id, {"transcription_status": "error", "transcription_error": str(e)})
        raise HTTPException(502, str(e))
    except Exception as e:
        store.update_video_task(task_id, {"transcription_status": "error", "transcription_error": str(e)})
        raise HTTPException(502, f"文案解析失败: {e}")
    if text:
        store.update_video_task(task_id, {
            "description": text,
            "transcript": text,
            "transcription_status": "done",
            "transcribed_at": datetime.utcnow().isoformat(),
        })
    else:
        store.update_video_task(task_id, {"transcription_status": "empty"})
    return JSONResponse({"text": text})
