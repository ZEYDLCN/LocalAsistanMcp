"""Server-Sent Events yanıtı — istemci kopunca üreteç zincirini **deterministik** kapatır.

Starlette'in `StreamingResponse`'u bağlantı koptuğunda gövde üretecini `aclose()` etmez; üreteç
`yield`'de askıdaysa `finally` blokları (ör. oturumu "meşgul" bırakmamak, bekleyen onayı iptal etmek)
ancak çöp toplama zamanında çalışırdı. Burada kopma dinlenir ve üreteç her durumda kapatılır.
"""

from __future__ import annotations

import contextlib

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class SSEResponse(StreamingResponse):
    def __init__(self, content, status_code: int = 200) -> None:
        super().__init__(
            content,
            status_code=status_code,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with anyio.create_task_group() as group:

                async def stream() -> None:
                    with contextlib.suppress(OSError):  # istemci gitti
                        await self.stream_response(send)
                    group.cancel_scope.cancel()

                async def watch() -> None:
                    await self.listen_for_disconnect(receive)
                    group.cancel_scope.cancel()

                group.start_soon(stream)
                group.start_soon(watch)
        finally:
            with anyio.CancelScope(shield=True):
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
        if self.background is not None:
            await self.background()
