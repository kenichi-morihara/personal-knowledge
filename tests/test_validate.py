"""Temporary synthetic records only; never written to the knowledge directories."""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

import yaml

spec = importlib.util.spec_from_file_location('validator', Path(__file__).resolve().parents[1] / 'scripts/validate.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'finance').mkdir()
        self.record = dict(id=str(uuid4()), type='obligation', updated_at='2026-10-25',
            sources=[dict(reference='Synthetic test source', captured_at='2026-10-25')],
            currency='JPY', amount_due='300000', opening_paid='0', amount_paid='0',
            amount_remaining='300000', due_date=None, status='unpaid', payments=[])

    def write(self, data=None, name='tax.md'):
        (self.root / 'finance' / name).write_text('---\n' + yaml.safe_dump(data or self.record, allow_unicode=True) + '---\n# Test\n')

    def errors(self):
        return validator.validate(self.root)[1]

    def pay(self):
        self.record.update(amount_paid='100000', amount_remaining='200000', status='partial',
            payments=[dict(id=str(uuid4()), amount='100000', paid_at='2026-10-25', source='Synthetic receipt')])

    def test_register_find_and_partial_payment(self):
        self.write()
        self.assertEqual(self.errors(), [])
        self.assertIn(self.record['id'], (self.root / 'finance/tax.md').read_text())
        self.pay()
        self.write()
        self.assertEqual(self.errors(), [])

    def test_full_payment(self):
        self.pay()
        self.record['payments'].append(dict(id=str(uuid4()), amount='200000', paid_at='2026-10-25', source='Synthetic second receipt'))
        self.record.update(amount_paid='300000', amount_remaining='0', status='paid')
        self.write()
        self.assertEqual(self.errors(), [])

    def test_inconsistent_payment_and_duplicate_receipt(self):
        self.pay()
        self.record['payments'].append(self.record['payments'][0].copy())
        self.write()
        result = '\n'.join(self.errors())
        self.assertIn('duplicate id', result)
        self.assertIn('amount_paid', result)

    def test_unknown_is_not_zero(self):
        self.record.update(amount_due=None, opening_paid=None, amount_paid=None, amount_remaining=None, status='unknown')
        self.write()
        self.assertEqual(self.errors(), [])
        self.record['amount_paid'] = '0'
        self.write()
        self.assertTrue(self.errors())

    def test_fractional_yen_is_rejected(self):
        self.record['amount_due'] = '100.5'
        self.write()
        self.assertIn('JPY fractional', '\n'.join(self.errors()))

    def test_duplicate_record_id(self):
        self.write()
        self.write(name='duplicate.md')
        self.assertIn('duplicate id', '\n'.join(self.errors()))

    def test_missing_source_and_copied_template(self):
        self.record.update(sources=[], template=True)
        self.write()
        result = '\n'.join(self.errors())
        self.assertIn('sources must be nonempty', result)
        self.assertIn('template copied', result)

    def test_balance_requires_timestamp_with_timezone(self):
        self.record.update(type='balance', amount='1000', account_id='test-account', institution='Test bank', observed_at='2026-10-25T10:00:00')
        self.write()
        self.assertIn('timezone', '\n'.join(self.errors()))
        self.record['observed_at'] += '+09:00'
        self.write()
        self.assertEqual(self.errors(), [])

    def test_nested_virtualenv_is_not_a_record(self):
        self.write()
        dependency = self.root / 'finance/ledger/.venv/lib/package'
        dependency.mkdir(parents=True)
        (dependency / 'LICENSE.md').write_text('Dependency license [not a record](absent.md)')
        count, errors = validator.validate(self.root)
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])

    def test_broken_relative_link(self):
        self.write()
        (self.root / 'README.md').write_text('[missing](missing.md)')
        self.assertIn('broken link', '\n'.join(self.errors()))


if __name__ == '__main__':
    unittest.main()
