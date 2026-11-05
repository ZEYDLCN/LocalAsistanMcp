"""Ajan döngüsünün arayüze/CLI'ye akıttığı olaylar (SSE ile JSON olarak taşınır)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# turn_start · status · text_delta · tool_call · approval_required · approval_resolved ·
# tool_result · usage · notice · error · turn_end
EVENT_TYPES = frozenset(
    {
        "turn_start",
        "status",
        "text_delta",
        "tool_call",
        "approval_required",
        "approval_resolved",
        "tool_result",
        "usage",
        "notice",
        "error",
        "turn_end",
    }
)


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}

    def to_sse(self) -> str:
        return f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"
