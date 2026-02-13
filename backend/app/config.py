from pydantic_settings import BaseSettings, SettingsConfigDict
import os
from pathlib import Path


class Settings(BaseSettings):
    # В этом проекте .env может быть заблокирован глобальными ignore-настройками,
    # а сервер иногда запускают не из папки backend/. Поэтому указываем env_file
    # абсолютными путями относительно backend/ — так config.env гарантированно подхватится.
    _backend_dir = Path(__file__).resolve().parent.parent
    model_config = SettingsConfigDict(
        env_file=(str(_backend_dir / ".env"), str(_backend_dir / "config.env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # SQLite по умолчанию (для быстрого старта без Docker/MySQL)
    # Чтобы использовать MySQL, задайте USE_MYSQL=true в .env
    use_mysql: bool = False
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "planing"
    mysql_password: str = "planing"
    mysql_db: str = "planing"
    sqlite_path: str = "planing.db"

    jira_secrets_file: str = "../jira_secrets.env"
    session_secret_key: str = "change-this-secret-key-in-production"

    # DevSamurai Timesheet Builder (TimePlanner) — для учета logtimeType=custom_task/Event и т.п.
    # ВАЖНО: JWT короткоживущий. Лучше хранить и обновлять через .env при необходимости.
    devsamurai_timesheet_base_url: str = "https://www.timesheet.atlas.devsamurai.com"
    devsamurai_timesheet_jwt: str = ""

    # Teamboard Public API (TimePlanner timelogs)
    # Документация: `https://api-docs.teamboard.cloud/v1/`
    # ВАЖНО: здесь нужен именно Bearer JWT токен (не UUID "API key").
    teamboard_base_url: str = "https://api.teamboard.cloud/v1"
    teamboard_bearer_jwt: str = ""

    # Telegram bot notifications
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_connect_timeout_seconds: float = 10.0
    telegram_read_timeout_seconds: float = 20.0

    # Slack notifications (Incoming Webhook)
    slack_enabled: bool = False
    slack_webhook_url: str = ""
    slack_channel: str = ""
    slack_connect_timeout_seconds: float = 10.0
    slack_read_timeout_seconds: float = 20.0

    @property
    def jira_secrets_file_abs(self) -> str:
        """Абсолютный путь к jira_secrets.env (относительно backend/)."""
        backend_dir = Path(__file__).resolve().parent.parent
        rel_path = Path(self.jira_secrets_file)
        if rel_path.is_absolute():
            return str(rel_path)
        return str((backend_dir / rel_path).resolve())

    @property
    def sqlalchemy_database_uri(self) -> str:
        if self.use_mysql:
            # mysql+pymysql://user:pass@host:port/db?charset=utf8mb4
            return (
                f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
                f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}"
                f"?charset=utf8mb4"
            )
        else:
            # SQLite (не требует установки MySQL/Docker)
            return f"sqlite:///{self.sqlite_path}"


settings = Settings()


