"""
视频下载服务 — 支持抖音、小红书、B站
使用 StepAudio ASR 进行语音识别
"""
import re
import os
import time
import hashlib
import logging
import subprocess
import tempfile
import base64
import threading
import sys
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse
import json
import requests
from .config import get_settings
from . import persistent_store as store

logger = logging.getLogger(__name__)
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_SHORTCUT_QUOTA_LOCK = threading.RLock()
MOBILE_VIDEO_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"

# StepAudio API 配置
API_BASE = get_settings().step_api_base.rstrip("/")
ASR_MODEL = get_settings().step_asr_model

def _stepaudio_headers(extra: dict | None = None) -> dict:
    api_key = get_settings().step_api_key.strip()
    if not api_key:
        raise RuntimeError("STEP_API_KEY 未配置，请在 .env 或云服务器环境变量中设置")
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra:
        headers.update(extra)
    return headers

# URL 模式
_URL_PATTERN = re.compile(r"https?://[\w./?=&%+#@!~*'()-]+")

_DOUYIN_PATTERNS = [
    r"(?:https?://)?(?:www\.)?douyin\.com/(?:video|note)/(\d+)",
    r"(?:https?://)?v\.douyin\.com/(\w+)",
]

_XHS_PATTERNS = [
    r"(?:https?://)?(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/(\w+)",
    r"(?:https?://)?xhslink\.com/(\w+)",
]

_BILI_PATTERNS = [
    r"(?:https?://)?(?:www\.)?bilibili\.com/video/(BV\w+)",
    r"(?:https?://)?b23\.tv/(\w+)",
]


