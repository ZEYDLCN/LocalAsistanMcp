"""OpenAI uyumlu (Chat Completions) sağlayıcı.

NVIDIA build.nvidia.com, OpenRouter, Groq, vLLM, Ollama, LM Studio gibi `POST {base}/chat/completions` uç noktasını
konuşan her hizmetle çalışır. Ajan döngüsü geçmişi **Anthropic Messages biçiminde** tutar; bu sağlayıcı geçmişi ve
araçları gidişte Chat Completions biçimine çevirir, akışı yine Anthropic biçimli bloklara (`text`, `tool_use`)
toplayıp `AssistantTurn` olarak döndürür. Böylece onay, iptal, geçmiş onarımı gibi her şey değişmeden çalışır.

Bilinçli kararlar:
* Yalnızca `httpx2` kullanılır (anthropic ve mcp zaten ona bağlı); ek bağımlılık yok.
* Düşünme içeriği (`reasoning_content`) geçmişe geri yazılmaz; yalnızca "düşünüyor" durumu gösterilir.
* Kesilmiş (`length`) turdaki araç çağrıları çalıştırılmaz (ajan döngüsü bunu zaten böyle ele alır).
* 429/502/503/504 en fazla iki kez (Retry-After'a saygılı) yeniden denenir; hiçbir çıktı üretilmeden önce.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx2

from .. import __version__
from ..config import Settings
from .events import Event
from .providers import AssistantTurn, PartialToolJsonError, ProviderError, ToolUse

_RETRYABLE = {429, 502, 503, 504}
_MAX_RETRY_AFTER = 30.0
_STOP_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class _RetryableError(ProviderError):
    def __init__(self, kind: str, message: str, retry_after: float | None) -> None:
        super().__init__(kind, message)
        self.retry_after = retry_after


# ------------------------------------------------------------------------ şema / mesaj dönüşümü
def simplify_schema(node: Any) -> Any:
    """Sıkı sunucuların (vLLM/NIM ayrıştırıcıları) sevmediği `anyOf: [X, null]` kalıbını `X`e indirger.

    İsteğe bağlılık zaten `required` listesinin dışında kalmakla ifade edilir; `default: null` da atılır."""
    if isinstance(node, list):
        return [simplify_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("properties", "$defs", "definitions", "patternProperties") and isinstance(value, dict):
            out[key] = {name: simplify_schema(sub) for name, sub in value.items()}
        else:
            out[key] = simplify_schema(value)
    options = out.get("anyOf")
    if isinstance(options, list) and len(options) == 2:
        real = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(real) == 1 and isinstance(real[0], dict):
            merged = {k: v for k, v in out.items() if k != "anyOf"}
            merged.update(real[0])
            if merged.get("default") is None:
                merged.pop("default", None)
            return merged
    return out


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic araç tanımları (`name`/`description`/`input_schema`) -> Chat Completions `tools`."""
    converted = []
    for tool in tools:
        schema = simplify_schema(tool.get("input_schema") or {"type": "object", "properties": {}})
        converted.append(
            {
                "type": "function",
                "function": {"name": tool["name"], "description": tool.get("description", ""), "parameters": schema},
            }
        )
    return converted


