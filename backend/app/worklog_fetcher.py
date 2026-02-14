"""
Получение данных о списанном времени (worklog) из Jira для команды.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import math
import re
import os

from sqlalchemy.orm import Session
from sqlalchemy import select

from .config import settings
from .jira_client import Jira, build_headers_from_env, find_field_id, load_env_file
from .models import ApiCredential, CredentialTeam, CredentialUser, Team, TeamConfig, TeamMember, User
import requests


def _comment_to_text(comment) -> str:
    """
    Jira worklog comment может приходить строкой (Server) или как ADF JSON (Cloud).
    На фронте ожидается строка — приводим к безопасному тексту.
    """
    if comment is None:
        return ""
    if isinstance(comment, str):
        return comment

    texts: list[str] = []

    def walk(node) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            t = node.get("type")
            if t == "text" and isinstance(node.get("text"), str):
                texts.append(node["text"])
            for child in (node.get("content") or []):
                walk(child)
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return

    # Попробуем разобрать ADF (doc/content/text)
    walk(comment)
    if texts:
        return " ".join(t.strip() for t in texts if t and t.strip()).strip()

    # Fallback — привести к строке (чтобы JS substring не падал)
    try:
        return str(comment)
    except Exception:
        return ""

def _coerce_issue_id(value) -> int | None:
    """
    Приводит issueId к int, если Jira/интеграции вернули его строкой.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            try:
                return int(s)
            except Exception:
                return None
    return None

def _coerce_issue_key(value) -> str:
    """
    Возвращает Jira-ключ формата ABC-123, если удалось извлечь из значения.
    """
    if not isinstance(value, str):
        return ""
    s = value.strip().upper()
    if re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", s):
        return s
    return ""

def _extract_issue_ref_from_worklog(worklog: dict) -> tuple[int | None, str]:
    """
    Пытается извлечь issueId/issueKey из worklog Jira в разных форматах.
    """
    # 1) Прямые поля
    issue_id = (
        _coerce_issue_id(worklog.get("issueId"))
        or _coerce_issue_id(worklog.get("issueID"))
        or _coerce_issue_id(worklog.get("issue_id"))
    )
    issue_key = (
        _coerce_issue_key(worklog.get("issueKey"))
        or _coerce_issue_key(worklog.get("issue"))
        or _coerce_issue_key(worklog.get("key"))
    )

    # 2) Вложенный объект issue (если вдруг приходит)
    issue_obj = worklog.get("issue")
    if isinstance(issue_obj, dict):
        if issue_id is None:
            issue_id = (
                _coerce_issue_id(issue_obj.get("id"))
                or _coerce_issue_id(issue_obj.get("issueId"))
            )
        if not issue_key:
            issue_key = _coerce_issue_key(issue_obj.get("key"))

    # 3) Fallback: парсим из self URL вида .../issue/28265/worklog/...
    if issue_id is None and not issue_key:
        self_url = str(worklog.get("self") or "").strip()
        if self_url:
            m = re.search(r"/issue/([^/]+)/worklog", self_url)
            if m:
                ref = (m.group(1) or "").strip()
                maybe_id = _coerce_issue_id(ref)
                if maybe_id is not None:
                    issue_id = maybe_id
                else:
                    issue_key = _coerce_issue_key(ref)

    return issue_id, issue_key

def _make_http_session_for_integrations() -> requests.Session:
    """
    HTTP-сессия для внешних интеграций (Teamboard/DevSamurai).
    По умолчанию НЕ использует системные proxy-переменные, чтобы локально не падать с WinError 10061.
    """
    s = requests.Session()
    use_system_proxy = (os.getenv("WORKLOG_USE_SYSTEM_PROXY") or "").strip().lower() in ("1", "true", "yes", "on")
    s.trust_env = use_system_proxy
    if not use_system_proxy:
        s.proxies = {}
    return s


