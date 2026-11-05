SHELL := /bin/bash
VENV ?= .venv
PY   := $(VENV)/bin/python

.PHONY: help setup ui seed doctor test test-fast e2e lint fmt cov qa clean

help:            ## Komutları listele
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup:           ## Sanal ortam + bağımlılıklar (Mac/Linux)
	./scripts/setup.sh

ui:              ## Web arayüzünü başlat
	$(PY) -m localasistan.cli ui

seed:            ## Örnek veri yükle (dosya, takvim, veritabanı)
	$(PY) -m localasistan.cli seed

doctor:          ## Kurulum denetimi
	$(PY) -m localasistan.cli doctor

test:            ## Tüm testler (tarayıcı E2E hariç)
	$(PY) -m pytest -m "not e2e"

test-fast:       ## Hızlı testler (stdio alt süreç testleri hariç)
	$(PY) -m pytest -m "not e2e and not stdio" -q

e2e:             ## Tarayıcı (Playwright) E2E — ilk kez: .venv/bin/playwright install chromium
	$(PY) -m pytest -m e2e

lint:            ## Ruff denetimi
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests

fmt:             ## Ruff biçimlendirme
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

cov:             ## Kapsama raporu
	$(PY) -m coverage erase
	$(PY) -m coverage run -m pytest -m "not e2e" -q
	$(PY) -m coverage combine -q
	$(PY) -m coverage report

qa: lint test    ## CI için: lint + testler

clean:
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage .coverage.* build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
