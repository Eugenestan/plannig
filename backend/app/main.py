from __future__ import annotations

from pathlib import Path
from typing import List
import uuid
import os
import base64
import json
from datetime import datetime, date, timedelta, timedelta

from fastapi import Body, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import delete, select, func
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.exc import SQLAlchemyError

from .db import Base, engine, get_db, SessionLocal
from .models import (
    AppUser, ApiCredential, CredentialTeam, CredentialUser, 
    CustomTeam, GanttState, ImproveTaskOrder, Team, 
    TeamMember, TeamConfig, TodoList, TodoTask, TodoSubtask, User
)
from .sync_jira import credential_has_any_team, sync_from_jira_for_credential
from .worklog_fetcher import get_team_worklog
from .config import settings
from .jira_client import Jira, load_env_file, find_field_id

app = FastAPI(title="Planing - Teams")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, max_age=86400 * 30)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

def _get_session_key(request: Request) -> str:
    if not hasattr(request, "session") or request.session is None:
        request.session = {}
    return (request.session.get("session_key") or "").strip()

def get_credential_from_session(request: Request, db: Session) -> ApiCredential:
    session_key = _get_session_key(request)
    if not session_key:
        raise RuntimeError("Не авторизован. Войдите еще раз.")
    cred = db.scalar(select(ApiCredential).where(ApiCredential.session_key == session_key))
    if cred is None:
        raise RuntimeError("Сессия не найдена. Войдите еще раз.")
    return cred

def get_app_user_from_session(request: Request, db: Session) -> AppUser:
    cred = get_credential_from_session(request, db)
    app_user = db.scalar(select(AppUser).where(AppUser.id == cred.app_user_id))
    if app_user is None:
        raise RuntimeError("Пользователь не найден.")
    print(f"[DEBUG] app_user: id={app_user.id}, email={app_user.email}")
    return app_user

def build_jira_client_from_api_key(api_key: str, email: str | None = None) -> tuple[Jira, str]:
    load_env_file(settings.jira_secrets_file_abs)
    base_url = (os.getenv("JIRA_BASE_URL") or "").strip()
    if not email:
        email = (os.getenv("JIRA_EMAIL") or "").strip()
    if not base_url:
        raise RuntimeError("JIRA_BASE_URL не настроен")

    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("API ключ не может быть пустым")

    headers = {"Accept": "application/json"}
    if email:
        raw = f"{email}:{api_key}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    
    jira = Jira(base_url, headers)
    api_prefix = jira.detect_api_prefix()
    return jira, api_prefix

def get_jira_client_for_request(request: Request, db: Session) -> tuple[Jira, str, ApiCredential]:
    cred = get_credential_from_session(request, db)
    jira, api_prefix = build_jira_client_from_api_key(cred.jira_api_key, email=cred.jira_email)
    return jira, api_prefix, cred

def check_team_access(db: Session, app_user_id: int, team_id: int, is_custom: bool = False):
    if is_custom:
        return db.scalar(select(CustomTeam).where(CustomTeam.id == team_id, CustomTeam.app_user_id == app_user_id))
    else:
        return db.scalar(
            select(Team)
            .join(CredentialTeam, CredentialTeam.team_id == Team.id)
            .join(ApiCredential, ApiCredential.id == CredentialTeam.credential_id)
            .where(ApiCredential.app_user_id == app_user_id, Team.id == team_id)
        )

@app.on_event("startup")
def _startup() -> None:
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
        return templates.TemplateResponse("db_down.html", {"request": request, "error": getattr(app.state, "db_error", "")}, status_code=503)
    
    try:
        app_user = get_app_user_from_session(request, db)
    except RuntimeError:
        error_msg = request.query_params.get("error")
        return templates.TemplateResponse("api_key_form.html", {"request": request, "error_msg": error_msg})
    
    jira_teams = db.scalars(
        select(Team)
        .join(CredentialTeam, CredentialTeam.team_id == Team.id)
        .join(ApiCredential, ApiCredential.id == CredentialTeam.credential_id)
        .where(ApiCredential.app_user_id == app_user.id)
        .distinct()
        .order_by(Team.name.asc())
    ).all()
    
    custom_teams = db.scalars(
        select(CustomTeam)
        .where(CustomTeam.app_user_id == app_user.id)
        .order_by(CustomTeam.name.asc())
    ).all()
    
    print(f"[DEBUG] index: jira_teams count={len(jira_teams)}, custom_teams count={len(custom_teams)}")
    all_teams = []
    for t in jira_teams:
        all_teams.append({"id": t.id, "name": t.name, "is_custom": False})
    for t in custom_teams:
        all_teams.append({"id": t.id, "name": t.name, "is_custom": True})
    
    sync_error = request.query_params.get("sync_error")
    error_msg = getattr(app.state, "sync_error", None) if sync_error else None
    return templates.TemplateResponse("teams.html", {"request": request, "teams": all_teams, "sync_error": error_msg})

