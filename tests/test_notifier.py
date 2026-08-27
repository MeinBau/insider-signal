from insider_signal.config import Settings
from insider_signal.notifier import (
    CompositeNotifier,
    ConsoleNotifier,
    DiscordNotifier,
    SlackNotifier,
    TelegramNotifier,
    build_notifier,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakePost:
    """requests.post 대체용: 호출 인자를 기록하고 미리 정해둔 응답/예외를 반환합니다."""

    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response or _FakeResponse()
        self._exc = exc

    def __call__(self, url, *, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self._exc is not None:
            raise self._exc
        return self._response


def test_slack_notifier_posts_expected_payload(monkeypatch):
    fake_post = _FakePost()
    monkeypatch.setattr("insider_signal.notifier.requests.post", fake_post)

    SlackNotifier("https://hooks.slack.com/services/x").notify(
        title="내부자 매수 신호: ACME (ACME)", body="신고인: John Doe", url="https://sec.gov/x"
    )

    assert len(fake_post.calls) == 1
    call = fake_post.calls[0]
    assert call["url"] == "https://hooks.slack.com/services/x"
    assert call["json"]["text"] == (
        "*내부자 매수 신호: ACME (ACME)*\n신고인: John Doe\nhttps://sec.gov/x"
    )
    assert call["timeout"] == 10


def test_discord_notifier_posts_expected_payload(monkeypatch):
    fake_post = _FakePost()
    monkeypatch.setattr("insider_signal.notifier.requests.post", fake_post)

    DiscordNotifier("https://discord.com/api/webhooks/x").notify(
        title="T", body="B", url="U"
    )

    call = fake_post.calls[0]
    assert call["url"] == "https://discord.com/api/webhooks/x"
    assert call["json"]["content"] == "**T**\nB\nU"


def test_telegram_notifier_posts_to_bot_api(monkeypatch):
    fake_post = _FakePost()
    monkeypatch.setattr("insider_signal.notifier.requests.post", fake_post)

    TelegramNotifier(bot_token="TOKEN", chat_id="12345").notify(
        title="T", body="B", url="U"
    )

    call = fake_post.calls[0]
    assert call["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert call["json"] == {"chat_id": "12345", "text": "T\nB\nU"}


def test_slack_notifier_raises_on_http_error(monkeypatch):
    fake_post = _FakePost(response=_FakeResponse(status_code=500))
    monkeypatch.setattr("insider_signal.notifier.requests.post", fake_post)

    try:
        SlackNotifier("https://hooks.slack.com/services/x").notify(title="T", body="B", url="U")
    except RuntimeError:
        pass
    else:
        raise AssertionError("HTTP 에러 시 예외가 발생해야 합니다")


def test_composite_notifier_isolates_failure_and_still_calls_others(monkeypatch):
    calls = []

    class _Working:
        def notify(self, *, title, body, url):
            calls.append("working")

    class _Broken:
        def notify(self, *, title, body, url):
            raise RuntimeError("네트워크 오류")

    composite = CompositeNotifier(notifiers=[_Broken(), _Working()])
    composite.notify(title="T", body="B", url="U")

    assert calls == ["working"]


def test_build_notifier_always_includes_console(monkeypatch):
    caplog_calls = []
    settings = Settings(
        slack_webhook_url="", discord_webhook_url="", telegram_bot_token="", telegram_chat_id=""
    )
    notifier = build_notifier(settings)

    assert len(notifier.notifiers) == 1
    assert isinstance(notifier.notifiers[0], ConsoleNotifier)


def test_build_notifier_adds_configured_channels_only():
    settings = Settings(
        slack_webhook_url="https://hooks.slack.com/x",
        discord_webhook_url="",
        telegram_bot_token="TOKEN",
        telegram_chat_id="",  # chat_id 없으면 텔레그램은 추가되지 않아야 함
    )
    notifier = build_notifier(settings)

    kinds = [type(n) for n in notifier.notifiers]
    assert kinds == [ConsoleNotifier, SlackNotifier]


def test_build_notifier_adds_telegram_only_when_both_token_and_chat_id_set():
    settings = Settings(
        slack_webhook_url="",
        discord_webhook_url="",
        telegram_bot_token="TOKEN",
        telegram_chat_id="12345",
    )
    notifier = build_notifier(settings)

    kinds = [type(n) for n in notifier.notifiers]
    assert kinds == [ConsoleNotifier, TelegramNotifier]
