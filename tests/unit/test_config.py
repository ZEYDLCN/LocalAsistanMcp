from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from localasistan.config import (
    DEFAULT_MODEL,
    Settings,
    detect_timezone,
    parse_dotenv,
    validate_timezone,
)
from localasistan.errors import ConfigError


class TestDotenv:
    def test_basic_pairs_and_comments(self):
        text = "\n# yorum\nFOO=bar\nexport BAZ=qux\n  SPACED = value with spaces  \nEMPTY=\n"
        assert parse_dotenv(text) == {"FOO": "bar", "BAZ": "qux", "SPACED": "value with spaces", "EMPTY": ""}

    def test_quotes_and_inline_comment(self):
        text = "A=\"x # not comment\"\nB='single'\nC=plain # trailing\nD=a#b\n"
        assert parse_dotenv(text) == {"A": "x # not comment", "B": "single", "C": "plain", "D": "a#b"}

    def test_invalid_lines_ignored(self):
        assert parse_dotenv("no equals\n=novalue\nBAD KEY=1\nOK=1") == {"OK": "1"}


class TestTimezone:
    def test_explicit_valid(self):
        assert detect_timezone({"LOCALASISTAN_TZ": "Europe/Istanbul"}) == "Europe/Istanbul"

    def test_explicit_invalid_raises(self):
        with pytest.raises(ConfigError, match="Unknown timezone"):
            detect_timezone({"LOCALASISTAN_TZ": "Mars/Olympus"})

    def test_tz_env_valid_and_colon_prefix(self):
        assert detect_timezone({"TZ": ":Europe/Berlin"}) == "Europe/Berlin"

    def test_tz_env_posix_string_ignored_falls_back(self, tmp_path: Path):
        assert detect_timezone({"TZ": "CET-1CEST"}, tmp_path / "missing") == "UTC"

    def test_localtime_symlink_macos_style(self, tmp_path: Path):
        zone = tmp_path / "var" / "db" / "timezone" / "zoneinfo" / "Europe" / "Istanbul"
        zone.parent.mkdir(parents=True)
        zone.write_bytes(b"")
        link = tmp_path / "localtime"
        link.symlink_to(zone)
        assert detect_timezone({}, link) == "Europe/Istanbul"

    def test_localtime_unusable_falls_back_to_utc(self, tmp_path: Path):
        plain = tmp_path / "localtime"
        plain.write_bytes(b"not a symlink")
        assert detect_timezone({}, plain) == "UTC"

    def test_validate_timezone(self):
        assert validate_timezone("UTC") == "UTC"
        with pytest.raises(ConfigError):
            validate_timezone("")


