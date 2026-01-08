## Planing — веб-интерфейс команд (MVP)

MVP поток:

1) Главная страница: список команд  
2) Выбор команды → выбор пользователей (checkbox) → сохранить в БД  
3) Редирект на страницу “дашборда” (пока заглушка)

Команды/пользователи можно подтянуть из Jira кнопкой **“Синхронизировать из Jira”** (используется ваш `jira_secrets.env`).

## Быстрый старт (SQLite, без Docker)

**По умолчанию используется SQLite** — не требует установки MySQL/Docker.

### 1) Настройка env для бэка

Из-за ограничений окружения шаблон лежит в `backend/config.example.env`.  
Создайте файл `backend/.env` и скопируйте туда содержимое (можно оставить как есть).

Также убедитесь, что `jira_secrets.env` заполнен (лежит в корне проекта).

### 2) Установка и запуск бэка

```powershell
cd C:\Users\Steve\planing\backend
py -m pip install -r .\requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Откройте в браузере: `http://127.0.0.1:8000/`

## Использование MySQL (опционально)

Если нужен MySQL вместо SQLite:

1) Поднимите MySQL (Docker):
```powershell
cd C:\Users\Steve\planing\backend
docker compose up -d
```

2) В `backend/.env` раскомментируйте:
```
USE_MYSQL=true
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=planing
MYSQL_PASSWORD=planing
MYSQL_DB=planing
```

## Примечания

- Таблицы создаются автоматически на старте (для MVP).
- Кнопка “Синхронизировать из Jira” делает upsert команд/пользователей и связывает их по данным из задач.
- SQLite файл `planing.db` создаётся автоматически в папке `backend/`.


