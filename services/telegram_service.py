"""
services/telegram_service.py
=============================
Telegram Bot API.

Every method used to retry three times, five seconds apart, whatever went
wrong — including "chat not found" and "bot was blocked by the user", which no
amount of retrying will fix. Fifteen seconds of a worker's life, then a `None`
return the caller read as an ordinary failure with no reason attached.

Now one `_call` decides: a permanent error is reported immediately and never
retried, a rate limit waits exactly as long as Telegram asked, and everything
else backs off. Failures raise `TelegramError` carrying Telegram's own words,
so what the dashboard shows is what Telegram actually said.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from exceptions import TelegramError

log = logging.getLogger(__name__)


class TelegramService:
    BASE_URL = "https://api.telegram.org/bot{token}/{method}"
    MAX_RETRIES = 3
    RETRY_DELAY = 5

    #: Telegram's own words for conditions that will never succeed on a retry.
    #: Matched case-insensitively against the API's `description`.
    PERMANENT = (
        "chat not found",
        "bot was blocked",
        "bot was kicked",
        "user is deactivated",
        "not enough rights",
        "have no rights",
        "chat_id is empty",
        "message is too long",
        "wrong file identifier",
        "unauthorized",
        "forbidden",
        "peer_id_invalid",
    )

    #: Telegram allows roughly 20 messages a minute to one group. Publishes are
    #: serialised per process, so a small floor between sends keeps a backlog
    #: re-fire from tripping the limit in the first place.
    MIN_INTERVAL = 3.0
    _last_send: float = 0.0

    def __init__(self, token: str, admin_chat_id: str = None):
        self.token = token
        self.admin_chat_id = admin_chat_id

    def _api_url(self, method: str) -> str:
        return self.BASE_URL.format(token=self.token, method=method)

    def _markdown_to_html(self, text: str) -> str:
        import re
        import html
        text = html.escape(text)
        # Convert links [text](url)
        text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)
        # Convert bold **text**
        text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
        # Convert italic *text*
        text = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
        return text

    @classmethod
    def _is_permanent(cls, description: str) -> bool:
        low = (description or "").lower()
        return any(marker in low for marker in cls.PERMANENT)

    @classmethod
    def _throttle(cls) -> None:
        gap = time.monotonic() - cls._last_send
        if gap < cls.MIN_INTERVAL:
            time.sleep(cls.MIN_INTERVAL - gap)
        cls._last_send = time.monotonic()

    def _call(self, method: str, *, data=None, json_body=None,
              file_path: str | None = None, file_field: str | None = None,
              timeout: int = 15) -> int:
        """One Telegram request, retried only where retrying can help.

        Returns the message id. Raises TelegramError carrying Telegram's own
        description — the dashboard shows that string verbatim, so it has to be
        the real reason and not "publish failed".
        """
        last_error = "no attempt was made"
        wait = self.RETRY_DELAY

        for attempt in range(1, self.MAX_RETRIES + 1):
            self._throttle()
            try:
                if file_path:
                    # Reopened per attempt: a retry on an already-consumed
                    # handle uploads zero bytes and Telegram rejects it.
                    with open(file_path, "rb") as handle:
                        response = httpx.post(
                            self._api_url(method), data=data,
                            files={file_field: handle}, timeout=timeout)
                else:
                    response = httpx.post(
                        self._api_url(method), data=data, json=json_body,
                        timeout=timeout)
                payload = response.json()

                if payload.get("ok"):
                    return payload["result"]["message_id"]

                last_error = payload.get("description", "unknown Telegram error")

                if self._is_permanent(last_error):
                    log.error("[Telegram] %s refused permanently: %s",
                              method, last_error)
                    raise TelegramError(last_error)

                # 429: Telegram states exactly how long to wait. Guessing five
                # seconds when it asked for thirty just burns the remaining
                # attempts and reports a rate limit as a hard failure.
                retry_after = (payload.get("parameters") or {}).get("retry_after")
                wait = float(retry_after) if retry_after else self.RETRY_DELAY * attempt
                log.warning("[Telegram] %s failed (%d/%d): %s — waiting %.0fs",
                            method, attempt, self.MAX_RETRIES, last_error, wait)

            except TelegramError:
                raise
            except FileNotFoundError as exc:
                raise TelegramError(f"Asset file is missing: {exc.filename}") from exc
            except Exception as exc:
                last_error = str(exc)
                wait = self.RETRY_DELAY * attempt
                log.warning("[Telegram] %s (%d/%d): %s",
                            method, attempt, self.MAX_RETRIES, last_error)

            if attempt < self.MAX_RETRIES:
                time.sleep(wait)

        raise TelegramError(f"{last_error} (after {self.MAX_RETRIES} attempts)")

    @staticmethod
    def _require_chat(chat_id: str) -> str:
        if not chat_id or "xxxxxxxxxx" in str(chat_id):
            raise TelegramError(f"Invalid Telegram chat id: '{chat_id}'")
        return str(chat_id)

    # ── the four things this bot sends ───────────────────────────────────────

    def publish_text(self, text: str, chat_id: str) -> int:
        chat_id = self._require_chat(chat_id)
        if len(text) > 4096:
            text = text[:4090] + "..."
        return self._call("sendMessage", json_body={
            "chat_id": chat_id,
            "text": self._markdown_to_html(text),
            "parse_mode": "HTML",
        })

    def publish_photo(self, photo_path: str, caption: str, chat_id: str) -> int:
        chat_id = self._require_chat(chat_id)
        if len(caption) > 1024:
            caption = caption[:1018] + "..."
        return self._call(
            "sendPhoto",
            data={"chat_id": chat_id, "caption": self._markdown_to_html(caption),
                  "parse_mode": "HTML"},
            file_path=photo_path, file_field="photo", timeout=60,
        )

    def publish_document(self, doc_path: str, caption: str, chat_id: str) -> int:
        chat_id = self._require_chat(chat_id)
        if len(caption) > 1024:
            caption = caption[:1018] + "..."
        return self._call(
            "sendDocument",
            data={"chat_id": chat_id, "caption": self._markdown_to_html(caption),
                  "parse_mode": "HTML"},
            file_path=doc_path, file_field="document", timeout=60,
        )

    def publish_poll(self, question: str, options: list, chat_id: str,
                     is_anonymous: bool = True, type: str = "regular",
                     correct_option_id: int = 0, explanation: str = "") -> int:
        chat_id = self._require_chat(chat_id)
        if len(question) > 300:
            question = question[:297] + "..."

        # sendPoll wants `options` as a pre-serialised JSON string when the
        # request is form-encoded; passing a list double-serialises it.
        payload = {
            "chat_id": chat_id,
            "question": question,
            "options": json.dumps([{"text": str(o)[:100]} for o in options]),
            "is_anonymous": str(is_anonymous).lower(),
            "type": type,
        }
        if type == "quiz":
            payload["correct_option_id"] = str(correct_option_id)
            if explanation:
                payload["explanation"] = explanation[:200]
        return self._call("sendPoll", data=payload)

    # ── operator-facing extras ───────────────────────────────────────────────

    def send_admin_alert(self, text: str) -> None:
        """Best-effort. An alert that fails must never mask the real failure."""
        log.error("[Telegram] %s", text)
        if not self.admin_chat_id or "xxxxxxxxxx" in str(self.admin_chat_id):
            return
        try:
            httpx.post(self._api_url("sendMessage"),
                       json={"chat_id": self.admin_chat_id, "text": text[:4000]},
                       timeout=10)
        except Exception:
            pass

    def get_member_count(self, chat_id: str) -> int | str:
        if not chat_id or "xxxxxxxxxx" in str(chat_id):
            return "--"
        try:
            response = httpx.post(self._api_url("getChatMemberCount"),
                                  json={"chat_id": chat_id}, timeout=10)
            data = response.json()
            return data["result"] if data.get("ok") else "--"
        except Exception:
            return "--"
