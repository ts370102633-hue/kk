import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Boolean, Float, Enum as SAEnum, ForeignKey
from sqlalchemy.orm import relationship
from .db import Base
import enum


class VoiceStatus(str, enum.Enum):
    processing = "processing"
    active = "active"
    failed = "failed"
    disabled = "disabled"


class JobStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, index=True)
    name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class Voice(Base):
    __tablename__ = "voices"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    speaker_name = Column(String)
    department = Column(String)
    language = Column(String, default="ZH")
    style_tags = Column(String)
    original_audio_key = Column(String)
    processed_audio_key = Column(String)
    voice_feature_key = Column(String)
    sample_audio_key = Column(String)
    consent_confirmed = Column(Boolean, default=False)
    consent_note = Column(String)
    status = Column(SAEnum(VoiceStatus), default=VoiceStatus.processing)
    error_message = Column(String)
    created_by = Column(String, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TTSJob(Base):
    __tablename__ = "tts_jobs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String)
    user_id = Column(String, ForeignKey("users.id"))
    voice_id = Column(String, ForeignKey("voices.id"))
    text = Column(String)
    language = Column(String, default="ZH")
    emotion = Column(String, default="natural")
    speed = Column(Float, default=1.0)
    status = Column(SAEnum(JobStatus), default=JobStatus.queued)
    output_audio_key = Column(String)
    duration_seconds = Column(Float)
    error_message = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
    voice = relationship("Voice", lazy="joined")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String)
    action = Column(String)
    target_type = Column(String)
    target_id = Column(String)
    detail = Column(String)
    ip = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
