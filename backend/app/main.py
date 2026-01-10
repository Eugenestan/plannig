from __future__ import annotations

from pathlib import Path
from typing import List
import uuid

from fastapi import Body, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError

from .db import Base, engine, get_db
from .models import ApiCredential, CredentialTeam, CredentialUser, GanttState, ImproveTaskOrder, Team, TeamMember, User
from .sync_jira import credential_has_any_team, sync_from_jira_for_credential
from .worklog_fetcher import get_team_worklog
from .config import settings
from .jira_client import Jira, load_env_file
import os
import base64


app = FastAPI(title="Planing - Teams")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Настройка сессий
# В cookie хранится только session_key (идентификатор). Сам API ключ хранится на сервере в SQLite.
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, max_age=86400 * 30)  # 30 дней

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _get_session_key(request: Request) -> str:
    if not hasattr(request, "session") or request.session is None:
        request.session = {}
    return (request.session.get("session_key") or "").strip()


def get_credential_from_session(request: Request, db: Session) -> ApiCredential:
    session_key = _get_session_key(request)
    if not session_key:
        raise RuntimeError("Не авторизован. Введите ключ на главной странице.")
    cred = db.scalar(select(ApiCredential).where(ApiCredential.session_key == session_key))
    if cred is None:
        raise RuntimeError("Сессия не найдена. Введите ключ на главной странице.")
    return cred


def build_jira_client_from_api_key(api_key: str) -> tuple[Jira, str]:
    """
    Создаёт Jira-клиент из ключа (Basic если есть email в jira_secrets.env, иначе Bearer).
    Возвращает (jira, api_prefix).
    """
    load_env_file(settings.jira_secrets_file_abs)
    base_url = (os.getenv("JIRA_BASE_URL") or "").strip()
    email = (os.getenv("JIRA_EMAIL") or "").strip()
    if not base_url:
        raise RuntimeError("JIRA_BASE_URL не настроен в конфигурации")

    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("Ключ не может быть пустым")

    headers = {"Accept": "application/json"}
    if email:
        raw = f"{email}:{api_key}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        jira = Jira(base_url, headers)
        api_prefix = jira.detect_api_prefix()
        return jira, api_prefix

    headers["Authorization"] = f"Bearer {api_key}"
    jira = Jira(base_url, headers)
    api_prefix = jira.detect_api_prefix()
    return jira, api_prefix


def get_jira_client_for_request(request: Request, db: Session) -> tuple[Jira, str, ApiCredential]:
    cred = get_credential_from_session(request, db)
    jira, api_prefix = build_jira_client_from_api_key(cred.jira_api_key)
    return jira, api_prefix, cred


@app.on_event("startup")
def _startup() -> None:
    # MVP: пытаемся создать таблицы автоматически.
    # Если MySQL ещё не поднят, НЕ валим весь сервер — показываем понятную страницу.
    app.state.db_ready = True
    app.state.db_error = ""
    try:
        Base.metadata.create_all(bind=engine)
    except SQLAlchemyError as e:
        app.state.db_ready = False
        app.state.db_error = str(e)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not getattr(app.state, "db_ready", True):
        return templates.TemplateResponse(
            "db_down.html",
            {"request": request, "error": getattr(app.state, "db_error", "")},
            status_code=503,
        )
    
    session_key = _get_session_key(request)
    cred = db.scalar(select(ApiCredential).where(ApiCredential.session_key == session_key)) if session_key else None
    if cred is None:
        # Показываем форму ввода ключа
        error_msg = request.query_params.get("error")
        return templates.TemplateResponse(
            "api_key_form.html",
            {
                "request": request,
                "error_msg": error_msg,
            }
        )
    
    # Если ключ есть, показываем список команд ТОЛЬКО этого credential
    teams = db.scalars(
        select(Team)
        .join(CredentialTeam, CredentialTeam.team_id == Team.id)
        .where(CredentialTeam.credential_id == cred.id)
        .order_by(Team.name.asc())
    ).all()
    sync_error = request.query_params.get("sync_error")
    error_msg = getattr(app.state, "sync_error", None) if sync_error else None
    return templates.TemplateResponse(
        "teams.html", {"request": request, "teams": teams, "sync_error": error_msg}
    )


