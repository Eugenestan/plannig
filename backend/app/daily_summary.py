from __future__ import annotations

import argparse
import base64
import os
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .jira_client import Jira, load_env_file
from .models import ApiCredential, Team, TeamTelegramSetting
from .telegram_notifier import send_message
from .worklog_fetcher import get_team_worklog

MSK_TZ = ZoneInfo("Europe/Moscow")


@dataclass(slots=True)
class TeamSummaryResult:
    team_id: int
    team_name: str
    chat_id_masked: str
    sent: bool
    reason: str
    duration_ms: int


def _mask_chat_id(chat_id: str) -> str:
    chat = (chat_id or "").strip()
    if len(chat) <= 4:
        return "****"
    return f"{chat[:2]}***{chat[-2:]}"


def _build_jira_client_from_credential(credential: ApiCredential) -> tuple[Jira, str]:
    load_env_file(settings.jira_secrets_file_abs)
    base_url = (os.getenv("JIRA_BASE_URL") or "").strip()
    if not base_url:
        raise RuntimeError("JIRA_BASE_URL не настроен в конфигурации")

    api_key = (credential.jira_api_key or "").strip()
    if not api_key:
        raise RuntimeError("Пустой Jira API key у credential")

    headers = {"Accept": "application/json"}
    email = (credential.jira_email or "").strip()
    if email:
        raw = f"{email}:{api_key}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    jira = Jira(base_url, headers)
    api_prefix = jira.detect_api_prefix()
    return jira, api_prefix


def _build_summary_text(team_name: str, rows: list[dict]) -> str:
    if not rows:
        return f"{team_name}\nСегодня списаний нет."

    lines = [team_name]
    for row in rows:
        user_name = (row.get("user_name") or "Неизвестный сотрудник").strip()
        hours = float(row.get("total_hours") or 0.0)
        lines.append(f"{user_name} - {hours:.1f} ч")
    return "\n".join(lines)


def _is_weekday_msk(now: datetime) -> bool:
    return now.astimezone(MSK_TZ).weekday() < 5


def run_daily_summary(*, dry_run: bool = False, force: bool = False, team_id: int | None = None) -> list[TeamSummaryResult]:
    now_msk = datetime.now(MSK_TZ)
    if not force and not _is_weekday_msk(now_msk):
        print("Skip: weekend in MSK")
        return []

    db = SessionLocal()
    try:
        query = (
            select(TeamTelegramSetting, Team, ApiCredential)
            .join(Team, Team.id == TeamTelegramSetting.team_id)
            .join(ApiCredential, ApiCredential.id == TeamTelegramSetting.credential_id)
            .where(TeamTelegramSetting.enabled.is_(True))
            .order_by(Team.name.asc())
        )
        if team_id is not None:
            query = query.where(TeamTelegramSetting.team_id == team_id)

        targets = db.execute(query).all()
        results: list[TeamSummaryResult] = []
        jira_cache: dict[int, tuple[Jira, str]] = {}

        for setting, team, credential in targets:
            started = perf_counter()
            masked = _mask_chat_id(setting.chat_id)
            try:
                jira_and_prefix = jira_cache.get(credential.id)
                if jira_and_prefix is None:
                    jira_and_prefix = _build_jira_client_from_credential(credential)
                    jira_cache[credential.id] = jira_and_prefix
                jira, api_prefix = jira_and_prefix

                rows = get_team_worklog(
                    db,
                    team.id,
                    days="today",
                    jira=jira,
                    api_prefix=api_prefix,
                    credential_id=credential.id,
                )
                text = _build_summary_text(team.name, rows)

                if dry_run:
                    print(f"[DRY-RUN] team_id={team.id} chat_id={masked}\n{text}\n")
                    sent = True
                    reason = "dry-run"
                else:
                    send_message(setting.chat_id, text)
                    sent = True
                    reason = "sent"
            except Exception as exc:  # noqa: BLE001
                sent = False
                reason = f"error: {exc}"

            elapsed_ms = int((perf_counter() - started) * 1000)
            results.append(
                TeamSummaryResult(
                    team_id=team.id,
                    team_name=team.name,
                    chat_id_masked=masked,
                    sent=sent,
                    reason=reason,
                    duration_ms=elapsed_ms,
                )
            )
            print(
                f"team_id={team.id} team={team.name} chat_id={masked} "
                f"status={'ok' if sent else 'fail'} reason={reason} duration_ms={elapsed_ms}"
            )

        return results
    finally:
        db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daily Telegram summary sender")
    parser.add_argument("--dry-run", action="store_true", help="Сформировать сводку без отправки")
    parser.add_argument("--force", action="store_true", help="Игнорировать проверку выходного дня")
    parser.add_argument("--team-id", type=int, default=None, help="Отправить только для одной команды")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    results = run_daily_summary(dry_run=args.dry_run, force=args.force, team_id=args.team_id)
    ok = sum(1 for r in results if r.sent)
    fail = sum(1 for r in results if not r.sent)
    print(f"Summary: ok={ok}, fail={fail}, total={len(results)}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