@app.post("/sync", response_class=RedirectResponse)
def sync(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        app_user = get_app_user_from_session(request, db)
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        sync_from_jira_for_credential(db, credential_id=cred.id, jira=jira, api_prefix=api_prefix)
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        app.state.sync_error = str(e)
        return RedirectResponse(url="/?sync_error=1", status_code=303)

@app.get("/teams/{team_id}", response_class=HTMLResponse)
def team_detail(request: Request, team_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    is_custom = request.query_params.get("custom") == "1"
    try:
        app_user = get_app_user_from_session(request, db)
        team = check_team_access(db, app_user.id, team_id, is_custom)
        if team is None:
            return templates.TemplateResponse("not_found.html", {"request": request, "message": "Команда не найдена"}, status_code=404)

        all_users = db.scalars(
            select(User)
            .join(CredentialUser, CredentialUser.user_id == User.id)
            .join(ApiCredential, ApiCredential.id == CredentialUser.credential_id)
            .where(ApiCredential.app_user_id == app_user.id)
            .distinct()
            .order_by(User.display_name.asc())
        ).all()
        
        selected_user_ids = set(db.scalars(
            select(TeamConfig.jira_user_id)
            .where(TeamConfig.app_user_id == app_user.id, TeamConfig.team_id == team_id, TeamConfig.is_custom == is_custom)
        ).all())
        
        return templates.TemplateResponse(
            "team_detail.html",
            {
                "request": request,
                "team": team,
                "is_custom": is_custom,
                "all_users": all_users,
                "selected_user_ids": selected_user_ids,
            },
        )
    except RuntimeError:
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        return templates.TemplateResponse("not_found.html", {"request": request, "message": f"Ошибка: {str(e)}"}, status_code=500)

@app.post("/teams/{team_id}/members", response_class=RedirectResponse)
def update_team_members(request: Request, team_id: int, user_ids: List[int] = Form(default=[]), db: Session = Depends(get_db)) -> RedirectResponse:
    is_custom = request.query_params.get("custom") == "1"
    try:
        app_user = get_app_user_from_session(request, db)
        team = check_team_access(db, app_user.id, team_id, is_custom)
        if team is None:
            return RedirectResponse(url="/", status_code=303)

        db.execute(delete(TeamConfig).where(
            TeamConfig.app_user_id == app_user.id, 
            TeamConfig.team_id == team_id,
            TeamConfig.is_custom == is_custom
        ))
        
        allowed_user_ids = set(db.scalars(
            select(CredentialUser.user_id)
            .join(ApiCredential, ApiCredential.id == CredentialUser.credential_id)
            .where(ApiCredential.app_user_id == app_user.id)
        ).all())
        
        for uid in user_ids:
            if uid in allowed_user_ids:
                db.add(TeamConfig(app_user_id=app_user.id, team_id=team_id, jira_user_id=uid, is_custom=is_custom))
                
        db.commit()
        return RedirectResponse(url=f"/teams/{team_id}/dashboard?{'custom=1' if is_custom else ''}", status_code=303)
    except Exception:
        return RedirectResponse(url="/", status_code=303)

@app.get("/teams/{team_id}/dashboard", response_class=HTMLResponse)
def team_dashboard(request: Request, team_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    is_custom = request.query_params.get("custom") == "1"
    try:
        app_user = get_app_user_from_session(request, db)
        team = check_team_access(db, app_user.id, team_id, is_custom)
        if team is None:
            return RedirectResponse(url="/", status_code=303)
        
        days_param = request.query_params.get("days", "today")
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "team": team, "is_custom": is_custom, "days": days_param}
        )
    except RuntimeError:
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        return templates.TemplateResponse("not_found.html", {"request": request, "message": f"Ошибка: {str(e)}"}, status_code=500)

@app.post("/verify-key", response_class=RedirectResponse)
def verify_api_key(request: Request, api_key: str = Form(...), email: str = Form(...)):
    """
    РџСЂРѕРІРµСЂСЏРµС‚ Рё СЃРѕС…СЂР°РЅСЏРµС‚ API РєР»СЋС‡ РІ СЃРµСЃСЃРёРё.
    Р’РђР–РќРћ: РЅР° РїСЂРѕРґРµ СЌС‚Рѕ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ СѓСЃС‚РѕР№С‡РёРІРѕ вЂ” Р±РµР· NameError Рё СЃ Р·Р°РїРѕР»РЅРµРЅРЅС‹Рј app_user_id.
    """
    from .db import SessionLocal

    api_key = (api_key or "").strip()
    email = (email or "").strip()
    if not api_key or not email:
        return RedirectResponse(url="/?error=" + "Р—Р°РїРѕР»РЅРёС‚Рµ email Рё РєР»СЋС‡", status_code=303)

    db = SessionLocal()
    try:
        # 1) Р’Р°Р»РёРґРёСЂСѓРµРј СЃРІСЏР·РєСѓ email+token
        jira, api_prefix = build_jira_client_from_api_key(api_key, email=email)
        test_response = jira.request("GET", f"{api_prefix}/serverInfo")
        if test_response.status_code != 200:
            return RedirectResponse(url="/?error=" + "РќРµРєРѕСЂСЂРµРєС‚РЅС‹Р№ РєР»СЋС‡ РёР»Рё email", status_code=303)

        # 2) Upsert AppUser
        app_user = db.scalar(select(AppUser).where(AppUser.email == email))
        if app_user is None:
            app_user = AppUser(email=email)
            db.add(app_user)
            db.flush()

        # 3) РЎРѕС…СЂР°РЅСЏРµРј credential (РІ СЃРµСЃСЃРёРё вЂ” С‚РѕР»СЊРєРѕ session_key)
        session_key = _get_session_key(request) or uuid.uuid4().hex
        request.session["session_key"] = session_key

        cred = db.scalar(select(ApiCredential).where(ApiCredential.session_key == session_key))
        if cred is None:
            cred = ApiCredential(
                session_key=session_key,
                jira_api_key=api_key,
                jira_email=email,
                app_user_id=app_user.id,
            )
            db.add(cred)
        else:
            cred.jira_api_key = api_key
            cred.jira_email = email
            cred.app_user_id = app_user.id
        db.flush()

        # 4) РЎРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ (РЅРµ РґРѕР»Р¶РЅР° РІР°Р»РёС‚СЊ Р»РѕРіРёРЅ)
        try:
            sync_from_jira_for_credential(db, credential_id=cred.id, jira=jira, api_prefix=api_prefix)
        except Exception:
            pass

        db.commit()
        return RedirectResponse(url="/", status_code=303)
    finally:
        db.close()
@app.get("/logout")
@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

@app.get("/api/teams/{team_id}/worklog")
def api_team_worklog(request: Request, team_id: int, days: str = "today", db: Session = Depends(get_db)):
    is_custom = request.query_params.get("custom") == "1"
    debug = request.query_params.get("debug") == "1"
    try:
        app_user = get_app_user_from_session(request, db)
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        if not check_team_access(db, app_user.id, team_id, is_custom):
            return JSONResponse({"success": False, "error": "Доступ запрещен"}, status_code=403)
        
        debug_out = {} if debug else None
        worklog_data = get_team_worklog(db, team_id, days=days, jira=jira, api_prefix=api_prefix, app_user_id=app_user.id, debug_out=debug_out)
        resp = {"success": True, "data": worklog_data}
        if debug and debug_out is not None:
            resp["debug"] = debug_out
        return JSONResponse(resp)
    except RuntimeError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=401)
    except Exception as e:
        import traceback
        print(f"Worklog error: {traceback.format_exc()}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/teams/{team_id}/epics")
