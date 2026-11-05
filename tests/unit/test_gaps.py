"""Kapsama raporunda görünen (çoğu hata-yolu) dalların doğrudan testleri."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from localasistan.client.agent import ChatSession
from localasistan.client.hub import _flatten_content, _maybe_json
from localasistan.config import Settings, load_dotenv_file
from localasistan.errors import (
    AlreadyExistsError,
    ConfigError,
    InvalidInputError,
    LocalAsistanError,
    NotFoundError,
)
from localasistan.logging_setup import configure_logging, get_logger
from localasistan.sandbox import Sandbox
from localasistan.server.files import FileService


class TestFileErrorTranslation:
    @pytest.mark.parametrize(
        ("exc", "expected", "fragment"),
        [
            (FileNotFoundError(2, "x"), NotFoundError, "does not exist"),
            (PermissionError(13, "x"), LocalAsistanError, "Permission denied"),
            (IsADirectoryError(21, "x"), InvalidInputError, "is a directory"),
            (NotADirectoryError(20, "x"), InvalidInputError, "not a directory"),
            (FileExistsError(17, "x"), AlreadyExistsError, "already exists"),
            (OSError(28, "No space left on device"), LocalAsistanError, "No space left"),
            (OSError(), LocalAsistanError, "Filesystem error"),
        ],
    )
    def test_translate(self, exc, expected, fragment):
        translated = FileService._translate(exc, "a/b.txt")
        assert type(translated) is expected and fragment in str(translated) and "/tmp" not in str(translated)

    def test_io_wrapper_translates_unexpected_oserrors_raised_by_the_filesystem(self, tmp_path, monkeypatch):
        service = FileService(Sandbox(tmp_path / "ws"))
        (tmp_path / "ws" / "a.txt").write_text("x")

        def fail(*_a, **_k):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr("pathlib.Path.read_bytes", fail)
        with pytest.raises(LocalAsistanError, match="Input/output error"):
            service.read_text("a.txt")


class TestHubContentFlattening:
    def block(self, kind, **kw):
        return SimpleNamespace(type=kind, **kw)

    def test_all_block_types(self):
        result = SimpleNamespace(
            content=[
                self.block("text", text="merhaba"),
                self.block("image", data="..", mime_type="image/png"),
                self.block("audio", data="..", mime_type="audio/wav"),
                self.block("resource", resource=SimpleNamespace(uri="file://a", text="gömülü metin")),
                self.block("resource", resource=SimpleNamespace(uri="file://b", blob="AAAA")),
                self.block("resource_link", uri="file://c"),
                self.block("mystery"),
            ],
            structured_content=None,
        )
        assert _flatten_content(result).splitlines() == [
            "merhaba",
            "[image content omitted]",
            "[audio content omitted]",
            "gömülü metin",
            "[resource file://b]",
            "[resource link file://c]",
        ]

    def test_empty_and_structured_fallbacks(self):
        assert _flatten_content(SimpleNamespace(content=[], structured_content=None)) == "(no output)"
        assert _flatten_content(SimpleNamespace(content=None, structured_content={"a": "ç"})) == '{"a": "ç"}'

    @pytest.mark.parametrize(
        ("text", "expected"),
        [('{"a": 1}', {"a": 1}), ("[1, 2]", [1, 2]), ("{broken", None), ("plain", None), ("", None)],
    )
    def test_maybe_json(self, text, expected):
        assert _maybe_json(text) == expected


class TestConfigAndLogging:
    def test_unreadable_dotenv_is_a_config_error(self, tmp_path):
        bad = tmp_path / ".env"
        bad.write_bytes(b"\xff\xfe\x00invalid utf8 \x80\x81")
        with pytest.raises(ConfigError, match="Could not read"):
            load_dotenv_file(bad)
        with pytest.raises(ConfigError):
            Settings.from_env({}, dotenv_path=bad)

    def test_dotenv_that_is_a_directory(self, tmp_path):
        (tmp_path / "d.env").mkdir()
        with pytest.raises(ConfigError):
            load_dotenv_file(tmp_path / "d.env")

    @pytest.mark.parametrize("value", ["yes", "ON", "evet", "1", "true"])
    def test_truthy_values(self, value):
        assert Settings.from_env({"LOCALASISTAN_REFUSAL_FALLBACK": value}).refusal_fallback is True

    @pytest.mark.parametrize("value", ["no", "OFF", "hayır", "0", "false"])
    def test_falsy_values(self, value):
        assert Settings.from_env({"LOCALASISTAN_REFUSAL_FALLBACK": value}).refusal_fallback is False

    def test_logging_goes_to_the_given_stream_and_reconfiguring_does_not_duplicate_handlers(self, tmp_path):
        import io

        first, second = io.StringIO(), io.StringIO()
        configure_logging("INFO", first)
        configure_logging("INFO", second)
        get_logger("t").warning("merhaba log")
        root = logging.getLogger("localasistan")
        assert len([h for h in root.handlers if getattr(h, "_localasistan", False)]) == 1
        assert "merhaba log" in second.getvalue() and first.getvalue() == ""
        assert root.propagate is False


class TestAgentHistoryRepair:
    def make(self, settings, messages):
        session = ChatSession("s", hub=None, provider=None, settings=settings, policy=lambda: "auto")  # type: ignore[arg-type]
        session.messages = messages
        return session

    def test_repair_handles_sdk_objects_and_ignores_other_shapes(self, settings):
        sdk_block = SimpleNamespace(type="tool_use", id="t1", name="x", input={})
        text_block = SimpleNamespace(type="text", text="hi")
        session = self.make(
            settings, [{"role": "user", "content": "q"}, {"role": "assistant", "content": [text_block, sdk_block]}]
        )
        session._repair_history()
        assert (
            session.messages[-1]["content"][0]["tool_use_id"] == "t1" and session.messages[-1]["content"][0]["is_error"]
        )
        # kullanıcı mesajıyla biten geçmişe dokunmaz
        before = list(session.messages)
        session._repair_history()
        assert session.messages == before
        # asistan metni (string) ile biten geçmişe dokunmaz
        text_only = self.make(settings, [{"role": "assistant", "content": "düz metin"}])
        text_only._repair_history()
        assert len(text_only.messages) == 1
        # boş geçmiş
        self.make(settings, [])._repair_history()
