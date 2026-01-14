"""
API endpoints для пользовательских команд (CustomTeam)
Добавить в конец main.py
"""

@app.post("/api/custom-teams")
def api_custom_teams_create(request: Request, db: Session = Depends(get_db), body: dict = Body(...)):
    """API endpoint для создания пользовательской команды."""
    from fastapi.responses import JSONResponse
    
    try:
        app_user = get_app_user_from_session(request, db)
    except RuntimeError as e:
        return JSONResponse(
            {"success": False, "error": "Не авторизован"},
            status_code=401,
        )
    except Exception as e:
        import traceback
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )
    
    try:
        name = body.get("name", "").strip()
        if not name:
            return JSONResponse({"success": False, "error": "Название команды обязательно"}, status_code=400)
        if len(name) > 30:
            return JSONResponse({"success": False, "error": "Название команды не должно превышать 30 символов"}, status_code=400)
        
        # Проверяем, нет ли уже команды с таким именем у этого пользователя
        existing = db.scalar(
            select(CustomTeam).where(CustomTeam.app_user_id == app_user.id, CustomTeam.name == name)
        )
        if existing:
            return JSONResponse({"success": False, "error": "Команда с таким названием уже существует"}, status_code=400)
        
        custom_team = CustomTeam(
            app_user_id=app_user.id,
            name=name
        )
        db.add(custom_team)
        db.commit()
        db.refresh(custom_team)
        
        return JSONResponse({
            "success": True,
            "data": {"id": custom_team.id, "name": custom_team.name}
        })
    except Exception as e:
        db.rollback()
        import traceback
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )


@app.get("/api/custom-teams")
def api_custom_teams_list(request: Request, db: Session = Depends(get_db)):
    """API endpoint для получения списка пользовательских команд."""
    from fastapi.responses import JSONResponse
    
    try:
        app_user = get_app_user_from_session(request, db)
    except RuntimeError as e:
        return JSONResponse({
            "success": True,
            "data": [],
        })
    except Exception as e:
        import traceback
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
        
        return JSONResponse({
            "success": True,
            "data": [{"id": ct.id, "name": ct.name} for ct in custom_teams],
        })
    except Exception as e:
        import traceback
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )
