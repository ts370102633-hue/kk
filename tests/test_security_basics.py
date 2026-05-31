import hashlib

from backend.app.main import hash_password, verify_password, _cors_origins
from backend.app.config import get_settings


def test_password_hash_uses_salted_pbkdf2():
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first.startswith("pbkdf2_sha256$")
    assert second.startswith("pbkdf2_sha256$")
    assert first != second
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("wrong password", first)


def test_legacy_sha256_passwords_still_verify():
    legacy = hashlib.sha256("legacy-password".encode()).hexdigest()

    assert verify_password("legacy-password", legacy)
    assert not verify_password("wrong-password", legacy)


def test_production_cors_defaults_to_same_origin_only():
    settings = get_settings()
    original_env = settings.app_env
    original_cors = settings.cors_origins
    try:
        settings.app_env = "production"
        settings.cors_origins = ""
        assert _cors_origins() == []

        settings.cors_origins = "https://voice.example.com, https://admin.example.com"
        assert _cors_origins() == ["https://voice.example.com", "https://admin.example.com"]
    finally:
        settings.app_env = original_env
        settings.cors_origins = original_cors