def api_team_epics(request: Request, team_id: int, db: Session = Depends(get_db)):
    is_custom = request.query_params.get("custom") == "1"
    try:
        app_user = get_app_user_from_session(request, db)
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        if not check_team_access(db, app_user.id, team_id, is_custom):
            return JSONResponse({"success": False, "error": "Доступ запрещен"}, status_code=403)
        
        jql = 'project = TNL AND type = Epic AND status NOT IN (Отменено, Done) AND assignee = currentUser() ORDER BY status ASC, updated ASC, parent DESC, created DESC'
        all_epics = []
        next_token = ""
        while True:
            data = jira.search_jql_page(jql=jql, fields=["key", "summary", "status", "updated", "created", "parent"], max_results=100, next_page_token=next_token)
            issues = data.get("issues", []) or data.get("values", [])
            if not issues: break
            for issue in issues:
                f = issue.get("fields", {})
                all_epics.append({
                    "key": issue.get("key"), "summary": f.get("summary"),
                    "status": f.get("status", {}).get("name") if isinstance(f.get("status"), dict) else str(f.get("status")),
                    "updated": f.get("updated"), "created": f.get("created"),
                    "parent": f.get("parent", {}).get("key") if isinstance(f.get("parent"), dict) else str(f.get("parent"))
                })
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token: break
        return JSONResponse({"success": True, "data": all_epics})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/epics/{epic_key}/issues")
