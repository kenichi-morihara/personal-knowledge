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
	  'make ledger-test     会計機能のテストを実行'

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
