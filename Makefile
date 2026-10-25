.DEFAULT_GOAL := help

PYTHON ?= python3.14
LEDGER_PYTHON := .beancount/.venv/bin/python
LEDGER_SCRIPT := finance/ledger/manage.py

.PHONY: help ledger-setup ledger-ready ledger-check ledger-summary ledger-build ledger-test fava

help:
	@printf '%s\n' \
	  'make ledger-setup    Beancount・Favaの仮想環境と依存関係を用意' \
	  'make fava            Favaを起動（http://127.0.0.1:5001、Ctrl+Cで終了）' \
	  'make ledger-summary  未払額・期限・出典をJSONで表示' \
	  'make ledger-check    原本とBeancount帳簿の整合性を検証' \
	  'make ledger-build    原本から帳簿を生成' \
	  'make ledger-test     会計機能のテストを実行' \
	  'make capture-setup   撮影機能とMac内OCRの初回セットアップ' \
	  'make capture-start   Android向け撮影ページを起動' \
	  'make capture-status  現在の撮影ページURLを表示' \
	  'make capture-key-copy 撮影用ログインキーをコピー' \
	  'make capture-pending 要確認の撮影記録を一覧表示' \
	  'make capture-stop    撮影ページだけを停止' \
	  'make capture-test    撮影機能のテストを実行'

ledger-setup:
	@test -x "$(LEDGER_PYTHON)" || "$(PYTHON)" -m venv .beancount/.venv
	@"$(LEDGER_PYTHON)" -m pip install -r finance/ledger/requirements.txt

ledger-ready:
	@test -x "$(LEDGER_PYTHON)" || { echo '先に make ledger-setup を実行してください。' >&2; exit 1; }

ledger-check: ledger-ready
	@"$(LEDGER_PYTHON)" "$(LEDGER_SCRIPT)" check

ledger-summary: ledger-ready
	@"$(LEDGER_PYTHON)" "$(LEDGER_SCRIPT)" summary

ledger-build: ledger-ready
	@"$(LEDGER_PYTHON)" "$(LEDGER_SCRIPT)" build

ledger-test: ledger-ready
	@"$(LEDGER_PYTHON)" -m unittest discover -s finance/ledger -p 'test_*.py' -v

fava: ledger-ready
	@"$(LEDGER_PYTHON)" "$(LEDGER_SCRIPT)" serve

CAPTURE_PYTHON := .venv/bin/python
CAPTURE_SCRIPT := scripts/tax_capture/run.py

.PHONY: capture-setup capture-ready capture-start capture-stop capture-status capture-key-copy capture-pending capture-test

capture-setup:
	@test -x "$(CAPTURE_PYTHON)" || "$(PYTHON)" -m venv .venv
	@"$(CAPTURE_PYTHON)" -m pip install -r requirements-capture.txt
	@"$(CAPTURE_PYTHON)" "$(CAPTURE_SCRIPT)" setup

capture-ready:
	@test -x "$(CAPTURE_PYTHON)" || { echo '先に make capture-setup を実行してください。' >&2; exit 1; }

capture-start: capture-ready
	@"$(CAPTURE_PYTHON)" "$(CAPTURE_SCRIPT)" start

capture-stop: capture-ready
	@"$(CAPTURE_PYTHON)" "$(CAPTURE_SCRIPT)" stop

capture-status: capture-ready
	@"$(CAPTURE_PYTHON)" "$(CAPTURE_SCRIPT)" status

capture-key-copy: capture-ready
	@"$(CAPTURE_PYTHON)" "$(CAPTURE_SCRIPT)" key-copy

capture-pending: capture-ready
	@"$(CAPTURE_PYTHON)" "$(CAPTURE_SCRIPT)" pending

capture-test: capture-ready
	@"$(CAPTURE_PYTHON)" -m unittest discover -s tests -p test_tax_capture.py -v
