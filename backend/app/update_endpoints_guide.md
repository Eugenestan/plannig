# Инструкция по обновлению endpoints для работы с AppUser

## Основные изменения:

1. **Заменить `get_credential_from_session` на `get_app_user_from_session`** во всех Todo и Gantt endpoints
2. **Заменить `credential_id` на `app_user_id`** во всех запросах к БД
3. **Обновить модели** - уже сделано в models.py

## Endpoints для обновления:

### Todo API:
- `/api/todo/lists` - GET, POST, DELETE
- `/api/todo/tasks` - GET, POST, PATCH, DELETE
- `/api/todo/tasks/{task_id}/subtasks` - POST, PATCH, DELETE

### Gantt API:
- `/api/teams/{team_id}/gantt/state` - GET, POST

### Improve API:
- Все endpoints, использующие ImproveTaskOrder

## Шаблон замены:

```python
# Старый код:
cred = get_credential_from_session(request, db)
query = select(TodoList).where(TodoList.credential_id == cred.id)

# Новый код:
app_user = get_app_user_from_session(request, db)
query = select(TodoList).where(TodoList.app_user_id == app_user.id)
```

## Создание объектов:

```python
# Старый код:
todo_list = TodoList(credential_id=cred.id, name=name, position=position)

# Новый код:
todo_list = TodoList(app_user_id=app_user.id, name=name, position=position)
```
