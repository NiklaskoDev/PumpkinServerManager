"""HTTP helpers for the Java side: JSON lookups with retries, and downloads that are checked before they are trusted."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import __version__
from ..errors import UserError
from ..util import human_bytes

log = logging.getLogger(__name__)

USER_AGENT = f"NiklaskoDev/PumpkinServerManager/{__version__} (+https://github.com/NiklaskoDev/PumpkinServerManager)"
ProgressCallback = Callable[[int, int], None]


class DownloadError(UserError):
    """A lookup or download failed; the message is safe to show to users."""


class BadDownloadError(DownloadError):
    """The file arrived but is not what was promised (size or checksum), so trying the same URL again cannot help."""


def new_client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=15.0), headers={"User-Agent": USER_AGENT}, follow_redirects=True, **kwargs
    )


def require_https(url: str, allowed_hosts: frozenset[str] | None = None) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise BadDownloadError(f"Refusing to download from {url!r}: only https:// addresses are allowed.")
    if allowed_hosts is not None and parts.hostname.lower() not in allowed_hosts:
        raise BadDownloadError(f"Refusing to download from {parts.hostname}: it is not one of the allowed hosts.")


async def get_json(
    client: httpx.AsyncClient, url: str, *, params: dict[str, Any] | None = None, what: str = "the server",
    attempts: int = 3, delay: float = 1.5, body: Any = None,
) -> Any:
    """GET ``url`` (or POST ``body`` as JSON when given) and return the JSON answer; transient failures are retried."""
    last: str = ""
    for attempt in range(attempts):
        try:
            resp = await client.get(url, params=params) if body is None else await client.post(url, params=params, json=body)
        except httpx.HTTPError as exc:
            last = f"could not reach {what}: {exc or exc.__class__.__name__}"
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise DownloadError(f"{what} sent something that is not JSON.") from None
            if resp.status_code == 404:
                raise DownloadError(f"{what} does not know that (HTTP 404).")
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise DownloadError(f"{what} answered HTTP {resp.status_code}.")
            last = f"{what} answered HTTP {resp.status_code}"
        if attempt + 1 < attempts:
            await asyncio.sleep(delay * (attempt + 1))
    raise DownloadError(last[:1].upper() + last[1:] + ".")


async def download(
    client: httpx.AsyncClient, url: str, dest: Path, *, size: int | None = None, sha1: str | None = None,
    sha256: str | None = None, sha512: str | None = None, progress: ProgressCallback | None = None,
    allowed_hosts: frozenset[str] | None = None, attempts: int = 3, delay: float = 2.0, what: str = "the file",
    max_size: int | None = None,
) -> None:
    """Stream ``url`` to ``dest`` and check size and checksums; ``dest`` only stays behind when everything matched.

    ``size`` must match exactly. ``max_size`` is only a ceiling for sources whose declared size is not reliable: the download
    stops as soon as more arrives, so a wrong or hostile address cannot fill the disk."""
    require_https(url, allowed_hosts)
    expected = {name: value.lower() for name, value in (("sha1", sha1), ("sha256", sha256), ("sha512", sha512)) if value}
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        hashers = {name: hashlib.new(name) for name in expected}
        received = 0
        try:
            async with client.stream("GET", url, timeout=httpx.Timeout(60.0, connect=15.0)) as resp:
                if resp.status_code == 404:
                    raise DownloadError(f"Downloading {what} failed: it is not there any more (HTTP 404).")
                if resp.status_code != 200:
                    raise httpx.HTTPError(f"HTTP {resp.status_code}")
                total = int(resp.headers.get("content-length") or size or 0)
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        await asyncio.to_thread(fh.write, chunk)
                        for hasher in hashers.values():
                            hasher.update(chunk)
                        received += len(chunk)
                        if max_size is not None and received > max_size:
                            raise BadDownloadError(f"Downloading {what} was stopped: it is much larger than the pack says ({human_bytes(max_size)} at most).")
                        if progress:
                            progress(received, total or size or 0)
            last = None
            break
        except DownloadError:
            dest.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            log.warning("Downloading %s: attempt %d/%d failed: %s", what, attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(delay * attempt)
    if last is not None:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"Downloading {what} failed: {last}") from last
    if size and received != size:
        dest.unlink(missing_ok=True)
        raise BadDownloadError(f"Downloading {what} was incomplete ({received} of {size} bytes).")
    for name, wanted in expected.items():
        if hashers[name].hexdigest() != wanted:
            dest.unlink(missing_ok=True)
            raise BadDownloadError(f"Downloading {what}: the {name.upper()} checksum does not match, so the file was discarded.")
