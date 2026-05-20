from __future__ import annotations
import traceback
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from ..auth import get_or_create_user, request_identity
from ..db import get_db
from ..models import AuditLog, JobStatus, TTSJob, Voice, VoiceStatus
from ..stepaudio_engine import StepAudioEngine
from ..schemas import JobOut, TTSCreate
from ..storage import download_to_temp, public_url, upload_path

router = APIRouter(prefix="/api/tts", tags=["tts"])


def _content_type(path: Path) -> str:
    s = path.suffix.lower()
    if s == ".wav": return "audio/wav"
    if s == ".mp3": return "audio/mpeg"
    return "application/octet-stream"


def serialize_job(job: TTSJob) -> JobOut:
    return JobOut(id=job.id, title=job.title, voice_id=job.voice_id,
        voice_name=job.voice.name if job.voice else None, text=job.text,
        language=job.language, emotion=job.emotion, speed=job.speed, status=job.status,
        output_audio_url=public_url(job.output_audio_key), duration_seconds=job.duration_seconds,
        error_message=job.error_message, created_at=job.created_at, completed_at=job.completed_at)


def _run_tts(job_id: str, voice_feature_key: str, text: str, language: str, speed: float, emotion: str):
    from ..db import SessionLocal
    db = SessionLocal()
    try:
        job = db.get(TTSJob, job_id)
        if not job: return
        job.status = JobStatus.processing; db.commit()
        feature = download_to_temp(voice_feature_key, suffix=Path(voice_feature_key).suffix)
        result = StepAudioEngine().synthesize(text, feature, language, speed, emotion, job_id)
        output_key = f"tts/{job_id}/output{result.audio_path.suffix}"
        upload_path(output_key, result.audio_path, content_type=_content_type(result.audio_path))
        job.output_audio_key = output_key
        job.duration_seconds = result.duration_seconds
        job.status = JobStatus.completed
        job.completed_at = datetime.utcnow()
        job.error_message = None
        db.commit()
    except Exception as exc:
        job = db.get(TTSJob, job_id)
        if job:
            job.status = JobStatus.failed
            job.error_message = f"{exc}\n{traceback.format_exc()[-2000:]}"
            db.commit()
    finally:
        db.close()


@router.post("", response_model=JobOut)
def create_tts(payload: TTSCreate, background_tasks: BackgroundTasks, identity=Depends(request_identity), db: Session = Depends(get_db)):
    voice = db.get(Voice, payload.voice_id)
    if not voice: raise HTTPException(status_code=404, detail="Voice not found")
    if voice.status != VoiceStatus.active:
        raise HTTPException(status_code=400, detail="声音还没有克隆完成")
    user = get_or_create_user(db, identity.get("email"), identity.get("name"))
    job = TTSJob(title=payload.title, user_id=user.id, voice_id=payload.voice_id, text=payload.text,
        language=payload.language, emotion=payload.emotion, speed=payload.speed, status=JobStatus.queued)
    db.add(job); db.commit(); db.refresh(job)
    if not voice.voice_feature_key:
        raise HTTPException(status_code=400, detail="声音特征文件缺失")
    background_tasks.add_task(_run_tts, job.id, voice.voice_feature_key, job.text, job.language, job.speed, job.emotion)
    db.refresh(job)
    return serialize_job(job)
