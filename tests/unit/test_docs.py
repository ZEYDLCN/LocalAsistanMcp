"""Doküman–kod tutarlılığı: README/PLAN'daki iddialar gerçek koddan sapmasın."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mcp import Client

from localasistan.config import Settings
from localasistan.server.app import build_server

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
PLAN = (ROOT / "docs" / "PLAN.md").read_text(encoding="utf-8")


def test_relative_links_and_images_in_readme_exist():
    targets = re.findall(r"\]\((?!https?://|#)([^)\s]+)\)", README)
    assert targets, "README'de göreli bağlantı bulunamadı"
    missing = [t for t in targets if not (ROOT / t.split("#")[0]).exists()]
    assert missing == [], f"README'deki kırık bağlantılar: {missing}"


def test_env_example_documents_every_setting_the_code_reads():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    source = (ROOT / "src" / "localasistan" / "config.py").read_text(encoding="utf-8")
    read = set(re.findall(r'"(LOCALASISTAN_[A-Z_]+|ANTHROPIC_API_KEY)"', source))
    read -= {"LOCALASISTAN_MAX_FILE_BYTES", "LOCALASISTAN_MAX_TOOL_OUTPUT_CHARS", "LOCALASISTAN_TOOL_TIMEOUT"}
    read -= {
        "LOCALASISTAN_APPROVAL_TIMEOUT",
        "LOCALASISTAN_DB_TIMEOUT",
        "LOCALASISTAN_LOG_LEVEL",
        "LOCALASISTAN_THINKING",
    }  # ileri düzey
    undocumented = sorted(name for name in read if name not in example)
    assert undocumented == [], f".env.example'da eksik: {undocumented}"


async def test_counts_in_docs_match_the_real_server(tmp_path):
    settings = Settings.from_env({"LOCALASISTAN_HOME": str(tmp_path)})
    async with Client(build_server(settings)) as client:
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        templates = (await client.list_resource_templates()).resource_templates
        prompts = (await client.list_prompts()).prompts
    groups = {p: sum(t.name.startswith(p) for t in tools) for p in ("fs_", "db_", "calendar_")}
    assert (len(tools), groups) == (25, {"fs_": 11, "db_": 5, "calendar_": 9})
    assert len(resources) + len(templates) == 5 and len(prompts) == 3
    assert "**25 araç** (dosya 11 · veritabanı 5 · takvim 9)" in README
    assert "**5 kaynak/şablon**" in README and "**3 prompt**" in README
    assert "25 araç (dosya 11, veritabanı 5, takvim 9)" in PLAN
    for tool in tools:
        assert f"`{tool.name}`" in README, f"README araç tablosunda yok: {tool.name}"


@pytest.mark.parametrize("name", ["setup.sh", "run.sh", "LocalAsistan.command"])
def test_launch_scripts_are_executable_and_have_a_shebang(name):
    path = ROOT / "scripts" / name
    assert path.read_text().startswith("#!/usr/bin/env bash") and path.stat().st_mode & 0o111
