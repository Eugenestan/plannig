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
Создайте файл `backend/config.env` и скопируйте туда содержимое (можно оставить как есть).

Также убедитесь, что `jira_secrets.env` заполнен (лежит в корне проекта).

### 2) Установка и запуск бэка

```powershell
cd C:\Users\Steve\planing\plannig\backend
py -m pip install -r .\requirements.txt

# (Важно) Одноразовая миграция SQLite после перехода на app_user_id
python -m app.migrate_sqlite_app_user_id
# (Новая) миграция таблицы Telegram-настроек команд
python -m app.migrate_team_telegram_settings

python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Откройте в браузере: `http://127.0.0.1:8000/`

## Использование MySQL (опционально)

Если нужен MySQL вместо SQLite:

1) Поднимите MySQL (Docker):
```powershell
cd C:\Users\Steve\planing\plannig\backend
docker compose up -d
```

2) В `backend/config.env` раскомментируйте:
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

## Telegram-сводка списанного времени

1) В `backend/config.env` добавьте:
```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<token от BotFather>
```

2) Для каждой Jira-команды откройте страницу команды (`/teams/{id}`), заполните `Chat ID` и включите флаг рассылки.

3) Проверка без отправки:
```powershell
cd C:\Users\Steve\planing\plannig\backend
python -m app.daily_summary --dry-run
```

4) Боевая отправка:
```powershell
python -m app.daily_summary
```

### Планировщик (Пн-Пт, 20:00 МСК)

Для Linux cron (рекомендуемо в прод):
```cron
CRON_TZ=Europe/Moscow
0 20 * * 1-5 cd /opt/planing/backend && /usr/bin/python3 -m app.daily_summary >> /var/log/planing_daily_summary.log 2>&1
```

Если timezone cron не поддерживается, задайте `TZ=Europe/Moscow` в окружении сервиса/контейнера.


