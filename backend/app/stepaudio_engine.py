"""
StepAudio 2.5 TTS 引擎 — 阶跃星辰云端 API
"""
from __future__ import annotations
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import soundfile as sf
import requests
from .config import get_settings


@dataclass
class CloneResult:
    feature_path: Path
    sample_path: Path
    processed_reference_path: Path | None = None


@dataclass
class SynthesisResult:
    audio_path: Path
    duration_seconds: float | None = None


class StepAudioEngine:
    def __init__(self):
        self.settings = get_settings()
        self.output_dir = Path(self.settings.openvoice_output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.api_base = self.settings.step_api_base
        self.api_key = self.settings.step_api_key
        self.model = self.settings.step_tts_model
        self.headers = {"Authorization": f"Bearer {self.api_key}"}

    def clone_voice(self, reference_audio: Path, voice_id: str) -> CloneResult:
        # 1. 上传音频
        file_id = self._upload_file(reference_audio)
        # 2. 克隆
        resp = requests.post(
            f"{self.api_base}/audio/voices",
            headers=self.headers,
            json={"file_id": file_id, "model": self.model},
            timeout=60,
        )
        resp.raise_for_status()
        step_voice_id = resp.json()["id"]
        # 保存 voice_id
        feature_path = self.output_dir / f"voice_{voice_id}.step_voice_id"
        feature_path.write_text(step_voice_id, encoding="utf-8")
        # 3. 示例音频
        sample_path = self.output_dir / f"voice_{voice_id}_sample.mp3"
        self._tts("声音克隆成功，以后可以直接选择这个声音朗读文案。", step_voice_id, sample_path)
        return CloneResult(feature_path=feature_path, sample_path=sample_path, processed_reference_path=reference_audio)

    def synthesize(self, text: str, target_feature_path: Path, language: str, speed: float, emotion: str, job_id: str) -> SynthesisResult:
        step_voice_id = target_feature_path.read_text(encoding="utf-8").strip()
        out_path = self.output_dir / f"tts_{job_id}.mp3"
        instruction = self._emotion_hint(emotion)
        self._tts(text, step_voice_id, out_path, instruction=instruction)
        return SynthesisResult(audio_path=out_path, duration_seconds=self._duration(out_path))

    def _upload_file(self, file_path: Path) -> str:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://api.stepfun.com/v1/files",
                headers=self.headers,
                files={"file": (file_path.name, f, "audio/wav")},
                data={"purpose": "storage"},
                timeout=60,
            )
        resp.raise_for_status()
        return resp.json()["id"]

    def _tts(self, text: str, voice_id: str, output_path: Path, instruction: str = ""):
        payload = {"model": self.model, "voice": voice_id, "input": text}
        if instruction:
            payload["instruction"] = instruction
        resp = requests.post(f"{self.api_base}/audio/speech", headers=self.headers, json=payload, timeout=120)
        resp.raise_for_status()
        output_path.write_bytes(resp.content)

    def _emotion_hint(self, emotion: str) -> str:
        m = {"happy": "开心愉快", "sad": "低沉悲伤", "angry": "严肃有力", "gentle": "温柔轻柔", "excited": "兴奋激动"}
        return f"用{m.get(emotion, '')}的语气" if emotion in m else ""

    def _duration(self, path: Path) -> float | None:
        try:
            data, sr = sf.read(str(path))
            return len(data) / sr
        except Exception:
            return None
