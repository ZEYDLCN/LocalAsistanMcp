"""Yapılandırma: ortam değişkenleri + isteğe bağlı `.env` dosyası.

Öncelik: gerçek ortam değişkeni (boş değilse) > `.env` > varsayılan.
Sunucu alt sürecine yalnızca gereken değerler aktarılır (`Settings.server_env`);
**API anahtarı sunucuya geçmez** (en az yetki ilkesi).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError

APPROVAL_POLICIES = ("ask_destructive", "ask_writes", "auto")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_PORT = 8765
_TRUE = {"1", "true", "yes", "on", "evet"}
_FALSE = {"0", "false", "no", "off", "hayir", "hayır", ""}


# --------------------------------------------------------------------------- .env
def parse_dotenv(text: str) -> dict[str, str]:
    """Basit `.env` ayrıştırıcı: `KEY=VALUE`, `export KEY=VALUE`, yorumlar, tek/çift tırnak."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # tırnaksız değerde satır içi yorumu ("  # ...") at
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()
        result[key] = value
    return result


def load_dotenv_file(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        return parse_dotenv(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Could not read {path.name}: {exc}") from exc


# ----------------------------------------------------------------------- saat dilimi
def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(f"Unknown timezone '{name}'. Use an IANA name such as 'Europe/Istanbul'.") from exc
    return name


def detect_timezone(env: Mapping[str, str], localtime_path: Path = Path("/etc/localtime")) -> str:
    """Saat dilimi: LOCALASISTAN_TZ (hata verir) > TZ (geçersizse yok sayılır) > /etc/localtime > UTC."""
    explicit = env.get("LOCALASISTAN_TZ", "").strip()
    if explicit:
        return validate_timezone(explicit)
    tz_env = env.get("TZ", "").strip().lstrip(":")
    if tz_env:
        try:
            return validate_timezone(tz_env)
        except ConfigError:
            pass
    try:
        target = os.path.realpath(localtime_path)
    except OSError:  # pragma: no cover - çok nadir
        target = ""
    marker = "zoneinfo/"
    if marker in target:
        candidate = target.split(marker, 1)[1]
        try:
            return validate_timezone(candidate)
        except ConfigError:
            pass
    return "UTC"


# ------------------------------------------------------------------------ yardımcılar
def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be one of 1/0, true/false, yes/no, on/off (got '{raw}').")


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer (got '{raw}').") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi} (got {value}).")
    return value


def _float(env: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number (got '{raw}').") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi} (got {value}).")
    return value


def _path(raw: str) -> Path:
    return Path(raw).expanduser()


# --------------------------------------------------------------------------- ayarlar
@dataclass(frozen=True)
class Settings:
    home: Path
    workspace: Path
    timezone: str
    # LLM
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    thinking: bool = True
    max_tokens: int = 64000
    max_iterations: int = 12
    refusal_fallback: bool = True
    approval: str = "ask_destructive"
    api_key: str | None = field(default=None, repr=False)
    # arayüz
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    log_level: str = "INFO"
    # sınırlar
    max_file_bytes: int = 5_000_000
    max_tool_output_chars: int = 40_000
    tool_timeout_seconds: float = 60.0
    approval_timeout_seconds: float = 300.0
    db_timeout_seconds: float = 5.0
    db_max_bytes: int = 256 * 1024 * 1024

    # -- türetilmiş yollar
    @property
    def calendar_db(self) -> Path:
        return self.home / "calendar.db"

    @property
    def user_db(self) -> Path:
        return self.home / "userdata.db"

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    # -- kurucu
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        dotenv_path: Path | None = None,
        localtime_path: Path = Path("/etc/localtime"),
    ) -> Settings:
        base = dict(os.environ if env is None else env)
        merged = dict(load_dotenv_file(dotenv_path))
        merged.update({k: v for k, v in base.items() if v.strip() != ""})

        home = _path(merged.get("LOCALASISTAN_HOME", "~/.localasistan"))
        workspace_raw = merged.get("LOCALASISTAN_WORKSPACE", "").strip()
        workspace = _path(workspace_raw) if workspace_raw else home / "workspace"

        effort = merged.get("LOCALASISTAN_EFFORT", "medium").strip().lower()
        if effort not in EFFORTS:
            raise ConfigError(f"LOCALASISTAN_EFFORT must be one of {', '.join(EFFORTS)} (got '{effort}').")
        approval = merged.get("LOCALASISTAN_APPROVAL", "ask_destructive").strip().lower()
        if approval not in APPROVAL_POLICIES:
            raise ConfigError(
                f"LOCALASISTAN_APPROVAL must be one of {', '.join(APPROVAL_POLICIES)} (got '{approval}')."
            )
        thinking_raw = merged.get("LOCALASISTAN_THINKING", "adaptive").strip().lower()
        if thinking_raw not in ("adaptive", "off"):
            raise ConfigError("LOCALASISTAN_THINKING must be 'adaptive' or 'off'.")
        log_level = merged.get("LOCALASISTAN_LOG_LEVEL", "INFO").strip().upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError("LOCALASISTAN_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL.")

        return cls(
            home=home,
            workspace=workspace,
            timezone=detect_timezone(merged, localtime_path),
            model=merged.get("LOCALASISTAN_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            effort=effort,
            thinking=thinking_raw == "adaptive",
            max_tokens=_int(merged, "LOCALASISTAN_MAX_TOKENS", 64000, 256, 128000),
            max_iterations=_int(merged, "LOCALASISTAN_MAX_ITERATIONS", 12, 1, 100),
            refusal_fallback=_bool(merged, "LOCALASISTAN_REFUSAL_FALLBACK", True),
            approval=approval,
            api_key=(merged.get("ANTHROPIC_API_KEY") or "").strip() or None,
            host=merged.get("LOCALASISTAN_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_int(merged, "LOCALASISTAN_PORT", DEFAULT_PORT, 1, 65535),
            log_level=log_level,
            max_file_bytes=_int(merged, "LOCALASISTAN_MAX_FILE_BYTES", 5_000_000, 1_000, 1_000_000_000),
            max_tool_output_chars=_int(merged, "LOCALASISTAN_MAX_TOOL_OUTPUT_CHARS", 40_000, 500, 2_000_000),
            tool_timeout_seconds=_float(merged, "LOCALASISTAN_TOOL_TIMEOUT", 60.0, 0.5, 3600.0),
            approval_timeout_seconds=_float(merged, "LOCALASISTAN_APPROVAL_TIMEOUT", 300.0, 0.1, 86400.0),
            db_timeout_seconds=_float(merged, "LOCALASISTAN_DB_TIMEOUT", 5.0, 0.05, 600.0),
        )

    # -- yardımcılar
    def with_overrides(self, **changes: object) -> Settings:
        return replace(self, **changes)  # type: ignore[arg-type]

    def ensure_dirs(self) -> None:
        for directory in (self.home, self.workspace):
            directory.mkdir(parents=True, exist_ok=True)

    def server_env(self) -> dict[str, str]:
        """MCP sunucu alt sürecine aktarılacak ortam (API anahtarı DAHİL DEĞİL)."""
        return {
            "LOCALASISTAN_HOME": str(self.home),
            "LOCALASISTAN_WORKSPACE": str(self.workspace),
            "LOCALASISTAN_TZ": self.timezone,
            "LOCALASISTAN_LOG_LEVEL": self.log_level,
            "LOCALASISTAN_MAX_FILE_BYTES": str(self.max_file_bytes),
            "LOCALASISTAN_DB_TIMEOUT": str(self.db_timeout_seconds),
        }

    def public_dict(self) -> dict[str, object]:
        """Gizli bilgi içermeyen özet (arayüz / `config` komutu için)."""
        return {
            "home": str(self.home),
            "workspace": str(self.workspace),
            "timezone": self.timezone,
            "model": self.model,
            "effort": self.effort,
            "thinking": "adaptive" if self.thinking else "off",
            "max_tokens": self.max_tokens,
            "max_iterations": self.max_iterations,
            "refusal_fallback": self.refusal_fallback,
            "approval": self.approval,
            "api_key": "set" if self.has_api_key else "missing",
            "host": self.host,
            "port": self.port,
        }
