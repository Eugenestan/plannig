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

    bot_token = (settings.slack_bot_token or "").strip()
    if not bot_token:
        raise SlackNotifierError("SLACK_BOT_TOKEN is empty")

    channel_id = (settings.slack_channel_id or "").strip()
    if not channel_id:
        raise SlackNotifierError("SLACK_CHANNEL_ID is empty")

    payload: dict[str, Any] = {
        "channel": channel_id,
        "text": text,
        "mrkdwn": False,
    }

    timeout = (settings.slack_connect_timeout_seconds, settings.slack_read_timeout_seconds)
    url = settings.slack_api_base_url.rstrip("/") + "/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    last_error: Exception | None = None

    for attempt in range(1, max(retries, 1) + 1):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if response.status_code >= 400:
                raise SlackNotifierError(f"Slack API HTTP {response.status_code}: {response.text[:300]}")
            data = response.json()
            if not data.get("ok"):
                raise SlackNotifierError(f"Slack API error: {data}")
            return data
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= max(retries, 1):
                break
            time.sleep(min(2.0 * attempt, 5.0))

    raise SlackNotifierError(f"Failed to send Slack message: {last_error}")
