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
from .slack_notifier import send_slack_message
from .telegram_notifier import send_message
from .worklog_fetcher import get_team_worklog

MSK_TZ = ZoneInfo("Europe/Moscow")
GLOBAL_SUMMARY_TEAM_ORDER = [3, 1, 2, 4]
GLOBAL_SUMMARY_TEAM_IDS = set(GLOBAL_SUMMARY_TEAM_ORDER)


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
        return f"{team_name}\nЗа вчера списаний нет."

    lines = [team_name]
    user_index = 1
    for row in rows:
        entries = row.get("entries") or []

        user_name = (row.get("user_name") or "Неизвестный сотрудник").strip()
        total_hours = float(row.get("total_hours") or 0.0)
        lines.append(f"{user_index}. {user_name} - {total_hours:.1f} ч")
        user_index += 1

        # Детализированный список задач/ивентов временно отключен.
        # Если потребуется вернуть:
        # 1) сортируем entries по time_spent_seconds,
        # 2) выводим строки "* ключ + название + время" (или event + время).

    if user_index == 1:
        lines.append("За вчера списаний нет.")
    return "\n".join(lines)


def _build_combined_summary_text(team_sections: list[tuple[str, list[dict]]]) -> str:
    blocks: list[str] = []
    for team_name, rows in team_sections:
        blocks.append(_build_summary_text(team_name, rows))
    return "\n\n".join(blocks)


def _is_weekday_msk(now: datetime) -> bool:
    return now.astimezone(MSK_TZ).weekday() < 5


def _send_to_enabled_channels(chat_id: str, text: str) -> None:
    sent_any = False
    if settings.telegram_enabled:
        send_message(chat_id, text)
        sent_any = True
    if settings.slack_enabled:
        send_slack_message(text)
        sent_any = True
    if not sent_any:
        raise RuntimeError("No channels enabled: set TELEGRAM_ENABLED=true and/or SLACK_ENABLED=true")


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
        else:
            # Авторассылка отправляет только команды из фиксированного списка.
            query = query.where(TeamTelegramSetting.team_id.in_(GLOBAL_SUMMARY_TEAM_IDS))

        targets = db.execute(query).all()
        results: list[TeamSummaryResult] = []
        jira_cache: dict[int, tuple[Jira, str]] = {}
        processed_team_ids: set[int] = set()

        # Спец-режим: единая сводка по выбранным командам (1 и 3).
        # Группируем по chat_id, чтобы в один чат уходило одно сообщение с несколькими командами.
        if team_id is None:
            grouped_targets: dict[str, list[tuple[TeamTelegramSetting, Team, ApiCredential]]] = {}
            for setting, team, credential in targets:
                if team.id not in GLOBAL_SUMMARY_TEAM_IDS:
                    continue
                grouped_targets.setdefault(setting.chat_id, []).append((setting, team, credential))

            for chat_id, grouped in grouped_targets.items():
                started = perf_counter()
                masked = _mask_chat_id(chat_id)
                try:
                    team_sections: list[tuple[str, list[dict]]] = []
                    order_map = {team_id_value: idx for idx, team_id_value in enumerate(GLOBAL_SUMMARY_TEAM_ORDER)}
                    grouped_sorted = sorted(
                        grouped,
                        key=lambda item: order_map.get(item[1].id, 10_000 + item[1].id),
                    )
                    grouped_team_ids: list[int] = []
                    for setting, team, credential in grouped_sorted:
                        jira_and_prefix = jira_cache.get(credential.id)
                        if jira_and_prefix is None:
                            jira_and_prefix = _build_jira_client_from_credential(credential)
                            jira_cache[credential.id] = jira_and_prefix
                        jira, api_prefix = jira_and_prefix

                        rows = get_team_worklog(
                            db,
                            team.id,
                            days="yesterday",
                            jira=jira,
                            api_prefix=api_prefix,
                            credential_id=credential.id,
                        )
                        team_sections.append((team.name, rows))
                        processed_team_ids.add(team.id)
                        grouped_team_ids.append(team.id)

                    text = _build_combined_summary_text(team_sections)
                    if dry_run:
                        print(f"[DRY-RUN] combined teams={grouped_team_ids} chat_id={masked}\n{text}\n")
                        sent = True
                        reason = "dry-run"
                    else:
                        _send_to_enabled_channels(chat_id, text)
                        sent = True
                        reason = "sent"
                except Exception as exc:  # noqa: BLE001
                    sent = False
                    reason = f"error: {exc}"

                elapsed_ms = int((perf_counter() - started) * 1000)
                results.append(
                    TeamSummaryResult(
                        team_id=0,
                        team_name="combined",
                        chat_id_masked=masked,
                        sent=sent,
                        reason=reason,
                        duration_ms=elapsed_ms,
                    )
                )
                print(
                    f"combined_teams={GLOBAL_SUMMARY_TEAM_ORDER} chat_id={masked} "
                    f"status={'ok' if sent else 'fail'} reason={reason} duration_ms={elapsed_ms}"
                )

        for setting, team, credential in targets:
            if team.id in processed_team_ids:
                continue
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
                    days="yesterday",
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
                    _send_to_enabled_channels(setting.chat_id, text)
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
