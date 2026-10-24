"""Validate Markdown records and payment arithmetic; no files are changed."""
import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys
from uuid import UUID

import yaml

DOMAINS = ('profile', 'finance', 'projects', 'sales', 'notes')
TYPES = {'profile', 'note', 'project', 'balance', 'obligation', 'opportunity'}


def validate(root):
    errors, ids = [], set()

    def require(ok, message):
        if not ok:
            errors.append(f'{path.relative_to(root)}: {message}')

    def valid_date(value):
        try:
            return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    def unique_id(value):
        try:
            UUID(value)
        except (ValueError, TypeError, AttributeError):
            require(False, 'id must be a UUID')
            return
        require(value not in ids, f'duplicate id: {value}')
        ids.add(value)

    def money(value, field, currency, negative=False):
        if value is None:
            return None
        try:
            if not isinstance(value, str) or not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
                raise ValueError
            number = Decimal(value)
            if not number.is_finite() or (not negative and number < 0):
                raise ValueError
            if currency == 'JPY' and number != number.to_integral_value():
                raise ValueError
            require(isinstance(currency, str) and bool(re.fullmatch('[A-Z]{3}', currency)), 'known amount requires a currency code')
            return number
        except (ValueError, InvalidOperation):
            require(False, f'{field}: invalid decimal string or JPY fractional amount')
            return None

    count = 0
    for domain in DOMAINS:
        for path in sorted((root / domain).rglob('*.md')):
            if path.name == 'README.md':
                continue
            count += 1
            text = path.read_text()
            require(text.startswith('---\n'), 'missing YAML frontmatter')
            if not text.startswith('---\n'):
                continue
            try:
                _, front, _ = text.split('---\n', 2)
                data = yaml.safe_load(front)
                if not isinstance(data, dict):
                    raise ValueError('frontmatter must be a mapping')
            except (ValueError, yaml.YAMLError) as exc:
                require(False, f'invalid frontmatter: {exc}')
                continue
            unique_id(data.get('id'))
            kind = data.get('type')
            require(kind in TYPES, 'unknown record type')
            require(not data.get('template'), 'template copied without removing template marker')
            require(valid_date(data.get('updated_at')), 'updated_at must be a quoted YYYY-MM-DD')
            sources = data.get('sources')
            require(isinstance(sources, list) and bool(sources), 'sources must be nonempty')
            for source in sources if isinstance(sources, list) else []:
                require(isinstance(source, dict) and bool(source.get('reference')) and valid_date(source.get('captured_at')), 'source requires reference and quoted captured_at date')
            for field in ('due_date', 'next_action_due'):
                require(data.get(field) is None or valid_date(data[field]), f'{field} must be null or quoted YYYY-MM-DD')
            currency = data.get('currency')
            require(currency is None or (isinstance(currency, str) and bool(re.fullmatch('[A-Z]{3}', currency))), 'currency must be null or three uppercase letters')
            if kind in ('balance', 'opportunity', 'project'):
                field = 'budget' if kind == 'project' else 'amount'
                money(data.get(field), field, currency, negative=kind == 'balance')
            if kind == 'balance':
                observed = data.get('observed_at')
                if observed is not None:
                    try:
                        require(isinstance(observed, str) and datetime.fromisoformat(observed).utcoffset() is not None, 'observed_at requires timezone')
                    except (TypeError, ValueError):
                        require(False, 'invalid observed_at')
                if data.get('amount') is not None:
                    require(bool(data.get('account_id')) and bool(data.get('institution')) and observed is not None, 'known balance requires account_id, institution and observed_at')
            if kind == 'opportunity':
                require(data.get('status') in {'unknown', 'lead', 'contacted', 'negotiating', 'won', 'lost', 'on_hold'}, 'invalid opportunity status')
            if kind == 'obligation':
                values = {k: money(data.get(k), k, currency) for k in ('amount_due', 'opening_paid', 'amount_paid', 'amount_remaining')}
                payments = data.get('payments')
                require(isinstance(payments, list), 'payments must be a list')
                total, complete = Decimal(0), True
                for payment in payments if isinstance(payments, list) else []:
                    if not isinstance(payment, dict):
                        require(False, 'payment must be a mapping')
                        complete = False
                        continue
                    unique_id(payment.get('id'))
                    amount = money(payment.get('amount'), 'payment.amount', currency)
                    require(amount is not None and amount > 0, 'payment.amount must be positive and known')
                    require(valid_date(payment.get('paid_at')) and bool(payment.get('source')), 'payment requires paid_at and source')
                    if amount is None:
                        complete = False
                    else:
                        total += amount
                due, opening, paid, remaining = (values[k] for k in ('amount_due', 'opening_paid', 'amount_paid', 'amount_remaining'))
                expected_paid = opening + total if opening is not None and complete else None
                require(paid == expected_paid, 'amount_paid must equal opening_paid + payments, or null when unknown')
                expected_remaining = due - paid if due is not None and paid is not None else None
                require(remaining == expected_remaining, 'amount_remaining must equal amount_due - amount_paid, or null when unknown')
                require(expected_remaining is None or expected_remaining >= 0, 'overpayment requires review')
                expected_status = 'unknown' if expected_remaining is None else ('paid' if expected_remaining == 0 else ('partial' if paid > 0 else 'unpaid'))
                require(data.get('status') == expected_status, f'status must be {expected_status}')

    for path in sorted(root.rglob('*.md')):
        if any(part.startswith('.') for part in path.relative_to(root).parts):
            continue
        # Ignore fenced examples when checking relative Markdown links.
        body = re.sub(r'```.*?```', '', path.read_text(), flags=re.S)
        for target in re.findall(r'\[[^\]]*\]\(([^\s)]+)\)', body):
            if re.match(r'[a-zA-Z][\w+.-]*:', target) or target.startswith('#'):
                continue
            target = target.split('#')[0]
            require((path.parent / target).exists(), f'broken link: {target}')
    return count, errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    count, errors = validate(args.root.resolve())
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        sys.exit(1)
    print(f'OK: {count} records; IDs, dates, sources, amounts, payments and links checked')
