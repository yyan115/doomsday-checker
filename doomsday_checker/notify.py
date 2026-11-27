"""Telegram delivery and local credential loading."""

import os
import re
from pathlib import Path

import httpx

MARKDOWN_V2 = "MarkdownV2"


class DeliveryError(Exception):
    """A Telegram request could not be completed."""


def _read_env_file(directory: Path) -> dict[str, str]:
    file_values = {}
    env_file = directory / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().removeprefix("export ")
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if separator:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                file_values[key.strip()] = value
    return file_values


def _bot_token(file_values: dict[str, str]) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", file_values.get("TELEGRAM_BOT_TOKEN", ""))
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Set a valid TELEGRAM_BOT_TOKEN in .env")
    return token


def _request(client: httpx.Client, token: str, method: str, action: str, **kwargs) -> object:
    try:
        response = client.request(method, f"https://api.telegram.org/bot{token}/{action}", **kwargs)
    except httpx.HTTPError as error:
        raise DeliveryError(f"Telegram connection failed ({type(error).__name__})") from None
    if response.status_code == 409 and action == "getUpdates":
        raise DeliveryError("Telegram chat lookup conflicts with a webhook or another poller")
    if not response.is_success:
        raise DeliveryError(f"Telegram returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise DeliveryError("Telegram returned an invalid response") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise DeliveryError(f"Telegram did not confirm {action}")
    return data.get("result")


def find_chat_id(client: httpx.Client, directory: Path, destination: str) -> str:
    """Find a chat that sent the setup command without consuming pending updates."""
    token = _bot_token(_read_env_file(directory))
    bot = _request(client, token, "GET", "getMe")
    username = bot.get("username") if isinstance(bot, dict) else None
    if not isinstance(username, str) or not re.fullmatch(r"[a-zA-Z0-9_]+", username):
        raise DeliveryError("Telegram did not return a bot username")
    if destination == "group" and bot.get("can_join_groups") is False:
        raise DeliveryError("This bot cannot join groups. Enable group membership in BotFather.")

    addressed_start = f"/start@{username}"
    commands = {addressed_start.lower()}
    chat_types = {"group", "supergroup"}
    setup = f"Add @{username} to the group and send {addressed_start} there."
    if destination == "private":
        commands.add("/start")
        chat_types = {"private"}
        setup = f"Send /start in a private chat with @{username}."

    updates = _request(client, token, "GET", "getUpdates")
    if not isinstance(updates, list):
        raise DeliveryError("Telegram did not return a list of updates")
    chat_ids = set()
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        command = message.get("text", "").strip().split(maxsplit=1)
        if chat.get("type") in chat_types and command and command[0].lower() in commands:
            chat_ids.add(chat["id"])
    if not chat_ids:
        raise DeliveryError(f"No {destination} setup command found. {setup}")
    if len(chat_ids) > 1:
        raise DeliveryError(f"Setup commands came from multiple {destination} chats")
    return str(chat_ids.pop())


class Telegram:
    def __init__(self, client: httpx.Client, directory: Path):
        self._client = client
        file_values = _read_env_file(directory)
        self._token = _bot_token(file_values)
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", file_values.get("TELEGRAM_CHAT_ID", ""))
        if not re.fullmatch(r"-?[0-9]+", self._chat_id):
            raise ValueError("Set a numeric TELEGRAM_CHAT_ID in .env")
        self.channel_id = (
            os.environ.get("TELEGRAM_CHANNEL_ID", file_values.get("TELEGRAM_CHANNEL_ID", ""))
            or None
        )
        if self.channel_id is not None and not re.fullmatch(r"-[1-9][0-9]*", self.channel_id):
            raise ValueError("Set a negative numeric TELEGRAM_CHANNEL_ID in .env")
        if self.channel_id == self._chat_id:
            raise ValueError(
                "Set separate Telegram destinations for the channel and health notices"
            )

    def send(
        self,
        message: str,
        photo: bytes | None = None,
        parse_mode: str | None = None,
        *,
        public: bool = False,
    ) -> object:
        chat_id = self.channel_id if public else self._chat_id
        if chat_id is None:
            raise DeliveryError("The Telegram channel is not configured")
        formatting = {"parse_mode": parse_mode} if parse_mode else {}
        if photo is None:
            return _request(
                self._client,
                self._token,
                "POST",
                "sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "link_preview_options": {"is_disabled": True},
                    **formatting,
                },
            )
        return _request(
            self._client,
            self._token,
            "POST",
            "sendPhoto",
            data={
                "chat_id": chat_id,
                "caption": message,
                "show_caption_above_media": "true",
                **formatting,
            },
            files={"photo": ("seat-map.png", photo, "image/png")},
        )
