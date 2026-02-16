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

Для Slack (опционально, параллельно с Telegram или отдельно, через `chat.postMessage`):
```
SLACK_ENABLED=true
SLACK_BOT_TOKEN=<xoxb token>
SLACK_CHANNEL_ID=<C... channel id>
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

## Telegram-уведомления о релизах

Скрипт отправляет общий список невыпущенных релизов (без разбивки по командам) с датой релиза на сегодня или раньше:

- фильтр: `released != true`
- фильтр: `releaseDate <= today (MSK)`
- формат строки: `Название - срок релиза`

1) Используются те же настройки каналов, что и для сводки времени (Telegram и/или Slack):
```env
# Telegram
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<token от BotFather>

# Slack (опционально)
SLACK_ENABLED=true
SLACK_BOT_TOKEN=<xoxb token>
SLACK_CHANNEL_ID=<C... channel id>
```

2) Проверка без отправки:
```powershell
cd C:\Users\Steve\planing\plannig\backend
python -m app.release_notifications --dry-run --force
```

3) Боевая отправка:
```powershell
python -m app.release_notifications
```

4) Пример cron (Пн-Пт, 10:00 МСК):
```cron
CRON_TZ=Europe/Moscow
0 10 * * 1-5 cd /opt/planing/backend && /usr/bin/python3 -m app.release_notifications >> /var/log/planing_release_notifications.log 2>&1
```

## Прод-деплой сводки в Telegram и Slack

Ниже шаги для развертывания отправки общей сводки по командам `3, 1, 2, 4`.

1) Обновите код на сервере и активируйте окружение:
```bash
cd /opt/planing/backend
source .venv/bin/activate
```

2) Убедитесь, что выполнена миграция Telegram-настроек команд:
```bash
python -m app.migrate_team_telegram_settings
```

3) В `config.env` задайте каналы доставки:
```env
# Telegram
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<bot token>

# Slack (chat.postMessage)
SLACK_ENABLED=true
SLACK_BOT_TOKEN=<xoxb token>
SLACK_CHANNEL_ID=<C...>
```

4) Для Telegram заполните `Chat ID` у команд на странице `/teams/{id}` и включите флаг рассылки.
Для общей сводки используются команды `3, 1, 2, 4` (фиксировано в коде).

5) Убедитесь, что Slack-бот приглашен в канал:
```text
/invite @<bot_name>
```

6) Проверка перед боем:
```bash
python -m app.daily_summary --dry-run --force
```

7) Боевая проверка (реальная отправка):
```bash
python -m app.daily_summary --force
```

8) Расписание на будни 20:00 МСК:
```cron
CRON_TZ=Europe/Moscow
0 20 * * 1-5 cd /opt/planing/backend && /opt/planing/backend/.venv/bin/python -m app.daily_summary >> /var/log/planing_daily_summary.log 2>&1
```

9) Диагностика:
```bash
tail -n 200 /var/log/planing_daily_summary.log
```


