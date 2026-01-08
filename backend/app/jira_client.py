import base64
import os
import time
from typing import Any, Dict, List, Optional

import requests


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
    def __init__(self, base_url: str, headers: Dict[str, str], timeout_s: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(headers)
        self.timeout_s = timeout_s

    def request(self, method: str, path: str, *, params: Optional[dict] = None, json_body: Optional[dict] = None) -> requests.Response:
        url = self.base_url + path
        r = self.session.request(method, url, params=params, json=json_body, timeout=self.timeout_s, allow_redirects=True)
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "3"))
            time.sleep(retry_after)
            r = self.session.request(method, url, params=params, json=json_body, timeout=self.timeout_s, allow_redirects=True)
        return r

    def detect_api_prefix(self, forced: str = "") -> str:
        forced = (forced or "").strip()
        if forced:
            return forced.rstrip("/")
        for prefix in ("/rest/api/3", "/rest/api/2"):
            r = self.request("GET", f"{prefix}/serverInfo")
            if r.status_code in (200, 401, 403):
                return prefix
        raise RuntimeError("Не удалось определить Jira REST API префикс. Укажите api_prefix.")

    def get_fields(self, api_prefix: str) -> List[dict]:
        r = self.request("GET", f"{api_prefix}/field")
        if r.status_code != 200:
            raise RuntimeError(f"Не удалось получить поля: HTTP {r.status_code}: {r.text}")
        return r.json()

    def search_jql_page(self, jql: str, fields: List[str], max_results: int, next_page_token: str = "") -> dict:
        body: Dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": max_results}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        r = self.request("POST", "/rest/api/3/search/jql", json_body=body)
        if r.status_code != 200:
            raise RuntimeError(f"Search (jql) failed: HTTP {r.status_code}: {r.text}")
        return r.json()

    def get_worklog(self, api_prefix: str, issue_key: str) -> dict:
        """Получить worklog для задачи."""
        r = self.request("GET", f"{api_prefix}/issue/{issue_key}/worklog")
        if r.status_code != 200:
            raise RuntimeError(f"Get worklog failed: HTTP {r.status_code}: {r.text}")
        return r.json()


def build_headers_from_env() -> tuple[str, Dict[str, str]]:
    base_url = (os.getenv("JIRA_BASE_URL") or "").strip()
    if not base_url:
        raise RuntimeError("Нужно задать JIRA_BASE_URL.")

    token = (os.getenv("JIRA_TOKEN") or "").strip()
    email = (os.getenv("JIRA_EMAIL") or "").strip()
    api_token = (os.getenv("JIRA_API_TOKEN") or "").strip()

    headers: Dict[str, str] = {"Accept": "application/json"}

    if email and api_token:
        raw = f"{email}:{api_token}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return base_url, headers

    if token:
        headers["Authorization"] = f"Bearer {token}"
        return base_url, headers

    raise RuntimeError("Нужна авторизация: JIRA_EMAIL+JIRA_API_TOKEN или JIRA_TOKEN.")


def find_field_id(fields: List[dict], field_name: str) -> str:
    target = field_name.strip().lower()
    for f in fields:
        if (f.get("name") or "").strip().lower() == target:
            return f["id"]
    for f in fields:
        if target in ((f.get("name") or "").strip().lower()):
            return f["id"]
    raise RuntimeError(f"Поле '{field_name}' не найдено.")


def extract_team_values(v: Any) -> List[dict]:
    """
    Возвращает список объектов команд (как приходят из Jira): минимум {id, name/title}.
    TEAM может быть dict или list[dict].
    """
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        out: List[dict] = []
        seen: set[str] = set()
        for item in v:
            if isinstance(item, dict):
                tid = str(item.get("id") or item.get("name") or item.get("title") or "")
                if tid and tid not in seen:
                    out.append(item)
                    seen.add(tid)
        return out
    return []


def normalize_user(u: Any) -> Optional[dict]:
    if not isinstance(u, dict):
        return None
    account_id = u.get("accountId")
    display = u.get("displayName") or u.get("name") or account_id or ""
    if not account_id:
        return None
    return {
        "accountId": account_id,
        "displayName": display,
        "email": u.get("emailAddress"),
        "active": bool(u.get("active", True)),
    }


def validate_api_key(api_key: str, base_url: str, email: str = "") -> tuple[bool, str]:
    """
    Проверяет валидность API ключа через Jira API.
    
    Args:
        api_key: API ключ для проверки (может быть JIRA_API_TOKEN или JIRA_TOKEN)
        base_url: Базовый URL Jira
        email: Email для Basic auth (опционально, если ключ - это JIRA_API_TOKEN)
        
    Returns:
        tuple[bool, str]: (is_valid, error_message)
    """
    if not api_key or not api_key.strip():
        return False, "Ключ не может быть пустым"
    
    api_key = api_key.strip()
    
    # Если есть email, сначала пробуем Basic auth (для JIRA_API_TOKEN)
    if email:
        try:
            headers: Dict[str, str] = {"Accept": "application/json"}
            raw = f"{email}:{api_key}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
            jira = Jira(base_url, headers, timeout_s=10)
            api_prefix = jira.detect_api_prefix()
            r = jira.request("GET", f"{api_prefix}/serverInfo")
            
            if r.status_code == 200:
                print(f"DEBUG: API key validated successfully with Basic auth")
                return True, ""
            else:
                print(f"DEBUG: Basic auth failed with status {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"DEBUG: Basic auth exception: {str(e)}")
    
    # Пробуем как Bearer token (для JIRA_TOKEN)
    try:
        headers: Dict[str, str] = {"Accept": "application/json"}
        headers["Authorization"] = f"Bearer {api_key}"
        jira = Jira(base_url, headers, timeout_s=10)
        api_prefix = jira.detect_api_prefix()
        r = jira.request("GET", f"{api_prefix}/serverInfo")
        
        if r.status_code == 200:
            print(f"DEBUG: API key validated successfully with Bearer token")
            return True, ""
        else:
            print(f"DEBUG: Bearer token failed with status {r.status_code}: {r.text[:200]}")
            return False, f"Неправильный ключ (HTTP {r.status_code})"
    except Exception as e:
        print(f"DEBUG: Bearer token exception: {str(e)}")
        return False, f"Ошибка проверки ключа: {str(e)}"
    
    # Если оба метода не сработали
    return False, "Неправильный ключ. Проверьте, что вы используете правильный API токен."