def _block_text(content: Any) -> str:
    """`tool_result.content` (str ya da metin blokları) -> düz metin."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(getattr(b, "text", "")) for b in content)
    return "" if content is None else str(content)


def _field(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic biçimli geçmiş -> Chat Completions `messages`.

    * `tool_result` blokları, ilgili asistan mesajının hemen ardından `role: tool` mesajları olur
      (varsa kullanıcının ek metni onlardan SONRA gelir).
    * `tool_use` blokları `tool_calls`a, argümanlar JSON metnine çevrilir.
    * `thinking`/`redacted_thinking` gibi sağlayıcıya özgü bloklar atılır."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "user":
            tool_messages: list[dict[str, Any]] = []
            texts: list[str] = []
            for block in content:
                kind = _field(block, "type")
                if kind == "tool_result":
                    body = _block_text(_field(block, "content"))
                    if _field(block, "is_error"):
                        body = f"ERROR: {body}"
                    tool_messages.append(
                        {"role": "tool", "tool_call_id": _field(block, "tool_use_id"), "content": body}
                    )
                elif kind == "text":
                    texts.append(_field(block, "text", ""))
            out.extend(tool_messages)
            if any(t for t in texts):
                out.append({"role": "user", "content": "\n".join(texts)})
            continue
        # assistant
        texts = []
        calls: list[dict[str, Any]] = []
        for block in content:
            kind = _field(block, "type")
            if kind == "text":
                texts.append(_field(block, "text", ""))
            elif kind == "tool_use":
                calls.append(
                    {
                        "id": _field(block, "id"),
                        "type": "function",
                        "function": {
                            "name": _field(block, "name"),
                            "arguments": json.dumps(_field(block, "input") or {}, ensure_ascii=False),
                        },
                    }
                )
        text = "".join(texts)
        if not text and not calls:
            continue  # boş asistan mesajı bazı sunucularda 400 üretir
        entry: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            entry["tool_calls"] = calls
        out.append(entry)
    return out


# ------------------------------------------------------------------------------ hata eşleme
def _error_text(raw: str) -> str:
    try:
        body = json.loads(raw)
    except ValueError:
        return raw.strip()[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:400]
        for key in ("detail", "message", "error", "title"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:400]
            if isinstance(value, list) and value:
                return json.dumps(value, ensure_ascii=False)[:400]
    return raw.strip()[:300]


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, min(float(raw), _MAX_RETRY_AFTER))
    except ValueError:
        return None


def build_headers(settings: Settings, *, stream: bool) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": f"localasistan/{__version__}"}
    if stream:
        headers["Accept"] = "text/event-stream"
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    return headers


def http_error(settings: Settings, status: int, raw: str, headers: Mapping[str, str]) -> ProviderError:
    detail = _error_text(raw)
    if status == 401:
        return ProviderError(
            "auth", f"The server rejected the API key (HTTP 401). Check LOCALASISTAN_API_KEY. {detail}"
        )
    if status == 403:
        return ProviderError("permission", f"Access denied (HTTP 403): {detail}")
    if status == 404:
        return ProviderError(
            "model",
            f"Not found (HTTP 404): {detail} Check LOCALASISTAN_BASE_URL and LOCALASISTAN_MODEL "
            f"('{settings.model}'); `localasistan models` lists the valid ids.",
        )
    if status in (400, 422):
        hint = ""
        if "tool" in detail.lower():
            hint = " This model or server may not support tool calling; try another model."
        return ProviderError("bad_request", f"The server rejected the request (HTTP {status}): {detail}{hint}")
    if status == 429:
        return _RetryableError("rate_limit", "Rate limit reached. Wait a moment and try again.", _retry_after(headers))
    if status in _RETRYABLE:
        return _RetryableError(
            "server", f"The server is having trouble (HTTP {status}). Try again.", _retry_after(headers)
        )
    if status >= 500:
        return ProviderError("server", f"The server is having trouble (HTTP {status}): {detail}")
    return ProviderError("api", f"Unexpected response (HTTP {status}): {detail}")


def _connection_error(exc: Exception) -> ProviderError:
    if isinstance(exc, httpx2.TimeoutException):
        return ProviderError("connection", "The server did not respond in time. Try again.")
    return ProviderError(
        "connection", f"Could not reach the server ({type(exc).__name__}). Check your network and URL."
    )


# ---------------------------------------------------------------------------------- sağlayıcı
class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        settings: Settings,
        client: httpx2.AsyncClient | None = None,
        *,
        retry_delays: tuple[float, ...] = (1.0, 3.0),
    ) -> None:
        self.settings = settings
        self._client = client
        self._retry_delays = retry_delays
        self._include_usage = True  # bazı sunucular `stream_options`ı reddeder: ilk 400'de kapatılır

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.base_url and self.settings.model)

    @property
    def missing_setting(self) -> str | None:
        if not self.settings.base_url:
            return "LOCALASISTAN_BASE_URL"
        if not self.settings.model:
            return "LOCALASISTAN_MODEL"
        return None

    def _get_client(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=httpx2.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0))
        return self._client

    def build_body(self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": to_openai_messages(system, messages),
            "max_tokens": self.settings.max_tokens,
            "stream": True,
        }
        if self._include_usage:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = to_openai_tools(tools)
        return body

    async def stream_turn(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> AsyncIterator[Event | AssistantTurn]:
        client = self._get_client()
        url = f"{self.settings.base_url}/chat/completions"
        headers = build_headers(self.settings, stream=True)
        attempt = 0
        while True:
            body = self.build_body(system=system, tools=tools, messages=messages)
            error: ProviderError | None = None
            try:
                async with client.stream("POST", url, json=body, headers=headers) as response:
                    if response.status_code < 400:
                        async for item in self._consume(response):
                            yield item
                        return
                    raw = (await response.aread()).decode("utf-8", "replace")
                    error = http_error(self.settings, response.status_code, raw, response.headers)
            except httpx2.HTTPError as exc:
                raise _connection_error(exc) from exc
            if error is None:  # pragma: no cover - başarılı yanıt yukarıda `return` eder
                return
            if self._include_usage and error.kind == "bad_request" and "stream_options" in str(error).lower():
                self._include_usage = False  # sunucu desteklemiyor: bir daha gönderme, hemen yeniden dene
                continue
            if isinstance(error, _RetryableError) and attempt < len(self._retry_delays):
                delay = error.retry_after if error.retry_after is not None else self._retry_delays[attempt]
                attempt += 1
                yield Event("notice", {"level": "info", "message": f"{error.message} Retrying in {delay:.0f}s…"})
                await asyncio.sleep(delay)
                continue
            raise ProviderError(error.kind, error.message)

    # ----------------------------------------------------------------------------- akış ayrıştırma
    async def _consume(self, response: httpx2.Response) -> AsyncIterator[Event | AssistantTurn]:
        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish: str | None = None
        usage: dict[str, int] = {}
        model: str | None = None
        thinking_announced = False
        saw_done = False

        try:
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue  # boş satır, `: keep-alive` yorumu, `event:` vb.
                payload = line[5:].strip()
                if payload == "[DONE]":
                    saw_done = True
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error") and not chunk.get("choices"):
                    raise ProviderError("api", f"The server reported an error: {_error_text(json.dumps(chunk))}")
                model = chunk.get("model") or model
                raw_usage = chunk.get("usage")
                if isinstance(raw_usage, dict):
                    usage = {
                        "input_tokens": int(raw_usage.get("prompt_tokens") or 0),
                        "output_tokens": int(raw_usage.get("completion_tokens") or 0),
                    }
                for choice in chunk.get("choices") or []:
                    if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                        continue  # n>1 desteklenmez: yalnızca ilk seçenek
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        text_parts.append(piece)
                        yield Event("text_delta", {"text": piece})
                    if not thinking_announced and (delta.get("reasoning_content") or delta.get("reasoning")):
                        thinking_announced = True
                        yield Event("status", {"state": "thinking"})
                    for call in delta.get("tool_calls") or []:
                        started = self._merge_call(calls, call)
                        if started:
                            yield Event("status", {"state": "preparing_tool", "name": started})
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        except httpx2.HTTPError as exc:
            raise _connection_error(exc) from exc

        if finish is None and not saw_done:
            raise ProviderError("connection", "The response stream ended unexpectedly. Try again.")
        yield self._to_turn(text_parts, calls, finish, usage, model)

    @staticmethod
    def _merge_call(calls: dict[int, dict[str, Any]], delta: Any) -> str | None:
        """Parça halinde gelen bir `tool_calls` öğesini biriktirir; yeni bir çağrı başladıysa adını döndürür."""
        if not isinstance(delta, dict):
            return None
        index = delta.get("index")
        if index is None:
            known = [i for i, c in calls.items() if delta.get("id") and c["id"] == delta["id"]]
            index = known[0] if known else len(calls)
        slot = calls.setdefault(index, {"id": None, "name": "", "args": []})
        if delta.get("id") and not slot["id"]:
            slot["id"] = delta["id"]
        function = delta.get("function") or {}
        started = None
        if function.get("name") and not slot["name"]:
            slot["name"] = function["name"]  # bazı sunucular adı her parçada tekrarlar: yalnızca ilkini al
            started = slot["name"]
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["args"].append(arguments)
        elif isinstance(arguments, dict):
            slot["args"].append(json.dumps(arguments))
        return started

    def _to_turn(
        self,
        text_parts: list[str],
        calls: dict[int, dict[str, Any]],
        finish: str | None,
        usage: dict[str, int],
        model: str | None,
    ) -> AssistantTurn:
        text = "".join(text_parts)
        content: list[Any] = []
        if text:
            content.append({"type": "text", "text": text})
        tool_uses: list[ToolUse] = []
        used_ids: set[str] = set()
        for index in sorted(calls):
            slot = calls[index]
            if not slot["name"]:
                continue  # adsız çağrı çalıştırılamaz
            raw_args = "".join(slot["args"]).strip() or "{}"
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be a JSON object")
            except ValueError as exc:
                if finish == "length":
                    args = {}  # kesilmiş: ajan döngüsü max_tokens dalında çalıştırmadan hata döndürür
                else:
                    raise PartialToolJsonError(f"{slot['name']}: {exc}") from exc
            call_id = slot["id"] if slot["id"] and slot["id"] not in used_ids else f"call_{uuid.uuid4().hex[:12]}"
            used_ids.add(call_id)
            content.append({"type": "tool_use", "id": call_id, "name": slot["name"], "input": args})
            tool_uses.append(ToolUse(call_id, slot["name"], args))

        stop = _STOP_REASONS.get(finish or "", "end_turn")
        if tool_uses and stop == "end_turn":
            stop = "tool_use"  # bazı sunucular araç çağrısı olsa da `stop` döndürür
        refusal = {"category": "content_filter", "explanation": None} if finish == "content_filter" else None
        return AssistantTurn(
            content=content,
            text=text,
            tool_uses=tool_uses,
            stop_reason=stop,
            usage=usage,
            model=model or self.settings.model,
            refusal=refusal,
        )


# -------------------------------------------------------------------------------- model listesi
def list_models(settings: Settings, client: httpx2.Client | None = None) -> list[str]:
    """`GET {base}/models` -> model kimlikleri (sıralı, tekrarsız). `localasistan models` komutu kullanır."""
    if not settings.base_url:
        raise ProviderError("config", "LOCALASISTAN_BASE_URL is not set.")
    owned = client is None
    http = client or httpx2.Client(timeout=httpx2.Timeout(15.0))
    try:
        response = http.get(f"{settings.base_url}/models", headers=build_headers(settings, stream=False))
    except httpx2.HTTPError as exc:
        raise _connection_error(exc) from exc
    finally:
        if owned:
            http.close()
    if response.status_code >= 400:
        err = http_error(settings, response.status_code, response.text, response.headers)
        raise ProviderError(err.kind, err.message)
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderError("api", "The server returned a non-JSON model list.") from exc
    items = body.get("data", []) if isinstance(body, dict) else body
    ids = {m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)}
    return sorted(ids)
