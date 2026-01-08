import argparse
import base64
import csv
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def configure_utf8_console() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def load_env_file(path: str) -> None:
    """
    Минимальный загрузчик env-файла формата KEY=VALUE.
    Не перезаписывает уже заданные переменные окружения.
    """
    p = (path or "").strip()
    if not p or not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
                v = v[1:-1]
            if k:
                os.environ.setdefault(k, v)


class Jira:
    def __init__(self, base_url: str, headers: Dict[str, str], timeout_s: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(headers)
        self.timeout_s = timeout_s

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> requests.Response:
        url = self.base_url + path
        r = self.session.request(
            method, url, params=params, json=json_body, timeout=self.timeout_s, allow_redirects=True
        )
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "3"))
            eprint(f"Rate limit (429). Sleep {retry_after}s and retry...")
            time.sleep(retry_after)
            r = self.session.request(
                method, url, params=params, json=json_body, timeout=self.timeout_s, allow_redirects=True
            )
        return r

    def detect_api_prefix(self, forced: str = "") -> str:
        forced = (forced or "").strip()
        if forced:
            return forced.rstrip("/")

        for prefix in ("/rest/api/3", "/rest/api/2"):
            r = self.request("GET", f"{prefix}/serverInfo")
            if r.status_code in (200, 401, 403):
                return prefix
        raise RuntimeError("Не удалось определить Jira REST API префикс. Укажите --api-prefix.")

    def get_fields(self, api_prefix: str) -> List[dict]:
        r = self.request("GET", f"{api_prefix}/field")
        if r.status_code != 200:
            raise RuntimeError(f"Не удалось получить поля: HTTP {r.status_code}: {r.text}")
        return r.json()

    def search_jql_page(self, jql: str, fields: List[str], max_results: int, next_page_token: str = "") -> dict:
        """
        Jira Cloud: /rest/api/3/search/jql (замена удалённого /search).
        Важно: у этого endpoint другой формат пагинации (nextPageToken).
        """
        body: Dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": max_results}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        r = self.request("POST", "/rest/api/3/search/jql", json_body=body)
        if r.status_code != 200:
            raise RuntimeError(f"Search (jql) failed: HTTP {r.status_code}: {r.text}")
        return r.json()


def build_headers_from_env() -> Tuple[str, Dict[str, str]]:
    base_url = (os.getenv("JIRA_BASE_URL") or "").strip()
    if not base_url:
        raise RuntimeError("Нужно задать JIRA_BASE_URL (например https://your-domain.atlassian.net).")

    token = (os.getenv("JIRA_TOKEN") or "").strip()
    email = (os.getenv("JIRA_EMAIL") or "").strip()
    api_token = (os.getenv("JIRA_API_TOKEN") or "").strip()

    headers: Dict[str, str] = {"Accept": "application/json"}

    # Jira Cloud API token -> Basic (email:token)
    if email and api_token:
        raw = f"{email}:{api_token}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return base_url, headers

    # Bearer (OAuth/PAT)
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return base_url, headers

    raise RuntimeError("Нужна авторизация: JIRA_EMAIL+JIRA_API_TOKEN (Cloud) или JIRA_TOKEN (Bearer).")


def find_field_id(fields: List[dict], field_name: str) -> str:
    target = field_name.strip().lower()
    for f in fields:
        if (f.get("name") or "").strip().lower() == target:
            return f["id"]
    for f in fields:
        if target in ((f.get("name") or "").strip().lower()):
            return f["id"]
    raise RuntimeError(f"Поле '{field_name}' не найдено в /field. Проверьте имя.")


def extract_team_values(v: Any) -> List[str]:
    """
    TEAM поле в Jira может быть:
    - строкой
    - объектом {value/name/title}
    - списком (multi-select) -> тогда возвращаем ВСЕ значения
    """
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, dict):
        s = (v.get("value") or v.get("name") or v.get("title") or "").strip()
        return [s] if s else []
    if isinstance(v, list):
        out: List[str] = []
        seen: set[str] = set()
        for item in v:
            for t in extract_team_values(item):
                if t not in seen:
                    out.append(t)
                    seen.add(t)
        return out
    s = str(v).strip()
    return [s] if s else []


def normalize_user(u: Any) -> Optional[dict]:
    """
    Приводит объект пользователя Jira к компактному виду.
    В Jira Cloud email часто отсутствует из-за privacy.
    """
    if not isinstance(u, dict):
        return None
    display = u.get("displayName") or u.get("name") or u.get("accountId") or ""
    account_id = u.get("accountId")
    email = u.get("emailAddress")
    key = account_id or u.get("key") or u.get("name") or display
    if not key:
        return None
    return {"key": key, "displayName": display, "accountId": account_id, "email": email}


