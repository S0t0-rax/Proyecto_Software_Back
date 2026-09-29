"""
app/core/config.py
──────────────────
Configuración centralizada usando pydantic-settings.
Lee automáticamente desde el archivo .env.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Base de Datos ──────────────────────────────────────────────────────────
    DATABASE_URL: str

    # ── Supabase ───────────────────────────────────────────────────────────────
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # ── Aplicación ─────────────────────────────────────────────────────────────
    APP_ENV: str = "development"
    SECRET_KEY: str = "dev-secret-key"
    ALLOWED_ORIGINS: list[str] = ["http://localhost:4200"]

    # ── WebSockets / Bloqueo ───────────────────────────────────────────────────
    WS_HEARTBEAT_INTERVAL: int = 30
    LOCK_EXPIRATION_SECONDS: int = 60


# Instancia singleton importable desde cualquier módulo
settings = Settings()
