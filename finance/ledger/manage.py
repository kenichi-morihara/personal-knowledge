"""Read-only projection of confirmed Markdown obligations into Beancount/Fava."""
import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from beancount import loader
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def quoted(value):
    return json.dumps(str(value), ensure_ascii=False)


def project(root):
    spec = importlib.util.spec_from_file_location('knowledge_validator', root / 'scripts/validate.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    _, errors = validator.validate(root)
    if errors:
        raise ValueError('\n'.join(errors))
    lines = ['option "title" "Personal obligations (recorded-state projection)"',
             'option "operating_currency" "JPY"',
             'option "operating_currency" "CAD"',
             '; Generated from Markdown. Edit source records, never this file.',
             '; Entry dates = record updated_at, not legal assessment or payment dates.',
             'OPEN_DATE open Expenses:RecordedObligations',
             'OPEN_DATE open Equity:UnallocatedPayments', '']
    summary, skipped = [], []
    for path in sorted((root / 'finance').rglob('*.md')):
        if path.name == 'README.md' or any(p.startswith('.') for p in path.relative_to(root).parts):
            continue
        data = yaml.safe_load(path.read_text().split('---\n', 2)[1])
        if data['type'] != 'obligation':
            continue
        relative = path.relative_to(root).as_posix()
        if any(data.get(key) is None for key in ('amount_due', 'opening_paid', 'amount_paid', 'amount_remaining', 'currency')):
            skipped.append(relative)
            continue
        account = 'Liabilities:Obligations:O' + data['id'].replace('-', '')
        updated = data['updated_at']
        currency = data['currency']
        due = Decimal(data['amount_due'])
        opening = Decimal(data['opening_paid'])
        remaining = Decimal(data['amount_remaining'])
        lines += [f'OPEN_DATE open {account} {currency}',
                  f'  source-file: {quoted(relative)}',
                  f'  record-id: {quoted(data["id"])}']
        if data.get('due_date'):
            lines += [f'  due-date: {data["due_date"]}']
        lines += [f'{updated} * "Recorded obligation"',
                  f'  source-file: {quoted(relative)}',
                  f'  sources: {quoted(json.dumps(data["sources"], ensure_ascii=False))}',
                  f'  {account} {-due} {currency}',
                  f'  Expenses:RecordedObligations {due} {currency}', '']
        if opening:
            lines += [f'{updated} * "Previously paid (date unknown)"',
                      f'  source-file: {quoted(relative)}',
                      f'  {account} {opening} {currency}',
                      f'  Equity:UnallocatedPayments {-opening} {currency}', '']
        for payment in data['payments']:
            amount = Decimal(payment['amount'])
            lines += [f'{updated} * "Recorded payment"',
                      f'  payment-id: {quoted(payment["id"])}',
                      f'  paid-at: {payment["paid_at"]}',
                      f'  source: {quoted(payment["source"])}',
                      f'  source-file: {quoted(relative)}',
                      f'  {account} {amount} {currency}',
                      f'  Equity:UnallocatedPayments {-amount} {currency}', '']
        # Next-day assertion checks Beancount's calculated liability after all postings.
        assertion_date = (date.fromisoformat(updated) + timedelta(days=1)).isoformat()
        lines += [f'{assertion_date} balance {account} {-remaining} {currency}', '']
        summary.append(dict(id=data['id'], source=relative, updated_at=updated,
                            due_date=data.get('due_date'), currency=currency,
                            amount_due=str(due), amount_paid=data['amount_paid'],
                            amount_remaining=str(remaining), account=account))
    opening_date = min((r['updated_at'] for r in summary), default=datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat())
    text = '\n'.join(lines).replace('OPEN_DATE open', opening_date + ' open') + '\n'
    _, bean_errors, _ = loader.load_string(text)
    if bean_errors:
        raise ValueError('Beancount validation failed: ' + '\n'.join(str(e) for e in bean_errors))
    return text, dict(obligations=summary, skipped_unknown=skipped,
                      bank_balances_included=False, timeline='record_updated_at')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'build', 'summary', 'serve'])
    args = parser.parse_args()
    try:
        text, report = project(ROOT)
        if args.command == 'summary':
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == 'check':
            print(f'OK: {len(report["obligations"])} confirmed obligations; {len(report["skipped_unknown"])} unknown records excluded; Beancount valid')
        else:
            directory = HERE / 'generated'
            directory.mkdir(exist_ok=True)
            target = directory / 'obligations.beancount'
            with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False, encoding='utf-8') as handle:
                handle.write(text)
                temporary = Path(handle.name)
            temporary.replace(target)
            if args.command == 'build':
                print(target)
            else:
                print('Fava: http://127.0.0.1:5001 — derived view; refresh after source changes by restarting', flush=True)
                env = {k: v for k, v in os.environ.items() if k != 'BEANCOUNT_FILE' and not k.startswith('FAVA_')}
                subprocess.run([str(Path(sys.executable).parent / 'fava'), '--host', '127.0.0.1', '--port', '5001', '--read-only', str(target)], check=True, env=env)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
