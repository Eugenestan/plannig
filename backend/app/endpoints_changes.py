"""
Файл с изменениями для endpoints - использовать для массовой замены
"""

# 1. Добавить функцию get_app_user_from_session после get_credential_from_session:

def get_app_user_from_session(request: Request, db: Session) -> AppUser:
    """Получает AppUser из сессии через ApiCredential."""
    cred = get_credential_from_session(request, db)
    app_user = db.scalar(select(AppUser).where(AppUser.id == cred.app_user_id))
    if app_user is None:
        raise RuntimeError("Пользователь не найден. Введите ключ на главной странице.")
    return app_user


# 2. Заменить во всех Todo endpoints:
# cred = get_credential_from_session(request, db)
# на:
# app_user = get_app_user_from_session(request, db)

# 3. Заменить во всех запросах:
# TodoList.credential_id == cred.id
# на:
# TodoList.app_user_id == app_user.id

# TodoTask.credential_id == cred.id
# на:
# TodoTask.app_user_id == app_user.id

# 4. Заменить при создании объектов:
# TodoList(credential_id=cred.id, ...)
# на:
# TodoList(app_user_id=app_user.id, ...)

# TodoTask(credential_id=cred.id, ...)
# на:
# TodoTask(app_user_id=app_user.id, ...)

# GanttState(credential_id=cred.id, ...)
# на:
# GanttState(app_user_id=app_user.id, ...)

# ImproveTaskOrder(credential_id=cred.id, ...)
# на:
# ImproveTaskOrder(app_user_id=app_user.id, ...)
