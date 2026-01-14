from __future__ import annotations

from typing import Dict, List, Tuple, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .jira_client import Jira, extract_team_values, find_field_id, normalize_user
from .models import CredentialTeam, CredentialUser, Team, TeamMember, User


def sync_all_jira_users(jira: Jira, api_prefix: str, db: Session) -> int:
    """
    Синхронизирует всех пользователей Jira через API /users/search.
    Возвращает количество созданных/обновленных пользователей.
    """
    created_count = 0
    start_at = 0
    max_results = 50  # Jira API ограничение
    
    while True:
        # Используем /users/search для получения всех пользователей
        params = {
            "startAt": start_at,
            "maxResults": max_results
        }
        r = jira.request("GET", f"{api_prefix}/users/search", params=params)
        
        if r.status_code != 200:
            # Если endpoint не поддерживается, пробуем альтернативный способ
            if r.status_code == 404:
                # Пробуем получить пользователей через /users
                r = jira.request("GET", f"{api_prefix}/users", params=params)
                if r.status_code != 200:
                    print(f"Warning: Cannot fetch users via /users/search or /users: HTTP {r.status_code}")
                    break
            else:
                print(f"Warning: Cannot fetch users: HTTP {r.status_code}")
                break
        
        users_data = r.json()
        if not users_data or not isinstance(users_data, list):
            break
        
        for user_data in users_data:
            nu = normalize_user(user_data)
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
                created_count += 1
            else:
                # Обновляем данные пользователя
                user.display_name = nu["displayName"] or user.display_name
                if nu.get("email"):
                    user.email = nu.get("email")
                user.active = bool(nu.get("active", True))
        
        if len(users_data) < max_results:
            break
        
        start_at += max_results
    
    db.flush()
    return created_count


def sync_from_jira_for_credential(
    db: Session,
    *,
    credential_id: int,
    jira: Jira,
    api_prefix: str,
    team_field_name: str = "TEAM",
    user_fields: List[str] | None = None,
    clear_existing_links: bool = True,
    sync_all_users: bool = True,  # Новый параметр для синхронизации всех пользователей
) -> Dict[str, int]:
    """
    Подтягивает команды и пользователей из Jira по задачам, где TEAM не пустой,
    и привязывает доступ к ним к конкретному credential.
    - upsert teams
    - upsert users (из задач + все пользователи Jira, если sync_all_users=True)
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
        # Но если sync_all_users=True, все равно синхронизируем пользователей
        if sync_all_users:
            try:
                users_count = sync_all_jira_users(jira, api_prefix, db)
                print(f"Synced {users_count} users from Jira API")
            except Exception as e:
                print(f"Warning: Failed to sync all users: {e}")
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

    # Синхронизируем всех пользователей Jira, если включено
    if sync_all_users:
        try:
            users_count = sync_all_jira_users(jira, api_prefix, db)
            print(f"Synced {users_count} additional users from Jira API")
            created_users += users_count
        except Exception as e:
            print(f"Warning: Failed to sync all users: {e}")

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