@app.post("/sync", response_class=RedirectResponse)
def sync(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    if not getattr(app.state, "db_ready", True):
        return RedirectResponse(url="/", status_code=303)
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        sync_from_jira_for_credential(db, credential_id=cred.id, jira=jira, api_prefix=api_prefix)
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        # Логируем ошибку и возвращаемся на главную с сообщением
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
        print(f"Sync error: {error_msg}", flush=True)
        # Сохраняем ошибку в app.state для отображения на главной
        app.state.sync_error = str(e)
        return RedirectResponse(url="/?sync_error=1", status_code=303)


@app.get("/teams/{team_id}", response_class=HTMLResponse)
def team_detail(request: Request, team_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    # авторизация + доступ к команде
    cred = get_credential_from_session(request, db)
    allowed = db.scalar(
        select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
    )
    if allowed is None:
        return templates.TemplateResponse("not_found.html", {"request": request, "message": "Команда не найдена"}, status_code=404)

    team = db.scalar(
        select(Team).options(joinedload(Team.members).joinedload(TeamMember.user)).where(Team.id == team_id)
    )
    if team is None:
        return templates.TemplateResponse("not_found.html", {"request": request, "message": "Команда не найдена"}, status_code=404)

    all_users = db.scalars(
        select(User)
        .join(CredentialUser, CredentialUser.user_id == User.id)
        .where(CredentialUser.credential_id == cred.id)
        .order_by(User.display_name.asc())
    ).all()
    selected_user_ids = {tm.user_id for tm in team.members}
    return templates.TemplateResponse(
        "team_detail.html",
        {
            "request": request,
            "team": team,
            "all_users": all_users,
            "selected_user_ids": selected_user_ids,
        },
    )


@app.post("/teams/{team_id}/members", response_class=RedirectResponse)
def update_team_members(
    request: Request,
    team_id: int,
    user_ids: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cred = get_credential_from_session(request, db)
    allowed = db.scalar(
        select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
    )
    if allowed is None:
        return RedirectResponse(url="/", status_code=303)

    team = db.scalar(select(Team).where(Team.id == team_id))
    if team is None:
        return RedirectResponse(url="/", status_code=303)

    # Перезаписываем состав команды (MVP)
    db.execute(delete(TeamMember).where(TeamMember.team_id == team_id))
    # разрешаем добавлять только пользователей текущего credential
    allowed_user_ids = {
        cu.user_id
        for cu in db.scalars(select(CredentialUser).where(CredentialUser.credential_id == cred.id)).all()
    }
    for uid in user_ids:
        if uid in allowed_user_ids:
            db.add(TeamMember(team_id=team_id, user_id=uid))
    db.commit()

    return RedirectResponse(url=f"/teams/{team_id}/dashboard", status_code=303)


@app.get("/teams/{team_id}/dashboard", response_class=HTMLResponse)
def team_dashboard(request: Request, team_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    cred = get_credential_from_session(request, db)
    allowed = db.scalar(
        select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
    )
    if allowed is None:
        return RedirectResponse(url="/", status_code=303)
    
    team = db.scalar(select(Team).where(Team.id == team_id))
    if team is None:
        return templates.TemplateResponse("not_found.html", {"request": request, "message": "Команда не найдена"}, status_code=404)

    days_param = request.query_params.get("days", "today")
    
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "team": team,
            "days": days_param,
        },
    )


@app.post("/verify-key", response_class=RedirectResponse)
def verify_api_key(request: Request, api_key: str = Form(...)):
    """Проверяет и сохраняет API ключ в сессии."""
    # NOTE: db нужен тут для записи ключа на сервере
    # FastAPI позволит получить его через Depends, но этот handler уже объявлен.
    # Поэтому создаём сессию вручную.
    from .db import SessionLocal

    api_key = (api_key or "").strip()
    if not api_key:
        return RedirectResponse(url="/?error=" + "Ключ не может быть пустым", status_code=303)

    db = SessionLocal()
    try:
        # 1) Пробуем сходить в Jira этим ключом и получить команды
        try:
            jira, api_prefix = build_jira_client_from_api_key(api_key)
        except Exception:
            return RedirectResponse(url="/?error=" + "Ключ не подходит, получите ключ в настройках Jira", status_code=303)

        has_teams = False
        try:
            has_teams = credential_has_any_team(jira=jira, api_prefix=api_prefix, team_field_name="TEAM")
        except Exception:
            has_teams = False

        if not has_teams:
            return RedirectResponse(url="/?error=" + "Ключ не подходит, получите ключ в настройках Jira", status_code=303)

        # 2) Сохраняем credential на сервере, в сессии — только session_key
        session_key = _get_session_key(request)
        if not session_key:
            session_key = uuid.uuid4().hex
            request.session["session_key"] = session_key

        cred = db.scalar(select(ApiCredential).where(ApiCredential.session_key == session_key))
        if cred is None:
            cred = ApiCredential(session_key=session_key, jira_api_key=api_key)
            db.add(cred)
            db.flush()
        else:
            cred.jira_api_key = api_key
            db.flush()

        # 3) Синхронизируем команды/пользователей и привязываем доступ только к этому credential
        sync_from_jira_for_credential(db, credential_id=cred.id, jira=jira, api_prefix=api_prefix, clear_existing_links=True)

        return RedirectResponse(url="/", status_code=303)
    finally:
        db.close()


@app.get("/logout", response_class=RedirectResponse)
@app.post("/logout", response_class=RedirectResponse)
def logout(request: Request):
    """Очищает сессию и перенаправляет на главную страницу."""
    # Инициализируем сессию, если её нет
    if not hasattr(request, "session") or request.session is None:
        request.session = {}
    
    # Удаляем credential с сервера (ключ) и чистим сессию
    from .db import SessionLocal
    session_key = _get_session_key(request)
    if session_key:
        db = SessionLocal()
        try:
            cred = db.scalar(select(ApiCredential).where(ApiCredential.session_key == session_key))
            if cred is not None:
                db.delete(cred)
                db.commit()
        finally:
            db.close()

    request.session.clear()
    
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/teams/{team_id}/worklog")
def api_team_worklog(request: Request, team_id: int, days: str = "today", db: Session = Depends(get_db)):
    """API endpoint для получения worklog данных (асинхронная загрузка)."""
    from fastapi.responses import JSONResponse
    
    try:
        # Получаем Jira клиент из server-side credential
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)
        
        # Передаем клиент напрямую в get_team_worklog
        worklog_data = get_team_worklog(db, team_id, days=days, jira=jira, api_prefix=api_prefix, credential_id=cred.id)
        return JSONResponse({
            "success": True,
            "data": worklog_data,
        })
    except RuntimeError as e:
        # Ошибка авторизации
        error_msg = str(e)
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=401,
        )
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Worklog error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.get("/api/teams/{team_id}/epics")
def api_team_epics(request: Request, team_id: int, db: Session = Depends(get_db)):
    """API endpoint для получения эпиков команды."""
    from fastapi.responses import JSONResponse
    
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)
        
        # JQL запрос для эпиков
        jql = 'project = TNL AND type = Epic AND status NOT IN (Отменено, Done) AND assignee = currentUser() ORDER BY status ASC, updated ASC, parent DESC, created DESC'
        
        # Получаем эпики
        all_epics = []
        next_token = ""
        page_size = 200
        
        while True:
            data = jira.search_jql_page(jql=jql, fields=["key", "summary", "status", "updated", "created", "parent"], max_results=page_size, next_page_token=next_token)
            issues = data.get("issues", []) or data.get("values", [])
            if not issues:
                break
            
            for issue in issues:
                fields = issue.get("fields", {})
                epic = {
                    "key": issue.get("key", ""),
                    "summary": fields.get("summary", ""),
                    "status": fields.get("status", {}).get("name", "") if isinstance(fields.get("status"), dict) else str(fields.get("status", "")),
                    "updated": fields.get("updated", ""),
                    "created": fields.get("created", ""),
                    "parent": fields.get("parent", {}).get("key", "") if isinstance(fields.get("parent"), dict) else str(fields.get("parent", "")),
                }
                all_epics.append(epic)
            
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token:
                break
        
        return JSONResponse({
            "success": True,
            "data": all_epics,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Epics error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.get("/api/teams/{team_id}/releases")
def api_team_releases(request: Request, team_id: int, db: Session = Depends(get_db)):
    """API endpoint для получения релизов команды."""
    from fastapi.responses import JSONResponse
    from datetime import datetime
    
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)
        
        # JQL запрос для эпиков с версиями исправления
        jql = 'project = TNL AND type = Epic AND status NOT IN (Отменено, Done) AND assignee = currentUser() AND fixVersion IS NOT EMPTY'
        
        # Получаем эпики
        all_releases = []
        next_token = ""
        page_size = 200
        
        while True:
            data = jira.search_jql_page(
                jql=jql, 
                fields=["key", "summary", "fixVersions"], 
                max_results=page_size, 
                next_page_token=next_token
            )
            issues = data.get("issues", []) or data.get("values", [])
            if not issues:
                break
            
            for issue in issues:
                fields = issue.get("fields", {})
                fix_versions = fields.get("fixVersions", [])
                
                # Берем первую версию исправления (обычно их одна)
                if fix_versions and len(fix_versions) > 0:
                    version = fix_versions[0]
                    release_date = None
                    
                    # Получаем дату релиза из версии
                    if isinstance(version, dict):
                        release_date_str = version.get("releaseDate")
                        if release_date_str:
                            try:
                                # Формат даты в Jira: "2025-12-31"
                                release_date = datetime.strptime(release_date_str, "%Y-%m-%d").date()
                            except:
                                pass
                        
                        version_name = version.get("name", "")
                    else:
                        version_name = str(version)
                    
                    if release_date:
                        all_releases.append({
                            "epic_key": issue.get("key", ""),
                            "epic_summary": fields.get("summary", ""),
                            "release_date": release_date.strftime("%Y-%m-%d"),
                            "release_date_obj": release_date.isoformat(),  # Для сортировки
                            "version_name": version_name,
                        })
            
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token:
                break
        
        # Сортируем по дате релиза (от ближайших к поздним)
        all_releases.sort(key=lambda x: x["release_date_obj"])
        
        return JSONResponse({
            "success": True,
            "data": all_releases,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Releases error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.post("/api/epics/{epic_key}/release-date")
def api_update_release_date(request: Request, epic_key: str, request_data: dict = Body(...), db: Session = Depends(get_db)):
    """API endpoint для обновления даты релиза эпика."""
    from fastapi.responses import JSONResponse
    
    release_date = request_data.get("release_date", "")
    if not release_date:
        return JSONResponse(
            {"success": False, "error": "Дата релиза не указана"},
            status_code=400,
        )
    
    try:
        jira, api_prefix, _cred = get_jira_client_for_request(request, db)
        
        # Получаем текущие fixVersions эпика
        issue_response = jira.request("GET", f"{api_prefix}/issue/{epic_key}?fields=fixVersions")
        if issue_response.status_code != 200:
            return JSONResponse(
                {"success": False, "error": f"Не удалось получить данные задачи: {issue_response.status_code}"},
                status_code=500,
            )
        
        issue_data = issue_response.json()
        fields = issue_data.get("fields", {})
        fix_versions = fields.get("fixVersions", [])
        
        if not fix_versions:
            return JSONResponse(
                {"success": False, "error": "У эпика нет версии исправления"},
                status_code=400,
            )
        
        # Берем первую версию
        version = fix_versions[0]
        version_id = version.get("id") if isinstance(version, dict) else None
        
        if not version_id:
            return JSONResponse(
                {"success": False, "error": "Не удалось получить ID версии"},
                status_code=400,
            )
        
        # Обновляем дату релиза версии
        # Формат даты: YYYY-MM-DD
        update_data = {
            "releaseDate": release_date
        }
        
        update_response = jira.request("PUT", f"{api_prefix}/version/{version_id}", json_body=update_data)
        
        if update_response.status_code not in (200, 204):
            return JSONResponse(
                {"success": False, "error": f"Не удалось обновить дату релиза: {update_response.status_code} - {update_response.text}"},
                status_code=500,
            )
        
        return JSONResponse({
            "success": True,
            "message": "Дата релиза обновлена",
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Update release date error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.get("/api/teams/{team_id}/done")
def api_team_done(request: Request, team_id: int, user_id: str, period: str = "today", db: Session = Depends(get_db)):
    """API endpoint для получения выполненных задач команды."""
    from fastapi.responses import JSONResponse
    from datetime import datetime, timedelta
    from dateutil import parser
    
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)
        
        # Получаем пользователя из БД
        user = db.query(User).filter(User.jira_account_id == user_id).first()
        
        if not user:
            return JSONResponse(
                {"success": False, "error": "Пользователь не найден"},
                status_code=404,
            )

        # пользователь должен принадлежать этому credential
        cu = db.scalar(
            select(CredentialUser).where(CredentialUser.credential_id == cred.id, CredentialUser.user_id == user.id)
        )
        if cu is None:
            return JSONResponse({"success": False, "error": "Пользователь не найден"}, status_code=404)
        
        # Определяем дату начала периода
        today = datetime.now().date()
        if period == "today":
            start_date = today
            end_date = today
        elif period == "yesterday":
            start_date = today - timedelta(days=1)
            end_date = start_date
        elif period == "week":
            start_date = today - timedelta(days=7)
            end_date = today
        else:
            start_date = today
            end_date = today
        
        # Формируем JQL запрос
        # Используем accountId для поиска
        account_id = user.jira_account_id
        if not account_id:
            return JSONResponse(
                {"success": False, "error": "У пользователя нет Jira account ID"},
                status_code=400,
            )
        jql = f'assignee = "{account_id}" AND status = Done'
        
        # Добавляем фильтр по дате завершения (resolved)
        jql += f' AND resolved >= "{start_date}" AND resolved <= "{end_date}"'
        jql += ' ORDER BY resolved DESC'
        
        # Получаем задачи
        all_tasks = []
        next_token = ""
        page_size = 200
        
        while True:
            data = jira.search_jql_page(
                jql=jql,
                fields=["key", "summary", "status", "resolved"],
                max_results=page_size,
                next_page_token=next_token
            )
            issues = data.get("issues", []) or data.get("values", [])
            if not issues:
                break
            
            for issue in issues:
                fields = issue.get("fields", {})
                resolved_str = fields.get("resolved")
                resolved_date = None
                
                if resolved_str:
                    try:
                        # Парсим дату из Jira формата (может быть ISO или другой формат)
                        if isinstance(resolved_str, str):
                            # Убираем миллисекунды и таймзону для упрощения
                            date_str = resolved_str.split('.')[0].split('+')[0].split('Z')[0]
                            # Пробуем разные форматы
                            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
                                try:
                                    resolved_date = datetime.strptime(date_str, fmt).date()
                                    break
                                except:
                                    continue
                            if not resolved_date:
                                # Пробуем через dateutil как fallback
                                try:
                                    resolved_date = parser.parse(resolved_str).date()
                                except:
                                    pass
                    except Exception as e:
                        print(f"Error parsing date {resolved_str}: {e}")
                        pass
                
                all_tasks.append({
                    "key": issue.get("key", ""),
                    "summary": fields.get("summary", ""),
                    "resolved_date": resolved_date.strftime("%Y-%m-%d") if resolved_date else None,
                })
            
            next_token = data.get("nextPageToken", "")
            if not next_token:
                break
        
        return JSONResponse({
            "success": True,
            "data": all_tasks,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Get done tasks error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.get("/api/teams/{team_id}/users")
def api_team_users(request: Request, team_id: int, db: Session = Depends(get_db)):
    """API endpoint для получения пользователей команды."""
    from fastapi.responses import JSONResponse
    
    try:
        cred = get_credential_from_session(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)

        team = db.scalar(select(Team).where(Team.id == team_id))
        if not team:
            return JSONResponse(
                {"success": False, "error": "Команда не найдена"},
                status_code=404,
            )
        
        members = db.query(TeamMember).filter(TeamMember.team_id == team_id).all()
        user_ids = {m.user_id for m in members}
        # фильтруем пользователей по credential
        allowed_user_ids = {
            cu.user_id
            for cu in db.scalars(select(CredentialUser).where(CredentialUser.credential_id == cred.id)).all()
        }
        user_ids = user_ids.intersection(allowed_user_ids)
        users = db.query(User).filter(User.id.in_(user_ids)).all()
        
        users_data = []
        for user in users:
            users_data.append({
                "id": user.id,
                "name": user.display_name or "",
                "display_name": user.display_name or "",
                "jira_account_id": user.jira_account_id or "",
            })
        
        return JSONResponse({
            "success": True,
            "data": users_data,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Get team users error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.get("/api/teams/{team_id}/no-release")
def api_team_no_release(request: Request, team_id: int, user_id: str = "", db: Session = Depends(get_db)):
    """API endpoint для получения задач без релиза."""
    from fastapi.responses import JSONResponse
    from datetime import datetime
    
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)

        if user_id:
            user = db.query(User).filter(User.jira_account_id == user_id).first()
            if user is None:
                return JSONResponse({"success": False, "error": "Пользователь не найден"}, status_code=404)
            cu = db.scalar(
                select(CredentialUser).where(CredentialUser.credential_id == cred.id, CredentialUser.user_id == user.id)
            )
            if cu is None:
                return JSONResponse({"success": False, "error": "Пользователь не найден"}, status_code=404)
        
        # Формируем JQL запрос
        jql = 'project = TNL AND status = "QA Done" AND fixVersion IS EMPTY'
        
        # Если выбран конкретный сотрудник, добавляем фильтр по assignee
        if user_id:
            jql += f' AND assignee = "{user_id}"'
        
        jql += ' ORDER BY created DESC'
        
        # Получаем задачи
        all_tasks = []
        next_token = ""
        page_size = 200
        
        while True:
            data = jira.search_jql_page(
                jql=jql,
                fields=["key", "summary", "assignee", "created"],
                max_results=page_size,
                next_page_token=next_token
            )
            issues = data.get("issues", []) or data.get("values", [])
            if not issues:
                break
            
            for issue in issues:
                fields = issue.get("fields", {})
                assignee = fields.get("assignee")
                assignee_name = ""
                
                if assignee:
                    if isinstance(assignee, dict):
                        assignee_name = assignee.get("displayName", "") or assignee.get("name", "")
                    else:
                        assignee_name = str(assignee)
                
                created_str = fields.get("created", "")
                created_date = None
                
                if created_str:
                    try:
                        # Парсим дату из Jira формата
                        if isinstance(created_str, str):
                            date_str = created_str.split('.')[0].split('+')[0].split('Z')[0]
                            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
                                try:
                                    created_date = datetime.strptime(date_str, fmt)
                                    break
                                except:
                                    continue
                    except Exception as e:
                        print(f"Error parsing created date {created_str}: {e}")
                        pass
                
                all_tasks.append({
                    "key": issue.get("key", ""),
                    "summary": fields.get("summary", ""),
                    "assignee": assignee_name,
                    "created": created_date.isoformat() if created_date else None,
                })
            
            next_token = data.get("nextPageToken", "")
            if not next_token:
                break
        
        return JSONResponse({
            "success": True,
            "data": all_tasks,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Get no-release tasks error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.get("/api/teams/{team_id}/improve")
def api_team_improve(request: Request, team_id: int, db: Session = Depends(get_db)):
    """API endpoint для получения задач Improve."""
    from fastapi.responses import JSONResponse
    from datetime import datetime
    
    try:
        # Подключаемся к Jira с ключом из сессии
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)
        
        # JQL запрос для задач Improve
        # assignee может быть пустым ИЛИ текущим пользователем
        jql = 'project = SDCS AND type IN (Улучшение, Проблема) AND (assignee IS EMPTY OR assignee = currentUser()) AND status IN (Согласование) ORDER BY created ASC'
        
        # Получаем задачи
        all_tasks = []
        next_token = ""
        page_size = 200
        
        while True:
            data = jira.search_jql_page(
                jql=jql,
                fields=["key", "summary", "created"],
                max_results=page_size,
                next_page_token=next_token
            )
            issues = data.get("issues", []) or data.get("values", [])
            if not issues:
                break
            
            for issue in issues:
                fields = issue.get("fields", {})
                created_str = fields.get("created", "")
                created_date = None
                
                if created_str:
                    try:
                        # Парсим дату из Jira формата
                        if isinstance(created_str, str):
                            date_str = created_str.split('.')[0].split('+')[0].split('Z')[0]
                            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
                                try:
                                    created_date = datetime.strptime(date_str, fmt)
                                    break
                                except:
                                    continue
                    except Exception as e:
                        print(f"Error parsing created date {created_str}: {e}")
                        pass
                
                all_tasks.append({
                    "key": issue.get("key", ""),
                    "summary": fields.get("summary", ""),
                    "created": created_date.isoformat() if created_date else None,
                })
            
            next_token = data.get("nextPageToken", "")
            if not next_token:
                break
        
        # Получаем сохраненный порядок задач для этого credential
        saved_orders = db.scalars(
            select(ImproveTaskOrder)
            .where(ImproveTaskOrder.credential_id == cred.id)
            .order_by(ImproveTaskOrder.position.asc())
        ).all()
        
        # Создаем словарь: task_key -> position
        order_map = {order.task_key: order.position for order in saved_orders}
        
        # Сортируем задачи: сначала по сохраненному порядку, затем по дате создания
        def sort_key(task):
            key = task["key"]
            if key in order_map:
                return (0, order_map[key])  # Задачи с сохраненным порядком идут первыми
            else:
                # Для новых задач используем дату создания (чем раньше, тем выше)
                created = task.get("created")
                if created:
                    try:
                        return (1, datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp())
                    except:
                        return (2, 0)  # Если не удалось распарсить дату
                return (2, 0)
        
        all_tasks.sort(key=sort_key)
        
        return JSONResponse({
            "success": True,
            "data": all_tasks,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Get improve tasks error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.post("/api/teams/{team_id}/improve/order")
async def api_team_improve_order(request: Request, team_id: int, db: Session = Depends(get_db)):
    """API endpoint для сохранения порядка задач в табе Improve."""
    from fastapi.responses import JSONResponse
    
    try:
        cred = get_credential_from_session(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)
        
        # Получаем массив ключей задач в новом порядке
        body = await request.json()
        task_keys = body.get("task_keys", [])
        if not isinstance(task_keys, list):
            return JSONResponse({"success": False, "error": "task_keys должен быть массивом"}, status_code=400)
        
        # Удаляем старые записи для этого credential
        db.execute(delete(ImproveTaskOrder).where(ImproveTaskOrder.credential_id == cred.id))
        
        # Создаем новые записи с новым порядком
        for position, task_key in enumerate(task_keys):
            if task_key:
                order_entry = ImproveTaskOrder(
                    credential_id=cred.id,
                    task_key=str(task_key),
                    position=position
                )
                db.add(order_entry)
        
        db.commit()
        
        return JSONResponse({
            "success": True,
            "message": "Порядок сохранен",
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Save improve order error: {traceback.format_exc()}")
        db.rollback()
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.get("/api/epics/{epic_key}/issues")
def api_epic_issues(request: Request, epic_key: str, db: Session = Depends(get_db)):
    """API endpoint для получения задач эпика."""
    from fastapi.responses import JSONResponse
    
    try:
        # достаточно просто авторизации
        jira, api_prefix, _cred = get_jira_client_for_request(request, db)
        
        # JQL запрос для задач эпика (используем parent или "Epic Link")
        # Пробуем оба варианта
        jql = f'parent = {epic_key} OR "Epic Link" = {epic_key}'
        
        # Получаем задачи
        all_issues = []
        next_token = ""
        page_size = 200
        
        while True:
            data = jira.search_jql_page(
                jql=jql, 
                fields=["key", "summary", "assignee", "timeoriginalestimate", "timespent"], 
                max_results=page_size, 
                next_page_token=next_token
            )
            issues = data.get("issues", []) or data.get("values", [])
            if not issues:
                break
            
            for issue in issues:
                fields = issue.get("fields", {})
                
                # Получаем ответственного
                assignee = fields.get("assignee")
                assignee_name = ""
                if isinstance(assignee, dict):
                    assignee_name = assignee.get("displayName", assignee.get("name", ""))
                elif assignee:
                    assignee_name = str(assignee)
                
                # Получаем исходную оценку (в секундах)
                time_original_estimate = fields.get("timeoriginalestimate", 0) or 0
                original_estimate_hours = time_original_estimate / 3600.0 if time_original_estimate else 0
                
                # Получаем списанное время (в секундах)
                time_spent = fields.get("timespent", 0) or 0
                time_spent_hours = time_spent / 3600.0 if time_spent else 0
                
                issue_data = {
                    "key": issue.get("key", ""),
                    "summary": fields.get("summary", ""),
                    "assignee": assignee_name,
                    "original_estimate_hours": round(original_estimate_hours, 2),
                    "time_spent_hours": round(time_spent_hours, 2),
                }
                all_issues.append(issue_data)
            
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token:
                break
        
        return JSONResponse({
            "success": True,
            "data": all_issues,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Epic issues error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.get("/api/teams/{team_id}/gantt")
def api_team_gantt(request: Request, team_id: int, db: Session = Depends(get_db)):
    """API endpoint для получения данных эпиков и задач для диаграммы Ганта."""
    from fastapi.responses import JSONResponse
    
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)
        
        # JQL запрос для эпиков
        jql = 'project = TNL AND type = Epic AND status NOT IN (Отменено, Done) AND assignee = currentUser() ORDER BY status ASC, updated ASC, parent DESC, created DESC'
        
        # Получаем эпики
        all_epics = []
        epic_keys = []
        epic_map = {}
        next_token = ""
        page_size = 200
        
        while True:
            data = jira.search_jql_page(
                jql=jql,
                fields=["key", "summary", "priority"],
                max_results=page_size,
                next_page_token=next_token
            )
            issues = data.get("issues", []) or data.get("values", [])
            if not issues:
                break
            
            for issue in issues:
                fields = issue.get("fields", {})
                priority = fields.get("priority", {})
                priority_name = priority.get("name", "") if isinstance(priority, dict) else str(priority)
                
                epic_key = issue.get("key", "")
                epic = {
                    "id": issue.get("id", ""),
                    "key": epic_key,
                    "summary": fields.get("summary", ""),
                    "priority": priority_name,
                    "tasks": [],
                }
                
                epic_keys.append(epic_key)
                epic_map[epic_key] = epic
                all_epics.append(epic)
            
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token:
                break
        
        # Теперь получаем все задачи всех эпиков одним запросом
        if epic_keys:
            # Строим JQL для всех задач эпиков
            # Используем OR для всех эпиков, но ограничим количество для избежания слишком длинных запросов
            # Если эпиков слишком много, разобьем на батчи
            batch_size = 50  # Jira может иметь ограничения на длину JQL
            all_tasks = []
            
            for i in range(0, len(epic_keys), batch_size):
                batch_keys = epic_keys[i:i + batch_size]
                # Строим условие для батча
                epic_conditions = []
                for key in batch_keys:
                    epic_conditions.append(f'parent = {key}')
                    epic_conditions.append(f'"Epic Link" = {key}')
                
                # Объединяем условия через OR
                conditions_str = ' OR '.join(epic_conditions)
                tasks_jql = f'project = TNL AND status != "Отменено" AND ({conditions_str})'
                
                try:
                    tasks_next_token = ""
                    while True:
                        # Запрашиваем все поля, чтобы найти Epic Link
                        # Используем * для получения всех полей, но это может быть медленно
                        # Альтернатива - запросить конкретные поля, но Epic Link может иметь разный ID
                        tasks_data = jira.search_jql_page(
                            jql=tasks_jql,
                            fields=["key", "summary", "components", "assignee", "timeoriginalestimate", "parent", "issuetype", "status"],
                            max_results=200,
                            next_page_token=tasks_next_token
                        )
                        batch_tasks = tasks_data.get("issues", []) or tasks_data.get("values", [])
                        if not batch_tasks:
                            break
                        all_tasks.extend(batch_tasks)
                        tasks_next_token = (tasks_data.get("nextPageToken") or "").strip()
                        if not tasks_next_token:
                            break
                except Exception as e:
                    print(f"Error fetching tasks batch {i}-{i+len(batch_keys)}: {e}")
            
            # Распределяем задачи по эпикам
            for task in all_tasks:
                task_fields = task.get("fields", {})
                
                # Определяем, к какому эпику относится задача
                epic_key = None
                parent = task_fields.get("parent")
                if parent:
                    if isinstance(parent, dict):
                        parent_key = parent.get("key", "")
                        if parent_key in epic_map:
                            epic_key = parent_key
                
                # Если не нашли через parent, задача могла попасть в результаты через "Epic Link" в JQL
                # Но в ответе API может не быть самого поля Epic Link
                # В этом случае проверяем, есть ли ключ задачи в списке эпиков (маловероятно, но на всякий случай)
                if not epic_key:
                    task_key = task.get("key", "")
                    # Если задача сама является эпиком из нашего списка, пропускаем
                    if task_key not in epic_map:
                        # Задача попала в результаты, но мы не можем определить её эпик
                        # Это может быть из-за того, что Epic Link не возвращается в fields
                        # Пропускаем эту задачу
                        continue
                
                if epic_key and epic_key in epic_map:
                    # Проверяем статус - исключаем задачи со статусом "Отменено"
                    status = task_fields.get("status", {})
                    status_name = ""
                    if isinstance(status, dict):
                        status_name = status.get("name", "")
                    elif isinstance(status, str):
                        status_name = status
                    
                    if status_name and "Отменено" in status_name:
                        continue  # Пропускаем отмененные задачи
                    
                    # Получаем тип задачи
                    issue_type = task_fields.get("issuetype", {})
                    issue_type_name = ""
                    if isinstance(issue_type, dict):
                        issue_type_name = issue_type.get("name", "")
                    elif isinstance(issue_type, str):
                        issue_type_name = issue_type
                    
                    # Получаем компоненты
                    components = task_fields.get("components", [])
                    component_names = [c.get("name", "") if isinstance(c, dict) else str(c) for c in components]
                    
                    # Получаем исполнителей
                    assignee = task_fields.get("assignee")
                    assignee_account_ids = []
                    
                    if assignee:
                        if isinstance(assignee, list):
                            for a in assignee:
                                if isinstance(a, dict):
                                    account_id = a.get("accountId", "")
                                    if account_id:
                                        assignee_account_ids.append(account_id)
                        elif isinstance(assignee, dict):
                            account_id = assignee.get("accountId", "")
                            if account_id:
                                assignee_account_ids.append(account_id)
                    
                    # Получаем исходную оценку в часах
                    time_original_estimate = task_fields.get("timeoriginalestimate", 0) or 0
                    original_estimate_hours = time_original_estimate / 3600.0 if time_original_estimate else 0
                    
                    epic_map[epic_key]["tasks"].append({
                        "id": task.get("id", ""),
                        "key": task.get("key", ""),
                        "summary": task_fields.get("summary", ""),
                        "components": component_names,
                        "assignees": assignee_account_ids,
                        "originalEstimate": round(original_estimate_hours, 2),
                        "type": issue_type_name,
                        "status": status_name,
                    })
        
        return JSONResponse({
            "success": True,
            "data": all_epics,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Gantt error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.get("/api/teams/{team_id}/gantt/state")
def api_team_gantt_state(request: Request, team_id: int, db: Session = Depends(get_db)):
    """API endpoint для загрузки сохраненного состояния диаграммы Ганта."""
    from fastapi.responses import JSONResponse
    import json
    
    try:
        cred = get_credential_from_session(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)
        
        gantt_state = db.scalar(
            select(GanttState).where(
                GanttState.credential_id == cred.id,
                GanttState.team_id == team_id
            )
        )
        
        if gantt_state:
            state_data = json.loads(gantt_state.state_data)
            expanded_epics = state_data.get("expandedEpics", {})
            # Убираем expandedEpics из state, чтобы не дублировать
            state_without_expanded = {k: v for k, v in state_data.items() if k != "expandedEpics"}
            return JSONResponse({
                "success": True,
                "data": {
                    "state": state_without_expanded,
                    "autoMode": gantt_state.auto_mode,
                    "expandedEpics": expanded_epics,
                },
            })
        else:
            return JSONResponse({
                "success": True,
                "data": {
                    "state": {"tasks": {}, "connections": []},
                    "autoMode": False,
                    "expandedEpics": {},
                },
            })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Gantt state load error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


@app.post("/api/teams/{team_id}/gantt/state")
def api_team_gantt_state_save(request: Request, team_id: int, db: Session = Depends(get_db), body: dict = Body(...)):
    """API endpoint для сохранения состояния диаграммы Ганта."""
    from fastapi.responses import JSONResponse
    import json
    
    try:
        cred = get_credential_from_session(request, db)
        allowed = db.scalar(
            select(CredentialTeam).where(CredentialTeam.credential_id == cred.id, CredentialTeam.team_id == team_id)
        )
        if allowed is None:
            return JSONResponse({"success": False, "error": "Команда не найдена"}, status_code=404)
        
        state_data = body.get("state", {})
        auto_mode = body.get("autoMode", False)
        expanded_epics = body.get("expandedEpics", {})
        
        # Включаем expandedEpics в state_data для сохранения
        if expanded_epics:
            state_data["expandedEpics"] = expanded_epics
        
        gantt_state = db.scalar(
            select(GanttState).where(
                GanttState.credential_id == cred.id,
                GanttState.team_id == team_id
            )
        )
        
        state_json = json.dumps(state_data)
        
        if gantt_state:
            gantt_state.state_data = state_json
            gantt_state.auto_mode = auto_mode
        else:
            gantt_state = GanttState(
                credential_id=cred.id,
                team_id=team_id,
                state_data=state_json,
                auto_mode=auto_mode,
            )
            db.add(gantt_state)
        
        db.commit()
        
        return JSONResponse({
            "success": True,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Gantt state save error: {traceback.format_exc()}")
        db.rollback()
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


