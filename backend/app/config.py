from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    database_url: str = "sqlite:///./data/stepaudio.db"
    local_storage_dir: str = "./data"

    openvoice_output_dir: str = "/tmp/openvoice_outputs"
    max_upload_mb: int = 500

    step_api_key: str = ""
    step_api_base: str = "https://api.stepfun.com/step_plan/v1"
    step_file_api_base: str = "https://api.stepfun.com/v1"
    step_asr_model: str = "stepaudio-2.5-asr"
    step_tts_model: str = "stepaudio-2.5-tts"

    admin_username: str = "admin"
    admin_password: str = "admin123"

    default_user_email: str = "demo@company.local"
    default_user_name: str = "Demo User"

    tikhub_enabled: bool = False
    tikhub_original_first_enabled: bool = False
    tikhub_api_base: str = "https://api.tikhub.dev"
    tikhub_api_key: str = ""
    tikhub_douyin_region: str = "CN"
    tikhub_timeout_seconds: int = 45


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[arg-type]