def api_epic_issues(request: Request, epic_key: str, db: Session = Depends(get_db)):
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        jql = f'parent = {epic_key} OR "Epic Link" = {epic_key}'
        all_issues = []
        next_token = ""
        while True:
            data = jira.search_jql_page(jql=jql, fields=["key", "summary", "assignee", "timeoriginalestimate", "timespent"], max_results=100, next_page_token=next_token)
            issues = data.get("issues", []) or data.get("values", [])
            if not issues: break
            for issue in issues:
                f = issue.get("fields", {})
                all_issues.append({
                    "key": issue.get("key"), "summary": f.get("summary"),
                    "assignee": f.get("assignee", {}).get("displayName", "") if f.get("assignee") else "",
                    "original_estimate_hours": (f.get("timeoriginalestimate") or 0) / 3600.0,
                    "time_spent_hours": (f.get("timespent") or 0) / 3600.0
                })
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token: break
        return JSONResponse({"success": True, "data": all_issues})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/teams/{team_id}/releases")
def api_team_releases(request: Request, team_id: int, db: Session = Depends(get_db)):
    is_custom = request.query_params.get("custom") == "1"
    try:
        app_user = get_app_user_from_session(request, db)
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        if not check_team_access(db, app_user.id, team_id, is_custom):
            return JSONResponse({"success": False, "error": "Доступ запрещен"}, status_code=403)
        
        jql = 'project = TNL AND type = Epic AND status NOT IN (Отменено, Done) AND assignee = currentUser() AND fixVersion IS NOT EMPTY'
        all_releases = []
        next_token = ""
        while True:
            data = jira.search_jql_page(jql=jql, fields=["key", "summary", "fixVersions"], max_results=100, next_page_token=next_token)
            issues = data.get("issues", []) or data.get("values", [])
            if not issues: break
            for issue in issues:
                f = issue.get("fields", {})
                for v in f.get("fixVersions", []):
                    if v.get("releaseDate"):
                        all_releases.append({
                            "epic_key": issue.get("key"), "epic_summary": f.get("summary"),
                            "release_date": v.get("releaseDate"), "version_name": v.get("name")
                        })
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token: break
        all_releases.sort(key=lambda x: x["release_date"])
        return JSONResponse({"success": True, "data": all_releases})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.post("/api/epics/{epic_key}/release-date")
async def api_update_release_date(request: Request, epic_key: str, db: Session = Depends(get_db)):
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        body = await request.json()
        release_date = body.get("release_date")
        if not release_date: return JSONResponse({"success": False, "error": "Дата не указана"}, status_code=400)
        
        issue_data = jira.request("GET", f"{api_prefix}/issue/{epic_key}?fields=fixVersions").json()
        fix_versions = issue_data.get("fields", {}).get("fixVersions", [])
        if not fix_versions: return JSONResponse({"success": False, "error": "Нет версии"}, status_code=400)
        
        version_id = fix_versions[0].get("id")
        res = jira.request("PUT", f"{api_prefix}/version/{version_id}", json_body={"releaseDate": release_date})
        if res.status_code not in (200, 204): return JSONResponse({"success": False, "error": "Ошибка обновления"}, status_code=500)
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/teams/{team_id}/users")
def api_team_users(request: Request, team_id: int, db: Session = Depends(get_db)):
    is_custom = request.query_params.get("custom") == "1"
    try:
        app_user = get_app_user_from_session(request, db)
        if not check_team_access(db, app_user.id, team_id, is_custom):
            return JSONResponse({"success": False, "error": "Доступ запрещен"}, status_code=403)
        
        user_ids = set(db.scalars(select(TeamConfig.jira_user_id).where(TeamConfig.app_user_id == app_user.id, TeamConfig.team_id == team_id, TeamConfig.is_custom == is_custom)).all())
        users = db.scalars(select(User).where(User.id.in_(user_ids))).all()
        return JSONResponse({"success": True, "data": [{"id": u.id, "display_name": u.display_name, "jira_account_id": u.jira_account_id} for u in users]})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/teams/{team_id}/done")
