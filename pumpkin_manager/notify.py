"""Optional outgoing webhook notifications (Discord, Slack-style or plain JSON)."""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from . import __version__
from .models import AppSettings
from .util import iso

log = logging.getLogger(__name__)

_COLORS = {"info": 0x3B82F6, "success": 0x22C55E, "warning": 0xF59E0B, "error": 0xEF4444}


class Notifier:
    def __init__(self, settings_getter: Callable[[], AppSettings], client: httpx.AsyncClient | None = None):
        self._settings = settings_getter
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0), headers={"User-Agent": f"pumpkin-server-manager/{__version__}"}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, event: str, title: str, message: str, level: str = "info", server: str | None = None) -> bool:
        cfg = self._settings()
        if not cfg.webhook_url or (cfg.webhook_events and event not in cfg.webhook_events):
            return False
        return await self._post(cfg.webhook_url, event, title, message, level, server)

    async def test(self, url: str) -> None:
        await self._post(url, "test", "Pumpkin Server Manager", "Webhook test message - notifications work.", "success", None, raise_errors=True)

    async def _post(
        self, url: str, event: str, title: str, message: str, level: str, server: str | None, raise_errors: bool = False
    ) -> bool:
        text = f"{title}\n{message}" if message else title
        if server:
            text = f"[{server}] {text}"
        if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
            body: dict = {
                "username": "Pumpkin Manager",
                "embeds": [{
                    "title": title[:250], "description": (message or "")[:3500], "color": _COLORS.get(level, _COLORS["info"]),
                    "footer": {"text": server or "Pumpkin Server Manager"}, "timestamp": iso(),
                }],
            }
        elif "hooks.slack.com" in url:
            body = {"text": text}
        else:
            body = {"event": event, "level": level, "title": title, "message": message, "server": server, "text": text, "timestamp": iso()}
        try:
            resp = await self._client.post(url, json=body)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("Webhook delivery failed: %s", exc)
            if raise_errors:
                raise
            return False
