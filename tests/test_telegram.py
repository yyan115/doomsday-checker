import json

import httpx
import pytest

from doomsday_checker.notify import DeliveryError, Telegram, find_chat_id


def message(chat_type, chat_id, text):
    return {"message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}


def chat_lookup(tmp_path, monkeypatch, updates):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=123:example\n", encoding="utf-8")

    def respond(request):
        assert request.method == "GET"
        assert not request.url.params
        if request.url.path == "/bot123:example/getMe":
            result = {"username": "example_bot", "can_join_groups": True}
        else:
            assert request.url.path == "/bot123:example/getUpdates"
            result = updates
        return httpx.Response(200, json={"ok": True, "result": result})

    return httpx.Client(transport=httpx.MockTransport(respond))


@pytest.mark.parametrize(("destination", "expected_id"), [("private", "78"), ("group", "-10079")])
def test_chat_lookup_filters_by_type_and_addressed_command(
    tmp_path, monkeypatch, destination, expected_id
):
    updates = [
        message("private", 78, "/start"),
        message("group", -80, "/start"),
        message("group", -81, "/start@another_bot"),
        message("supergroup", -10079, "/start@Example_Bot"),
        message("supergroup", -10079, "/start@example_bot"),
        message("channel", -10082, "/start@example_bot"),
    ]
    with chat_lookup(tmp_path, monkeypatch, updates) as client:
        assert find_chat_id(client, tmp_path, destination) == expected_id


def test_chat_lookup_rejects_multiple_groups(tmp_path, monkeypatch):
    updates = [
        message("group", -80, "/start@example_bot"),
        message("supergroup", -10081, "/start@example_bot"),
    ]
    with (
        chat_lookup(tmp_path, monkeypatch, updates) as client,
        pytest.raises(DeliveryError, match="multiple group chats"),
    ):
        find_chat_id(client, tmp_path, "group")


def test_alert_uses_configured_negative_group_id(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123:example\nTELEGRAM_CHAT_ID=-10079\n", encoding="utf-8"
    )

    def respond(request):
        assert request.url.path == "/bot123:example/sendMessage"
        assert json.loads(request.content)["chat_id"] == "-10079"
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        Telegram(client, tmp_path).send("Example alert")
