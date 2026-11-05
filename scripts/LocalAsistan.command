#!/usr/bin/env bash
# macOS: Finder'da çift tıklayarak başlatmak için. Terminal penceresi açık kaldığı sürece uygulama çalışır.
cd "$(dirname "$0")/.."
./scripts/run.sh
status=$?
echo; echo "LocalAsistan durdu (kod $status). Pencereyi kapatabilirsiniz."
read -r -n 1 -p "Kapatmak için bir tuşa basın…" _ || true
