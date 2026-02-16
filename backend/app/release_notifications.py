from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .daily_summary import _build_jira_client_from_credential, _mask_chat_id
from .db import SessionLocal
from .models import ApiCredential, Team, TeamTelegramSetting
from .release_fetcher import get_releases_for_current_user
from .telegram_notifier import send_message

MSK_TZ = ZoneInfo("Europe/Moscow")


@dataclass(slots=True)
class ReleaseNotificationResult:
    team_id: int
    team_name: str
    chat_id_masked: str
    sent: bool
    reason: str
    duration_ms: int


def _is_weekday_msk(now: datetime) -> bool:
    return now.astimezone(MSK_TZ).weekday() < 5


def _build_release_text(releases: list[dict]) -> str:
    lines = ["Релизы на сегодня и просроченные"]
    if not releases:
        lines.append("На сегодня релизов нет.")
        return "\n".join(lines)

    for item in releases:
        title = (item.get("epic_summary") or item.get("version_name") or item.get("epic_key") or "Без названия").strip()
        release_date = (item.get("release_date") or "").strip()
        lines.append(f"{title} - {release_date}")
    return "\n".join(lines)


def run_release_notifications(
    *,
    dry_run: bool = False,
    force: bool = False,
    team_id: int | None = None,
) -> list[ReleaseNotificationResult]:
    now_msk = datetime.now(MSK_TZ)
    if not force and not _is_weekday_msk(now_msk):
        print("Skip: weekend in MSK")
        return []

    today = now_msk.date()
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
        results: list[ReleaseNotificationResult] = []
        grouped_by_chat: dict[str, list[tuple[TeamTelegramSetting, Team, ApiCredential]]] = {}
        for setting, team, credential in targets:
            grouped_by_chat.setdefault(setting.chat_id, []).append((setting, team, credential))

        jira_cache: dict[int, tuple] = {}

        for chat_id, grouped_targets in grouped_by_chat.items():
            started = perf_counter()
            masked = _mask_chat_id(chat_id)

            try:
                unique_credential_ids: set[int] = set()
                merged_releases: list[dict] = []
                for _setting, _team, credential in grouped_targets:
                    if credential.id in unique_credential_ids:
                        continue
                    unique_credential_ids.add(credential.id)

                    jira_and_prefix = jira_cache.get(credential.id)
                    if jira_and_prefix is None:
                        jira_and_prefix = _build_jira_client_from_credential(credential)
                        jira_cache[credential.id] = jira_and_prefix
                    jira, _api_prefix = jira_and_prefix

                    releases = get_releases_for_current_user(
                        jira,
                        due_on_or_before=today,
                        only_unreleased=True,
                        only_current_user_assignee=False,
                    )
                    merged_releases.extend(releases)

                deduped_by_key: dict[tuple[str, str, str], dict] = {}
                for item in merged_releases:
                    dedup_key = (
                        (item.get("epic_key") or "").strip(),
                        (item.get("version_name") or "").strip(),
                        (item.get("release_date") or "").strip(),
                    )
                    deduped_by_key[dedup_key] = item
                deduped_releases = sorted(
                    deduped_by_key.values(),
                    key=lambda item: item["release_date_obj"],
                )
                text = _build_release_text(deduped_releases)

                if dry_run:
                    print(f"[DRY-RUN] chat_id={masked}\n{text}\n")
                    sent = True
                    reason = "dry-run"
                else:
                    send_message(chat_id, text)
                    sent = True
                    reason = "sent"
            except Exception as exc:  # noqa: BLE001
                sent = False
                reason = f"error: {exc}"

            elapsed_ms = int((perf_counter() - started) * 1000)
            results.append(
                ReleaseNotificationResult(
                    team_id=0,
                    team_name="combined",
                    chat_id_masked=masked,
                    sent=sent,
                    reason=reason,
                    duration_ms=elapsed_ms,
                )
            )
            print(
                f"scope=combined chat_id={masked} "
                f"status={'ok' if sent else 'fail'} reason={reason} duration_ms={elapsed_ms}"
            )

        return results
    finally:
        db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram release notifications sender")
    parser.add_argument("--dry-run", action="store_true", help="Сформировать сообщение без отправки")
    parser.add_argument("--force", action="store_true", help="Игнорировать проверку выходного дня")
    parser.add_argument("--team-id", type=int, default=None, help="Отправить только для одной команды")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    results = run_release_notifications(dry_run=args.dry_run, force=args.force, team_id=args.team_id)
    ok = sum(1 for r in results if r.sent)
    fail = sum(1 for r in results if not r.sent)
    print(f"Summary: ok={ok}, fail={fail}, total={len(results)}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
