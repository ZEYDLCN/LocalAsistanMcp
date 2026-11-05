"""`python -m localasistan.server` — MCP sunucusunu stdio üzerinden çalıştırır.

stdout MCP protokolüne ayrılmıştır; tüm loglar stderr'e gider.
Yapılandırma yalnızca ortam değişkenlerinden okunur (istemci bunları alt sürece aktarır).
"""

from __future__ import annotations

import sys

from ..config import Settings
from ..errors import ConfigError
from ..logging_setup import configure_logging
from .app import build_server


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"localasistan-server: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    configure_logging(settings.log_level)
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()
