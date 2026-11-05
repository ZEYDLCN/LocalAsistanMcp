from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp_types import Tool, ToolAnnotations

from localasistan.client.catalog import (
    ToolSpec,
    anthropic_tools,
    requires_approval,
    spec_from_mcp,
    strip_titles,
    to_anthropic_tool,
)


def tool(name="t", annotations=None, **kw) -> Tool:
    return Tool(
        name=name,
        description=kw.get("description", "d"),
        input_schema={"type": "object", "properties": {}},
        annotations=annotations,
    )


class TestSpecFromMcp:
    def test_read_only(self):
        spec = spec_from_mcp(tool(annotations=ToolAnnotations(read_only_hint=True)))
        assert (spec.read_only, spec.destructive, spec.kind) == (True, False, "read")

    def test_additive_write(self):
        spec = spec_from_mcp(tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False)))
        assert (spec.read_only, spec.destructive, spec.kind) == (False, False, "write")

    def test_destructive(self):
        spec = spec_from_mcp(tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True)))
        assert spec.kind == "destructive"

    @pytest.mark.parametrize("annotations", [None, ToolAnnotations(), ToolAnnotations(read_only_hint=False)])
    def test_missing_hints_follow_the_mcp_spec_defaults_most_cautious(self, annotations):
        spec = spec_from_mcp(tool(annotations=annotations))
        assert spec.read_only is False and spec.destructive is True

    def test_read_only_wins_over_contradicting_destructive_hint(self):
        spec = spec_from_mcp(tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=True)))
        assert spec.destructive is False and spec.kind == "read"

    def test_title_resolution_and_idempotent(self):
        assert (
            spec_from_mcp(tool(annotations=ToolAnnotations(title="Güzel Ad", idempotent_hint=True))).title == "Güzel Ad"
        )
        assert spec_from_mcp(tool(name="plain")).title == "plain"
        assert spec_from_mcp(tool(annotations=ToolAnnotations(idempotent_hint=True))).idempotent is True

    def test_missing_description_and_schema(self):
        bare = SimpleNamespace(name="x", description=None, input_schema=None, annotations=None, title=None)
        spec = spec_from_mcp(bare)
        assert spec.description == "" and spec.input_schema == {"type": "object", "properties": {}}


def spec(read_only=False, destructive=True) -> ToolSpec:
    return ToolSpec("x", "x", "d", {"type": "object"}, read_only=read_only, destructive=destructive)


class TestApprovalMatrix:
    @pytest.mark.parametrize(
        ("kind", "policy", "expected"),
        [
            ("read", "ask_destructive", False),
            ("read", "ask_writes", False),
            ("read", "auto", False),
            ("write", "ask_destructive", False),
            ("write", "ask_writes", True),
            ("write", "auto", False),
            ("destructive", "ask_destructive", True),
            ("destructive", "ask_writes", True),
            ("destructive", "auto", False),
        ],
    )
    def test_matrix(self, kind, policy, expected):
        s = {"read": spec(True, False), "write": spec(False, False), "destructive": spec(False, True)}[kind]
        assert requires_approval(s, policy) is expected

    def test_unknown_policy_falls_back_to_the_safe_default(self):
        assert requires_approval(spec(False, True), "whatever") is True
        assert requires_approval(spec(False, False), "whatever") is False
        assert requires_approval(spec(True, False), "whatever") is False


class TestSchemaConversion:
    def test_strip_titles_keeps_property_named_title(self):
        schema = {
            "title": "createArguments",
            "type": "object",
            "properties": {
                "title": {"title": "Title", "type": "string", "description": "Event title"},
                "n": {"title": "N", "anyOf": [{"type": "integer", "title": "Int"}, {"type": "null"}]},
            },
            "required": ["title"],
            "$defs": {"Thing": {"title": "Thing", "type": "object", "properties": {"title": {"type": "string"}}}},
        }
        cleaned = strip_titles(schema)
        assert "title" not in cleaned and cleaned["required"] == ["title"]
        assert cleaned["properties"]["title"] == {"type": "string", "description": "Event title"}
        assert cleaned["properties"]["n"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
        assert cleaned["$defs"]["Thing"] == {"type": "object", "properties": {"title": {"type": "string"}}}

    def test_strip_titles_does_not_mutate_input(self):
        schema = {"title": "X", "type": "object", "properties": {}}
        strip_titles(schema)
        assert schema["title"] == "X"

    def test_anthropic_tool_shape(self):
        s = ToolSpec(
            "fs_read",
            "Read",
            "Reads a file.",
            {"title": "A", "type": "object", "properties": {"p": {"type": "string"}}},
        )
        assert to_anthropic_tool(s) == {
            "name": "fs_read",
            "description": "Reads a file.",
            "input_schema": {"type": "object", "properties": {"p": {"type": "string"}}},
            "eager_input_streaming": True,
        }

    def test_empty_description_falls_back_to_title_and_schema_gets_type(self):
        s = ToolSpec("x", "Pretty", "", {"properties": {}})
        converted = to_anthropic_tool(s)
        assert converted["description"] == "Pretty" and converted["input_schema"]["type"] == "object"

    def test_sorted_and_invalid_names_skipped(self):
        specs = [
            ToolSpec(n, n, "d", {"type": "object"}) for n in ["zeta", "alpha", "bad name", "x" * 65, "ok-1_a", "çay"]
        ]
        tools, skipped = anthropic_tools(specs)
        assert [t["name"] for t in tools] == ["alpha", "ok-1_a", "zeta"]
        assert set(skipped) == {"bad name", "x" * 65, "çay"}
