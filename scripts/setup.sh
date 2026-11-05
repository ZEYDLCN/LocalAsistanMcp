#!/usr/bin/env bash
# LocalAsistan kurulumu (macOS ve Linux).  Kullanım:  ./scripts/setup.sh [--dev]
#   --dev  test/lint araçlarını da kurar (pytest, ruff, playwright)
set -euo pipefail
cd "$(dirname "$0")/.."

EXTRAS=""
for arg in "$@"; do
  case "$arg" in
    --dev) EXTRAS="[dev]" ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "Bilinmeyen seçenek: $arg" >&2; exit 2 ;;
  esac
done

VENV=".venv"
MIN_MAJOR=3; MIN_MINOR=11

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_MAJOR, $MIN_MINOR) else 1)" 2>/dev/null; then
      echo "$candidate"; return 0
    fi
  done
  return 1
}

if command -v uv >/dev/null 2>&1; then
  echo "▸ uv bulundu: sanal ortam hazırlanıyor (gerekirse uygun Python indirilir)…"
  uv venv --python ">=$MIN_MAJOR.$MIN_MINOR" "$VENV"
  uv pip install --python "$VENV/bin/python" -e ".$EXTRAS"
else
  if ! PY="$(find_python)"; then
    cat >&2 <<MSG
✗ Python $MIN_MAJOR.$MIN_MINOR veya üstü bulunamadı.
  macOS için en kolay yol:
    brew install python@3.12        (ya da:  brew install uv  ve scripts/setup.sh'i yeniden çalıştırın)
MSG
    exit 1
  fi
  echo "▸ $PY ile sanal ortam hazırlanıyor…"
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install -e ".$EXTRAS"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "▸ .env oluşturuldu — sohbet için ANTHROPIC_API_KEY değerini yazın."
fi

echo
"$VENV/bin/python" -m localasistan doctor || true
cat <<'MSG'

Hazır. Sonraki adımlar:
  ./scripts/run.sh                      web arayüzünü başlat
  .venv/bin/localasistan seed           örnek veri yükle (isteğe bağlı)
  .venv/bin/localasistan doctor --llm   API anahtarınızla gerçek bir deneme yap
MSG