def api_team_done(request: Request, team_id: int, user_id: str, period: str = "today", db: Session = Depends(get_db)):
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        today = date.today()
        if period == "today": start_date = today
        elif period == "yesterday": start_date = today - timedelta(days=1)
        else: start_date = today - timedelta(days=7)
        
        jql = f'assignee = "{user_id}" AND status = Done AND resolved >= "{start_date}" ORDER BY resolved DESC'
        all_tasks = []
        next_token = ""
        while True:
            data = jira.search_jql_page(jql=jql, fields=["key", "summary", "resolved"], max_results=100, next_page_token=next_token)
            issues = data.get("issues", []) or data.get("values", [])
            if not issues: break
            for issue in issues:
                all_tasks.append({"key": issue.get("key"), "summary": issue.get("fields", {}).get("summary"), "resolved_date": issue.get("fields", {}).get("resolved")})
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token: break
        return JSONResponse({"success": True, "data": all_tasks})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/teams/{team_id}/no-release")
def api_team_no_release(request: Request, team_id: int, user_id: str = "", db: Session = Depends(get_db)):
    try:
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        jql = 'project = TNL AND status = "QA Done" AND fixVersion IS EMPTY'
        if user_id: jql += f' AND assignee = "{user_id}"'
        jql += ' ORDER BY created DESC'
        all_tasks = []
        next_token = ""
        while True:
            data = jira.search_jql_page(jql=jql, fields=["key", "summary", "assignee", "created"], max_results=100, next_page_token=next_token)
            issues = data.get("issues", []) or data.get("values", [])
            if not issues: break
            for issue in issues:
                f = issue.get("fields", {})
                all_tasks.append({"key": issue.get("key"), "summary": f.get("summary"), "assignee": f.get("assignee", {}).get("displayName", "") if f.get("assignee") else "", "created": f.get("created")})
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token: break
        return JSONResponse({"success": True, "data": all_tasks})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/teams/{team_id}/improve")
def api_team_improve(request: Request, team_id: int, db: Session = Depends(get_db)):
    is_custom = request.query_params.get("custom") == "1"
    try:
        app_user = get_app_user_from_session(request, db)
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        if not check_team_access(db, app_user.id, team_id, is_custom):
            return JSONResponse({"success": False, "error": "Доступ запрещен"}, status_code=403)
        
        jql = 'project = SDCS AND type IN (Улучшение, Проблема) AND (assignee IS EMPTY OR assignee = currentUser()) AND status IN (Согласование) ORDER BY created ASC'
        all_tasks = []
        next_token = ""
        while True:
            data = jira.search_jql_page(jql=jql, fields=["key", "summary", "created"], max_results=100, next_page_token=next_token)
            issues = data.get("issues", []) or data.get("values", [])
            if not issues: break
            for issue in issues:
                all_tasks.append({"key": issue.get("key"), "summary": issue.get("fields", {}).get("summary"), "created": issue.get("fields", {}).get("created")})
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token: break
            
        saved_orders = db.scalars(select(ImproveTaskOrder).where(ImproveTaskOrder.app_user_id == app_user.id).order_by(ImproveTaskOrder.position.asc())).all()
        order_map = {o.task_key: o.position for o in saved_orders}
        all_tasks.sort(key=lambda t: (0, order_map[t["key"]]) if t["key"] in order_map else (1, t["created"]))
        return JSONResponse({"success": True, "data": all_tasks})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.post("/api/teams/{team_id}/improve/order")
