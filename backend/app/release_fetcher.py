from __future__ import annotations

from datetime import date, datetime

from .jira_client import Jira


RELEASES_JQL_BASE = (
    "project = TNL AND type = Epic "
    "AND status NOT IN (Отменено, Done) "
    "AND fixVersion IS NOT EMPTY"
)


def _parse_release_date(value: str | None) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def get_releases_for_current_user(
    jira: Jira,
    *,
    due_on_or_before: date | None = None,
    only_unreleased: bool = False,
    only_current_user_assignee: bool = True,
) -> list[dict]:
    """
    Возвращает релизы (fixVersions) эпиков из Jira.

    Дата релиза берется из version.releaseDate.
    """
    jql = RELEASES_JQL_BASE
    if only_current_user_assignee:
        jql += " AND assignee = currentUser()"

    all_releases: list[dict] = []
    next_token = ""
    page_size = 200

    while True:
        data = jira.search_jql_page(
            jql=jql,
            fields=["key", "summary", "fixVersions"],
            max_results=page_size,
            next_page_token=next_token,
        )
        issues = data.get("issues", []) or data.get("values", [])
        if not issues:
            break

        for issue in issues:
            fields = issue.get("fields", {})
            fix_versions = fields.get("fixVersions", [])
            if not fix_versions:
                continue

            # В проекте используется первая fixVersion.
            version = fix_versions[0]
            if not isinstance(version, dict):
                continue

            release_date = _parse_release_date(version.get("releaseDate"))
            if release_date is None:
                continue

            is_released = bool(version.get("released", False))
            if only_unreleased and is_released:
                continue

            if due_on_or_before is not None and release_date > due_on_or_before:
                continue

            epic_summary = (fields.get("summary") or "").strip()
            version_name = (version.get("name") or "").strip()
            all_releases.append(
                {
                    "epic_key": issue.get("key", ""),
                    "epic_summary": epic_summary,
                    "release_date": release_date.strftime("%Y-%m-%d"),
                    "release_date_obj": release_date.isoformat(),
                    "version_name": version_name,
                    "released": is_released,
                }
            )

        next_token = (data.get("nextPageToken") or "").strip()
        if not next_token:
            break

    all_releases.sort(key=lambda item: item["release_date_obj"])
    return all_releases
