"""
Получение данных о списанном времени (worklog) из Jira для команды.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from sqlalchemy.orm import Session

from .config import settings
from .jira_client import Jira, build_headers_from_env, find_field_id, load_env_file
from .models import Team, TeamMember, User


def get_team_worklog(
    db: Session,
    team_id: int,
    days: str | int = 8,
    *,
    team_field_name: str = "TEAM",
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

    members = db.query(TeamMember).filter(TeamMember.team_id == team_id).all()
    if not members:
        return []

    user_ids = {m.user_id for m in members}
    users = db.query(User).filter(User.id.in_(user_ids)).all()
    user_by_account_id: Dict[str, User] = {u.jira_account_id: u for u in users if u.jira_account_id}

    # Подключаемся к Jira
    load_env_file(settings.jira_secrets_file_abs)
    base_url, headers = build_headers_from_env()
    jira = Jira(base_url, headers)
    api_prefix = jira.detect_api_prefix()

    # Получаем поле TEAM
    fields = jira.get_fields(api_prefix)
    team_field_id = find_field_id(fields, team_field_name)

    # Вычисляем даты в зависимости от параметра days
    now = datetime.now()
    if days == "today":
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
        # JQL: задачи, где пользователь списывал время в нужный период
        # Формат: worklogAuthor = accountId AND worklogDate >= start_date AND worklogDate <= end_date
        jql = f'worklogAuthor = "{account_id}" AND worklogDate >= "{start_date_str}" AND worklogDate <= "{end_date_str}"'
        
        # Получаем все задачи для этого пользователя
        next_token = ""
        page_size = 200
        
        try:
            data = jira.search_jql_page(jql=jql, fields=["key", "summary"], max_results=page_size, next_page_token=next_token)
            issues = data.get("issues", []) or data.get("values", [])
            if issues:
                use_worklog_author = True
                # Добавляем задачи в множество
                for issue in issues:
                    issue_key = issue.get("key")
                    if issue_key:
                        all_issues_set.add(issue_key)
                
                # Получаем остальные страницы (ограничиваем до 5 страниц для производительности)
                page_count = 0
                max_pages = 5
                while page_count < max_pages:
                    next_token = (data.get("nextPageToken") or "").strip()
                    if not next_token:
                        break
                    data = jira.search_jql_page(jql=jql, fields=["key", "summary"], max_results=page_size, next_page_token=next_token)
                    issues = data.get("issues", []) or data.get("values", [])
                    if not issues:
                        break
                    for issue in issues:
                        issue_key = issue.get("key")
                        if issue_key:
                            all_issues_set.add(issue_key)
                    page_count += 1
        except Exception as e:
            # Если JQL не поддерживает worklogAuthor/worklogDate, пропускаем этого пользователя
            # и будем использовать старый метод
            print(f"Error with worklogAuthor JQL for {account_id}: {e}")
            pass
    
    # Если не получилось через worklogAuthor, используем старый метод (задачи команды)
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
    
    # Ограничиваем количество задач для производительности
    max_issues_to_check = 300
    issues_to_check = list(all_issues_set)[:max_issues_to_check]
    
    # Функция для получения worklog одной задачи
    def fetch_worklog_for_issue(issue_key: str) -> tuple[str, dict, str]:
        """Получить worklog для одной задачи. Возвращает (issue_key, worklog_data, issue_summary)."""
        try:
            worklog_data = jira.get_worklog(api_prefix, issue_key)
            worklogs = worklog_data.get("worklogs", [])
            
            # Получаем summary задачи
            issue_summary = issue_key  # По умолчанию
            try:
                issue_data = jira.request("GET", f"{api_prefix}/issue/{issue_key}?fields=summary")
                if issue_data.status_code == 200:
                    issue_json = issue_data.json()
                    issue_summary = issue_json.get("fields", {}).get("summary", issue_key)
            except:
                pass
            
            return (issue_key, {"worklogs": worklogs}, issue_summary)
        except Exception as e:
            # Возвращаем пустой результат при ошибке
            return (issue_key, {"worklogs": []}, issue_key)
    
    # Получаем worklog параллельно (до 10 одновременных запросов для стабильности)
    issue_worklogs: Dict[str, tuple] = {}
    max_workers = 10
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Запускаем все запросы параллельно
        future_to_issue = {
            executor.submit(fetch_worklog_for_issue, issue_key): issue_key 
            for issue_key in issues_to_check
        }
        
        # Собираем результаты по мере готовности
        for future in as_completed(future_to_issue):
            issue_key = future_to_issue[future]
            try:
                result = future.result()
                issue_worklogs[result[0]] = (result[1], result[2])
            except Exception as e:
                # При ошибке просто пропускаем эту задачу
                pass
    
    # Обрабатываем полученные worklog
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
                comment = wl.get("comment", "")

                # Проверяем, что worklog в нужном диапазоне дат и извлекаем дату
                worklog_date = None
                if started:
                    try:
                        # Jira формат: "2025-04-01T11:30:57.000+0300" или "2024-01-15T10:30:00.000Z"
                        # Преобразуем в формат, который понимает fromisoformat
                        date_str = started
                        if date_str.endswith("Z"):
                            date_str = date_str.replace("Z", "+00:00")
                        else:
                            # Формат: "2025-04-01T11:30:57.000+0300"
                            # Нужно преобразовать в "2025-04-01T11:30:57+03:00"
                            import re
                            # Убираем миллисекунды и исправляем часовой пояс
                            date_str = re.sub(r'\.\d{3}', '', date_str)  # Убираем .000
                            # Преобразуем +0300 в +03:00
                            date_str = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', date_str)
                        
                        wl_date = datetime.fromisoformat(date_str)
                        worklog_date = wl_date.date()
                        
                        # Сравниваем только дату (без времени)
                        if worklog_date < start_date.date() or worklog_date > end_date.date():
                            continue
                    except Exception as e:
                        # Если не удалось распарсить дату, пропускаем эту запись
                        # (не выводим warning, чтобы не засорять вывод)
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

    # Сортируем по убыванию времени
    result = sorted(user_worklog.values(), key=lambda x: x["total_seconds"], reverse=True)
    
    return result

