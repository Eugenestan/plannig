from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import CustomTeam

router = APIRouter()


@router.post("/api/custom-teams")
def api_custom_teams_create(
    request: Request,
    db: Session = Depends(get_db),
    body: dict = Body(...),
):
    """API endpoint для создания пользовательской команды."""
    # Локальный импорт исключает циклическую зависимость с main.py
    from .main import get_app_user_from_session

    try:
        app_user = get_app_user_from_session(request, db)
    except RuntimeError:
        return JSONResponse(
            {"success": False, "error": "Не авторизован"},
            status_code=401,
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )

    try:
        name = body.get("name", "").strip()
        if not name:
            return JSONResponse({"success": False, "error": "Название команды обязательно"}, status_code=400)
        if len(name) > 30:
            return JSONResponse(
                {"success": False, "error": "Название команды не должно превышать 30 символов"},
                status_code=400,
            )

        existing = db.scalar(
            select(CustomTeam).where(CustomTeam.app_user_id == app_user.id, CustomTeam.name == name)
        )
        if existing:
            return JSONResponse({"success": False, "error": "Команда с таким названием уже существует"}, status_code=400)

        custom_team = CustomTeam(app_user_id=app_user.id, name=name)
        db.add(custom_team)
        db.commit()
        db.refresh(custom_team)

        return JSONResponse(
            {"success": True, "data": {"id": custom_team.id, "name": custom_team.name}}
        )
    except Exception as e:
        db.rollback()
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )


@router.get("/api/custom-teams")
def api_custom_teams_list(request: Request, db: Session = Depends(get_db)):
    """API endpoint для получения списка пользовательских команд."""
    from .main import get_app_user_from_session

    try:
        app_user = get_app_user_from_session(request, db)
    except RuntimeError:
        return JSONResponse(
            {
                "success": True,
                "data": [],
            }
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )

    try:
        custom_teams = db.scalars(
            select(CustomTeam)
            .where(CustomTeam.app_user_id == app_user.id)
            .order_by(CustomTeam.name.asc())
        ).all()

        return JSONResponse(
            {
                "success": True,
                "data": [{"id": ct.id, "name": ct.name} for ct in custom_teams],
            }
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )
