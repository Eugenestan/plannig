from __future__ import annotations

import time
from typing import Any

import requests

from .config import settings


class SlackNotifierError(RuntimeError):
    pass


def send_slack_message(text: str, *, retries: int = 3) -> dict[str, Any]:
    if not settings.slack_enabled:
        raise SlackNotifierError("Slack disabled by SLACK_ENABLED=false")

    webhook_url = (settings.slack_webhook_url or "").strip()
    if not webhook_url:
        raise SlackNotifierError("SLACK_WEBHOOK_URL is empty")

    payload: dict[str, Any] = {"text": text}
    channel = (settings.slack_channel or "").strip()
    if channel:
        payload["channel"] = channel

    timeout = (settings.slack_connect_timeout_seconds, settings.slack_read_timeout_seconds)
    last_error: Exception | None = None

    for attempt in range(1, max(retries, 1) + 1):
        try:
            response = requests.post(webhook_url, json=payload, timeout=timeout)
            if response.status_code >= 400:
                raise SlackNotifierError(f"Slack webhook HTTP {response.status_code}: {response.text[:300]}")
            # Slack webhook обычно возвращает текст "ok"; нормализуем в dict.
            return {"ok": True, "status_code": response.status_code, "text": response.text}
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= max(retries, 1):
                break
            time.sleep(min(2.0 * attempt, 5.0))

    raise SlackNotifierError(f"Failed to send Slack message: {last_error}")
