"""Phone alerts: ntfy (no account needed) and Telegram."""

from __future__ import annotations

import asyncio
import html
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp


@dataclass
class Alert:
    title: str
    body: str
    url: str | None = None
    image: str | None = None
    urgent: bool = False
    button: str = "Open"


class Notifier:
    def __init__(self, settings: Callable[[], dict[str, Any]], *, telegram_api: str = "https://api.telegram.org",
                 log: Callable[[str], None] = print, retries: int = 3, retry_delay: float = 1.5):
        self.settings = settings
        self.telegram_api = telegram_api.rstrip("/")
        self.log = log
        self.retries = retries
        self.retry_delay = retry_delay
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=True)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    def channels(self, s: dict | None = None) -> list[str]:
        s = s or self.settings()
        out = []
        if s.get("ntfy_enabled") and s.get("ntfy_topic"):
            out.append("ntfy")
        if s.get("telegram_enabled") and s.get("telegram_token") and s.get("telegram_chat_id"):
            out.append("telegram")
        return out

    async def send(self, alert: Alert) -> dict[str, str | None]:
        """Send on every configured channel at once. Returns {channel: None if ok, else the error}."""
        s = self.settings()
        chans = self.channels(s)
        if not chans:
            return {}
        senders = {"ntfy": self._ntfy, "telegram": self._telegram}
        results = await asyncio.gather(*(self._with_retry(senders[c], s, alert) for c in chans))
        out = dict(zip(chans, results))
        for c, err in out.items():
            if err:
                self.log(f"{c} alert failed: {err}")
        return out

    async def _with_retry(self, fn, s: dict, alert: Alert) -> str | None:
        err = "unknown error"
        for attempt in range(self.retries):
            try:
                await fn(s, alert)
                return None
            except Exception as e:  # noqa: BLE001 - reported to the user
                err = friendly(e, fn)
                if isinstance(e, PermanentError):
                    break
                await asyncio.sleep(self.retry_delay * (attempt + 1))
        return err

    async def _ntfy(self, s: dict, a: Alert) -> None:
        payload: dict[str, Any] = {
            "topic": s["ntfy_topic"],
            "title": a.title,
            "message": a.body,
            "priority": 5 if a.urgent else 3,
            "tags": ["rotating_light"] if a.urgent else ["eyes"],
        }
        if a.url:
            payload["click"] = a.url
            payload["actions"] = [{"action": "view", "label": a.button, "url": a.url, "clear": True}]
        if a.image and a.image.startswith(("https://", "http://")):
            payload["icon"] = a.image
        headers = {}
        if s.get("ntfy_token"):
            headers["Authorization"] = "Bearer " + s["ntfy_token"]
        http = await self._http()
        async with http.post(str(s.get("ntfy_server") or "https://ntfy.sh").rstrip("/") + "/", json=payload, headers=headers) as r:
            if r.status >= 400:
                text = (await r.text())[:200]
                if r.status in (400, 401, 403):
                    raise PermanentError(f"ntfy said HTTP {r.status}: {text}")
                raise RuntimeError(f"ntfy said HTTP {r.status}: {text}")

    async def _telegram(self, s: dict, a: Alert) -> None:
        text = f"<b>{html.escape(a.title)}</b>\n{html.escape(a.body)}"
        payload: dict[str, Any] = {
            "chat_id": s["telegram_chat_id"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": not a.urgent,
        }
        if a.url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": a.button, "url": a.url}]]}
        http = await self._http()
        async with http.post(f"{self.telegram_api}/bot{s['telegram_token']}/sendMessage", json=payload) as r:
            if r.status >= 400:
                try:
                    desc = (await r.json()).get("description", "")
                except Exception:  # noqa: BLE001
                    desc = ""
                if r.status in (400, 401, 403, 404):
                    raise PermanentError(f"Telegram said: {desc or 'HTTP ' + str(r.status)}")
                raise RuntimeError(f"Telegram said HTTP {r.status} {desc}")

    async def telegram_find_chat(self, token: str) -> str | None:
        """After you message your bot once, find your chat id from its updates."""
        http = await self._http()
        async with http.get(f"{self.telegram_api}/bot{token}/getUpdates") as r:
            data = await r.json(content_type=None)
        if not data.get("ok"):
            raise PermanentError(data.get("description") or "Telegram did not accept that bot token")
        for upd in reversed(data.get("result") or []):
            msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                return str(chat["id"])
        return None


def friendly(e: Exception, fn) -> str:
    what = "ntfy" if "ntfy" in getattr(fn, "__name__", "") else "Telegram"
    if isinstance(e, PermanentError):
        return str(e)
    if isinstance(e, asyncio.TimeoutError):
        return f"{what} did not answer in time"
    if isinstance(e, aiohttp.ClientError):
        return f"could not reach {what}. Is this computer online?"
    return str(e) or e.__class__.__name__


class PermanentError(Exception):
    """A failure retrying will not fix (bad token, wrong chat)."""


def beep() -> None:
    """A few loud beeps on this computer, without blocking."""

    def run() -> None:
        try:
            import winsound  # type: ignore[import-not-found]

            for _ in range(6):
                winsound.Beep(1800, 300)
                winsound.Beep(1200, 220)
        except Exception:  # noqa: BLE001
            sys.stdout.write("\a\a\a")
            sys.stdout.flush()

    threading.Thread(target=run, daemon=True).start()