async def api_team_improve_order(request: Request, team_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        body = await request.json()
        task_keys = body.get("task_keys", [])
        db.execute(delete(ImproveTaskOrder).where(ImproveTaskOrder.app_user_id == app_user.id))
        for pos, key in enumerate(task_keys):
            db.add(ImproveTaskOrder(app_user_id=app_user.id, task_key=str(key), position=pos))
        db.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/teams/{team_id}/gantt")
def api_team_gantt(request: Request, team_id: int, db: Session = Depends(get_db)):
    is_custom = request.query_params.get("custom") == "1"
    try:
        app_user = get_app_user_from_session(request, db)
        jira, api_prefix, cred = get_jira_client_for_request(request, db)
        if not check_team_access(db, app_user.id, team_id, is_custom):
            return JSONResponse({"success": False, "error": "Доступ запрещен"}, status_code=403)
        
        jql = 'project = TNL AND type = Epic AND status NOT IN (Отменено, Done) AND assignee = currentUser() ORDER BY status ASC, updated ASC, parent DESC, created DESC'
        all_epics = []
        epic_keys = []
        epic_map = {}
        next_token = ""
        while True:
            data = jira.search_jql_page(jql=jql, fields=["key", "summary", "priority"], max_results=100, next_page_token=next_token)
            issues = data.get("issues", []) or data.get("values", [])
            if not issues: break
            for issue in issues:
                key = issue.get("key")
                epic = {"id": issue.get("id"), "key": key, "summary": issue.get("fields", {}).get("summary"), "tasks": []}
                all_epics.append(epic)
                epic_keys.append(key)
                epic_map[key] = epic
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token: break
            
        if epic_keys:
            conditions = " OR ".join([f'parent = {k} OR "Epic Link" = {k}' for k in epic_keys])
            tasks_jql = f'project = TNL AND status != "Отменено" AND ({conditions})'
            tasks_data = jira.search_jql_page(jql=tasks_jql, fields=["key", "summary", "assignee", "timeoriginalestimate", "parent", "status", "issuetype", "components"], max_results=500)
            for task in (tasks_data.get("issues", []) or tasks_data.get("values", [])):
                f = task.get("fields", {})
                parent = f.get("parent", {})
                pk = parent.get("key") if isinstance(parent, dict) else None
                if pk in epic_map:
                    epic_map[pk]["tasks"].append({
                        "id": task.get("id"), "key": task.get("key"), "summary": f.get("summary"),
                        "assignees": [f.get("assignee", {}).get("accountId")] if f.get("assignee") else [],
                        "originalEstimate": (f.get("timeoriginalestimate") or 0) / 3600.0,
                        "type": (f.get("issuetype", {}) or {}).get("name", "") if isinstance(f.get("issuetype"), dict) else str(f.get("issuetype", "")),
                        "components": [c.get("name", "") for c in (f.get("components") or []) if isinstance(c, dict)]
                    })
        return JSONResponse({"success": True, "data": all_epics})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/teams/{team_id}/gantt/state")
def api_team_gantt_state(request: Request, team_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        state = db.scalar(select(GanttState).where(GanttState.app_user_id == app_user.id, GanttState.team_id == team_id))
        if state:
            data = json.loads(state.state_data)
            return JSONResponse({"success": True, "data": {"state": {k:v for k,v in data.items() if k != "expandedEpics"}, "autoMode": state.auto_mode, "expandedEpics": data.get("expandedEpics", {})}})
        return JSONResponse({"success": True, "data": {"state": {"tasks": {}, "connections": []}, "autoMode": False, "expandedEpics": {}}})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.post("/api/teams/{team_id}/gantt/state")
async def api_team_gantt_state_save(request: Request, team_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        body = await request.json()
        state_data = body.get("state", {})
        if body.get("expandedEpics"): state_data["expandedEpics"] = body["expandedEpics"]
        state_json = json.dumps(state_data)
        gantt_state = db.scalar(select(GanttState).where(GanttState.app_user_id == app_user.id, GanttState.team_id == team_id))
        if gantt_state:
            gantt_state.state_data = state_json
            gantt_state.auto_mode = body.get("autoMode", False)
        else:
            db.add(GanttState(app_user_id=app_user.id, team_id=team_id, state_data=state_json, auto_mode=body.get("autoMode", False)))
        db.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

# TODO API
@app.get("/api/todo/lists")
def api_todo_lists(request: Request, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        lists = db.scalars(select(TodoList).where(TodoList.app_user_id == app_user.id).order_by(TodoList.position)).all()
        return JSONResponse({"success": True, "data": [{"id": l.id, "name": l.name, "position": l.position} for l in lists]})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.post("/api/todo/lists")
async def api_todo_lists_create(request: Request, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        body = await request.json()
        name = body.get("name", "").strip()
        if not name: return JSONResponse({"success": False, "error": "Имя обязательно"}, status_code=400)
        max_pos = db.scalar(select(func.max(TodoList.position)).where(TodoList.app_user_id == app_user.id)) or -1
        new_list = TodoList(app_user_id=app_user.id, name=name, position=max_pos + 1)
        db.add(new_list)
        db.commit()
        return JSONResponse({"success": True, "data": {"id": new_list.id, "name": new_list.name}})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.patch("/api/todo/lists/{list_id}")
async def api_todo_lists_update(request: Request, list_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        body = await request.json()
        todo_list = db.scalar(select(TodoList).where(TodoList.id == list_id, TodoList.app_user_id == app_user.id))
        if not todo_list: return JSONResponse({"success": False, "error": "Не найдено"}, status_code=404)
        if "name" in body: todo_list.name = body["name"].strip()
        db.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.delete("/api/todo/lists/{list_id}")
def api_todo_lists_delete(request: Request, list_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        todo_list = db.scalar(select(TodoList).where(TodoList.id == list_id, TodoList.app_user_id == app_user.id))
        if not todo_list: return JSONResponse({"success": False, "error": "Не найдено"}, status_code=404)
        db.delete(todo_list)
        db.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/todo/tasks")
def api_todo_tasks(request: Request, db: Session = Depends(get_db), list: str = None):
    try:
        app_user = get_app_user_from_session(request, db)
        query = select(TodoTask).where(TodoTask.app_user_id == app_user.id)
        if list:
            if list.startswith("custom-"): query = query.where(TodoTask.list_id == int(list.replace("custom-", "")))
            elif list == "my-day": query = query.where(((TodoTask.list_type == "my-day") | (TodoTask.due_date == date.today())) & (TodoTask.completed == False))
            elif list == "important": query = query.where(TodoTask.priority == "important", TodoTask.completed == False)
            elif list == "planned": query = query.where(TodoTask.due_date.isnot(None))
            elif list == "completed": query = query.where(TodoTask.completed == True)
        
        tasks = db.scalars(query.order_by(TodoTask.position)).all()
        task_ids = [t.id for t in tasks]
        subtasks_map = {}
        if task_ids:
            all_subtasks = db.scalars(select(TodoSubtask).where(TodoSubtask.task_id.in_(task_ids)).order_by(TodoSubtask.position)).all()
            for st in all_subtasks:
                if st.task_id not in subtasks_map: subtasks_map[st.task_id] = []
                subtasks_map[st.task_id].append({"id": st.id, "name": st.name, "completed": st.completed})
        
        return JSONResponse({"success": True, "data": [{
            "id": t.id, "name": t.name, "completed": t.completed, "priority": t.priority,
            "due_date": t.due_date.isoformat() if t.due_date else None, "notes": t.notes,
            "subtasks": subtasks_map.get(t.id, [])
        } for t in tasks]})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.post("/api/todo/tasks")
async def api_todo_tasks_create(request: Request, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        body = await request.json()
        name = body.get("name", "").strip()
        if not name: return JSONResponse({"success": False, "error": "Имя обязательно"}, status_code=400)
        list_id = body.get("list_id")
        list_type = body.get("list_type")
        max_pos = db.scalar(select(func.max(TodoTask.position)).where(TodoTask.app_user_id == app_user.id)) or -1
        new_task = TodoTask(app_user_id=app_user.id, list_id=list_id, list_type=list_type, name=name, position=max_pos + 1, priority=body.get("priority", "normal"))
        db.add(new_task)
        db.commit()
        return JSONResponse({"success": True, "data": {"id": new_task.id, "name": new_task.name}})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.patch("/api/todo/tasks/{task_id}")
async def api_todo_tasks_update(request: Request, task_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        body = await request.json()
        task = db.scalar(select(TodoTask).where(TodoTask.id == task_id, TodoTask.app_user_id == app_user.id))
        if not task: return JSONResponse({"success": False, "error": "Не найдено"}, status_code=404)
        if "name" in body: task.name = body["name"].strip()
        if "completed" in body: task.completed = body["completed"]
        if "priority" in body: task.priority = body["priority"]
        if "due_date" in body: task.due_date = date.fromisoformat(body["due_date"]) if body["due_date"] else None
        if "notes" in body: task.notes = body["notes"]
        db.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/todo/tasks/{task_id}")
def api_todo_tasks_get(request: Request, task_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        task = db.scalar(select(TodoTask).where(TodoTask.id == task_id, TodoTask.app_user_id == app_user.id).options(selectinload(TodoTask.subtasks)))
        if not task: return JSONResponse({"success": False, "error": "Не найдено"}, status_code=404)
        subtasks_raw = getattr(task, 'subtasks', None)
        if subtasks_raw is None:
            subtasks_list = []
        elif isinstance(subtasks_raw, TodoSubtask):
            subtasks_list = [subtasks_raw]
        else:
            try:
                subtasks_list = list(subtasks_raw)
            except TypeError:
                subtasks_list = [subtasks_raw]
        
        return JSONResponse({"success": True, "data": {
            "id": task.id, "name": task.name, "completed": task.completed, "priority": task.priority,
            "due_date": task.due_date.isoformat() if task.due_date else None, "notes": task.notes,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "subtasks": [{"id": s.id, "name": s.name, "completed": s.completed} for s in subtasks_list]
        }})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.delete("/api/todo/tasks/{task_id}")
def api_todo_tasks_delete(request: Request, task_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        task = db.scalar(select(TodoTask).where(TodoTask.id == task_id, TodoTask.app_user_id == app_user.id))
        if not task: return JSONResponse({"success": False, "error": "Не найдено"}, status_code=404)
        db.delete(task)
        db.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.post("/api/todo/tasks/{task_id}/subtasks")
async def api_todo_subtasks_create(request: Request, task_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        body = await request.json()
        task = db.scalar(select(TodoTask).where(TodoTask.id == task_id, TodoTask.app_user_id == app_user.id))
        if not task: return JSONResponse({"success": False, "error": "Не найдено"}, status_code=404)
        max_pos = db.scalar(select(func.max(TodoSubtask.position)).where(TodoSubtask.task_id == task_id)) or -1
        new_st = TodoSubtask(task_id=task_id, name=body.get("name", "").strip(), position=max_pos + 1)
        db.add(new_st)
        db.commit()
        return JSONResponse({"success": True, "data": {"id": new_st.id, "name": new_st.name}})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.patch("/api/todo/subtasks/{subtask_id}")
async def api_todo_subtasks_update(request: Request, subtask_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        body = await request.json()
        subtask = db.scalar(select(TodoSubtask).join(TodoTask).where(TodoSubtask.id == subtask_id, TodoTask.app_user_id == app_user.id))
        if not subtask: return JSONResponse({"success": False, "error": "Не найдено"}, status_code=404)
        if "name" in body: subtask.name = body["name"].strip()
        if "completed" in body: subtask.completed = body["completed"]
        db.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.delete("/api/todo/subtasks/{subtask_id}")
def api_todo_subtasks_delete(request: Request, subtask_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        subtask = db.scalar(select(TodoSubtask).join(TodoTask).where(TodoSubtask.id == subtask_id, TodoTask.app_user_id == app_user.id))
        if not subtask: return JSONResponse({"success": False, "error": "Не найдено"}, status_code=404)
        db.delete(subtask)
        db.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

# Custom Teams API
@app.post("/api/custom-teams")
async def api_custom_teams_create(request: Request, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        body = await request.json()
        name = body.get("name", "").strip()
        if not name: return JSONResponse({"success": False, "error": "Имя обязательно"}, status_code=400)
        new_team = CustomTeam(app_user_id=app_user.id, name=name)
        db.add(new_team)
        db.commit()
        db.refresh(new_team)
        return JSONResponse({"success": True, "data": {"id": new_team.id, "name": new_team.name}})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/custom-teams")
def api_custom_teams_list(request: Request, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        teams = db.scalars(select(CustomTeam).where(CustomTeam.app_user_id == app_user.id).order_by(CustomTeam.name.asc())).all()
        return JSONResponse({"success": True, "data": [{"id": t.id, "name": t.name} for t in teams]})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.post("/api/custom-teams/{team_id}/members")
async def api_custom_teams_update_members(request: Request, team_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        user_ids = await request.json()
        db.execute(delete(TeamConfig).where(TeamConfig.team_id == team_id, TeamConfig.app_user_id == app_user.id, TeamConfig.is_custom == True))
        for uid in user_ids:
            db.add(TeamConfig(app_user_id=app_user.id, team_id=team_id, jira_user_id=uid, is_custom=True))
        db.commit()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/custom-teams/{team_id}/members")
def api_custom_teams_get_members(request: Request, team_id: int, db: Session = Depends(get_db)):
    try:
        app_user = get_app_user_from_session(request, db)
        members = db.scalars(select(User).join(TeamConfig, TeamConfig.jira_user_id == User.id).where(TeamConfig.team_id == team_id, TeamConfig.app_user_id == app_user.id, TeamConfig.is_custom == True).order_by(User.display_name.asc())).all()
        return JSONResponse({"success": True, "data": [{"id": m.id, "display_name": m.display_name} for m in members]})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

