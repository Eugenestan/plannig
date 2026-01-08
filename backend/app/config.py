from pydantic_settings import BaseSettings, SettingsConfigDict
import os
from pathlib import Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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


