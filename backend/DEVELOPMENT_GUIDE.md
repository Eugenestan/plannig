# Руководство по разработке: Как избежать ошибок при рефакторинге

## Проблема: Рассинхрон между моделями и кодом

После миграции с `credential_id` на `app_user_id` в моделях (`ImproveTaskOrder`, `GanttState`, `TodoList`, `TodoTask`) код продолжал использовать старые поля, что приводило к ошибкам типа:
- `type object 'ImproveTaskOrder' has no attribute 'credential_id'`
- `type object 'GanttState' has no attribute 'credential_id'`
- `type object 'TodoTask' has no attribute 'credential_id'`

## Как избежать таких ошибок в будущем

### 1. **Перед любым рефакторингом моделей**

#### Шаг 1: Проверь все использования модели
```bash
# Найди все места, где используется модель
grep -r "ImproveTaskOrder\|GanttState\|TodoList\|TodoTask" --include="*.py" plannig/backend/app/
```

#### Шаг 2: Составь список всех полей модели
```python
# Открой models.py и выпиши все поля модели
# Например, для ImproveTaskOrder:
# - app_user_id (было credential_id)
# - task_key
# - position
```

#### Шаг 3: Найди все использования старого поля
```bash
# Найди все места, где используется старое поле
grep -r "\.credential_id\|credential_id\s*==\|credential_id\s*=" --include="*.py" plannig/backend/app/
```

### 2. **При рефакторинге: делай замены систематически**

#### Правило: Одна модель = один проход замены

1. **Найди все использования модели:**
   ```bash
   grep -n "ImproveTaskOrder" plannig/backend/app/main.py
   ```

2. **Для каждого использования проверь:**
   - Используется ли старое поле (`credential_id`)?
   - Нужно ли заменить на новое (`app_user_id`)?
   - Откуда брать значение? (`cred.app_user_id` вместо `cred.id`)

3. **Сделай замену во всех местах сразу:**
   ```python
   # БЫЛО:
   .where(ImproveTaskOrder.credential_id == cred.id)
   
   # СТАЛО:
   .where(ImproveTaskOrder.app_user_id == cred.app_user_id)
   ```

4. **Проверь создание объектов:**
   ```python
   # БЫЛО:
   ImproveTaskOrder(credential_id=cred.id, ...)
   
   # СТАЛО:
   ImproveTaskOrder(app_user_id=cred.app_user_id, ...)
   ```

### 3. **Чеклист перед коммитом**

- [ ] Все модели обновлены в `models.py`
- [ ] Все использования старых полей заменены на новые
- [ ] Все создания объектов используют новые поля
- [ ] Все запросы (SELECT/WHERE) используют новые поля
- [ ] Проверено через `grep`, что старых полей не осталось
- [ ] Локально протестированы все эндпоинты, которые используют эти модели

### 4. **Полезные команды для проверки**

```bash
# Найти все использования credential_id (кроме CredentialTeam/CredentialUser)
grep -r "credential_id" --include="*.py" plannig/backend/app/ | grep -v "CredentialTeam\|CredentialUser\|CredentialUser"

# Найти все использования конкретной модели
grep -r "ImproveTaskOrder\|GanttState\|TodoList\|TodoTask" --include="*.py" plannig/backend/app/

# Проверить, что все поля модели используются корректно
python -c "from app.models import ImproveTaskOrder; print([c.name for c in ImproveTaskOrder.__table__.columns])"
```

### 5. **Важно помнить**

- **`CredentialTeam` и `CredentialUser`** используют `credential_id` — это правильно, не трогай их!
- **`ApiCredential`** имеет `app_user_id` — используй `cred.app_user_id`, а не `cred.id`
- При создании объектов всегда проверяй, какие поля требуются в модели

### 6. **Если ошибка всё же произошла**

1. **Найди точное место ошибки:**
   - Смотри traceback — там будет указана строка
   - Открой файл и найди эту строку

2. **Проверь модель:**
   ```python
   # Открой models.py и проверь, какие поля есть у модели
   # Например:
   class ImproveTaskOrder(Base):
       app_user_id: Mapped[int] = ...  # НЕТ credential_id!
   ```

3. **Замени использование:**
   - Если в коде `Model.credential_id` → замени на `Model.app_user_id`
   - Если `cred.id` → замени на `cred.app_user_id`

4. **Проверь все похожие места:**
   - Используй `grep` для поиска всех использований этой модели
   - Исправь все сразу, чтобы не возвращаться

## Пример: Правильный рефакторинг

**До:**
```python
# models.py
class ImproveTaskOrder(Base):
    credential_id: Mapped[int] = ...

# main.py
saved_orders = db.scalars(
    select(ImproveTaskOrder)
    .where(ImproveTaskOrder.credential_id == cred.id)
).all()

order_entry = ImproveTaskOrder(
    credential_id=cred.id,
    task_key=task_key,
    position=position
)
```

**После:**
```python
# models.py
class ImproveTaskOrder(Base):
    app_user_id: Mapped[int] = ...  # Изменили поле

# main.py
saved_orders = db.scalars(
    select(ImproveTaskOrder)
    .where(ImproveTaskOrder.app_user_id == cred.app_user_id)  # Заменили поле и источник
).all()

order_entry = ImproveTaskOrder(
    app_user_id=cred.app_user_id,  # Заменили поле и источник
    task_key=task_key,
    position=position
)
```

## Итог

**Главное правило:** При изменении модели всегда проверяй все места, где эта модель используется, и обновляй их синхронно. Один пропуск = одна ошибка в рантайме.
