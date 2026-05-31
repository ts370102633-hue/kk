from pathlib import Path
import tempfile
from .config import get_settings

settings = get_settings()
_storage_dir = Path(settings.local_storage_dir)
_storage_dir.mkdir(parents=True, exist_ok=True)


def upload_fileobj(object_key: str, fileobj, length: int = 0, content_type: str = "application/octet-stream") -> str:
    dest = _storage_dir / object_key
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(fileobj.read())
    return object_key


def upload_path(object_key: str, src: Path, content_type: str = "application/octet-stream") -> str:
    dest = _storage_dir / object_key
    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(str(src), str(dest))
    return object_key


def public_url(object_key: str) -> str | None:
    if not object_key:
        return None
    return f"/files/{object_key}"


def download_to_temp(object_key: str, suffix: str = ".wav") -> Path:
    src = _storage_dir / object_key
    tmp = Path(tempfile.mktemp(suffix=suffix))
    import shutil
    shutil.copy2(str(src), str(tmp))
    return tmp


def ensure_bucket():
    _storage_dir.mkdir(parents=True, exist_ok=True)
