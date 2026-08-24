"""알림 채널 추상화.

무엇이 설정되어 있든 상관없이 항상 ConsoleNotifier가 포함되어 로그/감사 기록 역할을 합니다.
개별 채널 전송 실패가 전체 polling을 죽이면 안 되므로, CompositeNotifier는 채널별로
예외를 잡아서 로그만 남기고 계속 진행합니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import requests

from .config import Settings

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, *, title: str, body: str, url: str) -> None: ...


class ConsoleNotifier:
    def notify(self, *, title: str, body: str, url: str) -> None:
        logger.info("[ALERT] %s\n%s\n%s", title, body, url)


class SlackNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    def notify(self, *, title: str, body: str, url: str) -> None:
        text = f"*{title}*\n{body}\n{url}"
        resp = requests.post(self._webhook_url, json={"text": text}, timeout=10)
        resp.raise_for_status()


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    def notify(self, *, title: str, body: str, url: str) -> None:
        content = f"**{title}**\n{body}\n{url}"
        resp = requests.post(self._webhook_url, json={"content": content}, timeout=10)
        resp.raise_for_status()


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    def notify(self, *, title: str, body: str, url: str) -> None:
        text = f"{title}\n{body}\n{url}"
        api_url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        resp = requests.post(
            api_url, json={"chat_id": self._chat_id, "text": text}, timeout=10
        )
        resp.raise_for_status()


@dataclass
class CompositeNotifier:
    notifiers: list[Notifier]

    def notify(self, *, title: str, body: str, url: str) -> None:
        for notifier in self.notifiers:
            try:
                notifier.notify(title=title, body=body, url=url)
            except Exception:  # noqa: BLE001 - 알림 실패로 전체 루프를 죽이지 않음
                logger.exception("알림 전송 실패: %s", type(notifier).__name__)


def build_notifier(settings: Settings) -> CompositeNotifier:
    notifiers: list[Notifier] = [ConsoleNotifier()]

    if settings.slack_webhook_url:
        notifiers.append(SlackNotifier(settings.slack_webhook_url))
    if settings.discord_webhook_url:
        notifiers.append(DiscordNotifier(settings.discord_webhook_url))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        notifiers.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))

    if len(notifiers) == 1:
        logger.warning(
            "SLACK_WEBHOOK_URL / DISCORD_WEBHOOK_URL / TELEGRAM_BOT_TOKEN+CHAT_ID 중 "
            "아무것도 설정되지 않아 콘솔 로그로만 알림이 출력됩니다."
        )

    return CompositeNotifier(notifiers=notifiers)
