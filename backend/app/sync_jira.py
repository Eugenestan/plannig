from __future__ import annotations

from typing import Dict, List, Tuple, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .jira_client import Jira, extract_team_values, find_field_id, normalize_user
from .models import CredentialTeam, CredentialUser, Team, TeamMember, User


def sync_from_jira_for_credential(
    db: Session,
    *,
    credential_id: int,
    jira: Jira,
    api_prefix: str,
    team_field_name: str = "TEAM",
    user_fields: List[str] | None = None,
    clear_existing_links: bool = True,
) -> Dict[str, int]:
    """
    Подтягивает команды и пользователей из Jira по задачам, где TEAM не пустой,
    и привязывает доступ к ним к конкретному credential.
    - upsert teams
    - upsert users
    - upsert team_members (связь)
    - upsert credential_teams / credential_users (изоляция пользователей)
    """
    user_fields = user_fields or ["assignee"]

    if clear_existing_links:
        db.execute(delete(CredentialTeam).where(CredentialTeam.credential_id == credential_id))
        db.execute(delete(CredentialUser).where(CredentialUser.credential_id == credential_id))
        db.flush()

    fields = jira.get_fields(api_prefix)
    
    # Пытаемся найти поле TEAM, если не найдено - просто возвращаем пустой результат
    try:
        team_field_id = find_field_id(fields, team_field_name)
    except RuntimeError:
        # Поле TEAM не найдено - это нормально, не все Jira инстансы имеют это поле
        # Возвращаем пустой результат синхронизации
        return {"teams_created": 0, "users_created": 0, "links_created": 0}

    jql = f'"{team_field_id}" is not EMPTY'
    page_size = 200
    next_token = ""

    created_teams = 0
    created_users = 0
    created_links = 0

    while True:
        data = jira.search_jql_page(jql=jql, fields=[team_field_id] + user_fields, max_results=page_size, next_page_token=next_token)
        issues = data.get("issues", []) or data.get("values", [])
        if not issues:
            break

        for issue in issues:
            f = issue.get("fields", {})
            teams = extract_team_values(f.get(team_field_id))
            if not teams:
                continue

            # upsert users from user_fields once per issue
            issue_users: List[User] = []
            for uf in user_fields:
                raw = f.get(uf)
                if isinstance(raw, list):
                    items = raw
                else:
                    items = [raw]
                for item in items:
                    nu = normalize_user(item)
                    if not nu:
                        continue
                    user = db.scalar(select(User).where(User.jira_account_id == nu["accountId"]))
                    if user is None:
                        user = User(
                            jira_account_id=nu["accountId"],
                            display_name=nu["displayName"] or nu["accountId"],
                            email=nu.get("email"),
                            active=bool(nu.get("active", True)),
                        )
                        db.add(user)
                        db.flush()
                        created_users += 1
                    else:
                        # лёгкий апдейт имени/почты
                        user.display_name = nu["displayName"] or user.display_name
                        if nu.get("email"):
                            user.email = nu.get("email")
                        user.active = bool(nu.get("active", True))
                    issue_users.append(user)

                    # привязка user к credential (изоляция)
                    cu = db.scalar(
                        select(CredentialUser).where(
                            CredentialUser.credential_id == credential_id,
                            CredentialUser.user_id == user.id,
                        )
                    )
                    if cu is None:
                        db.add(CredentialUser(credential_id=credential_id, user_id=user.id))

            for t in teams:
                jira_team_id = str(t.get("id") or "")
                name = (t.get("name") or t.get("title") or "").strip()
                if not jira_team_id or not name:
                    continue
                team = db.scalar(
                    select(Team).where(Team.jira_field_id == team_field_id, Team.jira_team_id == jira_team_id)
                )
                if team is None:
                    team = Team(jira_field_id=team_field_id, jira_team_id=jira_team_id, name=name)
                    db.add(team)
                    db.flush()
                    created_teams += 1
                else:
                    team.name = name
                    # Обновляем имя команды, если изменилось
                    db.flush()

                # привязка team к credential (изоляция)
                ct = db.scalar(
                    select(CredentialTeam).where(
                        CredentialTeam.credential_id == credential_id,
                        CredentialTeam.team_id == team.id,
                    )
                )
                if ct is None:
                    db.add(CredentialTeam(credential_id=credential_id, team_id=team.id))

                # связываем пользователей с командами (проверяем дубликаты в БД и в рамках задачи)
                seen_in_issue: set[int] = set()
                for user in issue_users:
                    if user.id in seen_in_issue:
                        continue  # уже добавили этого пользователя для этой команды в этой задаче
                    exists = db.scalar(
                        select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == user.id)
                    )
                    if exists is None:
                        db.add(TeamMember(team_id=team.id, user_id=user.id))
                        created_links += 1
                        seen_in_issue.add(user.id)

        next_token = (data.get("nextPageToken") or "").strip()
        if not next_token:
            break

    db.commit()
    return {"teams_created": created_teams, "users_created": created_users, "links_created": created_links}


def credential_has_any_team(
    *,
    jira: Jira,
    api_prefix: str,
    team_field_name: str = "TEAM",
) -> bool:
    """
    Проверка для авторизации: есть ли хотя бы одна команда, доступная по ключу.
    """
    fields = jira.get_fields(api_prefix)
    team_field_id = find_field_id(fields, team_field_name)
    jql = f'"{team_field_id}" is not EMPTY'
    data = jira.search_jql_page(jql=jql, fields=[team_field_id], max_results=1, next_page_token="")
    issues = data.get("issues", []) or data.get("values", [])
    for issue in issues:
        f = issue.get("fields", {})
        teams = extract_team_values(f.get(team_field_id))
        if teams:
            return True
    return False


