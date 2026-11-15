from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Pondok Berkah Bersama API"
    app_env: str = "local"
    app_timezone: str = "Asia/Jakarta"
    database_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/pondok_berkah"
    api_key: str = ""
    api_previous_key: str = ""
    jwt_secret: str = ""
    jwt_issuer: str = "farm-management-api"
    jwt_audience: str = "farm-management-frontend"
    jwt_ttl_hours: int = Field(default=8, ge=1, le=24)
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    google_cloud_project_id: str = ""
    google_cloud_storage_bucket: str = ""
    google_cloud_storage_prefix: str = ""
    google_application_credentials: str = ""
    log_level: str = "INFO"

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    def validate_runtime_secrets(self) -> None:
        if not self.api_key or len(self.api_key) < 24:
            raise RuntimeError("API_KEY must contain at least 24 characters")
        if not self.jwt_secret or len(self.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must contain at least 32 characters")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
