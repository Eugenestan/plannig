## Jira teams (через поле TEAM)

Этот проект содержит один скрипт `jira_teams.py`, который получает **список команд** из Jira через REST API.

Под “командами” здесь понимаются **значения кастомного поля** (по умолчанию поле называется `TEAM`) в задачах.
Скрипт:

- Ищет задачи, где поле TEAM не пустое
- Собирает уникальные значения TEAM
- Сохраняет результат в `teams.json` или `teams.csv`

## 1) Установка

```powershell
cd C:\Users\Steve\planing
py -m pip install -r .\requirements.txt
```

Если в консоли “кракозябры”:

```powershell
chcp 65001
$env:PYTHONUTF8=1
```

## 2) Файл с ключом (секреты)

Создайте файл `jira_secrets.env` рядом со скриптом (он в `.gitignore`):

```text
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=ATATT...
```

Либо (если у вас Bearer токен):

```text
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_TOKEN=YOUR_BEARER_TOKEN
```

Шаблон лежит в `jira_secrets.env.example`.

## 3) Команда запуска

Минимально:

```powershell
python .\jira_teams.py --out teams.csv
```

## 4) Сотрудники в каждой команде

Выгрузить уникальных пользователей по каждой команде (по умолчанию из поля `assignee`):

```powershell
python .\jira_teams.py --out teams.csv --members-out team_members.csv
```

Если хотите учитывать несколько user-полей (например `assignee` и `reporter`):

```powershell
python .\jira_teams.py --user-fields "assignee,reporter" --out teams.csv --members-out team_members.csv
```

Опционально:

- Указать имя поля:

```powershell
python .\jira_teams.py --team-field-name TEAM --out teams.json
```

- Ограничить проектом:

```powershell
python .\jira_teams.py --project ABC --out teams.csv
```

- Добавить фильтр JQL:

```powershell
python .\jira_teams.py --jql "statusCategory != Done" --out teams.csv
```


