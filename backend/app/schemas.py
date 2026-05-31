from pydantic import BaseModel
from datetime import datetime


class HealthOut(BaseModel):
    ok: bool
    env: str
    openvoice_mode: str = "stepaudio"


class VoiceOut(BaseModel):
    id: str
    name: str
    speaker_name: str | None = None
    department: str | None = None
    language: str
    style_tags: str | None = None
    status: str
    error_message: str | None = None
    sample_audio_url: str | None = None
    original_audio_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class JobOut(BaseModel):
    id: str
    title: str | None = None
    voice_id: str | None = None
    voice_name: str | None = None
    text: str | None = None
    language: str | None = None
    emotion: str | None = None
    speed: float | None = None
    status: str
    output_audio_url: str | None = None
    duration_seconds: float | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class TTSCreate(BaseModel):
    voice_id: str
    title: str | None = None
    text: str
    language: str = "ZH"
    emotion: str = "natural"
    speed: float = 1.0
