"""
Диагностика worklog для одной команды/одного сотрудника.

Запуск на проде:
    cd /opt/planing/backend
    source .venv/bin/activate
    python -m app.worklog_debug --team-id 1 --user "Роженцев" --days previous_workday

Скрипт берёт первый ApiCredential, чьи команды содержат указанный team_id
(или Team по имени, если --team-name задано), и выводит:
  - сводку по сотруднику (часы, количество entries)
  - debug.sources.* (teamboard/devsamurai): http_errors_by_user_id,
    skipped_no_team_member, by_type_included/skipped, payload assignees
  - первые 30 entries сотрудника с типами/комментариями/датами
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import select

from .daily_summary import _build_jira_client_from_credential
from .db import SessionLocal
from .models import ApiCredential, CredentialTeam, Team, TeamTelegramSetting, User
from .worklog_fetcher import get_team_worklog


def _resolve_team_and_credential(
    db,
    team_id: int | None,
    team_name: str | None,
    credential_id: int | None,
) -> tuple[Team, ApiCredential]:
    if team_name:
        team = db.scalar(select(Team).where(Team.name == team_name))
        if team is None:
            raise SystemExit(f"Team with name '{team_name}' not found")
        team_id = team.id
    if team_id is None:
        raise SystemExit("Provide --team-id or --team-name")
    team = db.get(Team, team_id)
    if team is None:
        raise SystemExit(f"Team id={team_id} not found")

    if credential_id is not None:
        cred = db.get(ApiCredential, credential_id)
        if cred is None:
            raise SystemExit(f"ApiCredential id={credential_id} not found")
        return team, cred

    # Приоритет: credential, через который реально шлётся daily_summary в Telegram.
    cred = db.scalar(
        select(ApiCredential)
        .join(TeamTelegramSetting, TeamTelegramSetting.credential_id == ApiCredential.id)
        .where(TeamTelegramSetting.team_id == team_id, TeamTelegramSetting.enabled.is_(True))
        .order_by(ApiCredential.id.desc())
    )
    if cred is not None:
        return team, cred

    # Fallback: любой credential, у которого команда привязана.
    creds = db.execute(
        select(ApiCredential)
        .join(CredentialTeam, CredentialTeam.credential_id == ApiCredential.id)
        .where(CredentialTeam.team_id == team_id)
        .order_by(ApiCredential.id.desc())
    ).scalars().all()
    if not creds:
        raise SystemExit(f"No ApiCredential bound to team_id={team_id}")

    if len(creds) > 1:
        print("[hint] multiple credentials bound to this team. Picking newest one.")
        for c in creds:
            print(f"  - credential_id={c.id} app_user_id={c.app_user_id} email={getattr(c, 'jira_email', '')!r}")
        print("Use --credential-id <id> to override.\n")
    return team, creds[0]


def _truncate_strings(obj: Any, max_len: int = 300) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= max_len else obj[:max_len] + f"… [+{len(obj) - max_len} chars]"
    if isinstance(obj, list):
        return [_truncate_strings(x, max_len) for x in obj]
    if isinstance(obj, dict):
        return {k: _truncate_strings(v, max_len) for k, v in obj.items()}
    return obj


def _find_user_row(rows: list[dict], needle: str) -> dict | None:
    n = (needle or "").strip().lower()
    if not n:
        return None
    for r in rows:
        name = (r.get("user_name") or "").lower()
        acc = (r.get("user_account_id") or "").lower()
        if n in name or n in acc:
            return r
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Worklog debug for a team/user")
    parser.add_argument("--team-id", type=int, default=None)
    parser.add_argument("--team-name", type=str, default=None)
    parser.add_argument("--credential-id", type=int, default=None,
                        help="Override credential to use (defaults to daily_summary credential)")
    parser.add_argument("--days", type=str, default="previous_workday")
    parser.add_argument(
        "--user",
        type=str,
        default=None,
        help="Filter by user name substring or jira account id substring",
    )
    parser.add_argument("--max-entries", type=int, default=30)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        team, cred = _resolve_team_and_credential(
            db, args.team_id, args.team_name, args.credential_id
        )
        print(
            f"[credential] id={cred.id} app_user_id={cred.app_user_id} "
            f"email={getattr(cred, 'jira_email', '')!r}"
        )
        jira, api_prefix = _build_jira_client_from_credential(cred)

        debug_out: dict = {}
        rows = get_team_worklog(
            db,
            team.id,
            days=args.days,
            jira=jira,
            api_prefix=api_prefix,
            credential_id=cred.id,
            app_user_id=cred.app_user_id,
            is_custom=False,
            debug_out=debug_out,
        )

        print(f"=== team_id={team.id} team='{team.name}' days={args.days} ===")
        print(f"users with worklog rows: {len(rows)}")

        if args.user:
            row = _find_user_row(rows, args.user)
            if row is None:
                # пробуем найти пользователя в БД, чтобы показать его jira_account_id
                like = f"%{args.user}%"
                u = db.scalar(
                    select(User).where(
                        (User.display_name.ilike(like)) | (User.jira_account_id.ilike(like))
                    )
                )
                if u is None:
                    print(f"\n[user] '{args.user}' not found in team rows and not in users table")
                else:
                    print(
                        f"\n[user] '{u.display_name}' (account_id={u.jira_account_id}) "
                        f"present in DB but NOT in team rows (no worklog at all)"
                    )
            else:
                entries = row.get("entries") or []
                print(
                    f"\n[user] {row.get('user_name')} "
                    f"(account_id={row.get('user_account_id')}): "
                    f"total_hours={row.get('total_hours'):.2f} "
                    f"total_seconds={row.get('total_seconds')} "
                    f"entries_count={len(entries)}"
                )
                print(f"\n[entries top {args.max_entries}]")
                for i, e in enumerate(entries[: args.max_entries], 1):
                    secs = int(e.get("time_spent_seconds") or 0)
                    print(
                        f"  {i:>2}. date={e.get('worklog_date')} "
                        f"sec={secs:>6} "
                        f"key={e.get('issue_key')!r} "
                        f"summary={(e.get('issue_summary') or '')[:40]!r} "
                        f"comment={(e.get('comment') or '')[:80]!r}"
                    )

        print("\n=== debug.sources ===")
        sources = (debug_out.get("sources") or {})
        print(json.dumps(_truncate_strings(sources), ensure_ascii=False, indent=2))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