class TestSettings:
    def test_defaults(self, tmp_path: Path):
        s = Settings.from_env({"LOCALASISTAN_HOME": str(tmp_path)}, localtime_path=tmp_path / "x")
        assert s.home == tmp_path
        assert s.workspace == tmp_path / "workspace"
        assert s.calendar_db == tmp_path / "calendar.db"
        assert s.user_db == tmp_path / "userdata.db"
        assert s.model == DEFAULT_MODEL == "claude-opus-5-5"
        assert s.effort == "medium"
        assert s.approval == "ask_destructive"
        assert s.refusal_fallback is True
        assert s.thinking is True
        assert s.api_key is None and not s.has_api_key
        assert s.port == 8765 and s.host == "127.0.0.1"

    def test_overrides_and_workspace(self, tmp_path: Path):
        env = {
            "LOCALASISTAN_HOME": str(tmp_path),
            "LOCALASISTAN_WORKSPACE": str(tmp_path / "ws"),
            "LOCALASISTAN_TZ": "Europe/Istanbul",
            "LOCALASISTAN_MODEL": "claude-sonnet-5-5",
            "LOCALASISTAN_EFFORT": "HIGH",
            "LOCALASISTAN_APPROVAL": "ask_writes",
            "LOCALASISTAN_REFUSAL_FALLBACK": "0",
            "LOCALASISTAN_THINKING": "off",
            "LOCALASISTAN_PORT": "9000",
            "LOCALASISTAN_MAX_ITERATIONS": "3",
            "ANTHROPIC_API_KEY": "sk-test",
        }
        s = Settings.from_env(env)
        assert s.workspace == tmp_path / "ws"
        assert s.timezone == "Europe/Istanbul"
        assert (s.model, s.effort, s.approval) == ("claude-sonnet-5-5", "high", "ask_writes")
        assert s.refusal_fallback is False and s.thinking is False
        assert s.port == 9000 and s.max_iterations == 3
        assert s.api_key == "sk-test"

    def test_tilde_expansion(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HOME", str(tmp_path))
        s = Settings.from_env({"LOCALASISTAN_HOME": "~/la"})
        assert s.home == tmp_path / "la"

    @pytest.mark.parametrize(
        ("var", "value"),
        [
            ("LOCALASISTAN_EFFORT", "ultra"),
            ("LOCALASISTAN_APPROVAL", "whenever"),
            ("LOCALASISTAN_THINKING", "sometimes"),
            ("LOCALASISTAN_LOG_LEVEL", "LOUD"),
            ("LOCALASISTAN_PORT", "0"),
            ("LOCALASISTAN_PORT", "99999"),
            ("LOCALASISTAN_PORT", "abc"),
            ("LOCALASISTAN_MAX_TOKENS", "10"),
            ("LOCALASISTAN_REFUSAL_FALLBACK", "maybe"),
            ("LOCALASISTAN_TOOL_TIMEOUT", "fast"),
            ("LOCALASISTAN_TZ", "Nowhere/Land"),
        ],
    )
    def test_invalid_values_rejected(self, var: str, value: str):
        with pytest.raises(ConfigError):
            Settings.from_env({var: value})

    def test_env_beats_dotenv_and_empty_env_ignored(self, tmp_path: Path):
        dotenv = tmp_path / ".env"
        dotenv.write_text("ANTHROPIC_API_KEY=from-file\nLOCALASISTAN_MODEL=file-model\nLOCALASISTAN_PORT=1111\n")
        s = Settings.from_env(
            {"LOCALASISTAN_MODEL": "env-model", "ANTHROPIC_API_KEY": "", "LOCALASISTAN_PORT": "  "},
            dotenv_path=dotenv,
        )
        assert s.model == "env-model"
        assert s.api_key == "from-file"
        assert s.port == 1111

    def test_missing_dotenv_is_fine(self, tmp_path: Path):
        assert Settings.from_env({}, dotenv_path=tmp_path / "nope.env").model == DEFAULT_MODEL

    def test_server_env_excludes_secrets(self, tmp_path: Path):
        s = Settings.from_env({"LOCALASISTAN_HOME": str(tmp_path), "ANTHROPIC_API_KEY": "sk-secret"})
        env = s.server_env()
        assert "sk-secret" not in " ".join(env.values())
        assert not any("KEY" in k for k in env)
        assert env["LOCALASISTAN_HOME"] == str(tmp_path)
        assert env["LOCALASISTAN_TZ"] == s.timezone

    def test_public_dict_and_repr_hide_key(self):
        s = Settings.from_env({"ANTHROPIC_API_KEY": "sk-secret"})
        assert "sk-secret" not in repr(s)
        assert "sk-secret" not in str(s.public_dict())
        assert s.public_dict()["api_key"] == "set"

    def test_ensure_dirs(self, tmp_path: Path):
        s = Settings.from_env({"LOCALASISTAN_HOME": str(tmp_path / "h")})
        s.ensure_dirs()
        assert s.home.is_dir() and s.workspace.is_dir()

    def test_with_overrides(self):
        s = Settings.from_env({}).with_overrides(approval="auto")
        assert s.approval == "auto"


class TestOpenAICompatSettings:
    ENV: ClassVar[dict[str, str]] = {
        "LOCALASISTAN_PROVIDER": "openai_compat",
        "LOCALASISTAN_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "LOCALASISTAN_MODEL": "z-ai/glm-test",
        "LOCALASISTAN_API_KEY": "nvapi-secret",
    }

    def settings(self, tmp_path: Path, **extra: str) -> Settings:
        return Settings.from_env({"LOCALASISTAN_HOME": str(tmp_path), **self.ENV, **extra})

    def test_anthropic_stays_the_default_and_ignores_the_compat_variables(self, tmp_path: Path):
        s = Settings.from_env(
            {"LOCALASISTAN_HOME": str(tmp_path), "LOCALASISTAN_API_KEY": "nvapi-x", "ANTHROPIC_API_KEY": "sk-a"}
        )
        assert (s.provider, s.base_url, s.model, s.api_key, s.max_tokens) == (
            "anthropic",
            None,
            DEFAULT_MODEL,
            "sk-a",
            64000,
        )

    def test_compat_reads_its_own_key_url_and_model_and_a_safer_token_default(self, tmp_path: Path):
        s = self.settings(tmp_path, ANTHROPIC_API_KEY="sk-ant-other")
        assert s.provider == "openai_compat" and s.api_key == "nvapi-secret"
        assert s.base_url == "https://integrate.api.nvidia.com/v1" and s.model == "z-ai/glm-test"
        assert s.max_tokens == 8192
        assert self.settings(tmp_path, LOCALASISTAN_MAX_TOKENS="4000").max_tokens == 4000

    def test_compat_has_no_default_model(self, tmp_path: Path):
        env = {k: v for k, v in self.ENV.items() if k != "LOCALASISTAN_MODEL"}
        assert Settings.from_env({"LOCALASISTAN_HOME": str(tmp_path), **env}).model == ""

    @pytest.mark.parametrize("value", ["OpenAI-Compat", " openai_compat "])
    def test_provider_name_is_normalised(self, tmp_path: Path, value: str):
        assert self.settings(tmp_path, LOCALASISTAN_PROVIDER=value).provider == "openai_compat"

    def test_unknown_provider_is_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="LOCALASISTAN_PROVIDER"):
            self.settings(tmp_path, LOCALASISTAN_PROVIDER="gemini")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://api.example.com/v1/", "https://api.example.com/v1"),
            ("https://api.example.com/v1/chat/completions", "https://api.example.com/v1"),
            ("https://api.example.com/v1/chat/completions/", "https://api.example.com/v1"),
            ("http://localhost:11434/v1", "http://localhost:11434/v1"),
            ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
            ("http://[::1]:8000/v1", "http://[::1]:8000/v1"),
        ],
    )
    def test_base_url_is_normalised(self, tmp_path: Path, raw: str, expected: str):
        assert self.settings(tmp_path, LOCALASISTAN_BASE_URL=raw).base_url == expected

    @pytest.mark.parametrize(
        ("raw", "needle"),
        [
            ("ftp://example.com/v1", r"http\(s\) URL"),
            ("not a url", r"http\(s\) URL"),
            ("https://", r"http\(s\) URL"),
            ("https://user:pw@example.com/v1", "credentials"),
            ("https://example.com/v1?key=abc", "query string"),
            ("http://api.example.com/v1", "plain http"),
            ("http://192.168.1.20:8000/v1", "plain http"),
        ],
    )
    def test_dangerous_or_malformed_base_urls_are_rejected(self, tmp_path: Path, raw: str, needle: str):
        with pytest.raises(ConfigError, match=needle):
            self.settings(tmp_path, LOCALASISTAN_BASE_URL=raw)

    def test_plain_http_to_a_remote_host_is_fine_without_a_key(self, tmp_path: Path):
        env = {k: v for k, v in self.ENV.items() if k != "LOCALASISTAN_API_KEY"}
        s = Settings.from_env(
            {"LOCALASISTAN_HOME": str(tmp_path), **env, "LOCALASISTAN_BASE_URL": "http://192.168.1.20:11434/v1"}
        )
        assert s.base_url == "http://192.168.1.20:11434/v1" and s.api_key is None

    def test_the_key_never_reaches_the_server_process_or_public_views(self, tmp_path: Path):
        s = self.settings(tmp_path)
        assert "nvapi-secret" not in str(s.server_env()) and "LOCALASISTAN_API_KEY" not in s.server_env()
        assert "nvapi-secret" not in repr(s) and "nvapi-secret" not in str(s.public_dict())
        assert s.public_dict()["provider"] == "openai_compat"
        assert s.public_dict()["base_url"] == "https://integrate.api.nvidia.com/v1"

    def test_dotenv_file_works_for_the_new_variables(self, tmp_path: Path):
        dotenv = tmp_path / ".env"
        dotenv.write_text(
            "LOCALASISTAN_PROVIDER=openai_compat\nLOCALASISTAN_BASE_URL=https://x.example/v1\n"
            "LOCALASISTAN_MODEL=m\nLOCALASISTAN_API_KEY=k\n"
        )
        s = Settings.from_env({"LOCALASISTAN_HOME": str(tmp_path)}, dotenv_path=dotenv)
        assert (s.provider, s.base_url, s.model, s.api_key) == ("openai_compat", "https://x.example/v1", "m", "k")
