"""MCP araç tanımlarını (`tools/list`) istemci tarafı modele ve Anthropic araç şemasına çevirir."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config import APPROVAL_POLICIES

_ANTHROPIC_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any] = field(compare=False, hash=False)
    read_only: bool = False
    destructive: bool = True
    idempotent: bool = False

    @property
    def kind(self) -> str:
        """Arayüz rozeti: read | write | destructive."""
        if self.read_only:
            return "read"
        return "destructive" if self.destructive else "write"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "input_schema": self.input_schema,
            "kind": self.kind,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "idempotent": self.idempotent,
        }


def spec_from_mcp(tool: Any) -> ToolSpec:
    """`mcp_types.Tool` -> ToolSpec. Eksik annotation, MCP şartnamesindeki varsayılanlarla (en temkinli) yorumlanır:
    readOnlyHint=false, destructiveHint=true."""
    a = getattr(tool, "annotations", None)
    read_only = bool(a and a.read_only_hint)
    destructive_hint = None if a is None else a.destructive_hint
    destructive = False if read_only else (True if destructive_hint is None else bool(destructive_hint))
    title = (a.title if a and a.title else None) or getattr(tool, "title", None) or tool.name
    return ToolSpec(
        name=tool.name,
        title=title,
        description=(tool.description or "").strip(),
        input_schema=dict(tool.input_schema or {"type": "object", "properties": {}}),
        read_only=read_only,
        destructive=destructive,
        idempotent=bool(a and a.idempotent_hint),
    )


def requires_approval(spec: ToolSpec, policy: str) -> bool:
    if policy not in APPROVAL_POLICIES:
        policy = "ask_destructive"  # bilinmeyen politika => güvenli taraf
    if policy == "auto" or spec.read_only:
        return False
    if policy == "ask_writes":
        return True
    return spec.destructive


def strip_titles(node: Any) -> Any:
    """JSON şemasındaki gürültülü `title` açıklamalarını atar (özellik ADLARI olan "title"a dokunmaz)."""
    if isinstance(node, list):
        return [strip_titles(item) for item in node]
    if not isinstance(node, dict):
        return node
    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs", "definitions", "patternProperties") and isinstance(value, dict):
            cleaned[key] = {name: strip_titles(sub) for name, sub in value.items()}
        else:
            cleaned[key] = strip_titles(value)
    return cleaned


def to_anthropic_tool(spec: ToolSpec) -> dict[str, Any]:
    """Anthropic Messages API araç tanımı (akışlı istek + istemci araçları => eager_input_streaming)."""
    schema = strip_titles(spec.input_schema)
    schema.setdefault("type", "object")
    return {
        "name": spec.name,
        "description": spec.description or spec.title,
        "input_schema": schema,
        "eager_input_streaming": True,
    }


def anthropic_tools(specs: list[ToolSpec]) -> tuple[list[dict[str, Any]], list[str]]:
    """(araç listesi, atlanan adlar). Sıra ada göre sabittir (prompt cache kararlılığı)."""
    tools: list[dict[str, Any]] = []
    skipped: list[str] = []
    for spec in sorted(specs, key=lambda s: s.name):
        if _ANTHROPIC_TOOL_NAME.match(spec.name):
            tools.append(to_anthropic_tool(spec))
        else:
            skipped.append(spec.name)
    return tools, skipped
