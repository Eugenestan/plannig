"""
Новая логика авторизации для вставки в main.py
"""

def verify_api_key_new(request: Request, api_key: str = Form(...), email: str = Form(...)):
    """
    Новая логика авторизации:
    1. Проверяет API ключ и email
    2. Проверяет наличие поля TEAM в Jira
    3. Находит или создает AppUser по email
    4. Создает или обновляет ApiCredential, привязав к AppUser
    5. Синхронизирует команды и всех пользователей Jira
    6. Перенаправляет на страницу выбора команд
    """
    from .db import SessionLocal
    from .jira_client import find_field_id
    import uuid

    api_key = (api_key or "").strip()
    email = (email or "").strip()
    
    if not api_key:
        return RedirectResponse(url="/?error=" + "Ключ не может быть пустым", status_code=303)
    if not email:
        return RedirectResponse(url="/?error=" + "Email не может быть пустым", status_code=303)

    db = SessionLocal()
    try:
        # 1) Проверяем API ключ и подключаемся к Jira
        try:
            jira, api_prefix = build_jira_client_from_api_key(api_key, email=email)
            test_response = jira.request("GET", f"{api_prefix}/serverInfo")
            if test_response.status_code != 200:
                error_text = test_response.text[:200] if test_response.text else ""
                return RedirectResponse(
                    url="/?error=" + f"Ключ не подходит (HTTP {test_response.status_code}): {error_text}", 
                    status_code=303
                )
        except RuntimeError as e:
            error_msg = str(e)
            return RedirectResponse(
                url="/?error=" + f"Ошибка проверки ключа: {error_msg}", 
                status_code=303
            )
        except Exception as e:
            import traceback
            error_msg = str(e)
            print(f"Error validating API key: {error_msg}")
            print(traceback.format_exc())
            return RedirectResponse(
                url="/?error=" + f"Ключ не подходит: {error_msg}", 
                status_code=303
            )

        # 2) Проверяем наличие поля TEAM
        try:
            fields = jira.get_fields(api_prefix)
            team_field_id = find_field_id(fields, "TEAM")
            print(f"Поле TEAM найдено: {team_field_id}")
        except RuntimeError as e:
            error_msg = str(e)
            return RedirectResponse(
                url="/?error=" + f"Поле TEAM не найдено в Jira. {error_msg}", 
                status_code=303
            )
        except Exception as e:
            import traceback
            error_msg = str(e)
            print(f"Error checking TEAM field: {error_msg}")
            print(traceback.format_exc())
            return RedirectResponse(
                url="/?error=" + f"Ошибка проверки поля TEAM: {error_msg}", 
                status_code=303
            )

        # 3) Находим или создаем AppUser по email
        app_user = db.scalar(select(AppUser).where(AppUser.email == email))
        if app_user is None:
            app_user = AppUser(email=email)
            db.add(app_user)
            db.flush()
            print(f"Создан новый AppUser: {email}")
        else:
            print(f"Найден существующий AppUser: {email}")

        # 4) Создаем или обновляем ApiCredential, привязав к AppUser
        session_key = _get_session_key(request)
        if not session_key:
            session_key = uuid.uuid4().hex
            request.session["session_key"] = session_key

        cred = db.scalar(select(ApiCredential).where(ApiCredential.session_key == session_key))
        if cred is None:
            cred = ApiCredential(
                app_user_id=app_user.id,
                session_key=session_key,
                jira_api_key=api_key,
                jira_email=email
            )
            db.add(cred)
        else:
            # Обновляем credential, но привязываем к тому же AppUser (если email совпадает)
            cred.jira_api_key = api_key
            cred.jira_email = email
            cred.app_user_id = app_user.id
        db.flush()

        # 5) Синхронизируем команды и всех пользователей Jira
        try:
            sync_result = sync_from_jira_for_credential(
                db, 
                credential_id=cred.id, 
                jira=jira, 
                api_prefix=api_prefix, 
                clear_existing_links=True,
                sync_all_users=True  # Синхронизируем всех пользователей Jira
            )
            print(f"Sync completed: {sync_result}")
        except Exception as sync_error:
            import traceback
            error_msg = str(sync_error)
            print(f"Warning: Failed to sync teams/users: {sync_error}")
            print(traceback.format_exc())
            return RedirectResponse(
                url="/?error=" + f"Ошибка синхронизации команд: {error_msg}", 
                status_code=303
            )

        db.commit()
        return RedirectResponse(url="/", status_code=303)
    finally:
        db.close()
