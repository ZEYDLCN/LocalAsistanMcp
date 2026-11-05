"""Yerel web arayüzü için saldırı yüzeyi daraltma (ham ASGI ara katmanı — akışı bozmaz).

* **Host denetimi (DNS rebinding):** yalnızca loopback adları + yapılandırılan bağlama adresi.
* **Origin denetimi (CSRF/çapraz kaynak):** `Origin` varsa Host ile birebir aynı olmalı.
* **İçerik türü:** durum değiştiren isteklerde `application/json` zorunlu (CORS "basit istek" hilesini kapatır;
  başka bir site bu başlıkla ön-kontrolsüz istek atamaz).
* Güvenlik başlıkları: katı CSP, nosniff, çerçevelenme yasağı, referrer yok.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOOPBACK = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


def split_host(value: str) -> tuple[str, int | None]:
    """`host[:port]` -> (küçük harfli host, port|None). IPv6 köşeli parantezli olabilir."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        host = value[: end + 1]
        rest = value[end + 1 :]
        return host, int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
    host, sep, port = value.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return value, None


class LocalOnlyMiddleware:
    def __init__(self, app: ASGIApp, *, port: int, extra_hosts: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.port = port
        self.hosts = LOOPBACK | {h.lower() for h in extra_hosts}

    def _host_ok(self, header: str) -> bool:
        host, port = split_host(header)
        return host in self.hosts and (port is None or port == self.port)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        if not self._host_ok(host):
            await self._reject(send, 403, "Invalid Host header (this interface is local-only).")
            return
        origin = headers.get("origin")
        if origin is not None:
            parsed = urlsplit(origin)
            origin_host = parsed.netloc.lower()
            if origin == "null" or parsed.scheme not in ("http", "https") or origin_host != host.strip().lower():
                await self._reject(send, 403, "Cross-origin requests are not allowed.")
                return
        if scope["method"] in _UNSAFE_METHODS:
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                await self._reject(send, 415, "Content-Type must be application/json.")
                return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                message = {
                    **message,
                    "headers": [*message.get("headers", []), *(h for h in _SECURITY_HEADERS if h[0] not in existing)],
                }
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _reject(send: Send, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