def iterate_issues(
    jira: Jira,
    api_prefix: str,
    *,
    jql: str,
    fields: List[str],
    max_issues: int,
    raw_pages_out: Optional[List[dict]] = None,
) -> List[dict]:
    """
    Возвращает список issues (с полями), используя:
    - legacy GET {api_prefix}/search (если доступен)
    - либо Jira Cloud POST /rest/api/3/search/jql (если /search удалён и отдаёт 410)
    """
    page_size = 200
    fetched = 0
    out: List[dict] = []

    # Legacy /search
    start_at = 0
    params = {"jql": jql, "fields": ",".join(fields), "startAt": start_at, "maxResults": page_size}
    r = jira.request("GET", f"{api_prefix}/search", params=params)

    if r.status_code == 200:
        data = r.json()
        if raw_pages_out is not None:
            raw_pages_out.append(
                {
                    "method": "GET",
                    "path": f"{api_prefix}/search",
                    "params": params,
                    "status": r.status_code,
                    "response": data,
                }
            )
        while True:
            issues = data.get("issues", [])
            if not issues:
                break
            for issue in issues:
                out.append(issue)
                fetched += 1
                if max_issues and fetched >= max_issues:
                    return out
            start_at += len(issues)
            total = data.get("total")
            if total is not None and start_at >= int(total):
                break
            params = {"jql": jql, "fields": ",".join(fields), "startAt": start_at, "maxResults": page_size}
            r = jira.request("GET", f"{api_prefix}/search", params=params)
            if r.status_code != 200:
                raise RuntimeError(f"Search failed: HTTP {r.status_code}: {r.text}")
            data = r.json()
            if raw_pages_out is not None:
                raw_pages_out.append(
                    {
                        "method": "GET",
                        "path": f"{api_prefix}/search",
                        "params": params,
                        "status": r.status_code,
                        "response": data,
                    }
                )
        return out

    if r.status_code == 410:
        next_token = ""
        while True:
            data = jira.search_jql_page(jql=jql, fields=fields, max_results=page_size, next_page_token=next_token)
            if raw_pages_out is not None:
                raw_pages_out.append(
                    {
                        "method": "POST",
                        "path": "/rest/api/3/search/jql",
                        "json": (
                            {"jql": jql, "fields": fields, "maxResults": page_size}
                            if not next_token
                            else {"jql": jql, "fields": fields, "maxResults": page_size, "nextPageToken": next_token}
                        ),
                        "status": 200,
                        "response": data,
                    }
                )
            issues = data.get("issues", []) or data.get("values", [])
            if not issues:
                break
            for issue in issues:
                out.append(issue)
                fetched += 1
                if max_issues and fetched >= max_issues:
                    return out
            next_token = (data.get("nextPageToken") or "").strip()
            if not next_token:
                break
        return out

    raise RuntimeError(f"Search failed: HTTP {r.status_code}: {r.text}")


def collect_teams(
    jira: Jira,
    api_prefix: str,
    team_field_id: str,
    project_key: Optional[str],
    jql_extra: Optional[str],
    max_issues: int,
    raw_pages_out: Optional[List[dict]] = None,
) -> Dict[str, int]:
    jql_parts = [f'"{team_field_id}" is not EMPTY']
    if project_key:
        jql_parts.insert(0, f'project = "{project_key}"')
    if jql_extra:
        jql_parts.append(f"({jql_extra})")
    jql = " AND ".join(jql_parts)

    teams: Dict[str, int] = {}
    issues = iterate_issues(
        jira, api_prefix, jql=jql, fields=[team_field_id], max_issues=max_issues, raw_pages_out=raw_pages_out
    )
    for issue in issues:
        f = issue.get("fields", {})
        for team in extract_team_values(f.get(team_field_id)):
            teams[team] = teams.get(team, 0) + 1
    return teams


def collect_team_members_and_counts(
    jira: Jira,
    api_prefix: str,
    team_field_id: str,
    user_fields: List[str],
    project_key: Optional[str],
    jql_extra: Optional[str],
    max_issues: int,
    raw_pages_out: Optional[List[dict]] = None,
) -> Tuple[Dict[str, int], Dict[str, Dict[str, dict]]]:
    jql_parts = [f'"{team_field_id}" is not EMPTY']
    if project_key:
        jql_parts.insert(0, f'project = "{project_key}"')
    if jql_extra:
        jql_parts.append(f"({jql_extra})")
    jql = " AND ".join(jql_parts)

    fields = [team_field_id] + user_fields
    issues = iterate_issues(
        jira, api_prefix, jql=jql, fields=fields, max_issues=max_issues, raw_pages_out=raw_pages_out
    )

    teams: Dict[str, int] = {}
    team_to_users: Dict[str, Dict[str, dict]] = {}

    for issue in issues:
        f = issue.get("fields", {})
        issue_teams = extract_team_values(f.get(team_field_id))
        if not issue_teams:
            continue
        for team in issue_teams:
            teams[team] = teams.get(team, 0) + 1
            team_to_users.setdefault(team, {})
            for uf in user_fields:
                raw = f.get(uf)
                if isinstance(raw, list):
                    for item in raw:
                        nu = normalize_user(item)
                        if nu:
                            team_to_users[team][nu["key"]] = nu
                else:
                    nu = normalize_user(raw)
                    if nu:
                        team_to_users[team][nu["key"]] = nu

    return teams, team_to_users


