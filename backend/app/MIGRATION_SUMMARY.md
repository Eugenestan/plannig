# Резюме переработки механизма авторизации

## ✅ Выполнено:

### 1. Модели данных (models.py):
- ✅ Создана модель `AppUser` - пользователь приложения по email
- ✅ Обновлена модель `ApiCredential` - привязана к `AppUser`
- ✅ Обновлены модели `TodoList`, `TodoTask` - привязаны к `AppUser`
- ✅ Обновлена модель `GanttState` - привязана к `AppUser`
- ✅ Обновлена модель `ImproveTaskOrder` - привязана к `AppUser`
- ✅ Создана модель `TeamConfig` - конфигурация команды для каждого пользователя
- ✅ Создана модель `CustomTeam` - пользовательские команды

### 2. Синхронизация (sync_jira.py):
- ✅ Добавлена функция `sync_all_jira_users()` - получает всех пользователей Jira через API
- ✅ Обновлена функция `sync_from_jira_for_credential()` - добавлен параметр `sync_all_users=True`

### 3. Авторизация:
- ✅ Создан файл `new_auth_logic.py` с новой логикой авторизации
- ✅ Логика: проверка API ключа → проверка поля TEAM → создание/поиск AppUser по email → создание ApiCredential

### 4. Frontend:
- ✅ Обновлен `teams.html` - добавлена кнопка "Создать свою команду" с модалкой
- ✅ Добавлен JavaScript для создания пользовательских команд

### 5. API endpoints:
- ✅ Создан файл `custom_teams_api.py` с endpoints для пользовательских команд

## ⚠️ Требуется доработка:

### 1. Функция авторизации (main.py):
- ⚠️ Исправить синтаксические ошибки в `verify_api_key` (строки 289-290)
- ⚠️ Заменить старую логику на новую из `new_auth_logic.py`

### 2. Добавить функцию `get_app_user_from_session`:
```python
def get_app_user_from_session(request: Request, db: Session) -> AppUser:
    """Получает AppUser из сессии через ApiCredential."""
    cred = get_credential_from_session(request, db)
    app_user = db.scalar(select(AppUser).where(AppUser.id == cred.app_user_id))
    if app_user is None:
        raise RuntimeError("Пользователь не найден. Введите ключ на главной странице.")
    return app_user
```

### 3. Обновить все Todo endpoints:
- Заменить `cred = get_credential_from_session(request, db)` на `app_user = get_app_user_from_session(request, db)`
- Заменить `TodoList.credential_id == cred.id` на `TodoList.app_user_id == app_user.id`
- Заменить `TodoTask.credential_id == cred.id` на `TodoTask.app_user_id == app_user.id`
- Заменить `TodoList(credential_id=cred.id, ...)` на `TodoList(app_user_id=app_user.id, ...)`
- Заменить `TodoTask(credential_id=cred.id, ...)` на `TodoTask(app_user_id=app_user.id, ...)`

### 4. Обновить Gantt endpoints:
- Заменить `GanttState.credential_id == cred.id` на `GanttState.app_user_id == app_user.id`
- Заменить `GanttState(credential_id=cred.id, ...)` на `GanttState(app_user_id=app_user.id, ...)`

### 5. Обновить Improve endpoints:
- Заменить `ImproveTaskOrder.credential_id == cred.id` на `ImproveTaskOrder.app_user_id == app_user.id`
- Заменить `ImproveTaskOrder(credential_id=cred.id, ...)` на `ImproveTaskOrder(app_user_id=app_user.id, ...)`

### 6. Обновить endpoint "/" (index):
- Показывать и обычные команды (Team) и пользовательские (CustomTeam)
- Использовать `app_user` вместо `cred` для получения CustomTeam

### 7. Обновить team_detail:
- Использовать `TeamConfig` для хранения выбранных пользователей для каждого AppUser
- При сохранении состава команды создавать записи в `TeamConfig`

### 8. Добавить API endpoints из `custom_teams_api.py` в main.py

### 9. Обновить импорты в main.py:
```python
from .models import ApiCredential, AppUser, CredentialTeam, CredentialUser, CustomTeam, GanttState, ImproveTaskOrder, Team, TeamConfig, TeamMember, TodoList, TodoTask, TodoSubtask, User
```

### 10. Миграция базы данных:
- Создать миграцию для добавления таблиц `app_users`, `team_configs`, `custom_teams`
- Добавить колонку `app_user_id` в `api_credentials`
- Изменить `credential_id` на `app_user_id` в таблицах `todo_lists`, `todo_tasks`, `gantt_state`, `improve_task_order`
- Мигрировать существующие данные (создать AppUser для каждого уникального email из ApiCredential)

## Файлы для вставки:
1. `new_auth_logic.py` - новая функция `verify_api_key`
2. `custom_teams_api.py` - API endpoints для пользовательских команд
3. `endpoints_changes.py` - инструкции по обновлению endpoints