def get_team_worklog(
    db: Session,
    team_id: int,
    days: str | int = 8,
    *,
    team_field_name: str = "TEAM",
    jira: "Jira | None" = None,
    api_prefix: str | None = None,
    credential_id: int | None = None,
    app_user_id: int | None = None,
    debug_out: dict | None = None,
) -> List[Dict]:
    """
    Получить списанное время для всех пользователей команды за последние N дней.

    Returns:
        List[Dict] с полями:
        - user_id: int
        - user_name: str
        - user_account_id: str
        - total_seconds: int (сумма секунд)
        - total_hours: float (сумма часов)
        - entries: List[Dict] (детали по каждому списанию)
    """
    # Получаем команду и её пользователей
    team = db.get(Team, team_id)
    if not team:
        return []

    # Ограничиваем доступ: если передан credential_id, убеждаемся что команда доступна
    if credential_id is not None:
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == credential_id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return []

    if app_user_id is not None:
        # Получаем состав команды из TeamConfig для этого пользователя
        user_ids = set(db.scalars(
            select(TeamConfig.jira_user_id)
            .where(TeamConfig.app_user_id == app_user_id, TeamConfig.team_id == team_id)
        ).all())
    else:
        # Fallback на общий состав команды
        members = db.query(TeamMember).filter(TeamMember.team_id == team_id).all()
        user_ids = {m.user_id for m in members}

    if not user_ids:
        return []

    # Фильтруем по доступности через credentials пользователя (если передан app_user_id или credential_id)
    filter_user_ids = None
    if app_user_id is not None:
        filter_user_ids = set(db.scalars(
            select(CredentialUser.user_id)
            .join(ApiCredential, ApiCredential.id == CredentialUser.credential_id)
            .where(ApiCredential.app_user_id == app_user_id)
        ).all())
    elif credential_id is not None:
        filter_user_ids = set(db.scalars(
            select(CredentialUser.user_id).where(CredentialUser.credential_id == credential_id)
        ).all())

    if filter_user_ids is not None:
        user_ids = user_ids.intersection(filter_user_ids)
        if not user_ids:
            return []

    users = db.query(User).filter(User.id.in_(user_ids)).all()
    user_by_account_id: Dict[str, User] = {u.jira_account_id: u for u in users if u.jira_account_id}

    # Подключаемся к Jira (если не передан клиент)
    if jira is None or api_prefix is None:
        load_env_file(settings.jira_secrets_file_abs)
        base_url, headers = build_headers_from_env()
        jira = Jira(base_url, headers)
        api_prefix = jira.detect_api_prefix()

    # Получаем поле TEAM
    fields = jira.get_fields(api_prefix)
    team_field_id = find_field_id(fields, team_field_name)

    # Вычисляем даты в зависимости от параметра days
    now = datetime.now()
    if days == "previous_workday":
        # Ищем последний рабочий день перед текущей датой.
        # Пн -> Пт, Вт -> Пн, Вс -> Пт и т.д.
        previous_day = now - timedelta(days=1)
        while previous_day.weekday() >= 5:  # 5=Saturday, 6=Sunday
            previous_day -= timedelta(days=1)
        start_date = datetime(previous_day.year, previous_day.month, previous_day.day, 0, 0, 0)
        end_date = datetime(previous_day.year, previous_day.month, previous_day.day, 23, 59, 59)
    elif days == "today":
        # Сегодня
        start_date = datetime(now.year, now.month, now.day, 0, 0, 0)
        end_date = datetime(now.year, now.month, now.day, 23, 59, 59)
    elif days == "yesterday":
        # Вчера
        yesterday = now - timedelta(days=1)
        start_date = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0)
        end_date = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59)
    else:
        # За последние N дней (по умолчанию 8)
        days_int = int(days) if isinstance(days, str) else days
        end_date = now
        start_date = end_date - timedelta(days=days_int)
    
    start_date_str = start_date.strftime("%Y-%m-%d")
    end_date_str = end_date.strftime("%Y-%m-%d")

    if debug_out is None:
        debug_out = {}
    debug_out.setdefault("sources", {})

    def _seconds_to_human(seconds: int) -> str:
        if seconds <= 0:
            return ""
        # Jira-стиль "1h 30m"
        m = int(round(seconds / 60.0))
        h = m // 60
        mm = m % 60
        if h and mm:
            return f"{h}h {mm}m"
        if h:
            return f"{h}h"
        return f"{mm}m"

    def _fetch_devsamurai_timelogs() -> list[dict]:
        """
        DevSamurai Timesheet Builder / TimePlanner.
        Возвращает список timelog объектов. Это НЕ Jira worklog (issueId/worklogId часто null).
        """
        jwt = (settings.devsamurai_timesheet_jwt or "").strip()
        if not jwt:
            return []
        base = (settings.devsamurai_timesheet_base_url or "").strip().rstrip("/")
        if not base:
            return []

        url = f"{base}/tbt/v1/timelogs/search"
        members = [u.jira_account_id for u in users if u.jira_account_id]
        if not members:
            return []
        payload = {"members": members, "startDate": start_date_str, "endDate": end_date_str}
        headers = {"accept": "application/json", "content-type": "application/json", "authorization": jwt}

        with _make_http_session_for_integrations() as session:
            r = session.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"DevSamurai timelogs/search failed: HTTP {r.status_code}: {r.text}")
        data = r.json()
        return data if isinstance(data, list) else []

    def _fetch_teamboard_timelogs() -> list[dict]:
        """
        Teamboard Public API: GET /timeplanner/timelogs
        https://api-docs.teamboard.cloud/v1/#tag/timeplanner-timelogs/GET/timeplanner/timelogs

        Требует Authorization: Bearer <JWT>.
        """
        token = (settings.teamboard_bearer_jwt or "").strip()
        if not token:
            return []

        base = (settings.teamboard_base_url or "").strip().rstrip("/")
        if not base:
            return []

        user_ids = [u.jira_account_id for u in users if u.jira_account_id]
        if not user_ids:
            return []

        url = f"{base}/timeplanner/timelogs"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

        # Пагинация: limit/offset/hasMore
        # Teamboard API: limit ∈ [1..100], default=50
        limit = 100
        offset = 0
        out: list[dict] = []
        active_user_ids = list(dict.fromkeys(user_ids))
        with _make_http_session_for_integrations() as session:
            for _ in range(50):  # safety
                if not active_user_ids:
                    break
                params = [
                    ("from", start_date_str),
                    ("to", end_date_str),
                    ("limit", str(limit)),
                    ("offset", str(offset)),
                ]
                for uid in active_user_ids:
                    params.append(("userIds", uid))

                r = session.get(url, headers=headers, params=params, timeout=30)
                if r.status_code == 403:
                    # Teamboard может валить весь ответ, если среди userIds есть невалидные аккаунты.
                    # Пробуем исключить их и повторить запрос.
                    invalid_ids: list[str] = []
                    try:
                        payload_403 = r.json() or {}
                        errors = payload_403.get("errors") or []
                        if isinstance(errors, list):
                            for err in errors:
                                if not isinstance(err, str):
                                    continue
                                m = re.search(r"Invalid user accounts:\s*(.+)$", err, flags=re.IGNORECASE)
                                if not m:
                                    continue
                                invalid_ids.extend([x.strip() for x in m.group(1).split(",") if x.strip()])
                    except Exception:
                        pass
                    if not invalid_ids:
                        raise RuntimeError(f"Teamboard timelogs failed: HTTP {r.status_code}: {r.text}")

                    active_user_ids = [uid for uid in active_user_ids if uid not in set(invalid_ids)]
                    # Повторяем тот же offset уже без битых userIds
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"Teamboard timelogs failed: HTTP {r.status_code}: {r.text}")

                payload = r.json() or {}
                data = payload.get("data") or []
                if isinstance(data, list):
                    out.extend([x for x in data if isinstance(x, dict)])

                has_more = bool(payload.get("hasMore"))
                if not has_more:
                    break
                offset = int(payload.get("offset") or offset) + int(payload.get("limit") or limit)

        return out

    def _fetch_issue_key_summary(issue_ref: int | str) -> tuple[str, str]:
        """
        Получить key+summary по issueId или issueKey.
        """
        try:
            r = jira.request("GET", f"{api_prefix}/issue/{issue_ref}?fields=summary")
            if r.status_code == 200:
                j = r.json()
                key = (j.get("key") or "").strip()
                fallback = str(issue_ref)
                return (
                    key or fallback,
                    (j.get("fields", {}) or {}).get("summary") or key or fallback,
                )
        except Exception:
            pass
        fallback = str(issue_ref)
        return (fallback, fallback)

    def _get_worklogs_via_updated() -> List[dict]:
        """
        Быстрый и полный способ собрать worklog'и: /worklog/updated -> /worklog/list.
        Важно: updated возвращает изменения по времени обновления, поэтому берем since с запасом,
        а фильтруем уже по started (дате списания).
        """
        # запас, чтобы не пропускать worklog'и, которые добавили/переотредактировали "задним числом"
        since_dt = start_date - timedelta(days=2)
        since_ms = int(since_dt.timestamp() * 1000)

        worklog_ids: list[int] = []
        seen_ids: set[int] = set()

        # Пагинация делается через until/lastPage: пока lastPage=false, делаем since=until
        safety = 0
        while True:
            safety += 1
            if safety > 50:
                break
            r = jira.request("GET", f"{api_prefix}/worklog/updated?since={since_ms}")
            if r.status_code != 200:
                raise RuntimeError(f"worklog/updated failed: HTTP {r.status_code}: {r.text}")
            payload = r.json() or {}
            for v in (payload.get("values") or []):
                wid = v.get("worklogId")
                if isinstance(wid, int) and wid not in seen_ids:
                    seen_ids.add(wid)
                    worklog_ids.append(wid)
            if payload.get("lastPage") is True:
                break
            until_ms = payload.get("until")
            if not isinstance(until_ms, int) or until_ms <= since_ms:
                break
            since_ms = until_ms

        if not worklog_ids:
            return []

        # Jira ограничивает payload — бьем на чанки
        chunk_size = 1000
        out: list[dict] = []
        for i in range(0, len(worklog_ids), chunk_size):
            chunk = worklog_ids[i : i + chunk_size]
            r = jira.request("POST", f"{api_prefix}/worklog/list", json_body={"ids": chunk})
            if r.status_code != 200:
                raise RuntimeError(f"worklog/list failed: HTTP {r.status_code}: {r.text}")
            data = r.json() or []
            if isinstance(data, list):
                out.extend([x for x in data if isinstance(x, dict)])
        return out

    # Собираем worklog по пользователям
    user_worklog: Dict[int, Dict] = {}
    for user in users:
        user_worklog[user.id] = {
            "user_id": user.id,
            "user_name": user.display_name or user.jira_account_id,
            "user_account_id": user.jira_account_id,
            "total_seconds": 0,
            "total_hours": 0.0,
            "entries": [],
        }

    # Новый основной путь: берем все worklog'и через /worklog/updated (полнее и быстрее).
    # Если Jira не поддерживает — fallback на старый JQL/issue-worklog подход.
    try:
        raw_worklogs = _get_worklogs_via_updated()

        # Сначала соберем метаданные по issueId/issueKey, чтобы не дергать Jira на каждую запись
        issue_ids: set[int] = set()
        issue_keys: set[str] = set()

        normalized: list[dict] = []
        for wl in raw_worklogs:
            author = wl.get("author") or {}
            account_id = author.get("accountId")
            if not account_id or account_id not in user_by_account_id:
                continue

            started = wl.get("started")
            if not started:
                continue

            # started может быть "2025-04-01T11:30:57.000+0300" или "...Z"
            try:
                date_str = started
                if date_str.endswith("Z"):
                    date_str = date_str.replace("Z", "+00:00")
                else:
                    import re
                    date_str = re.sub(r"\.\d{3}", "", date_str)
                    date_str = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", date_str)
                wl_dt = datetime.fromisoformat(date_str)
            except Exception:
                continue

            # Фильтруем по диапазону именно started (дата списания), а не updatedTime
            if wl_dt.date() < start_date.date() or wl_dt.date() > end_date.date():
                continue

            issue_id, issue_key = _extract_issue_ref_from_worklog(wl)
            if issue_id is not None:
                issue_ids.add(issue_id)
            if issue_key:
                issue_keys.add(issue_key)

            normalized.append({
                "account_id": account_id,
                "started": started,
                "worklog_date": wl_dt.date().strftime("%Y-%m-%d"),
                "time_spent_seconds": int(wl.get("timeSpentSeconds") or 0),
                "time_spent": wl.get("timeSpent") or "",
                "comment": _comment_to_text(wl.get("comment")),
                "issue_id": issue_id,
                "issue_key": issue_key,
            })

        issue_meta: Dict[int, tuple[str, str]] = {}
        issue_meta_by_key: Dict[str, tuple[str, str]] = {}
        if issue_ids:
            with ThreadPoolExecutor(max_workers=10) as ex:
                future_to_id = {ex.submit(_fetch_issue_key_summary, iid): iid for iid in issue_ids}
                for fut in as_completed(future_to_id):
                    iid = future_to_id[fut]
                    try:
                        issue_meta[iid] = fut.result()
                    except Exception:
                        issue_meta[iid] = (str(iid), str(iid))
        if issue_keys:
            with ThreadPoolExecutor(max_workers=10) as ex:
                future_to_key = {ex.submit(_fetch_issue_key_summary, ik): ik for ik in issue_keys}
                for fut in as_completed(future_to_key):
                    ik = future_to_key[fut]
                    try:
                        issue_meta_by_key[ik] = fut.result()
                    except Exception:
                        issue_meta_by_key[ik] = (ik, ik)

        for item in normalized:
            user = user_by_account_id[item["account_id"]]
            user_data = user_worklog[user.id]
            user_data["total_seconds"] += item["time_spent_seconds"]
            user_data["total_hours"] = user_data["total_seconds"] / 3600.0

            issue_key = ""
            issue_summary = ""
            iid = item.get("issue_id")
            if isinstance(iid, int) and iid in issue_meta:
                issue_key, issue_summary = issue_meta[iid]
            elif item.get("issue_key") in issue_meta_by_key:
                issue_key, issue_summary = issue_meta_by_key[item["issue_key"]]
            elif item.get("issue_key"):
                issue_key = str(item["issue_key"])
                issue_summary = str(item["issue_key"])
            elif isinstance(iid, int):
                issue_key = str(iid)
                issue_summary = str(iid)

            user_data["entries"].append({
                "issue_key": issue_key,
                "issue_summary": issue_summary,
                "started": item["started"],
                "worklog_date": item["worklog_date"],
                "time_spent_seconds": item["time_spent_seconds"],
                "time_spent": item["time_spent"],
                "comment": item["comment"],
            })
    except Exception as e:
        # Fallback (старый подход) — оставляем на всякий случай для несовместимых инстансов.
        # Важно: сюда попадаем только если новый метод совсем не работает.
        print(f"worklog/updated path failed, fallback to legacy: {e}")

        # Для каждого пользователя ищем задачи, где он списывал время в нужный период
        # Используем JQL с worklogAuthor и worklogDate
        account_ids = [u.jira_account_id for u in users if u.jira_account_id]

        # Ограничиваем количество пользователей для производительности
        max_users_to_check = 20  # Уменьшено для ускорения
        account_ids_to_check = account_ids[:max_users_to_check]

        all_issues_set = set()  # Множество для уникальных задач
        use_worklog_author = False

        # Пробуем использовать worklogAuthor для каждого пользователя
        for account_id in account_ids_to_check:
            jql = f'worklogAuthor = "{account_id}" AND worklogDate >= "{start_date_str}" AND worklogDate <= "{end_date_str}"'

            next_token = ""
            page_size = 200

            try:
                data = jira.search_jql_page(jql=jql, fields=["key", "summary"], max_results=page_size, next_page_token=next_token)
                issues = data.get("issues", []) or data.get("values", [])
                if issues:
                    use_worklog_author = True
                    for issue in issues:
                        issue_key = issue.get("key")
                        if issue_key:
                            all_issues_set.add(issue_key)
            except Exception:
                pass

        if not use_worklog_author or not all_issues_set:
            jql = f'"{team_field_id}" = "{team.jira_team_id}"'
            next_token = ""
            page_size = 200
            while True:
                data = jira.search_jql_page(jql=jql, fields=["key", "summary"], max_results=page_size, next_page_token=next_token)
                issues = data.get("issues", []) or data.get("values", [])
                if not issues:
                    break
                for issue in issues:
                    issue_key = issue.get("key")
                    if issue_key:
                        all_issues_set.add(issue_key)
                next_token = (data.get("nextPageToken") or "").strip()
                if not next_token:
                    break

        max_issues_to_check = 300
        issues_to_check = list(all_issues_set)[:max_issues_to_check]

        def fetch_worklog_for_issue(issue_key: str) -> tuple[str, dict, str]:
            try:
                worklog_data = jira.get_worklog(api_prefix, issue_key)
                worklogs = worklog_data.get("worklogs", [])
                issue_summary = issue_key
                try:
                    issue_data = jira.request("GET", f"{api_prefix}/issue/{issue_key}?fields=summary")
                    if issue_data.status_code == 200:
                        issue_json = issue_data.json()
                        issue_summary = issue_json.get("fields", {}).get("summary", issue_key)
                except Exception:
                    pass
                return (issue_key, {"worklogs": worklogs}, issue_summary)
            except Exception:
                return (issue_key, {"worklogs": []}, issue_key)

        issue_worklogs: Dict[str, tuple] = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_issue = {executor.submit(fetch_worklog_for_issue, issue_key): issue_key for issue_key in issues_to_check}
            for future in as_completed(future_to_issue):
                try:
                    result = future.result()
                    issue_worklogs[result[0]] = (result[1], result[2])
                except Exception:
                    pass

        for issue_key in issues_to_check:
            if issue_key not in issue_worklogs:
                continue
            worklog_data, issue_summary = issue_worklogs[issue_key]
            worklogs = worklog_data.get("worklogs", [])

            for wl in worklogs:
                author = wl.get("author", {})
                account_id = author.get("accountId")
                if not account_id or account_id not in user_by_account_id:
                    continue

                user = user_by_account_id[account_id]
                started = wl.get("started")
                time_spent_seconds = wl.get("timeSpentSeconds", 0)
                comment = _comment_to_text(wl.get("comment"))

                worklog_date = None
                if started:
                    try:
                        date_str = started
                        if date_str.endswith("Z"):
                            date_str = date_str.replace("Z", "+00:00")
                        else:
                            import re
                            date_str = re.sub(r"\.\d{3}", "", date_str)
                            date_str = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", date_str)
                        wl_date = datetime.fromisoformat(date_str)
                        worklog_date = wl_date.date()
                        if worklog_date < start_date.date() or worklog_date > end_date.date():
                            continue
                    except Exception:
                        continue

                user_data = user_worklog[user.id]
                user_data["total_seconds"] += time_spent_seconds
                user_data["total_hours"] = user_data["total_seconds"] / 3600.0

                user_data["entries"].append({
                    "issue_key": issue_key,
                    "issue_summary": issue_summary,
                    "started": started,
                    "worklog_date": worklog_date.strftime("%Y-%m-%d") if worklog_date else None,
                    "time_spent_seconds": time_spent_seconds,
                    "time_spent": wl.get("timeSpent", ""),
                    "comment": comment,
                })

    # Дополнительно: DevSamurai (TimePlanner/Timesheet Builder) timelogs типа Event/custom_task
    # Они не являются Jira worklog, поэтому добавляем отдельным источником.
    try:
        dev_logs = _fetch_devsamurai_timelogs()
        debug_out["sources"]["devsamurai"] = {"enabled": True, "count": len(dev_logs)}
        for tl in dev_logs:
            account_id = tl.get("assignee")
            if not account_id or account_id not in user_by_account_id:
                continue
            date_s = tl.get("date")  # YYYY-MM-DD
            if not date_s:
                continue

            # час может быть float (0.02)
            hours = tl.get("hour") or 0
            try:
                seconds = int(round(float(hours) * 3600))
            except Exception:
                seconds = 0
            if seconds <= 0:
                continue

            # summary может содержать ключ Jira, но это "событие", поэтому issue_key пустой
            summary = (tl.get("summary") or "").strip()
            log_type = (tl.get("logtimeType") or "").strip()
            comment = summary
            if log_type:
                # чтобы было понятно, что это не Jira worklog
                comment = f"[TimePlanner:{log_type}] {summary}" if summary else f"[TimePlanner:{log_type}]"

            user = user_by_account_id[account_id]
            user_data = user_worklog[user.id]
            user_data["total_seconds"] += seconds
            user_data["total_hours"] = user_data["total_seconds"] / 3600.0
            user_data["entries"].append({
                "issue_key": "",
                "issue_summary": "Event" if log_type else "TimePlanner",
                "started": tl.get("loggedAt"),
                "worklog_date": date_s,
                "time_spent_seconds": seconds,
                "time_spent": _seconds_to_human(seconds),
                "comment": comment,
            })
    except Exception as e:
        debug_out["sources"]["devsamurai"] = {"enabled": bool((settings.devsamurai_timesheet_jwt or "").strip()), "error": str(e)}
        print(f"DevSamurai timelogs fetch failed: {e}")

    # Альтернатива/дополнение: Teamboard Public API timelogs
    # (если в Teamboard есть Event-типы, они приходят здесь отдельным type, issueId может быть null)
    try:
        tb_logs = _fetch_teamboard_timelogs()
        debug_out["sources"]["teamboard"] = {
            "enabled": bool((settings.teamboard_bearer_jwt or "").strip()),
            "count": len(tb_logs),
        }

        issue_ids: set[int] = set()
        normalized_tb: list[dict] = []
        included_events = 0
        skipped_issue_logs = 0
        skipped_issue_logs_non_numeric = 0

        for tl in tb_logs:
            account_id = tl.get("assignee")
            if not account_id or account_id not in user_by_account_id:
                continue

            date_s = tl.get("date")
            if not date_s:
                continue

            # ВАЖНО: Teamboard возвращает как "event" (custom_task и т.п.), так и логи по Jira issue.
            # Jira-логи мы уже считаем через Jira worklog (чтобы не было дублей),
            # поэтому исключаем только записи с РЕАЛЬНЫМ issueId.
            raw_issue_id = tl.get("issueId")
            issue_id = _coerce_issue_id(raw_issue_id)
            if issue_id is not None:
                skipped_issue_logs += 1
                continue

            seconds = tl.get("timeSpentSeconds") or 0
            try:
                seconds = int(seconds)
            except Exception:
                seconds = 0
            if seconds <= 0:
                continue

            # Иногда в event прилетают странные "issueId" (пустая строка/"null"/нечисловые),
            # не считаем их Jira-логами и не отбрасываем запись.
            if raw_issue_id not in (None, "") and issue_id is None:
                skipped_issue_logs_non_numeric += 1

            normalized_tb.append({
                "account_id": account_id,
                "date": date_s,
                "seconds": seconds,
                "type": (tl.get("type") or "").strip(),
                "notes": (tl.get("notes") or "").strip(),
                "summary": (tl.get("summary") or "").strip() if isinstance(tl.get("summary"), str) else "",
                "issue_id": issue_id,
                "started": ((tl.get("info") or {}) if isinstance(tl.get("info"), dict) else {}).get("started"),
            })
            included_events += 1

        # Обновим debug-статистику уже после фильтра (чтобы было видно, что мы не дублируем Jira issue logs)
        debug_out["sources"]["teamboard"].update({
            "included_events": included_events,
            "skipped_issue_logs": skipped_issue_logs,
            "skipped_issue_logs_non_numeric": skipped_issue_logs_non_numeric,
        })

        issue_meta: Dict[int, tuple[str, str]] = {}
        if issue_ids:
            with ThreadPoolExecutor(max_workers=10) as ex:
                future_to_id = {ex.submit(_fetch_issue_key_summary, iid): iid for iid in issue_ids}
                for fut in as_completed(future_to_id):
                    iid = future_to_id[fut]
                    try:
                        issue_meta[iid] = fut.result()
                    except Exception:
                        issue_meta[iid] = (str(iid), str(iid))

        for item in normalized_tb:
            user = user_by_account_id[item["account_id"]]
            user_data = user_worklog[user.id]
            user_data["total_seconds"] += item["seconds"]
            user_data["total_hours"] = user_data["total_seconds"] / 3600.0

            issue_key = ""
            issue_summary = item["summary"] or (item["type"] or "Event")
            if item.get("issue_id") is not None:
                ik, isum = issue_meta.get(item["issue_id"], ("", ""))
                issue_key = ik
                issue_summary = isum or issue_summary

            comment = item["notes"] or item["summary"]
            if item["type"]:
                comment = f"[Teamboard:{item['type']}] {comment}".strip()

            user_data["entries"].append({
                "issue_key": issue_key,
                "issue_summary": issue_summary,
                "started": item.get("started"),
                "worklog_date": item["date"],
                "time_spent_seconds": item["seconds"],
                "time_spent": _seconds_to_human(item["seconds"]),
                "comment": comment,
            })
    except Exception as e:
        debug_out["sources"]["teamboard"] = {"enabled": bool((settings.teamboard_bearer_jwt or "").strip()), "error": str(e)}
        print(f"Teamboard timelogs fetch failed: {e}")

    # Сортируем по убыванию времени
    result = sorted(user_worklog.values(), key=lambda x: x["total_seconds"], reverse=True)
    
    return result