def write_output(teams: Dict[str, int], out_path: str) -> None:
    ext = os.path.splitext(out_path.lower())[1]
    rows = [{"team": t, "issues": n} for t, n in sorted(teams.items(), key=lambda x: x[0].lower())]

    if ext in (".json", ""):
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"teams": rows}, f, ensure_ascii=False, indent=2)
        return

    if ext == ".csv":
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["team", "issues"])
            w.writeheader()
            w.writerows(rows)
        return

    raise RuntimeError("out должен быть .json или .csv")


def write_members_output(team_to_users: Dict[str, Dict[str, dict]], out_path: str) -> None:
    ext = os.path.splitext(out_path.lower())[1]
    rows: List[dict] = []
    for team, users in sorted(team_to_users.items(), key=lambda x: x[0].lower()):
        for u in users.values():
            rows.append(
                {
                    "team": team,
                    "displayName": u.get("displayName"),
                    "accountId": u.get("accountId"),
                    "email": u.get("email"),
                    "key": u.get("key"),
                }
            )

    if ext in (".json", ""):
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"teamMembers": rows}, f, ensure_ascii=False, indent=2)
        return

    if ext == ".csv":
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["team", "displayName", "accountId", "email", "key"])
            w.writeheader()
            w.writerows(rows)
        return

    raise RuntimeError("members-out должен быть .json или .csv")


def main() -> int:
    configure_utf8_console()
    p = argparse.ArgumentParser(description="Получить список команд (значения поля TEAM) из Jira через REST API.")
    p.add_argument(
        "--secrets-file",
        default="jira_secrets.env",
        help="Файл с секретами (KEY=VALUE). По умолчанию jira_secrets.env",
    )
    p.add_argument("--team-field-name", default="TEAM", help="Имя поля команды (например TEAM или Team).")
    p.add_argument("--api-prefix", default="", help='Явно задать "/rest/api/3" или "/rest/api/2" (опционально).')
    p.add_argument("--project", default="", help="Ключ проекта (опционально).")
    p.add_argument("--jql", default="", help="Дополнительный JQL (опционально).")
    p.add_argument("--max-issues", type=int, default=0, help="Ограничить количество задач (0 = без ограничения).")
    p.add_argument(
        "--user-fields",
        default="assignee",
        help='Какие user-поля считать "сотрудниками" команды: например "assignee,reporter".',
    )
    p.add_argument("--out", default="teams.json", help="Вывод: teams.json или teams.csv")
    p.add_argument(
        "--members-out",
        default="",
        help="Если задано — выгрузить сотрудников по командам в team_members.csv/json",
    )
    p.add_argument(
        "--dump-raw",
        default="",
        help="Сохранить полный сырой JSON-ответ(ы) Jira поиска в файл (для отладки сопоставления).",
    )
    args = p.parse_args()

    load_env_file(args.secrets_file)
    base_url, headers = build_headers_from_env()
    jira = Jira(base_url, headers)
    api_prefix = jira.detect_api_prefix(args.api_prefix)

    fields = jira.get_fields(api_prefix)
    team_field_id = find_field_id(fields, args.team_field_name)
    eprint(f"TEAM field: {args.team_field_name} -> {team_field_id}")

    user_fields = [x.strip() for x in (args.user_fields or "").split(",") if x.strip()]
    raw_pages: Optional[List[dict]] = [] if (args.dump_raw or "").strip() else None

    if (args.members_out or "").strip():
        teams, team_to_users = collect_team_members_and_counts(
            jira,
            api_prefix=api_prefix,
            team_field_id=team_field_id,
            user_fields=user_fields or ["assignee"],
            project_key=(args.project.strip() or None),
            jql_extra=(args.jql.strip() or None),
            max_issues=(args.max_issues or 0),
            raw_pages_out=raw_pages,
        )
        write_output(teams, args.out)
        write_members_output(team_to_users, args.members_out)
        if raw_pages is not None:
            with open(args.dump_raw, "w", encoding="utf-8") as f:
                json.dump({"pages": raw_pages}, f, ensure_ascii=False, indent=2)
        eprint(
            f"OK. Teams: {len(teams)}. Members rows: {sum(len(u) for u in team_to_users.values())}. "
            f"Output: {args.out}, {args.members_out}"
        )
        return 0

    teams = collect_teams(
        jira,
        api_prefix=api_prefix,
        team_field_id=team_field_id,
        project_key=(args.project.strip() or None),
        jql_extra=(args.jql.strip() or None),
        max_issues=(args.max_issues or 0),
        raw_pages_out=raw_pages,
    )
    write_output(teams, args.out)
    if raw_pages is not None:
        with open(args.dump_raw, "w", encoding="utf-8") as f:
            json.dump({"pages": raw_pages}, f, ensure_ascii=False, indent=2)
    eprint(f"OK. Teams: {len(teams)}. Output: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


