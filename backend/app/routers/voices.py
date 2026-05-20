from __future__ import annotations
import io
import logging
import traceback
import subprocess
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session
from ..auth import get_or_create_user, request_identity
from ..config import get_settings
from ..db import get_db
from ..models import AuditLog, Voice, VoiceStatus
from ..stepaudio_engine import StepAudioEngine
from ..schemas import VoiceOut
from ..storage import download_to_temp, public_url, upload_fileobj, upload_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/voices", tags=["voices"])
settings = get_settings()
ALLOWED_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


def _content_type(path: Path) -> str:
    s = path.suffix.lower()
    if s == ".wav": return "audio/wav"
    if s == ".mp3": return "audio/mpeg"
    return "application/octet-stream"


def _extract_audio_from_video(video_path: Path) -> Path:
    audio_path = video_path.with_suffix(".wav")
    subprocess.run(["ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "pcm_s16le", "-ar", "22050", "-ac", "1", str(audio_path)],
        check=True, capture_output=True)
    return audio_path


def serialize_voice(voice: Voice) -> VoiceOut:
    return VoiceOut(id=voice.id, name=voice.name, speaker_name=voice.speaker_name, department=voice.department,
        language=voice.language, style_tags=voice.style_tags, status=voice.status, error_message=voice.error_message,
        sample_audio_url=public_url(voice.sample_audio_key), original_audio_url=public_url(voice.original_audio_key),
        created_at=voice.created_at, updated_at=voice.updated_at)


@router.get("", response_model=list[VoiceOut])
def list_voices(db: Session = Depends(get_db)):
    return [serialize_voice(v) for v in db.query(Voice).order_by(Voice.created_at.desc()).limit(200).all()]


@router.get("/{voice_id}", response_model=VoiceOut)
def get_voice(voice_id: str, db: Session = Depends(get_db)):
    voice = db.get(Voice, voice_id)
    if not voice: raise HTTPException(status_code=404, detail="Voice not found")
    return serialize_voice(voice)


def _run_clone(voice_id: str, original_audio_key: str, suffix: str):
    logger.info(f"[CLONE] 开始克隆声音 {voice_id}")
    from ..db import SessionLocal
    db = SessionLocal()
    try:
        voice = db.get(Voice, voice_id)
        if not voice: return
        reference = download_to_temp(original_audio_key, suffix=suffix)
        if suffix.lower() in VIDEO_SUFFIXES:
            logger.info(f"[CLONE] 提取视频音轨...")
            reference = _extract_audio_from_video(reference)
        result = StepAudioEngine().clone_voice(reference, voice_id=voice_id)
        feature_key = f"voices/{voice_id}/feature{result.feature_path.suffix}"
        sample_key = f"voices/{voice_id}/sample{result.sample_path.suffix}"
        upload_path(feature_key, result.feature_path, content_type=_content_type(result.feature_path))
        upload_path(sample_key, result.sample_path, content_type=_content_type(result.sample_path))
        voice.voice_feature_key = feature_key
        voice.sample_audio_key = sample_key
        voice.status = VoiceStatus.active
        voice.error_message = None
        voice.updated_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        logger.error(f"[CLONE] 克隆失败: {exc}\n{traceback.format_exc()[-500:]}")
        voice = db.get(Voice, voice_id)
        if voice:
            voice.status = VoiceStatus.failed
            voice.error_message = f"{exc}\n{traceback.format_exc()[-2000:]}"
            voice.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


@router.post("", response_model=VoiceOut)
def create_voice(request: Request, background_tasks: BackgroundTasks,
    name: str = Form(...), speaker_name: str | None = Form(None), department: str | None = Form(None),
    language: str = Form("ZH"), style_tags: str | None = Form(None),
    consent_confirmed: bool = Form(False), consent_note: str | None = Form(None),
    file: UploadFile = File(...), identity=Depends(request_identity), db: Session = Depends(get_db)):
    if not consent_confirmed:
        raise HTTPException(status_code=400, detail="必须确认该声音已获得合法授权")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的格式：{suffix}")
    content = file.file.read()
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件不能超过 {settings.max_upload_mb}MB")
    user = get_or_create_user(db, identity.get("email"), identity.get("name"))
    voice = Voice(name=name, speaker_name=speaker_name, department=department, language=language, style_tags=style_tags,
        original_audio_key="pending", consent_confirmed=True, consent_note=consent_note,
        status=VoiceStatus.processing, created_by=user.id)
    db.add(voice); db.commit(); db.refresh(voice)
    object_key = f"voices/{voice.id}/original{suffix}"
    upload_fileobj(object_key, io.BytesIO(content), length=len(content), content_type=file.content_type or "audio/mpeg")
    voice.original_audio_key = object_key
    db.add(AuditLog(user_id=user.id, action="voice.uploaded", target_type="voice", target_id=voice.id,
        ip=request.client.host if request.client else None))
    db.commit()
    background_tasks.add_task(_run_clone, voice.id, object_key, suffix)
    db.refresh(voice)
    return serialize_voice(voice)


@router.delete("/{voice_id}")
def delete_voice(voice_id: str, db: Session = Depends(get_db)):
    voice = db.get(Voice, voice_id)
    if not voice: raise HTTPException(status_code=404, detail="Voice not found")
    db.delete(voice); db.commit()
    return {"ok": True}