@dataclass
class VideoCandidate:
    url: str
    source: str
    width: int = 0
    height: int = 0
    bitrate: int = 0
    codec: str = ""
    content_length: int = 0
    headers: dict = field(default_factory=dict)
    fps: int = 0

    @property
    def quality(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        if self.height:
            return f"{self.height}p"
        return "unknown"

    def score(self) -> tuple[int, int, int, int, int, int]:
        codec_rank = 2 if "265" in self.codec.lower() or "hevc" in self.codec.lower() else 1
        if "bit_rate" in self.source or "bitrate" in self.source:
            source_rank = 7
        elif "play_addr" in self.source or "playAddr" in self.source or "PlayAddr" in self.source:
            source_rank = 6
        elif "shortcut_api" in self.source:
            source_rank = 5
        elif "shortcut_capture" in self.source:
            source_rank = 4
        elif "download" in self.source:
            source_rank = 1
        else:
            source_rank = 2
        short_side = min(self.width, self.height) if self.width and self.height else max(self.width, self.height)
        long_side = max(self.width, self.height)
        return (short_side, long_side, self.fps, self.bitrate, self.content_length, codec_rank, source_rank)


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _best_candidate(candidates: list[VideoCandidate]) -> VideoCandidate:
    usable = [c for c in candidates if c.url]
    if not usable:
        raise RuntimeError("未找到可用视频流")
    return sorted(usable, key=lambda c: c.score(), reverse=True)[0]


def _dedupe_candidates(candidates: list[VideoCandidate]) -> list[VideoCandidate]:
    by_url: dict[str, VideoCandidate] = {}
    for c in candidates:
        if not c.url:
            continue
        existing = by_url.get(c.url)
        if not existing:
            by_url[c.url] = c
            continue
        if c.score() > existing.score():
            if existing.headers and not c.headers:
                c.headers = existing.headers
            by_url[c.url] = c
        else:
            if c.headers:
                existing.headers.update(c.headers)
            existing.width = max(existing.width, c.width)
            existing.height = max(existing.height, c.height)
            existing.bitrate = max(existing.bitrate, c.bitrate)
            existing.content_length = max(existing.content_length, c.content_length)
            if not existing.codec and c.codec:
                existing.codec = c.codec
    return list(by_url.values())


def _ordered_xhs_candidates(candidates: list[VideoCandidate]) -> list[VideoCandidate]:
    """XHS originVideoKey 是无水印/原片线索，优先尝试；失败再退回普通 stream。"""
    usable = [c for c in candidates if c.url]
    origin = [c for c in usable if re.search(r"origin[_-]?video[_-]?key|origin_video_key|originVideoKey", c.source, re.I)]
    regular = [c for c in usable if c not in origin]
    return origin + sorted(regular, key=lambda c: c.score(), reverse=True)


def _candidate_info(candidate: VideoCandidate) -> dict:
    return {
        "quality": candidate.quality,
        "width": candidate.width,
        "height": candidate.height,
        "bitrate": candidate.bitrate,
        "fps": candidate.fps,
        "codec": candidate.codec,
        "quality_source": candidate.source,
    }


def _candidate_diagnostics(candidates: list[VideoCandidate], selected: VideoCandidate | None = None, limit: int = 12) -> list[dict]:
    selected_url = selected.url if selected else ""
    rows = []
    for c in sorted(candidates, key=lambda item: item.score(), reverse=True)[:limit]:
        try:
            parsed = urlparse(c.url)
            host = parsed.netloc
        except Exception:
            host = ""
        rows.append({
            "selected": bool(selected_url and c.url == selected_url),
            "source": c.source,
            "quality": c.quality,
            "width": c.width,
            "height": c.height,
            "bitrate": c.bitrate,
            "fps": c.fps,
            "codec": c.codec,
            "content_length": c.content_length,
            "url_host": host,
            "url_hash": hashlib.sha256(c.url.encode()).hexdigest()[:12] if c.url else "",
        })
    return rows


def _probe_video_metadata(file_path: str) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_name,bit_rate",
                "-of", "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            stream = (data.get("streams") or [{}])[0]
            width = _to_int(stream.get("width"))
            height = _to_int(stream.get("height"))
            if width or height:
                return {
                    "width": width,
                    "height": height,
                    "quality": f"{width}x{height}" if width and height else f"{height}p" if height else "unknown",
                    "codec": stream.get("codec_name") or "",
                    "bitrate": _to_int(stream.get("bit_rate")),
                }
    except Exception:
        pass

    try:
        result = subprocess.run(["ffmpeg", "-i", file_path], capture_output=True, text=True, timeout=30)
        text = (result.stderr or "") + (result.stdout or "")
        m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", text)
        if m:
            width, height = _to_int(m.group(1)), _to_int(m.group(2))
            return {"width": width, "height": height, "quality": f"{width}x{height}"}
    except Exception:
        pass
    return {}


def _result_quality_info(candidate: VideoCandidate, file_path: str) -> dict:
    info = _candidate_info(candidate)
    actual = _probe_video_metadata(file_path)
    for key, value in actual.items():
        if value:
            info[key] = value
    return info


def _format_bitrate(bitrate: int) -> str:
    if not bitrate:
        return ""
    return f"{bitrate / 1000 / 1000:.1f}Mbps"


def _cookie_setting_key(name: str) -> str:
    return f"video_cookie_{name.lower()}"


def _hd_min_short_side() -> int:
    raw = os.getenv("VIDEO_HD_MIN_SHORT_SIDE", os.getenv("VIDEO_HD_MIN_HEIGHT", "1080")).strip()
    try:
        return max(0, int(raw))
    except Exception:
        return 1080


def _result_short_side(result: dict) -> int:
    width = _to_int(result.get("width"))
    height = _to_int(result.get("height"))
    if width and height:
        return min(width, height)
    return max(width, height)


def _candidate_short_side(candidate: VideoCandidate) -> int:
    if candidate.width and candidate.height:
        return min(candidate.width, candidate.height)
    return max(candidate.width, candidate.height)


def _apply_actual_file_metadata(candidate: VideoCandidate, file_path: str) -> None:
    actual = _probe_video_metadata(file_path)
    if actual.get("width"):
        candidate.width = _to_int(actual.get("width"))
    if actual.get("height"):
        candidate.height = _to_int(actual.get("height"))
    if actual.get("codec"):
        candidate.codec = str(actual.get("codec") or "")
    if actual.get("bitrate"):
        candidate.bitrate = _to_int(actual.get("bitrate"))
    try:
        candidate.content_length = max(candidate.content_length, os.path.getsize(file_path))
    except Exception:
        pass


def _result_meets_hd_threshold(result: dict) -> bool:
    threshold = _hd_min_short_side()
    if threshold <= 0:
        return True
    return _result_short_side(result) >= threshold


def _below_original_threshold_reason(result: dict | None, source_label: str = "第三方原画接口") -> str:
    threshold = _hd_min_short_side()
    if result:
        quality = result.get("quality") or "未知画质"
        short_side = _result_short_side(result)
        if short_side:
            return f"{source_label}实际只返回 {quality}，短边 {short_side}P 低于 {threshold}P 原画阈值"
        return f"{source_label}未能确认真实画质，低于 {threshold}P 原画阈值"
    return f"{source_label}未返回可用原画视频"


def _delete_downloaded_result_file(result: dict | None) -> None:
    if not result:
        return
    path = result.get("file_path")
    if not path:
        return
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _low_quality_fallback_reason(result: dict | None, shortcut_reason: str = "") -> str:
    threshold = _hd_min_short_side()
    if result:
        quality = result.get("quality") or "未知画质"
        short_side = _result_short_side(result)
        if short_side:
            base = f"自建解析最高只拿到 {quality}，短边 {short_side}P 低于 {threshold}P 原画阈值"
        else:
            base = f"自建解析未能确认真实画质，低于 {threshold}P 原画阈值"
    else:
        base = "自建解析失败"
    if shortcut_reason:
        reason = f"{base}；{shortcut_reason}"
    else:
        reason = f"{base}；第三方原画接口没有可用结果，已保留自建解析可下载版本"
    if result and result.get("platform") in {"xhs", "douyin"} and not result.get("platform_cookie_configured"):
        reason += "；当前未配置平台 Cookie，仍是匿名公开页解析，平台通常只返回 720P 预览流"
    return reason


def _cookie_header(name: str) -> str:
    try:
        stored = store.get_app_setting_value(_cookie_setting_key(name), "").strip()
        if stored:
            return stored
    except Exception:
        pass
    return os.getenv(name, "").strip()


def _platform_cookie_info(platform: str) -> dict:
    env_name = {"xhs": "XHS_COOKIE", "douyin": "DOUYIN_COOKIE"}.get(platform, "")
    if not env_name:
        return {"configured": False, "source": ""}
    try:
        stored = store.get_app_setting_value(_cookie_setting_key(env_name), "").strip()
    except Exception:
        stored = ""
    env_value = os.getenv(env_name, "").strip()
    if stored:
        return {"configured": True, "source": "admin"}
    if env_value:
        return {"configured": True, "source": "env"}
    return {"configured": False, "source": ""}


def _playwright_cookie_header(ctx, url: str) -> str:
    try:
        cookies = ctx.cookies([url])
    except Exception:
        try:
            cookies = ctx.cookies()
        except Exception:
            cookies = []
    pairs = []
    for cookie in cookies or []:
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value is not None:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


_SHORTCUT_CONFIG_CACHE = {"loaded_at": 0.0, "data": {}}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _shortcut_api_enabled() -> bool:
    return _env_bool("VIDEO_SHORTCUT_API_ENABLED", True)


def _ytdlp_fallback_enabled() -> bool:
    return _env_bool("VIDEO_YTDLP_FALLBACK_ENABLED", True)


def _douyin_shortcut_fallback_enabled() -> bool:
    return _env_bool("VIDEO_DOUYIN_SHORTCUT_FALLBACK_ENABLED", False)


def _shortcut_original_first_enabled() -> bool:
    return _env_bool("VIDEO_SHORTCUT_ORIGINAL_FIRST_ENABLED", False)


def _require_shortcut_original() -> bool:
    return _env_bool("VIDEO_REQUIRE_ORIGINAL_API", False)


def _shortcut_api_base() -> str:
    return os.getenv("VIDEO_SHORTCUT_API_BASE", "https://a.jiejing.fun").strip().rstrip("/")


def _shortcut_auth_code() -> str:
    return os.getenv("VIDEO_SHORTCUT_AUTH_CODE", os.getenv("QSY_AUTH_CODE", "")).strip()


def _tikhub_enabled() -> bool:
    if os.getenv("TIKHUB_ENABLED") is not None:
        return _env_bool("TIKHUB_ENABLED", False)
    return bool(get_settings().tikhub_enabled)


def _tikhub_original_first_enabled() -> bool:
    if os.getenv("TIKHUB_ORIGINAL_FIRST_ENABLED") is not None:
        return _env_bool("TIKHUB_ORIGINAL_FIRST_ENABLED", False)
    return bool(get_settings().tikhub_original_first_enabled)


def _tikhub_api_base() -> str:
    return (os.getenv("TIKHUB_API_BASE") or get_settings().tikhub_api_base or "https://api.tikhub.dev").strip().rstrip("/")


def _tikhub_api_key() -> str:
    return (os.getenv("TIKHUB_API_KEY") or get_settings().tikhub_api_key or "").strip()


def _tikhub_timeout() -> int:
    return _to_int(os.getenv("TIKHUB_TIMEOUT_SECONDS") or get_settings().tikhub_timeout_seconds, 45)


def _shortcut_daily_limit() -> int:
    raw = os.getenv("VIDEO_SHORTCUT_DAILY_LIMIT", "20").strip()
    try:
        return max(0, int(raw))
    except Exception:
        return 20


def _shortcut_rate_limit_cooldown_seconds() -> int:
    raw = os.getenv("VIDEO_SHORTCUT_RATE_LIMIT_COOLDOWN_SECONDS", "300").strip()
    try:
        return max(0, int(raw))
    except Exception:
        return 300


def _shortcut_quota_path() -> Path:
    return Path(get_settings().local_storage_dir).expanduser().resolve() / "video_shortcut_quota.json"


def _shortcut_quota_state() -> dict:
    today = date.today().isoformat()
    path = _shortcut_quota_path()
    with _SHORTCUT_QUOTA_LOCK:
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            state = {}
        if state.get("date") != today:
            state = {"date": today, "used": 0, "exhausted": False, "reason": "", "rate_limited_until": 0}
        state.setdefault("used", 0)
        state.setdefault("exhausted", False)
        state.setdefault("reason", "")
        state.setdefault("rate_limited_until", 0)
        return state


def _write_shortcut_quota_state(state: dict) -> None:
    with _SHORTCUT_QUOTA_LOCK:
        path = _shortcut_quota_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _shortcut_unavailable_reason() -> str:
    state = _shortcut_quota_state()
    limit = _shortcut_daily_limit()
    if state.get("exhausted"):
        return state.get("reason") or "第三方原画接口今日额度已用完，已使用备用解析"
    rate_limited_until = float(state.get("rate_limited_until") or 0)
    now = time.time()
    if rate_limited_until > now:
        remaining = max(1, int(rate_limited_until - now))
        return state.get("reason") or f"第三方原画接口临时限流，约 {remaining} 秒后再试"
    if limit and int(state.get("used") or 0) >= limit:
        reason = f"第三方原画接口今日 {limit} 次高清额度已用完，已使用备用解析"
        state.update({"exhausted": True, "reason": reason})
        _write_shortcut_quota_state(state)
        return reason
    return ""


def _record_shortcut_success(platform: str, endpoint: str) -> dict:
    state = _shortcut_quota_state()
    state["used"] = int(state.get("used") or 0) + 1
    state["last_platform"] = platform
    state["last_endpoint"] = endpoint
    state["last_used_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    state["rate_limited_until"] = 0
    state["reason"] = ""
    limit = _shortcut_daily_limit()
    if limit and state["used"] >= limit:
        state["exhausted"] = True
        state["reason"] = f"第三方原画接口今日 {limit} 次高清额度已用完，后续自动使用备用解析"
    _write_shortcut_quota_state(state)
    return state


def _looks_like_shortcut_quota_limit(text: str) -> bool:
    lower = text.lower()
    keywords = [
        "429", "too many requests", "rate", "quota", "limit", "limited", "exceed", "exceeded", "maximum",
        "次数", "限额", "限制", "上限", "今日", "每天", "明日", "高清次数", "额度",
    ]
    return any(keyword in lower for keyword in keywords)


def _mark_shortcut_rate_limited(reason: str) -> None:
    state = _shortcut_quota_state()
    cooldown = _shortcut_rate_limit_cooldown_seconds()
    state.update({
        "exhausted": False,
        "reason": reason or f"第三方原画接口临时限流，约 {cooldown} 秒后再试",
        "rate_limited_until": time.time() + cooldown if cooldown else 0,
        "last_rate_limited_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    _write_shortcut_quota_state(state)


def _load_shortcut_config() -> dict:
    now = time.time()
    cached = _SHORTCUT_CONFIG_CACHE.get("data") or {}
    if cached and now - float(_SHORTCUT_CONFIG_CACHE.get("loaded_at") or 0) < 3600:
        return cached

    config_url = os.getenv("VIDEO_SHORTCUT_CONFIG_URL", "https://qsy.jiejing.fun/qsy.json").strip()
    if not config_url:
        return {}

    try:
        resp = requests.get(config_url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            _SHORTCUT_CONFIG_CACHE.update({"loaded_at": now, "data": data})
            return data
    except Exception as e:
        logger.warning(f"快捷指令配置加载失败，使用内置默认值: {e}")
    return cached if isinstance(cached, dict) else {}


def _shortcut_endpoint(config: dict, key: str, fallback_path: str) -> str:
    value = (config.get(key) or "").strip() if isinstance(config.get(key), str) else ""
    if value.startswith("http"):
        return value
    return f"{_shortcut_api_base()}{fallback_path}"


def _video_headers_from_shortcut(data: dict, default_referer: str) -> dict:
    headers = {
        "User-Agent": data.get("User-Agent") or MOBILE_VIDEO_UA,
        "Referer": data.get("Referer") or default_referer,
        "Accept": data.get("Accept") or "*/*",
    }
    return {k: v for k, v in headers.items() if v}


def _collect_shortcut_urls(body: dict) -> list[str]:
    urls: list[str] = []

    def add(value):
        if isinstance(value, str) and value.startswith(("http://", "https://")) and _looks_like_video_url(value, ""):
            urls.append(value)

    if not isinstance(body, dict):
        return urls

    for item in body.get("urls") or []:
        add(item)
    for item in body.get("lives") or []:
        if isinstance(item, dict):
            add(item.get("video"))
    for key in ["video", "video_url", "videoUrl", "play_url", "playUrl"]:
        add(body.get(key))

    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _platform_referer(platform: str) -> str:
    if platform == "xhs":
        return "https://www.xiaohongshu.com/"
    if platform == "douyin":
        return "https://www.douyin.com/"
    return ""


def _mobile_video_headers(referer: str = "") -> dict:
    headers = {
        "User-Agent": MOBILE_VIDEO_UA,
        "Accept": "*/*",
    }
    if referer is not None:
        headers["Referer"] = referer
    return headers


def _collect_video_urls_from_payload(payload) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def visit(value, depth: int) -> None:
        if depth > 12 or value is None:
            return
        if isinstance(value, str):
            text = value.replace("\\u002F", "/").replace("\\/", "/")
            if text.startswith(("http://", "https://")) and _looks_like_video_url(text, "") and text not in seen:
                seen.add(text)
                urls.append(text)
            return
        if isinstance(value, list):
            for item in value[:300]:
                visit(item, depth + 1)
            return
        if isinstance(value, dict):
            for item in value.values():
                visit(item, depth + 1)

    visit(payload, 0)
    return urls


def _candidate_headers_for_provider(platform: str, source: str) -> dict:
    # TikHub/third-party direct links are most stable with a mobile UA and an empty
    # Referer; platform homepage is still used by non-TikHub paths when needed.
    referer = "" if "tikhub" in source else _platform_referer(platform)
    return _mobile_video_headers(referer)


def _collect_bit_rate_candidates_from_payload(payload, platform: str, source_prefix: str) -> list[VideoCandidate]:
    candidates: list[VideoCandidate] = []
    base_url = _platform_referer(platform)

    def add_from_addr(addr, source: str, meta: dict) -> None:
        for raw_url in _douyin_addr_urls(addr):
            url = _normalize_url(_remove_douyin_watermark(raw_url), base_url)
            if not url or not _looks_like_video_url(url, ""):
                continue
            c = VideoCandidate(
                url=url,
                source=source,
                width=_to_int(meta.get("width")),
                height=_to_int(meta.get("height")),
                bitrate=_to_int(meta.get("bitrate")),
                codec=str(meta.get("codec") or ""),
                content_length=_to_int(meta.get("content_length")),
                fps=_to_int(meta.get("fps")),
                headers=_candidate_headers_for_provider(platform, source_prefix),
            )
            candidates.append(c)

    def visit(value, path: str, depth: int) -> None:
        if depth > 12 or value is None:
            return
        if isinstance(value, list):
            for idx, item in enumerate(value[:300]):
                visit(item, f"{path}[{idx}]", depth + 1)
            return
        if not isinstance(value, dict):
            return

        for key in ["bit_rate", "bitRate", "bitrateInfo", "bitrate_info", "BitrateInfo", "Bitrate"]:
            items = value.get(key)
            if not isinstance(items, list):
                continue
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                gear = item.get("gear_name") or item.get("GearName") or item.get("quality_type") or item.get("QualityType") or idx
                play_addr = (
                    item.get("play_addr")
                    or item.get("playAddr")
                    or item.get("PlayAddr")
                    or item.get("play_addr_h264")
                    or item.get("PlayAddrH264")
                    or item.get("play_addr_265")
                    or item.get("play_addr_bytevc1")
                    or item.get("PlayAddrBytevc1")
                )
                if not play_addr:
                    continue
                addr_meta = play_addr if isinstance(play_addr, dict) else {}
                meta = {
                    "width": item.get("width") or item.get("Width") or addr_meta.get("width") or addr_meta.get("Width"),
                    "height": item.get("height") or item.get("Height") or addr_meta.get("height") or addr_meta.get("Height"),
                    "bitrate": item.get("bit_rate") or item.get("bitrate") or item.get("Bitrate") or item.get("BitRate") or item.get("bandwidth"),
                    "content_length": item.get("data_size") or item.get("DataSize") or item.get("file_size") or item.get("FileSize") or addr_meta.get("data_size"),
                    "codec": _douyin_codec(item.get("video_encode_type"), item.get("format"), item.get("Format"), item),
                    "fps": item.get("fps") or item.get("FPS") or item.get("frame_rate") or item.get("FrameRate"),
                }
                add_from_addr(play_addr, f"{source_prefix}:bit_rate:{gear}", meta)

        for child_key, child in value.items():
            visit(child, f"{path}.{child_key}" if path else child_key, depth + 1)

    visit(payload, "", 0)
    return _dedupe_candidates(candidates)


def _collect_play_addr_candidates_from_payload(payload, platform: str, source_prefix: str) -> list[VideoCandidate]:
    candidates: list[VideoCandidate] = []
    seen: set[str] = set()
    base_url = _platform_referer(platform)
    allowed_url_keys = re.compile(
        r"play[_-]?addr|playUrl|play_url|master[_-]?url|original[_-]?video[_-]?url|videoUrl|video_url|url_list|urlList|UrlList|baseUrl|base_url",
        re.I,
    )
    blocked_path = re.compile(r"download[_-]?addr|downloadUrl|download_url|cover|image|avatar|poster|watermark|playwm", re.I)

    def add_url(raw_url: str, path: str, meta: dict | None = None) -> None:
        if blocked_path.search(path):
            return
        url = _normalize_url(_remove_douyin_watermark(raw_url), base_url)
        if not url or url in seen or not _looks_like_video_url(url, ""):
            return
        seen.add(url)
        c = _candidate_from_url_meta(url, f"{source_prefix}:{path[-80:]}")
        c.headers = _candidate_headers_for_provider(platform, source_prefix)
        if meta:
            c.width = c.width or _to_int(meta.get("width"))
            c.height = c.height or _to_int(meta.get("height"))
            c.bitrate = c.bitrate or _to_int(meta.get("bitrate"))
            c.codec = c.codec or str(meta.get("codec") or "")
            c.fps = c.fps or _to_int(meta.get("fps"))
        candidates.append(c)

    def visit(value, path: str, depth: int, meta: dict | None = None) -> None:
        if depth > 12 or value is None:
            return
        meta = meta or {}
        if isinstance(value, str):
            if value.startswith(("http://", "https://", "//", "/")) and allowed_url_keys.search(path):
                add_url(value, path, meta)
            return
        if isinstance(value, list):
            for idx, item in enumerate(value[:300]):
                visit(item, f"{path}[{idx}]", depth + 1, meta)
            return
        if not isinstance(value, dict):
            return

        next_meta = {
            "width": value.get("width") or value.get("Width") or meta.get("width"),
            "height": value.get("height") or value.get("Height") or meta.get("height"),
            "bitrate": value.get("bitrate") or value.get("bit_rate") or value.get("bandwidth") or meta.get("bitrate"),
            "codec": value.get("codec") or value.get("video_encode_type") or meta.get("codec"),
            "fps": value.get("fps") or value.get("FPS") or value.get("frame_rate") or meta.get("fps"),
        }
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(child, str) and child.startswith(("http://", "https://", "//", "/")) and allowed_url_keys.search(child_path):
                add_url(child, child_path, next_meta)
            else:
                visit(child, child_path, depth + 1, next_meta)

    visit(payload, "", 0)
    return _dedupe_candidates(candidates)


def _collect_xhs_tikhub_data_candidate(payload, source_prefix: str) -> list[VideoCandidate]:
    """Extract one Xiaohongshu TikHub video URL from response.data only."""
    candidates: list[VideoCandidate] = []
    seen: set[str] = set()
    base_url = _platform_referer("xhs")
    video_key = re.compile(
        r"(^|\.)(video|videoUrl|video_url|playUrl|play_url|downloadUrl|download_url|originUrl|origin_url|originalUrl|original_url|originVideoUrl|origin_video_url|originalVideoUrl|original_video_url|masterUrl|master_url|baseUrl|base_url|url|urlList|url_list)(\.|$|\[)",
        re.I,
    )
    blocked_key = re.compile(r"cover|image|avatar|poster|watermark|live_photo", re.I)

    def priority(path: str) -> int:
        lower = path.lower()
        if "origin" in lower or "original" in lower:
            return 500
        if "master" in lower:
            return 450
        if "play" in lower:
            return 400
        if "download" in lower:
            return 300
        if "videourl" in lower or "video_url" in lower:
            return 250
        if ".video" in lower or lower.endswith("video"):
            return 200
        return 100

    def meta_from(value: dict, inherited: dict | None = None) -> dict:
        inherited = inherited or {}
        if not isinstance(value, dict):
            return inherited
        return {
            "width": value.get("width") or value.get("Width") or value.get("w") or inherited.get("width"),
            "height": value.get("height") or value.get("Height") or value.get("h") or inherited.get("height"),
            "bitrate": value.get("bitrate") or value.get("bit_rate") or value.get("bandwidth") or inherited.get("bitrate"),
            "codec": value.get("codec") or value.get("format") or value.get("video_encode_type") or inherited.get("codec"),
            "fps": value.get("fps") or value.get("frame_rate") or inherited.get("fps"),
            "content_length": value.get("data_size") or value.get("file_size") or inherited.get("content_length"),
        }

    def add(raw_url: str, path: str, meta: dict | None = None) -> None:
        if blocked_key.search(path):
            return
        url = _normalize_url(raw_url, base_url)
        if not url or url in seen or not _looks_like_video_url(url, ""):
            return
        seen.add(url)
        c = _candidate_from_url_meta(url, f"{source_prefix}:data.{path[-90:]}")
        c.headers = _candidate_headers_for_provider("xhs", source_prefix)
        if meta:
            c.width = c.width or _to_int(meta.get("width"))
            c.height = c.height or _to_int(meta.get("height"))
            c.bitrate = c.bitrate or _to_int(meta.get("bitrate"))
            c.codec = c.codec or str(meta.get("codec") or "")
            c.fps = c.fps or _to_int(meta.get("fps"))
            c.content_length = c.content_length or _to_int(meta.get("content_length"))
        candidates.append(c)

    def visit(value, path: str, depth: int, meta: dict | None = None) -> None:
        if depth > 10 or value is None:
            return
        meta = meta or {}
        if isinstance(value, str):
            if value.startswith(("http://", "https://", "//", "/")) and video_key.search(path):
                add(value, path, meta)
            return
        if isinstance(value, list):
            if video_key.search(path):
                for idx, item in enumerate(value[:20]):
                    if isinstance(item, str):
                        add(item, f"{path}[{idx}]", meta)
                    else:
                        visit(item, f"{path}[{idx}]", depth + 1, meta)
                return
            for idx, item in enumerate(value[:20]):
                visit(item, f"{path}[{idx}]", depth + 1, meta)
            return
        if not isinstance(value, dict):
            return

        next_meta = meta_from(value, meta)
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if blocked_key.search(child_path):
                continue
            if isinstance(child, str) and child.startswith(("http://", "https://", "//", "/")) and video_key.search(child_path):
                add(child, child_path, next_meta)
            else:
                visit(child, child_path, depth + 1, next_meta)

    visit(payload, "", 0)
    deduped = _dedupe_candidates(candidates)
    if not deduped:
        return []
    best = sorted(deduped, key=lambda c: (priority(c.source), c.score()), reverse=True)[0]
    return [best]


def _first_text_field(payload, keys: tuple[str, ...]) -> str:
    found = ""

    def visit(value, depth: int) -> None:
        nonlocal found
        if found or depth > 8 or value is None:
            return
        if isinstance(value, list):
            for item in value[:80]:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                found = candidate.strip()
                return
        for item in value.values():
            visit(item, depth + 1)

    visit(payload, 0)
    return found[:1000]


def _xhs_note_id(url: str) -> str:
    for pattern in [
        r"/(?:explore|discovery/item)/([0-9a-fA-F]{16,40})",
        r"[?&]note_id=([0-9a-fA-F]{16,40})",
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return ""


def _tikhub_candidates(platform: str, url: str, force: bool = False) -> tuple[list[VideoCandidate], dict]:
    if not force and not _tikhub_enabled():
        return [], {"tikhub_reason": "TikHub 未启用"}
    api_key = _tikhub_api_key()
    if not api_key:
        return [], {"tikhub_reason": "TIKHUB_API_KEY 未配置"}

    if platform == "douyin":
        path = "/api/v1/douyin/web/fetch_video_high_quality_play_url"
        aweme_id = _douyin_aweme_id(url)
        region = (os.getenv("TIKHUB_DOUYIN_REGION") or get_settings().tikhub_douyin_region or "CN").strip() or "CN"
        params = {"share_url": url, "region": region}
        if aweme_id:
            params["aweme_id"] = aweme_id
        source = "tikhub:douyin_high_quality"
        referer = "https://www.douyin.com/"
    elif platform == "xhs":
        path = "/api/v1/xiaohongshu/app_v2/get_video_note_detail"
        params = {"share_text": url}
        source = "tikhub:xhs_app_v2"
        referer = "https://www.xiaohongshu.com/"
    else:
        return [], {"tikhub_reason": f"TikHub 暂不支持平台: {platform}"}

    try:
        resp = requests.get(
            f"{_tikhub_api_base()}{path}",
            params=params,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=_tikhub_timeout(),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [], {"tikhub_reason": f"TikHub 请求失败：{str(e)[:180]}"}

    if str(data.get("code", 200)) not in {"0", "200", "None"}:
        message = data.get("message_zh") or data.get("message") or json.dumps(data, ensure_ascii=False)[:220]
        return [], {"tikhub_reason": f"TikHub 返回异常：{message}"}

    if platform == "xhs":
        response_data = data.get("data") if isinstance(data, dict) else data
        candidates = _collect_xhs_tikhub_data_candidate(response_data, source)
        bit_rate_candidates = []
        play_addr_candidates = candidates
    else:
        bit_rate_candidates = _collect_bit_rate_candidates_from_payload(data, platform, source)
        play_addr_candidates = _collect_play_addr_candidates_from_payload(data, platform, source)
        candidates = _dedupe_candidates(bit_rate_candidates + play_addr_candidates)

    if not candidates:
        if platform == "xhs":
            return [], {"tikhub_reason": "TikHub 小红书响应 data 未返回 video/play_url/download_url/origin_url/master_url 可用视频地址"}
        return [], {"tikhub_reason": "TikHub 未返回 bit_rate/play_addr/original_video_url 可用视频地址"}

    return candidates, {
        "title": _first_text_field(data, ("title", "desc", "description", "display_title")) or f"{platform}_video",
        "description": _first_text_field(data, ("desc", "description", "title", "display_title")),
        "endpoint": source,
        "tikhub_bit_rate_candidates": len(bit_rate_candidates),
    }


def _download_tikhub_video(platform: str, url: str, output_dir: str, force: bool = False) -> tuple[dict | None, str]:
    candidates, meta = _tikhub_candidates(platform, url, force=force)
    if not candidates:
        return None, meta.get("tikhub_reason", "")

    best = _best_candidate(candidates)
    fhash = hashlib.md5(best.url.encode()).hexdigest()[:12]
    fpath = os.path.join(output_dir, f"{fhash}.mp4")
    referer = _platform_referer(platform)
    _download_candidate_file(best, fpath, referer)
    fsize = os.path.getsize(fpath)
    return {
        "title": meta.get("title") or f"{platform}_video",
        "description": meta.get("description") or "",
        "file_path": fpath,
        "file_size": fsize,
        "platform": platform,
        "platform_cookie": _platform_cookie_info(platform),
        "platform_cookie_configured": _platform_cookie_info(platform)["configured"],
        "candidate_count": len(candidates),
        "candidate_diagnostics": _candidate_diagnostics(candidates, best),
        **_result_quality_info(best, fpath),
    }, ""


def _mark_download_mode(result: dict, mode: str) -> dict:
    result["download_mode"] = mode
    return result


def _download_original_provider_video(platform: str, url: str, output_dir: str) -> dict:
    if platform not in {"douyin", "xhs"}:
        raise RuntimeError("原视频接口目前只支持抖音和小红书")
    result, reason = _download_shortcut_video(platform, url, output_dir, require_hd=False)
    if not result:
        raise RuntimeError(reason or "原视频接口未返回可用视频")
    return _mark_download_mode(result, "original")


def _download_builtin_video(platform: str, url: str, output_dir: str) -> dict:
    if platform == "douyin":
        return _mark_download_mode(_download_douyin_builtin(url, output_dir), "builtin")
    if platform == "xhs":
        return _mark_download_mode(_download_xhs_builtin(url, output_dir), "builtin")
    if platform == "bilibili":
        return _mark_download_mode(download_bilibili(url, output_dir), "builtin")
    raise RuntimeError(f"自建解析暂不支持平台: {platform}")


def _download_tikhub_forced_video(platform: str, url: str, output_dir: str) -> dict:
    if platform not in {"douyin", "xhs"}:
        raise RuntimeError("TikHub 付费下载目前只支持抖音和小红书")
    result, reason = _download_tikhub_video(platform, url, output_dir, force=True)
    if not result:
        raise RuntimeError(reason or "TikHub 未返回可用视频")
    return _mark_download_mode(result, "tikhub")


XHS_4K_PROBE_PROVIDERS = {
    "original": {
        "label": "原视频接口 xhshdvideo",
        "charges": "原视频接口额度",
    },
    "tikhub_app_v2": {
        "label": "TikHub App V2 get_video_note_detail",
        "path": "/api/v1/xiaohongshu/app_v2/get_video_note_detail",
        "charges": "TikHub",
    },
    "tikhub_app_v1": {
        "label": "TikHub App V1 get_video_note_info",
        "path": "/api/v1/xiaohongshu/app/get_video_note_info",
        "charges": "TikHub",
    },
    "tikhub_web_v4": {
        "label": "TikHub Web V4 get_note_info_v4",
        "path": "/api/v1/xiaohongshu/web/get_note_info_v4",
        "charges": "TikHub",
    },
    "tikhub_web_v5_cookie": {
        "label": "TikHub Web V5 自带 Cookie",
        "path": "/api/v1/xiaohongshu/web/get_note_info_v5",
        "charges": "TikHub",
        "method": "POST",
    },
}


def _xhs_note_id_and_xsec_token(url: str) -> tuple[str, str]:
    target = url
    if "xhslink.com" in urlparse(url).netloc:
        try:
            resp = requests.get(
                url,
                allow_redirects=False,
                timeout=10,
                headers={"User-Agent": MOBILE_VIDEO_UA},
            )
            target = resp.headers.get("location") or url
        except Exception:
            target = url
    parsed = urlparse(target)
    note_id = ""
    m = re.search(r"/(?:explore|discovery/item)/([0-9a-fA-F]{16,40})", parsed.path)
    if m:
        note_id = m.group(1)
    query = parse_qs(parsed.query)
    xsec_token = (query.get("xsec_token") or query.get("xsecToken") or [""])[0]
    return note_id, xsec_token


def _download_single_xhs_candidate(candidate: VideoCandidate, output_dir: str) -> tuple[str, int]:
    fhash = hashlib.md5(candidate.url.encode()).hexdigest()[:12]
    fpath = os.path.join(output_dir, f"{fhash}.mp4")
    _download_candidate_file(candidate, fpath, "https://www.xiaohongshu.com/")
    return fpath, os.path.getsize(fpath)


def _probe_xhs_tikhub_endpoint(url: str, provider: str, output_dir: str) -> dict:
    provider_config = XHS_4K_PROBE_PROVIDERS[provider]
    api_key = _tikhub_api_key()
    if not api_key:
        raise RuntimeError("TIKHUB_API_KEY 未配置")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if provider_config.get("method") == "POST":
        note_id, xsec_token = _xhs_note_id_and_xsec_token(url)
        cookie = _cookie_header("XHS_COOKIE")
        if not note_id or not xsec_token:
            raise RuntimeError("Web V5 需要从分享链接解析 note_id 和 xsec_token")
        if not cookie:
            raise RuntimeError("Web V5 需要先在管理员后台配置小红书 Cookie")
        resp = requests.post(
            f"{_tikhub_api_base()}{provider_config['path']}",
            headers={**headers, "Content-Type": "application/json"},
            json={"note_id": note_id, "xsec_token": xsec_token, "cookie": cookie, "proxy": ""},
            timeout=_tikhub_timeout(),
        )
    else:
        resp = requests.get(
            f"{_tikhub_api_base()}{provider_config['path']}",
            params={"share_text": url},
            headers=headers,
            timeout=_tikhub_timeout(),
        )
    resp.raise_for_status()
    data = resp.json()
    if str(data.get("code", 200)) not in {"0", "200", "None"}:
        message = data.get("message_zh") or data.get("message") or json.dumps(data, ensure_ascii=False)[:220]
        raise RuntimeError(f"TikHub 返回异常：{message}")

    response_data = data.get("data") if isinstance(data, dict) else data
    candidates = _collect_xhs_tikhub_data_candidate(response_data, f"xhs_probe:{provider}")
    if not candidates:
        raise RuntimeError("该接口未返回可用视频地址")

    best = candidates[0]
    fpath, fsize = _download_single_xhs_candidate(best, output_dir)
    return {
        "provider": provider,
        "provider_label": provider_config["label"],
        "charges": provider_config["charges"],
        "platform": "xhs",
        "file_size": fsize,
        "candidate_count": len(candidates),
        "candidate_diagnostics": _candidate_diagnostics(candidates, best, limit=1),
        **_result_quality_info(best, fpath),
    }


def probe_xhs_4k_endpoint(url: str, provider: str, output_dir: str) -> dict:
    provider = (provider or "").strip()
    if provider not in XHS_4K_PROBE_PROVIDERS:
        raise RuntimeError("不支持的探测接口")
    if get_platform(url) != "xhs":
        raise RuntimeError("只支持小红书链接")

    if provider == "original":
        result, reason = _download_shortcut_video("xhs", url, output_dir, require_hd=False)
        if not result:
            raise RuntimeError(reason or "原视频接口未返回可用视频")
        result.update({
            "provider": provider,
            "provider_label": XHS_4K_PROBE_PROVIDERS[provider]["label"],
            "charges": XHS_4K_PROBE_PROVIDERS[provider]["charges"],
        })
        return result
    return _probe_xhs_tikhub_endpoint(url, provider, output_dir)


def _shortcut_candidates(platform: str, url: str) -> tuple[list[VideoCandidate], dict]:
    if not _shortcut_api_enabled():
        return [], {}
    unavailable_reason = _shortcut_unavailable_reason()
    if unavailable_reason:
        logger.warning(unavailable_reason)
        return [], {"shortcut_fallback_reason": unavailable_reason}

    config = _load_shortcut_config()
    version = str(config.get("Version") or os.getenv("VIDEO_SHORTCUT_API_VERSION", "22"))
    auth_code = _shortcut_auth_code()
    endpoint_plan = {
        "xhs": [("xhshdvideo", "/xhshdvideo")],
        "douyin": [("dyhd", "/dyhd")],
    }.get(platform, [])

    last_error = ""
    for endpoint_key, fallback_path in endpoint_plan:
        endpoint = _shortcut_endpoint(config, endpoint_key, fallback_path)
        try:
            resp = requests.get(
                endpoint,
                params={"url": url, "v": version, "kl": auth_code},
                headers={"User-Agent": "Shortcuts/3210 CFNetwork/1568.200.51 Darwin/24.1.0"},
                timeout=35,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            last_error = f"{endpoint_key}: {e}"
            if _looks_like_shortcut_quota_limit(str(e)):
                reason = f"第三方原画接口请求失败：{str(e)[:160]}"
                if _shortcut_rate_limit_cooldown_seconds() > 0:
                    _mark_shortcut_rate_limited(reason)
                return [], {"shortcut_fallback_reason": reason}
            logger.warning(f"快捷指令接口 {endpoint_key} 请求失败: {e}")
            continue

        if str(data.get("code", 0)) not in {"0", "200", "None"}:
            message = data.get("msg") or data.get("message") or json.dumps(data, ensure_ascii=False)[:200]
            last_error = f"{endpoint_key}: {message}"
            if _looks_like_shortcut_quota_limit(str(message)):
                reason = f"第三方原画接口请求失败：{message}"
                if _shortcut_rate_limit_cooldown_seconds() > 0:
                    _mark_shortcut_rate_limited(reason)
                return [], {"shortcut_fallback_reason": reason}
            logger.warning(f"快捷指令接口 {endpoint_key} 返回异常: {message}")
            continue

        body = data.get("body") if isinstance(data.get("body"), dict) else data
        media_urls = _collect_shortcut_urls(body)
        if not media_urls:
            last_error = f"{endpoint_key}: 未返回视频地址"
            logger.warning(f"快捷指令接口 {endpoint_key} 未返回视频地址: {data.get('msg') or ''}")
            continue

        headers = _video_headers_from_shortcut(data, "https://www.xiaohongshu.com/" if platform == "xhs" else "https://www.douyin.com/")
        candidates = []
        for media_url in media_urls:
            c = _candidate_from_url_meta(media_url, f"shortcut_api:{endpoint_key}")
            c.headers = headers
            candidates.append(c)

        meta = {
            "title": (body.get("title") or data.get("title") or f"{platform}_video")[:200],
            "description": (body.get("title") or data.get("msg") or "")[:1000],
            "message": data.get("msg") or "",
            "endpoint": endpoint_key,
            "shortcut_quota": _shortcut_quota_state(),
        }
        return candidates, meta

    if last_error:
        return [], {"shortcut_fallback_reason": f"第三方原画接口未返回可用高清视频：{last_error[:180]}"}
    return [], {}


def _launch_chromium(p, headless: bool = True):
    args = ["--no-sandbox"]
    preferred_channel = os.getenv("VIDEO_BROWSER_CHANNEL", "chrome").strip()
    if preferred_channel:
        try:
            return p.chromium.launch(channel=preferred_channel, headless=headless, args=args)
        except Exception as e:
            logger.warning(f"启动浏览器 channel={preferred_channel} 失败，回退到 Playwright Chromium: {e}")
    return p.chromium.launch(headless=headless, args=args)


def _candidate_from_url_meta(url: str, source: str, content_length: int = 0, content_type: str = "") -> VideoCandidate:
    width = 0
    height = 0
    codec = ""

    for m in re.finditer(r"(?<!\d)(\d{3,5})[xX*](\d{3,5})(?!\d)", url):
        a, b = _to_int(m.group(1)), _to_int(m.group(2))
        if a and b:
            width, height = a, b

    for pattern in [r"ratio=(\d{3,4})p", r"[?&]quality=(\d{3,4})", r"_(\d{3,4})p(?:_|\\.|&)"]:
        m = re.search(pattern, url, re.I)
        if m:
            height = max(height, _to_int(m.group(1)))

    lower = url.lower() + " " + content_type.lower()
    if "h265" in lower or "hevc" in lower or "bytevc1" in lower:
        codec = "h265"
    elif "h264" in lower or "avc" in lower:
        codec = "h264"

    return VideoCandidate(url=url, source=source, width=width, height=height, codec=codec, content_length=content_length)


def _looks_like_video_url(url: str, content_type: str = "") -> bool:
    lower_url = url.lower()
    lower_type = content_type.lower()
    if "video/" in lower_type:
        return True
    if any(ext in lower_url for ext in [".mp4", ".m4v", ".mov"]):
        return True
    if lower_type and "octet-stream" not in lower_type:
        return False
    return any(mark in lower_url for mark in ["videotx", "video/tos", "aweme", "sns-video"])


def _content_length_from_headers(headers: dict) -> int:
    length = _to_int(headers.get("content-length") or headers.get("Content-Length"))
    if length:
        return length
    content_range = headers.get("content-range") or headers.get("Content-Range") or ""
    m = re.search(r"/(\d+)$", content_range)
    return _to_int(m.group(1)) if m else 0


def _capture_mobile_media_candidates(page, target_url: str, wait_ms: int = 8000) -> list[VideoCandidate]:
    candidates: list[VideoCandidate] = []
    seen = set()

    def on_response(response):
        try:
            headers = response.headers
            content_type = headers.get("content-type", "")
            media_url = response.url
            if response.status >= 400 or not _looks_like_video_url(media_url, content_type):
                return
            if media_url in seen:
                return
            seen.add(media_url)
            resource_type = ""
            try:
                resource_type = response.request.resource_type
            except Exception:
                pass
            candidates.append(_candidate_from_url_meta(
                media_url,
                f"shortcut_capture:{resource_type or 'response'}",
                content_length=_content_length_from_headers(headers),
                content_type=content_type,
            ))
        except Exception as e:
            logger.debug(f"媒体响应捕获失败: {e}")

    page.on("response", on_response)
    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=wait_ms)
    except Exception:
        page.wait_for_timeout(wait_ms)
    try:
        page.evaluate("""() => {
            const v = document.querySelector('video');
            if (v) {
                v.muted = true;
                v.playsInline = true;
                const p = v.play();
                if (p && p.catch) p.catch(() => {});
            }
        }""")
        page.wait_for_timeout(2500)
    except Exception:
        pass
    return candidates


def _browser_video_candidate_dicts(page, platform: str) -> list[dict]:
    try:
        data = page.evaluate(
            """(platform) => {
                const out = [];
                const seen = new Set();
                const maxItems = 180;
                const videoUrlRe = /(mp4|m4v|mov|videotx|video\\/tos|aweme|sns-video|bytevc1|h265|h264)/i;

                function add(url, source, meta) {
                    if (!url || typeof url !== 'string') return;
                    url = url.replace(/\\\\u002F/g, '/').replace(/\\\\\\//g, '/');
                    if (!/^https?:\\/\\//i.test(url) && !/^\\/\\//.test(url)) return;
                    if (!videoUrlRe.test(url)) return;
                    if (seen.has(url) || out.length >= maxItems) return;
                    seen.add(url);
                    meta = meta || {};
                    out.push({
                        url,
                        source,
                        width: Number(meta.width || meta.w || 0) || 0,
                        height: Number(meta.height || meta.h || 0) || 0,
                        bitrate: Number(meta.bitrate || meta.bit_rate || meta.video_bitrate || meta.avg_bitrate || meta.bandwidth || 0) || 0,
                        codec: String(meta.codec || meta.format || meta.codecs || meta.encode_type || '')
                    });
                }

                function firstUrls(value) {
                    if (!value) return [];
                    if (typeof value === 'string') return [value];
                    if (Array.isArray(value)) return value.filter(x => typeof x === 'string');
                    if (typeof value === 'object') {
                        if (Array.isArray(value.url_list)) return value.url_list.filter(Boolean);
                        if (Array.isArray(value.urlList)) return value.urlList.filter(Boolean);
                        if (Array.isArray(value.UrlList)) return value.UrlList.filter(Boolean);
                        if (Array.isArray(value.URLList)) return value.URLList.filter(Boolean);
                        if (Array.isArray(value.backupUrls)) return value.backupUrls.filter(Boolean);
                        if (Array.isArray(value.backup_urls)) return value.backup_urls.filter(Boolean);
                        if (Array.isArray(value.urls)) return value.urls.filter(Boolean);
                        if (typeof value.url === 'string') return [value.url];
                        if (typeof value.Url === 'string') return [value.Url];
                        if (typeof value.URL === 'string') return [value.URL];
                        if (typeof value.master_url === 'string') return [value.master_url];
                        if (typeof value.masterUrl === 'string') return [value.masterUrl];
                        if (typeof value.baseUrl === 'string') return [value.baseUrl];
                        if (typeof value.base_url === 'string') return [value.base_url];
                        if (typeof value.play_url === 'string') return [value.play_url];
                        if (typeof value.download_url === 'string') return [value.download_url];
                    }
                    return [];
                }

                function metaFromObject(obj, inherited) {
                    const meta = Object.assign({}, inherited || {});
                    if (!obj || typeof obj !== 'object') return meta;
                    for (const key of ['width', 'height', 'Width', 'Height', 'w', 'h', 'bitrate', 'bit_rate', 'Bitrate', 'BitRate', 'video_bitrate', 'avg_bitrate', 'bandwidth', 'data_size', 'DataSize', 'file_size', 'FileSize', 'codec', 'format', 'Format', 'codecs', 'encode_type', 'video_encode_type']) {
                        if (obj[key] !== undefined && obj[key] !== null && obj[key] !== '') meta[key] = obj[key];
                    }
                    const text = JSON.stringify(obj).slice(0, 1600).toLowerCase();
                    if (!meta.codec && (text.includes('h265') || text.includes('hevc') || text.includes('bytevc1'))) meta.codec = 'h265';
                    if (!meta.codec && (text.includes('h264') || text.includes('avc'))) meta.codec = 'h264';
                    return meta;
                }

                function visit(value, path, depth, meta) {
                    if (out.length >= maxItems || depth > 8 || value === null || value === undefined) return;
                    if (typeof value === 'string') {
                        if (/url|addr|video|play|download|master|stream|backup|h26|bytevc1/i.test(path)) add(value, path, meta);
                        return;
                    }
                    if (typeof value !== 'object') return;
                    const nextMeta = metaFromObject(value, meta);

                    for (const url of firstUrls(value)) add(url, path, nextMeta);

                    if (Array.isArray(value)) {
                        for (let i = 0; i < Math.min(value.length, 80); i++) visit(value[i], `${path}[${i}]`, depth + 1, nextMeta);
                        return;
                    }

                    for (const [key, child] of Object.entries(value)) {
                        const childPath = path ? `${path}.${key}` : key;
                        if (/url|addr|video|play|download|master|stream|backup|h265|h264|bytevc1|bit_rate|bitrate|bitrateInfo|PlayAddr|UrlList/i.test(key)) {
                            const childMeta = metaFromObject(child, nextMeta);
                            for (const url of firstUrls(child)) add(url, childPath, childMeta);
                        }
                        visit(child, childPath, depth + 1, nextMeta);
                    }
                }

                const commonNames = ['__NEXT_DATA__', '__NUXT__', '__APOLLO_STATE__'];
                const names = platform === 'douyin'
                    ? ['_ROUTER_DATA', 'RENDER_DATA', '__UNIVERSAL_DATA_FOR_REHYDRATION__', ...commonNames]
                    : ['__INITIAL_STATE__', '__INITIAL_SSR_STATE__', '__INITIAL_DATA__', '__REDUX_STATE__', ...commonNames];
                for (const name of names) {
                    try {
                        const value = window[name];
                        if (value) visit(value, `window.${name}`, 0, {});
                    } catch (_) {}
                }

                for (const script of Array.from(document.querySelectorAll('script'))) {
                    if (out.length >= maxItems) break;
                    let text = (script.textContent || '').trim();
                    if (!text || !/(mp4|videotx|video\\/tos|aweme|sns-video|masterUrl|master_url|bit_rate|bitrateInfo|PlayAddr|UrlList|bytevc1)/i.test(text)) continue;
                    if (script.id === 'RENDER_DATA') {
                        try { text = decodeURIComponent(text); } catch (_) {}
                    }
                    if ((text.startsWith('{') && text.endsWith('}')) || (text.startsWith('[') && text.endsWith(']'))) {
                        try {
                            visit(JSON.parse(text), `script#${script.id || script.type || 'json'}`, 0, {});
                            continue;
                        } catch (_) {}
                    }
                    const urlRe = /https?:\\\\?\\/\\\\?\\/[^"'<>{}\\s]+/g;
                    let m;
                    while ((m = urlRe.exec(text)) && out.length < maxItems) {
                        add(m[0].replace(/\\\\\\//g, '/'), `script:${script.id || 'inline'}`, {});
                    }
                }
                return out;
            }""",
            platform,
        )
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug(f"页面候选流扫描失败: {e}")
        return []


def _append_browser_video_candidates(candidates: list[VideoCandidate], items: list[dict], base_url: str, source_prefix: str) -> None:
    seen = {c.url for c in candidates}
    for item in items or []:
        source = str(item.get("source") or "page_data")
        if re.search(r"(related|recommend|cover|avatar|image|poster)", source, re.I):
            continue
        raw_url = str(item.get("url") or "").replace("\\u002F", "/").replace("\\/", "/")
        url = _normalize_url(raw_url, base_url)
        if not url or url in seen or not _looks_like_video_url(url, ""):
            continue
        seen.add(url)
        candidates.append(VideoCandidate(
            url=url,
            source=f"{source_prefix}:{source}",
            width=_to_int(item.get("width")),
            height=_to_int(item.get("height")),
            bitrate=_to_int(item.get("bitrate")),
            codec=str(item.get("codec") or ""),
        ))


def _douyin_addr_urls(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://", "//", "/")) else []
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(_douyin_addr_urls(item))
        return urls
    if not isinstance(value, dict):
        return []

    urls = []
    for key in [
        "url_list", "urlList", "UrlList", "URLList", "urls", "Urls",
        "url", "Url", "URL", "uri", "Uri", "baseUrl", "base_url", "play_url", "download_url",
    ]:
        item = value.get(key)
        if isinstance(item, list):
            urls.extend([str(x) for x in item if isinstance(x, str)])
        elif isinstance(item, str):
            urls.append(item)
    return [u for u in urls if u.startswith(("http://", "https://", "//", "/"))]


def _douyin_codec(*values) -> str:
    text = " ".join(str(v or "") for v in values).lower()
    if "bytevc1" in text or "h265" in text or "h.265" in text or "hevc" in text:
        return "h265"
    if "h264" in text or "h.264" in text or "avc" in text:
        return "h264"
    return ""


def _append_douyin_addr_candidates(
    candidates: list[VideoCandidate],
    addr,
    source: str,
    base_url: str,
    meta: dict | None = None,
) -> None:
    meta = meta or {}
    addr_meta = addr if isinstance(addr, dict) else {}
    width = _to_int(addr_meta.get("width") or addr_meta.get("Width") or meta.get("width"))
    height = _to_int(addr_meta.get("height") or addr_meta.get("Height") or meta.get("height"))
    bitrate = _to_int(
        meta.get("bitrate")
        or meta.get("bit_rate")
        or addr_meta.get("bitrate")
        or addr_meta.get("bit_rate")
        or addr_meta.get("Bitrate")
        or addr_meta.get("BitRate")
    )
    content_length = _to_int(
        meta.get("content_length")
        or meta.get("data_size")
        or addr_meta.get("data_size")
        or addr_meta.get("DataSize")
        or addr_meta.get("file_size")
        or addr_meta.get("FileSize")
    )
    codec = meta.get("codec") or _douyin_codec(source, meta, addr_meta)
    for raw_url in _douyin_addr_urls(addr):
        url = _normalize_url(_remove_douyin_watermark(raw_url), base_url)
        if not url:
            continue
        if not _looks_like_video_url(url, ""):
            continue
        candidates.append(VideoCandidate(
            url=url,
            source=source,
            width=width,
            height=height,
            bitrate=bitrate,
            codec=codec,
            content_length=content_length,
            fps=_to_int(meta.get("fps")),
        ))


def _append_douyin_video_candidates(candidates: list[VideoCandidate], video: dict, base_url: str, source_prefix: str) -> None:
    if not isinstance(video, dict):
        return
    start_count = len(candidates)
    base_meta = {
        "width": video.get("width") or video.get("Width") or video.get("origin_cover", {}).get("width"),
        "height": video.get("height") or video.get("Height") or video.get("origin_cover", {}).get("height"),
        "bitrate": video.get("bit_rate") or video.get("bitrate"),
        "fps": video.get("fps") or video.get("FPS") or video.get("frame_rate"),
    }

    bitrate_items = []
    for key in ["bit_rate", "bitRate", "bitrate", "bitrateInfo", "bitrate_info", "BitrateInfo", "Bitrate"]:
        value = video.get(key)
        if isinstance(value, list):
            bitrate_items.extend(value)
    for idx, item in enumerate(bitrate_items):
        if not isinstance(item, dict):
            continue
        gear = item.get("gear_name") or item.get("GearName") or item.get("quality_type") or item.get("QualityType") or idx
        meta = {
            "width": item.get("width") or item.get("Width") or base_meta.get("width"),
            "height": item.get("height") or item.get("Height") or base_meta.get("height"),
            "bitrate": item.get("bit_rate") or item.get("bitrate") or item.get("Bitrate") or item.get("BitRate"),
            "content_length": item.get("data_size") or item.get("DataSize") or item.get("file_size") or item.get("FileSize"),
            "codec": _douyin_codec(item.get("video_encode_type"), item.get("format"), item.get("Format"), item),
            "fps": item.get("fps") or item.get("FPS") or item.get("frame_rate") or base_meta.get("fps"),
        }
        for addr_key in ["play_addr", "playAddr", "PlayAddr", "PlayAddrH264", "play_addr_h264", "play_addr_265", "play_addr_bytevc1", "PlayAddrBytevc1"]:
            if addr_key in item:
                _append_douyin_addr_candidates(candidates, item.get(addr_key), f"{source_prefix}:bit_rate:{gear}", base_url, meta)

    addr_keys = [
        "play_addr", "playAddr", "PlayAddr",
        "play_addr_h264", "playAddrH264", "PlayAddrH264",
        "play_addr_265", "play_addr_bytevc1", "playAddrBytevc1", "PlayAddrBytevc1",
    ]
    for key in addr_keys:
        if key in video:
            meta = dict(base_meta)
            if re.search(r"265|bytevc1", key, re.I):
                meta["codec"] = "h265"
            elif re.search(r"264", key, re.I):
                meta["codec"] = "h264"
            _append_douyin_addr_candidates(candidates, video.get(key), f"{source_prefix}:{key}", base_url, meta)

    # download_addr often points to watermark or recompressed media. Keep it only
    # as a last-resort fallback when no play_addr/bit_rate candidate exists.
    if len(candidates) == start_count:
        for key in ["download_addr", "downloadAddr", "DownloadAddr"]:
            if key in video:
                _append_douyin_addr_candidates(candidates, video.get(key), f"{source_prefix}:{key}:fallback", base_url, base_meta)


def _append_douyin_candidates_from_payload(candidates: list[VideoCandidate], payload, base_url: str, source_prefix: str) -> str:
    description = ""

    def visit(value, path: str, depth: int) -> None:
        nonlocal description
        if depth > 8 or value is None:
            return
        if isinstance(value, list):
            for idx, child in enumerate(value[:80]):
                visit(child, f"{path}[{idx}]", depth + 1)
            return
        if not isinstance(value, dict):
            return

        if isinstance(value.get("desc"), str) and len(value.get("desc", "")) > len(description):
            description = value.get("desc", "")[:1000]
        if isinstance(value.get("description"), str) and len(value.get("description", "")) > len(description):
            description = value.get("description", "")[:1000]

        video = value.get("video") or value.get("Video")
        if isinstance(video, dict):
            _append_douyin_video_candidates(candidates, video, base_url, f"{source_prefix}:{path}.video")

        for key, child in value.items():
            if key in {"video", "Video"}:
                continue
            visit(child, f"{path}.{key}" if path else key, depth + 1)

    visit(payload, source_prefix, 0)
    return description


def _douyin_aweme_id(*values: str) -> str:
    for value in values:
        if not value:
            continue
        for pattern in [
            r"/(?:video|note)/(\d{8,30})",
            r'"aweme_id"\s*:\s*"(\d{8,30})"',
            r'"awemeId"\s*:\s*"(\d{8,30})"',
            r"aweme_id=(\d{8,30})",
        ]:
            m = re.search(pattern, value)
            if m:
                return m.group(1)
    return ""


def _douyin_web_detail_candidates(page, url: str) -> tuple[list[VideoCandidate], str, str]:
    aweme_id = _douyin_aweme_id(url, page.url, page.content()[:300000])
    if not aweme_id:
        return [], "", "未识别到 aweme_id"
    api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}&aid=6383&device_platform=webapp"
    try:
        resp = page.request.get(api_url, timeout=20000, headers={"Referer": page.url, "Accept": "application/json"})
        if resp.status >= 400:
            return [], "", f"web_detail HTTP {resp.status}"
        data = resp.json()
    except Exception as e:
        return [], "", f"web_detail 请求失败: {str(e)[:120]}"
    candidates: list[VideoCandidate] = []
    description = _append_douyin_candidates_from_payload(candidates, data, page.url, "web_detail")
    return candidates, description, ""


def _append_xhs_origin_key_candidates(candidates: list[VideoCandidate], page_src: str) -> None:
    seen = {c.url for c in candidates}
    keys = []
    patterns = [
        r'"originVideoKey"\s*:\s*"([^"]{8,300})"',
        r'"origin_video_key"\s*:\s*"([^"]{8,300})"',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, page_src):
            key = m.group(1).replace("\\u002F", "/").replace("\\/", "/").strip()
            if key and key not in keys:
                keys.append(key)
    for key in keys[:6]:
        url = key if key.startswith(("http://", "https://")) else f"https://sns-video-bd.xhscdn.com/{key}"
        url = url.replace("http://", "https://")
        if url in seen:
            continue
        seen.add(url)
        candidates.append(VideoCandidate(
            url=url,
            source="originVideoKey",
            codec="h265" if "h265" in key.lower() or "hevc" in key.lower() else "",
        ))


def extract_url(text: str) -> Optional[str]:
    """从分享文本中提取 URL"""
    urls = _URL_PATTERN.findall(text)
    for u in urls:
        u = u.rstrip(".,;:!?）)】》>")
        if get_platform(u):
            return u
    return None


def get_platform(url: str) -> Optional[str]:
    """识别平台"""
    for p in _DOUYIN_PATTERNS:
        if re.search(p, url):
            return "douyin"
    for p in _XHS_PATTERNS:
        if re.search(p, url):
            return "xhs"
    for p in _BILI_PATTERNS:
        if re.search(p, url):
            return "bilibili"
    return None


def _remove_douyin_watermark(url: str) -> str:
    """去除抖音水印"""
    if not url:
        return url
    return url.replace("/playwm/", "/play/").replace("ratio=720p", "ratio=1080p")


def _normalize_url(url: str, referer: str = "") -> str:
    """规范化 URL"""
    if not url:
        return url
    url = url.strip()
    if re.match(r'https?://[^/]', url):
        return url
    if url.startswith("https:///") or url.startswith("http:///"):
        path = url.split(":///", 1)[1]
        host = "aweme.snssdk.com"
        if referer:
            try: host = urlparse(referer).netloc or host
            except: pass
        return f"https://{host}/{path}"
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        host = "aweme.snssdk.com"
        if referer:
            try: host = urlparse(referer).netloc or host
            except: pass
        return f"https://{host}{url}"
    return url


def _download_file(url: str, dest_path: str, referer: str = ""):
    """下载文件"""
    hdrs = {
        "User-Agent": MOBILE_VIDEO_UA,
        "Referer": referer or "https://www.douyin.com/",
        "Accept": "*/*",
    }
    resp = requests.get(url, headers=hdrs, stream=True, timeout=120)
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct or "application/json" in ct or (ct and not _looks_like_video_url(url, ct)):
        raise RuntimeError(f"CDN 返回 {ct}，不是视频")
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
            if chunk:
                f.write(chunk)
    size = os.path.getsize(dest_path)
    if size < 10000:
        raise RuntimeError(f"文件太小: {size} 字节")


def _download_candidate_file(candidate: VideoCandidate, dest_path: str, referer: str = ""):
    hdrs = {
        "User-Agent": MOBILE_VIDEO_UA,
        "Referer": referer or "https://www.douyin.com/",
        "Accept": "*/*",
        **(candidate.headers or {}),
    }
    host = urlparse(candidate.url).netloc or "unknown"
    try:
        resp = requests.get(candidate.url, headers=hdrs, stream=True, timeout=120)
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            raise RuntimeError(f"CDN 下载失败：HTTP {resp.status_code}（{host}）")
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct or "application/json" in ct or (ct and not _looks_like_video_url(candidate.url, ct)):
            raise RuntimeError(f"CDN 返回 {ct or '未知类型'}，不是视频（{host}）")
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
    except requests.Timeout as e:
        raise RuntimeError(f"CDN 下载超时（{host}）：TikHub 已返回视频地址，但服务器拉取原视频太慢或被 CDN 卡住") from e
    except requests.RequestException as e:
        raise RuntimeError(f"CDN 下载连接失败（{host}）：{str(e)[:160]}") from e
    size = os.path.getsize(dest_path)
    if size < 10000:
        raise RuntimeError(f"文件太小: {size} 字节")


def _extract_asr_text(payload: dict) -> str:
    result = payload.get("result")
    if isinstance(result, list):
        texts = [item.get("text", "").strip() for item in result if isinstance(item, dict)]
        return "\n".join([text for text in texts if text])
    if isinstance(result, dict):
        return (result.get("text") or "").strip()
    data = payload.get("data")
    if isinstance(data, dict):
        return (data.get("text") or "").strip()
    return (payload.get("text") or "").strip()


def _extract_sse_asr_text(response: requests.Response) -> str:
    final_text = ""
    deltas: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            logger.debug(f"忽略无法解析的 ASR SSE 行: {line[:120]}")
            continue

        event_type = event.get("type")
        if event_type == "error":
            raise RuntimeError(event.get("message") or "StepAudio ASR SSE 返回错误")
        if event_type == "transcript.text.delta":
            delta = event.get("delta") or ""
            if delta:
                deltas.append(delta)
        elif event_type == "transcript.text.done":
            final_text = (event.get("text") or "").strip()
        elif event.get("text"):
            final_text = str(event.get("text")).strip()
    return final_text or "".join(deltas).strip()


def _transcribe_step_plan_sse(audio_path: str) -> str:
    audio_data = base64.b64encode(Path(audio_path).read_bytes()).decode("ascii")
    payload = {
        "audio": {
            "data": audio_data,
            "input": {
                "transcription": {
                    "model": ASR_MODEL,
                    "language": "zh",
                    "enable_itn": True,
                },
                "format": {
                    "type": "mp3",
                },
            },
        },
    }
    response = requests.post(
        f"{API_BASE}/audio/asr/sse",
        headers=_stepaudio_headers({"Content-Type": "application/json", "Accept": "text/event-stream"}),
        json=payload,
        stream=True,
        timeout=180,
    )
    if response.status_code != 200:
        logger.warning(f"ASR SSE 转写失败: {response.status_code} {response.text[:300]}")
        raise RuntimeError(f"StepAudio 文案解析失败: HTTP {response.status_code}")
    return _extract_sse_asr_text(response)


def _transcribe_legacy_multipart(audio_path: str) -> str:
    with open(audio_path, "rb") as f:
        response = requests.post(
            f"{API_BASE}/audio/transcriptions",
            headers=_stepaudio_headers(),
            files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
            data={"model": "step-asr", "response_format": "json"},
            timeout=180,
        )

    if response.status_code != 200:
        logger.warning(f"ASR 转写失败: {response.status_code} {response.text[:300]}")
        raise RuntimeError(f"StepAudio 文案解析失败: HTTP {response.status_code}")

    try:
        payload = response.json()
    except Exception:
        payload = {"text": response.text}
    return _extract_asr_text(payload)


def _transcribe_with_stepaudio(video_path: str) -> str:
    """使用 StepAudio ASR 进行语音识别"""
    audio_path = ""
    try:
        # 提取小体积音频，降低上传耗时和 API 处理压力。
        audio_path = video_path + ".mp3"
        result = subprocess.run(
            ["ffmpeg", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", "-y", audio_path],
            capture_output=True, timeout=120
        )

        if result.returncode != 0:
            err = result.stderr.decode(errors="ignore")[:300]
            logger.warning(f"音频提取失败: {err}")
            raise RuntimeError("音频提取失败，视频可能没有可识别的音轨")

        # 检查音频文件大小
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
            logger.warning("音频文件太小或不存在")
            return ""

        logger.info(f"音频提取成功: {audio_path}, 大小: {os.path.getsize(audio_path)} 字节")

        if "/step_plan/" in API_BASE:
            text = _transcribe_step_plan_sse(audio_path)
        else:
            # 兼容旧开放平台的表单上传转写接口。
            text = _transcribe_legacy_multipart(audio_path)
        if text:
            logger.info(f"ASR 识别成功: {text[:100]}...")
        else:
            logger.warning("ASR 完成但未返回文本")
        return text

    except Exception as e:
        logger.warning(f"ASR 错误: {e}")
        raise
    finally:
        # 清理临时文件
        if audio_path:
            try: os.remove(audio_path)
            except: pass


def _download_shortcut_video(platform: str, url: str, output_dir: str, require_hd: bool = False) -> tuple[dict | None, str]:
    shortcut_candidates, shortcut_meta = _shortcut_candidates(platform, url)
    if not shortcut_candidates:
        return None, shortcut_meta.get("shortcut_fallback_reason", "")

    best = _best_candidate(shortcut_candidates)
    fhash = hashlib.md5(best.url.encode()).hexdigest()[:12]
    fpath = os.path.join(output_dir, f"{fhash}.mp4")
    referer = "https://www.xiaohongshu.com/" if platform == "xhs" else "https://www.douyin.com/"
    _download_candidate_file(best, fpath, referer)
    fsize = os.path.getsize(fpath)
    result = {
        "title": shortcut_meta.get("title") or f"{platform}_video",
        "description": shortcut_meta.get("description") or "",
        "file_path": fpath,
        "file_size": fsize,
        "platform": platform,
        "platform_cookie": _platform_cookie_info(platform),
        "platform_cookie_configured": _platform_cookie_info(platform)["configured"],
        "candidate_count": len(shortcut_candidates),
        "candidate_diagnostics": _candidate_diagnostics(shortcut_candidates, best),
        **_result_quality_info(best, fpath),
    }
    if require_hd and not _result_meets_hd_threshold(result):
        reason = _below_original_threshold_reason(result, "第三方原画接口")
        _delete_downloaded_result_file(result)
        return None, reason

    result["shortcut_quota"] = _record_shortcut_success(platform, shortcut_meta.get("endpoint", ""))
    return result, ""


def _download_ytdlp_video(platform: str, url: str, output_dir: str) -> tuple[dict | None, str]:
    if platform != "douyin" or not _ytdlp_fallback_enabled():
        return None, "yt-dlp 兜底未启用"
    try:
        import yt_dlp  # noqa: F401
    except Exception:
        return None, "yt-dlp 未安装，无法执行低清兜底"

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "ytdlp_%(id)s.%(ext)s")
    cmd = [
        sys.executable,
        "-m", "yt_dlp",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "-f", "bv*+ba/best",
        "-o", output_template,
        "--add-header", "Referer: https://www.douyin.com/",
        "--add-header", "User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    ]
    if cookie := _cookie_header("DOUYIN_COOKIE"):
        cmd.extend(["--add-header", f"Cookie: {cookie}"])
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, "yt-dlp 兜底超时"
    except Exception as e:
        return None, f"yt-dlp 兜底启动失败：{str(e)[:160]}"

    if result.returncode != 0:
        detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip().splitlines()
        return None, f"yt-dlp 兜底失败：{(detail[-1] if detail else '')[:220]}"

    files = [p for p in out_dir.glob("ytdlp_*") if p.is_file() and p.stat().st_size > 10000]
    if not files:
        return None, "yt-dlp 兜底完成但未生成可用视频文件"
    fpath = max(files, key=lambda p: p.stat().st_size)
    candidate = VideoCandidate(url=str(fpath), source="yt_dlp")
    info = _probe_video_metadata(str(fpath))
    candidate.width = _to_int(info.get("width"))
    candidate.height = _to_int(info.get("height"))
    candidate.codec = str(info.get("codec") or "")
    candidate.bitrate = _to_int(info.get("bitrate"))
    return {
        "title": fpath.stem.replace("ytdlp_", "") or "douyin_video",
        "description": "",
        "file_path": str(fpath),
        "file_size": fpath.stat().st_size,
        "platform": "douyin",
        "platform_cookie": _platform_cookie_info("douyin"),
        "platform_cookie_configured": _platform_cookie_info("douyin")["configured"],
        "quality_source": "yt_dlp",
        "candidate_count": 1,
        "candidate_diagnostics": _candidate_diagnostics([candidate], candidate),
        **_result_quality_info(candidate, str(fpath)),
    }, ""


def download_douyin(url: str, output_dir: str) -> dict:
    """下载抖音视频：可配置原画接口优先；否则自建解析 + yt-dlp + 原画接口兜底。"""
    tikhub_error = None
    tikhub_reason = ""
    if _tikhub_original_first_enabled():
        try:
            tikhub_result, tikhub_reason = _download_tikhub_video("douyin", url, output_dir)
            if tikhub_result:
                return tikhub_result
        except Exception as e:
            tikhub_error = e

    shortcut_error = None
    shortcut_reason = ""
    if _shortcut_original_first_enabled():
        try:
            shortcut_result, shortcut_reason = _download_shortcut_video(
                "douyin", url, output_dir, require_hd=_require_shortcut_original()
            )
            if shortcut_result:
                return shortcut_result
        except Exception as e:
            shortcut_error = e
        if _require_shortcut_original():
            reasons = []
            if tikhub_error:
                reasons.append(f"TikHub 失败：{str(tikhub_error)[:120]}")
            elif tikhub_reason:
                reasons.append(tikhub_reason)
            if shortcut_error:
                reasons.append(f"第三方原画接口失败：{str(shortcut_error)[:160]}")
            elif shortcut_reason:
                reasons.append(shortcut_reason)
            reason = "；".join(reasons) or "原画供应商未返回可用视频"
            raise RuntimeError(f"原画接口不可用，已取消下载低清版本：{reason}")

    local_result = None
    local_error = None
    try:
        local_result = _download_douyin_builtin(url, output_dir)
        if _result_meets_hd_threshold(local_result):
            return local_result
    except Exception as e:
        local_error = e

    ytdlp_result = None
    ytdlp_reason = ""
    try:
        ytdlp_result, ytdlp_reason = _download_ytdlp_video("douyin", url, output_dir)
        if ytdlp_result and _result_meets_hd_threshold(ytdlp_result):
            _delete_downloaded_result_file(local_result)
            return ytdlp_result
        if ytdlp_result and not local_result:
            local_result = ytdlp_result
    except Exception as e:
        ytdlp_reason = f"yt-dlp 兜底异常：{str(e)[:160]}"

    if _douyin_shortcut_fallback_enabled():
        try:
            shortcut_result, shortcut_reason = _download_shortcut_video("douyin", url, output_dir, require_hd=True)
            if shortcut_result:
                _delete_downloaded_result_file(local_result)
                return shortcut_result
        except Exception as e:
            shortcut_error = e
    else:
        shortcut_reason = "抖音第三方原画接口默认未启用，避免消耗快捷指令额度"

    if local_result:
        fallback_parts = []
        if ytdlp_reason:
            fallback_parts.append(ytdlp_reason)
        if shortcut_error:
            fallback_parts.append(f"第三方原画接口尝试失败：{str(shortcut_error)[:120]}")
        elif shortcut_reason:
            fallback_parts.append(shortcut_reason)
        reason = "；".join(fallback_parts)
        local_result["shortcut_fallback_reason"] = _low_quality_fallback_reason(local_result, reason)
        return local_result

    errors = [f"自建解析失败：{local_error}"]
    if ytdlp_reason:
        errors.append(ytdlp_reason)
    if shortcut_error:
        errors.append(f"第三方原画接口失败：{shortcut_error}")
    elif shortcut_reason:
        errors.append(shortcut_reason)
    raise RuntimeError("；".join(errors))


def _download_douyin_builtin(url: str, output_dir: str, shortcut_fallback_reason: str = "") -> dict:
    """使用内置解析下载抖音视频"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _launch_chromium(p)
        extra_headers = {}
        if cookie := _cookie_header("DOUYIN_COOKIE"):
            extra_headers["Cookie"] = cookie
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            extra_http_headers=extra_headers or None,
        )
        page = ctx.new_page()
        try:
            captured_candidates = _capture_mobile_media_candidates(page, url)
            time.sleep(1)

            description = ""

            # 提取视频地址
            video_data = page.evaluate("""() => {
                try {
                    const candidates = [];
                    function firstUrl(addr) {
                        if (!addr) return [];
                        if (Array.isArray(addr.url_list)) return addr.url_list.filter(Boolean);
                        if (addr.url) return [addr.url];
                        if (addr.uri && /^https?:/.test(addr.uri)) return [addr.uri];
                        return [];
                    }
                    function pushAddr(addr, source, meta) {
                        if (!addr) return;
                        meta = meta || {};
                        const urls = firstUrl(addr);
                        for (const url of urls) {
                            candidates.push({
                                url,
                                source,
                                width: meta.width || addr.width || 0,
                                height: meta.height || addr.height || 0,
                                bitrate: meta.bitrate || addr.bit_rate || addr.bitrate || 0,
                                codec: meta.codec || addr.codec || ''
                            });
                        }
                    }
                    function collectFromVideo(v) {
                        if (!v) return;
                        const base = { width: v.width || 0, height: v.height || 0 };
                        pushAddr(v.download_addr, 'download_addr', base);
                        pushAddr(v.play_addr_bytevc1, 'play_addr_bytevc1', {...base, codec: 'h265'});
                        pushAddr(v.play_addr_265, 'play_addr_265', {...base, codec: 'h265'});
                        pushAddr(v.play_addr_h264, 'play_addr_h264', {...base, codec: 'h264'});
                        pushAddr(v.play_addr, 'play_addr', base);
                        if (Array.isArray(v.bit_rate)) {
                            for (const item of v.bit_rate) {
                                pushAddr(item.play_addr || item.playAddr, 'bit_rate:' + (item.gear_name || item.quality_type || ''), {
                                    width: item.play_addr && item.play_addr.width || base.width,
                                    height: item.play_addr && item.play_addr.height || base.height,
                                    bitrate: item.bit_rate || item.bitrate || 0,
                                    codec: item.is_bytevc1 ? 'h265' : (item.format || '')
                                });
                            }
                        }
                    }
                    if (window._ROUTER_DATA) {
                        var ld = window._ROUTER_DATA.loaderData || {};
                        for (var key in ld) {
                            if (key.indexOf('video') > -1 && ld[key] && ld[key].videoInfoRes) {
                                var item = ld[key].videoInfoRes.item_list[0];
                                if (item && item.video) {
                                    collectFromVideo(item.video);
                                    return {candidates, desc: item.desc || ''};
                                }
                            }
                        }
                    }
                    return {candidates, desc: ''};
                } catch(e) { return null; }
            }""")

            candidates = list(captured_candidates)
            if video_data and video_data.get("candidates"):
                for c in video_data["candidates"]:
                    candidates.append(VideoCandidate(
                        url=_normalize_url(_remove_douyin_watermark(c.get("url", "")), page.url),
                        source=c.get("source", "douyin"),
                        width=_to_int(c.get("width")),
                        height=_to_int(c.get("height")),
                        bitrate=_to_int(c.get("bitrate")),
                        codec=c.get("codec", ""),
                    ))

            web_detail_candidates, web_detail_desc, web_detail_reason = _douyin_web_detail_candidates(page, url)
            candidates.extend(web_detail_candidates)
            if web_detail_desc and len(web_detail_desc) > len(description):
                description = web_detail_desc
            if web_detail_reason:
                logger.info(f"抖音 web detail 未返回可用候选: {web_detail_reason}")

            _append_browser_video_candidates(
                candidates,
                _browser_video_candidate_dicts(page, "douyin"),
                page.url,
                "douyin_page",
            )

            # 备用方案：video 标签
            try:
                el = page.query_selector("video")
                if el:
                    src = el.get_attribute("src")
                    if src and "blob:" not in src:
                        candidates.append(VideoCandidate(_normalize_url(_remove_douyin_watermark(src), page.url), "video_tag"))
            except: pass

            if not candidates:
                raise RuntimeError("无法提取抖音视频地址")

            if video_data and video_data.get("desc") and len(video_data.get("desc", "")) > len(description):
                description = video_data.get("desc", "")[:1000]
            candidates = _dedupe_candidates(candidates)
            ordered = sorted(candidates, key=lambda c: c.score(), reverse=True)
            best = None
            fpath = ""
            download_errors = []
            for candidate in ordered:
                if best and fpath:
                    best_short = _candidate_short_side(best)
                    best_size = os.path.getsize(fpath) if os.path.exists(fpath) else 0
                    claimed_short = _candidate_short_side(candidate)
                    larger_unknown = not claimed_short and candidate.content_length and candidate.content_length > best_size * 1.08
                    if claimed_short <= best_short and not larger_unknown:
                        continue

                video_src = candidate.url
                claimed_short = _candidate_short_side(candidate)
                fhash = hashlib.md5(video_src.encode()).hexdigest()[:12]
                target_path = os.path.join(output_dir, f"{fhash}.mp4")
                cookie_header = _playwright_cookie_header(ctx, video_src) or _cookie_header("DOUYIN_COOKIE")
                if cookie_header:
                    candidate.headers["Cookie"] = cookie_header
                try:
                    _download_candidate_file(candidate, target_path, page.url)
                    _apply_actual_file_metadata(candidate, target_path)
                    actual_short = _candidate_short_side(candidate)
                    if not best or candidate.score() > best.score():
                        if fpath and fpath != target_path:
                            try:
                                os.remove(fpath)
                            except Exception:
                                pass
                        best = candidate
                        fpath = target_path
                    else:
                        try:
                            os.remove(target_path)
                        except Exception:
                            pass
                    if claimed_short and actual_short >= claimed_short:
                        break
                except Exception as e:
                    download_errors.append(f"{candidate.source}: {str(e)[:120]}")
                    try:
                        if os.path.exists(target_path):
                            os.remove(target_path)
                    except Exception:
                        pass

            if not best or not fpath:
                raise RuntimeError("抖音候选视频流均下载失败：" + "；".join(download_errors[:5]))
            fsize = os.path.getsize(fpath)

            # 获取标题
            title = "douyin_video"
            try:
                t = page.query_selector("title")
                if t: title = t.inner_text().strip()[:200]
            except: pass

            # ASR 语音识别（跳过，由用户手动触发）
            # if fsize > 1024:
            #     spoken = _transcribe_with_stepaudio(fpath)
            #     if spoken:
            #         description = spoken

            return {
                "title": title,
                "description": description,
                "file_path": fpath,
                "file_size": fsize,
                "platform": "douyin",
                "platform_cookie": _platform_cookie_info("douyin"),
                "platform_cookie_configured": _platform_cookie_info("douyin")["configured"],
                "shortcut_fallback_reason": shortcut_fallback_reason,
                "candidate_count": len(candidates),
                "candidate_diagnostics": _candidate_diagnostics(candidates, best),
                **_result_quality_info(best, fpath),
            }

        finally:
            ctx.close()
            browser.close()


def download_xhs(url: str, output_dir: str) -> dict:
    """下载小红书视频：可配置原画接口优先；否则自建解析低清时再用原画接口兜底。"""
    tikhub_error = None
    tikhub_reason = ""
    if _tikhub_original_first_enabled():
        try:
            tikhub_result, tikhub_reason = _download_tikhub_video("xhs", url, output_dir)
            if tikhub_result:
                return tikhub_result
        except Exception as e:
            tikhub_error = e

    shortcut_error = None
    shortcut_reason = ""
    if _shortcut_original_first_enabled():
        try:
            shortcut_result, shortcut_reason = _download_shortcut_video(
                "xhs", url, output_dir, require_hd=_require_shortcut_original()
            )
            if shortcut_result:
                return shortcut_result
        except Exception as e:
            shortcut_error = e
        if _require_shortcut_original():
            reasons = []
            if tikhub_error:
                reasons.append(f"TikHub 失败：{str(tikhub_error)[:120]}")
            elif tikhub_reason:
                reasons.append(tikhub_reason)
            if shortcut_error:
                reasons.append(f"第三方原画接口失败：{str(shortcut_error)[:160]}")
            elif shortcut_reason:
                reasons.append(shortcut_reason)
            reason = "；".join(reasons) or "原画供应商未返回可用视频"
            raise RuntimeError(f"原画接口不可用，已取消下载低清版本：{reason}")

    local_result = None
    local_error = None
    try:
        local_result = _download_xhs_builtin(url, output_dir)
        if _result_meets_hd_threshold(local_result):
            return local_result
    except Exception as e:
        local_error = e

    try:
        shortcut_result, shortcut_reason = _download_shortcut_video("xhs", url, output_dir, require_hd=True)
        if shortcut_result:
            if local_result:
                shortcut_result["shortcut_fallback_reason"] = _low_quality_fallback_reason(
                    local_result,
                    "已使用第三方原画接口取得更高画质",
                )
                shortcut_rows = list(shortcut_result.get("candidate_diagnostics") or [])
                local_rows = []
                for row in local_result.get("candidate_diagnostics") or []:
                    copied = dict(row)
                    copied["selected"] = False
                    copied["source"] = f"自建解析:{copied.get('source', '')}"
                    local_rows.append(copied)
                shortcut_result["candidate_diagnostics"] = shortcut_rows + local_rows
                shortcut_result["candidate_count"] = len(shortcut_rows) + len(local_rows)
            _delete_downloaded_result_file(local_result)
            return shortcut_result
    except Exception as e:
        shortcut_result, shortcut_reason = None, ""
        shortcut_error = e

    if local_result:
        reason = shortcut_reason or (f"第三方原画接口尝试失败：{str(shortcut_error)[:120]}" if shortcut_error else "")
        local_result["shortcut_fallback_reason"] = _low_quality_fallback_reason(local_result, reason)
        return local_result

    errors = [f"自建解析失败：{local_error}"]
    if shortcut_error:
        errors.append(f"第三方原画接口失败：{shortcut_error}")
    elif shortcut_reason:
        errors.append(shortcut_reason)
    raise RuntimeError("；".join(errors))


def _download_xhs_builtin(url: str, output_dir: str, shortcut_fallback_reason: str = "") -> dict:
    """使用内置解析下载小红书视频"""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _launch_chromium(p)
        extra_headers = {}
        if cookie := _cookie_header("XHS_COOKIE"):
            extra_headers["Cookie"] = cookie
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            extra_http_headers=extra_headers or None,
        )
        page = ctx.new_page()
        try:
            captured_candidates = _capture_mobile_media_candidates(page, url)
            page_src = page.content()

            description = ""
            video_title = "xhs_video"

            # 提取视频地址
            candidates = list(captured_candidates)
            _append_xhs_origin_key_candidates(candidates, page_src)
            for m in re.finditer(r'"(?:master_url|masterUrl)"\s*:\s*"([^"]+)"', page_src):
                raw_url = m.group(1).replace("\\u002F", "/").replace("\\/", "/")
                window = page_src[max(0, m.start() - 900):m.end() + 900]
                codec = "h265" if "h265" in window.lower() or "265" in raw_url.lower() else "h264" if "h264" in window.lower() else ""
                width = _to_int((re.search(r'"width"\s*:\s*(\d+)', window) or [None, 0])[1])
                height = _to_int((re.search(r'"height"\s*:\s*(\d+)', window) or [None, 0])[1])
                bitrate = _to_int((re.search(r'"(?:video_bitrate|bitrate|avg_bitrate)"\s*:\s*(\d+)', window) or [None, 0])[1])
                source = "master_url"
                candidates.append(VideoCandidate(raw_url, source, width, height, bitrate, codec))

            _append_browser_video_candidates(
                candidates,
                _browser_video_candidate_dicts(page, "xhs"),
                page.url,
                "xhs_page",
            )

            if not candidates:
                src = page.evaluate("() => { var v=document.querySelector('video'); return v?v.src:null; }")
                if src and "blob:" not in src:
                    candidates.append(VideoCandidate(src, "video_tag"))

            if not candidates:
                raise RuntimeError("无法提取小红书视频地址")

            # 提取描述
            desc_patterns = [
                r'"desc"\s*:\s*"((?:\\.|[^"\\]){5,4000})"',
                r'"content"\s*:\s*"((?:\\.|[^"\\]){5,4000})"',
            ]
            for pattern in desc_patterns:
                for m in re.finditer(pattern, page_src):
                    d = m.group(1).replace("\\n", "\n").replace("\\u002F", "/").replace("\\/", "/")
                    if len(d) > len(description):
                        description = d

            title_m = re.search(r'"title"\s*:\s*"([^"]{2,200})"', page_src)
            if title_m: video_title = title_m.group(1).replace("\\u002F", "/")

            normalized = []
            for c in candidates:
                c.url = _normalize_url(c.url.replace("\\u002F", "/").replace("http://", "https://"), page.url)
                normalized.append(c)
            ordered = _ordered_xhs_candidates(normalized)
            best = None
            fpath = ""
            download_errors = []
            for candidate in ordered:
                if best and fpath:
                    best_short = _candidate_short_side(best)
                    best_size = os.path.getsize(fpath) if os.path.exists(fpath) else 0
                    claimed_short = _candidate_short_side(candidate)
                    larger_unknown = not claimed_short and candidate.content_length and candidate.content_length > best_size * 1.08
                    if claimed_short <= best_short and not larger_unknown:
                        continue

                video_src = candidate.url
                claimed_short = _candidate_short_side(candidate)
                fhash = hashlib.md5(video_src.encode()).hexdigest()[:12]
                target_path = os.path.join(output_dir, f"{fhash}.mp4")
                cookie_header = _playwright_cookie_header(ctx, video_src) or _cookie_header("XHS_COOKIE")
                if cookie_header:
                    candidate.headers["Cookie"] = cookie_header
                try:
                    _download_candidate_file(candidate, target_path, page.url)
                    _apply_actual_file_metadata(candidate, target_path)
                    actual_short = _candidate_short_side(candidate)
                    if not best or candidate.score() > best.score():
                        if fpath and fpath != target_path:
                            try:
                                os.remove(fpath)
                            except Exception:
                                pass
                        best = candidate
                        fpath = target_path
                    else:
                        try:
                            os.remove(target_path)
                        except Exception:
                            pass
                    if claimed_short and actual_short >= claimed_short:
                        break
                except Exception as e:
                    download_errors.append(f"{candidate.source}: {str(e)[:120]}")
                    try:
                        if os.path.exists(target_path):
                            os.remove(target_path)
                    except Exception:
                        pass

            if not best or not fpath:
                raise RuntimeError("小红书候选视频流均下载失败：" + "；".join(download_errors[:4]))
            fsize = os.path.getsize(fpath)

            return {
                "title": video_title,
                "description": description,
                "file_path": fpath,
                "file_size": fsize,
                "platform": "xhs",
                "platform_cookie": _platform_cookie_info("xhs"),
                "platform_cookie_configured": _platform_cookie_info("xhs")["configured"],
                "shortcut_fallback_reason": shortcut_fallback_reason,
                "candidate_count": len(normalized),
                "candidate_diagnostics": _candidate_diagnostics(normalized, best),
                **_result_quality_info(best, fpath),
            }

        finally:
            ctx.close()
            browser.close()


def download_bilibili(url: str, output_dir: str) -> dict:
    """下载 B站视频"""
    bvid = None
    for p in _BILI_PATTERNS:
        m = re.search(p, url)
        if m:
            bvid = m.group(1)
            break
    if not bvid:
        raise RuntimeError("无法解析B站视频ID")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    if cookie := _cookie_header("BILIBILI_COOKIE"):
        headers["Cookie"] = cookie

    resp = requests.get(f"https://www.bilibili.com/video/{bvid}", headers=headers, timeout=15)
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*\(function', resp.text)
    if not m:
        raise RuntimeError("无法解析B站页面数据")

    state = __import__('json').loads(m.group(1))
    vd = state.get("videoData", {})
    cid = state.get("cid") or (vd.get("pages", [{}])[0].get("cid"))
    title = vd.get("title", "bilibili_video")
    description = vd.get("desc", "") or ""

    api_url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=127&fnval=4048&fourk=1"
    resp = requests.get(api_url, headers=headers, timeout=15)
    play = resp.json()
    if play.get("code") != 0:
        raise RuntimeError(play.get("message", "B站API错误"))

    dash = play.get("data", {}).get("dash", {})
    videos = sorted(
        dash.get("video", []),
        key=lambda x: (
            _to_int(x.get("height")),
            _to_int(x.get("width")),
            _to_int(x.get("bandwidth")),
            _to_int(x.get("id")),
        ),
        reverse=True,
    )
    audios = sorted(dash.get("audio", []), key=lambda x: x.get("bandwidth", 0), reverse=True)
    best_v = videos[0] if videos else None
    best_a = audios[0] if audios else None

    if not best_v:
        raise RuntimeError("B站未返回视频流")

    best_candidate = VideoCandidate(
        url=best_v.get("baseUrl") or best_v.get("base_url"),
        source=f"dash:{best_v.get('id', '')}",
        width=_to_int(best_v.get("width")),
        height=_to_int(best_v.get("height")),
        bitrate=_to_int(best_v.get("bandwidth")),
        codec=best_v.get("codecs", ""),
    )

    fhash = hashlib.md5(bvid.encode()).hexdigest()[:12]
    fpath = os.path.join(output_dir, f"{fhash}.mp4")

    if best_a:
        vpath = fpath + ".v"
        apath = fpath + ".a"
        _download_file_raw(best_candidate.url, vpath, headers)
        _download_file_raw(best_a.get("baseUrl") or best_a.get("base_url"), apath, headers)
        subprocess.run(["ffmpeg", "-i", vpath, "-i", apath, "-c", "copy", "-y", fpath],
                       capture_output=True, timeout=120, check=True)
        os.remove(vpath)
        os.remove(apath)
    else:
        _download_file_raw(best_candidate.url, fpath, headers)

    fsize = os.path.getsize(fpath)

    return {
        "title": title[:200],
        "description": description,
        "file_path": fpath,
        "file_size": fsize,
        "platform": "bilibili",
        **_result_quality_info(best_candidate, fpath),
    }


def _download_file_raw(url: str, dest_path: str, headers: dict):
    """下载原始文件"""
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        **headers,
    }
    resp = requests.get(url, headers=hdrs, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
            if chunk:
                f.write(chunk)


def download_video(url: str, output_dir: str, mode: str = "auto") -> dict:
    """统一下载接口"""
    platform = get_platform(url)
    if not platform:
        raise RuntimeError("不支持的链接，请提供抖音、小红书或B站链接")

    mode = (mode or "auto").strip().lower()
    if mode == "original":
        return _download_original_provider_video(platform, url, output_dir)
    if mode == "builtin":
        return _download_builtin_video(platform, url, output_dir)
    if mode == "tikhub":
        return _download_tikhub_forced_video(platform, url, output_dir)

    if platform == "douyin":
        return _mark_download_mode(download_douyin(url, output_dir), "auto")
    elif platform == "xhs":
        return _mark_download_mode(download_xhs(url, output_dir), "auto")
    elif platform == "bilibili":
        return _mark_download_mode(download_bilibili(url, output_dir), "auto")
    else:
        raise RuntimeError(f"不支持的平台: {platform}")
