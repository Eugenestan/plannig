# Ручная замена для Todo endpoints

Файл `main.py` может быть открыт в редакторе и не сохраняется автоматически. Выполните следующие замены вручную:

## Замены для всех Todo endpoints (функции `api_todo_*`):

1. **Заменить `cred = get_credential_from_session` на `app_user = get_app_user_from_session`** во всех Todo функциях:
   - `api_todo_lists` (строка ~1468)
   - `api_todo_lists_create` (строка ~1508)
   - `api_todo_lists_update` (строка ~1560)
   - `api_todo_lists_delete` (строка ~1599)
   - `api_todo_tasks` (строка ~1637)
   - `api_todo_tasks_create` (строка ~1735)
   - `api_todo_tasks_get` (строка ~1800)
   - `api_todo_tasks_update` (строка ~1867)
   - `api_todo_tasks_delete` (строка ~1921)
   - `api_todo_subtasks_create` (строка ~1958)
   - `api_todo_subtasks_update` (строка ~2016)
   - `api_todo_subtasks_delete` (строка ~2059)

2. **Заменить `TodoList.credential_id == cred.id` на `TodoList.app_user_id == app_user.id`**

3. **Заменить `TodoTask.credential_id == cred.id` на `TodoTask.app_user_id == app_user.id`**

4. **Заменить `credential_id=cred.id` на `app_user_id=app_user.id`** при создании объектов `TodoList` и `TodoTask`

## Используйте поиск и замену в редакторе:

1. Откройте `main.py` в редакторе
2. Найдите все вхождения `cred = get_credential_from_session` внутри функций `api_todo_*`
3. Замените на `app_user = get_app_user_from_session`
4. Найдите все вхождения `TodoList.credential_id == cred.id` и замените на `TodoList.app_user_id == app_user.id`
5. Найдите все вхождения `TodoTask.credential_id == cred.id` и замените на `TodoTask.app_user_id == app_user.id`
6. Найдите все вхождения `credential_id=cred.id` в контексте `TodoList(` или `TodoTask(` и замените на `app_user_id=app_user.id`
