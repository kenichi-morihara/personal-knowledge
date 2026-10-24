"""Financial projection tests using temporary synthetic records only."""
import importlib.util
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from beancount import loader
from beancount.core import data as bean_data
import yaml

spec = importlib.util.spec_from_file_location('ledger_manager', Path(__file__).with_name('manage.py'))
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'finance').mkdir()
        shutil.copyfile(manager.ROOT / 'scripts/validate.py', self.root / 'scripts/validate.py')
        self.record = dict(id=str(uuid4()), type='obligation', updated_at='2026-10-25',
            sources=[dict(reference='Synthetic notice', captured_at='2026-10-25')],
            currency='JPY', amount_due='200000', opening_paid='0', amount_paid='50000',
            amount_remaining='150000', due_date='2026-11-01', status='partial',
            payments=[dict(id=str(uuid4()), amount='50000', paid_at='2026-10-20', source='Synthetic receipt')])

    def write(self, record=None, name='tax.md'):
        (self.root / 'finance' / name).write_text('---\n' + yaml.safe_dump(record or self.record) + '---\n# Synthetic\n')

    def test_empty(self):
        text, report = manager.project(self.root)
        self.assertEqual(report['obligations'], [])
        self.assertEqual(loader.load_string(text)[1], [])

    def test_partial_payment_balances_and_sources(self):
        self.write()
        before = (self.root / 'finance/tax.md').read_bytes()
        text, report = manager.project(self.root)
        entries, errors, _ = loader.load_string(text)
        self.assertEqual(errors, [])
        account = report['obligations'][0]['account']
        total = sum(p.units.number for e in entries if isinstance(e, bean_data.Transaction)
                    for p in e.postings if p.account == account)
        self.assertEqual(str(total), '-150000')
        self.assertIn('paid-at: 2026-10-20', text)
        self.assertIn('source-file: "finance/tax.md"', text)
        self.assertIn('due-date: 2026-11-01', text)
        self.assertNotIn('Assets:Bank', text)
        self.assertEqual((self.root / 'finance/tax.md').read_bytes(), before)
        json.dumps(report)

    def test_full_payment(self):
        self.record['payments'][0]['amount'] = '200000'
        self.record.update(amount_paid='200000', amount_remaining='0', status='paid')
        self.write()
        text, report = manager.project(self.root)
        self.assertEqual(report['obligations'][0]['amount_remaining'], '0')
        self.assertEqual(loader.load_string(text)[1], [])

    def test_unknown_excluded_not_zero(self):
        self.record.update(amount_due=None, opening_paid=None, amount_paid=None, amount_remaining=None, status='unknown')
        self.write()
        _, report = manager.project(self.root)
        self.assertEqual(report['obligations'], [])
        self.assertEqual(report['skipped_unknown'], ['finance/tax.md'])

    def test_inconsistent_or_duplicate_payment_rejected(self):
        self.record['payments'].append(self.record['payments'][0].copy())
        self.write()
        with self.assertRaises(ValueError):
            manager.project(self.root)

    def test_separate_currencies_and_account_ids(self):
        self.write()
        second = dict(self.record, id=str(uuid4()), currency='CAD', amount_due='100.50',
                      opening_paid='20.25', amount_paid='20.25', amount_remaining='80.25', payments=[])
        self.write(second, name='cad.md')
        text, report = manager.project(self.root)
        self.assertEqual({x['currency'] for x in report['obligations']}, {'CAD', 'JPY'})
        self.assertEqual(len({x['account'] for x in report['obligations']}), 2)
        self.assertEqual(loader.load_string(text)[1], [])


if __name__ == '__main__':
    unittest.main()
