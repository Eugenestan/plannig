## DEPLOY (production)

Цель: задеплоить так, чтобы сервис не упал из‑за зависимостей/миграций/конфига.

### Что хранить на сервере (не в git)
- `plannig/jira_secrets.env`: Jira URL + email + token.
- `plannig/backend/config.env`: переменные backend.
- `plannig/backend/planing.db`: SQLite база (если используете SQLite).

### 0) Подготовка окружения
На сервере нужен Python 3.10+.

```bash
cd /opt/planing/plannig/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 1) Конфиг
Создайте/обновите `plannig/backend/config.env`:

```env
# путь до файла с jira токеном (относительно backend/)
JIRA_SECRETS_FILE=../jira_secrets.env

# Teamboard TimePlanner timelogs (считаем ТОЛЬКО ивенты, без дублей Jira worklog)
TEAMBOARD_BEARER_JWT=eyJ...
TEAMBOARD_BASE_URL=https://api.teamboard.cloud/v1

# (опционально) DevSamurai timesheet
# DEVSAMURAI_TIMESHEET_JWT=JWT eyJ...
# DEVSAMURAI_TIMESHEET_BASE_URL=https://www.timesheet.atlas.devsamurai.com
```

Важно:
- `TEAMBOARD_BEARER_JWT` — это **JWT** (начинается с `eyJ...`). Он имеет срок жизни: если перестанет работать — обновить.

### 2) Миграции БД (SQLite)
Если вы деплоите существующую базу SQLite со старой схемой (credential_id), выполните **один раз**:

```bash
python -m app.migrate_sqlite_app_user_id
```

Рекомендуется заранее сделать бэкап `planing.db`.

### 3) Запуск (без --reload)
Проверка локально:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

#### Пример systemd unit
`/etc/systemd/system/planing.service`:

```ini
[Unit]
Description=Planing (FastAPI)
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/planing/plannig/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/planing/plannig/backend/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Далее:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now planing
sudo systemctl status planing
```

### 4) Smoke-check после деплоя
- `GET /` должен вернуть 200.
- В UI после логина проверить таб «Списано времени».
- Для диагностики можно дернуть:
  - `/api/teams/<id>/worklog?days=today&debug=1`
