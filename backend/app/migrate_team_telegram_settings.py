"""
Миграция: создание таблицы team_telegram_settings.

Запуск:
  python -m app.migrate_team_telegram_settings
"""

from __future__ import annotations

from sqlalchemy import text

from .db import engine
from .models import TeamTelegramSetting


def run() -> None:
    # Создаем таблицу, если ее еще нет.
    TeamTelegramSetting.__table__.create(bind=engine, checkfirst=True)

    # Для старых БД подстрахуемся индексом по credential_id.
    with engine.begin() as con:
        dialect = con.dialect.name
        if dialect == "sqlite":
            con.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_team_telegram_settings_credential_id "
                    "ON team_telegram_settings (credential_id)"
                )
            )
        elif dialect == "mysql":
            # MySQL не поддерживает IF NOT EXISTS для CREATE INDEX во всех версиях.
            existing = con.execute(
                text(
                    "SELECT COUNT(1) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = 'team_telegram_settings' "
                    "AND index_name = 'ix_team_telegram_settings_credential_id'"
                )
            ).scalar_one()
            if not existing:
                con.execute(
                    text(
                        "CREATE INDEX ix_team_telegram_settings_credential_id "
                        "ON team_telegram_settings (credential_id)"
                    )
                )


if __name__ == "__main__":
    run()
    print("OK: team_telegram_settings migration finished")
