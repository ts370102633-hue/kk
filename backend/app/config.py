from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    database_url: str = "sqlite:///./openvoice.db"
    local_storage_dir: str = "./data"

    openvoice_output_dir: str = "/tmp/openvoice_outputs"
    max_upload_mb: int = 500

    step_api_key: str = ""
    step_api_base: str = "https://api.stepfun.com/step_plan/v1"
    step_tts_model: str = "stepaudio-2.5-tts"

    default_user_email: str = "demo@company.local"
    default_user_name: str = "Demo User"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[arg-type]
