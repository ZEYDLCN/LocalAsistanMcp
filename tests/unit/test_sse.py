from __future__ import annotations

import asyncio

import pytest
from starlette.responses import StreamingResponse

from localasistan.web.sse import SSEResponse


def scope(spec: str = "2.4") -> dict:
    return {"type": "http", "asgi": {"spec_version": spec}, "method": "POST", "path": "/"}


class Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")


async def never(*_):
    await asyncio.sleep(3600)
    return {"type": "http.disconnect"}


async def test_normal_completion_sends_all_chunks_and_closes_the_generator():
    closed: list[bool] = []

    async def gen():
        try:
            yield "data: 1\n\n"
            yield "data: 2\n\n"
        finally:
            closed.append(True)

    rec = Recorder()
    await SSEResponse(gen())(scope(), never, rec.send)
    assert rec.body == b"data: 1\n\ndata: 2\n\n"
    assert rec.messages[0]["status"] == 200
    headers = dict(rec.messages[0]["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream") and headers[b"cache-control"] == b"no-store"
    assert rec.messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert closed == [True]


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_client_gone_while_generator_is_suspended_at_yield_still_runs_finally(spec):
    """Sorun senaryosu: send() başarısız olur, üreteç `yield`'de askıda kalır."""
    closed: list[bool] = []

    async def gen():
        try:
            yield "data: 1\n\n"
            await asyncio.sleep(30)
            yield "data: 2\n\n"
        finally:
            closed.append(True)

    sent = 0

    async def send(message: dict) -> None:
        nonlocal sent
        if message["type"] == "http.response.body":
            sent += 1
            raise OSError("client disconnected")

    await asyncio.wait_for(SSEResponse(gen())(scope(spec), never, send), 5)
    assert sent == 1 and closed == [True]


async def test_plain_streaming_response_leaves_the_generator_suspended_on_oserror():
    """Karşılaştırma: Starlette'in kendi sınıfı bu durumda finally'yi çalıştırmaz (SSEResponse'un varlık nedeni)."""
    closed: list[bool] = []

    async def gen():
        try:
            yield "data: 1\n\n"
            await asyncio.sleep(30)
        finally:
            closed.append(True)

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    generator = gen()
    with pytest.raises(Exception):  # noqa: B017 - ClientDisconnect
        await StreamingResponse(generator)(scope("2.4"), never, send)
    assert closed == []  # üreteç hâlâ askıda
    await generator.aclose()


async def test_disconnect_message_cancels_a_generator_that_is_waiting():
    closed: list[bool] = []
    started = asyncio.Event()

    async def gen():
        try:
            yield "data: hello\n\n"
            started.set()
            await asyncio.sleep(30)  # ör. onay bekliyor
        finally:
            closed.append(True)

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    rec = Recorder()
    await asyncio.wait_for(SSEResponse(gen())(scope("2.3"), receive, rec.send), 5)
    assert closed == [True] and rec.body == b"data: hello\n\n"


async def test_generator_error_propagates_but_is_closed():
    closed: list[bool] = []

    async def gen():
        try:
            yield "data: 1\n\n"
            raise RuntimeError("boom")
        finally:
            closed.append(True)

    with pytest.raises(BaseException) as info:
        await SSEResponse(gen())(scope(), never, Recorder().send)
    assert "boom" in repr(info.value) and closed == [True]
