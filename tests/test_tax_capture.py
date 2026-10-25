"""Capture, auth and Git storage tests. Synthetic images/text in temporary repositories only."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import yaml
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts/tax_capture'))
from core import Captures, extract, MAX_IMAGE
from app import create_app
from mcp_store import Store

JPEG = b'\xff\xd8\xffsynthetic-image-for-injected-ocr'
LINES = [dict(text=text, confidence=.99) for text in (
    '令和8年度 住民税 納付書', '納付金額 50,000円', '納期限 令和8年11月2日')]


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.state = base / 'repo', base / 'state'
        self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Synthetic test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.bare = base / 'origin.git'
        subprocess.run(['git', 'init', '--bare', str(self.bare)], check=True, capture_output=True)
        self.git('remote', 'add', 'origin', str(self.bare))
        (self.root / 'README.md').write_text('# Synthetic fixture\n')
        self.git('add', 'README.md')
        self.git('commit', '-m', 'Synthetic fixture')
        self.git('push', '-u', 'origin', 'main')
        self.store = Store(self.root, base / 'shared-store-state')
        self.captures = Captures(self.state, self.store, lambda image: LINES)
        self.origin = 'https://capture.example.invalid'
        self.client = TestClient(create_app(self.captures, self.origin, 'test-only-owner-key'), base_url=self.origin, follow_redirects=False)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def login(self):
        response = self.client.post('/login', headers={'Origin': self.origin}, data={'key': 'test-only-owner-key'})
        self.assertEqual(response.status_code, 303)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('Secure', response.headers['set-cookie'])
        page = self.client.get('/')
        return json.loads(re.search(r'const csrf=("[^"]+")', page.text)[1])

    def test_auth_and_csrf(self):
        self.assertEqual(self.client.post('/capture', content=JPEG).status_code, 401)
        token = self.login()
        self.assertEqual(self.client.post('/capture', headers={'Origin': 'https://attacker.invalid', 'X-Capture-CSRF': token}, content=JPEG).status_code, 403)
        self.assertEqual(self.client.post('/capture', headers={'Origin': self.origin}, content=JPEG).status_code, 403)
        self.assertEqual(self.client.get('/images/file.jpg').status_code, 404)
        self.assertEqual(self.client.get('/', headers={'Host': 'attacker.invalid'}).status_code, 400)

    def test_upload_records_note_not_payment_and_deduplicates(self):
        token = self.login()
        headers = {'Origin': self.origin, 'X-Capture-CSRF': token}
        response = self.client.post('/capture', headers=headers, content=JPEG)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result['synced'])
        self.assertEqual(result['fields']['amount_due'], '50000')
        content = (self.root / result['path']).read_text()
        data = yaml.safe_load(content.split('---\n', 2)[1])
        self.assertEqual(data['type'], 'note')
        self.assertEqual(data['capture_status'], 'needs_review')
        self.assertIsNone(data['amount_paid'])
        self.assertIsNone(data['amount_remaining'])
        self.assertEqual(data['due_date'], '2026-11-02')
        self.assertNotIn('令和8年度 住民税 納付書', content)
        head = self.git('rev-parse', 'HEAD')
        again = self.client.post('/capture', headers=headers, content=JPEG)
        self.assertTrue(again.json()['duplicate'])
        self.assertEqual(head, self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(len(list((self.state / 'images').glob('*'))), 1)
        self.assertEqual((next((self.state / 'images').glob('*')).stat().st_mode & 0o777), 0o600)

    def test_rephotographed_notice_is_flagged(self):
        first = self.captures.ingest(JPEG)
        second = self.captures.ingest(JPEG + b'different-photo')
        self.assertEqual(second['possible_duplicate_of'], first['path'])
        self.assertEqual(len(self.git('ls-files', 'finance/taxes/captures').splitlines()), 2)

    def test_dirty_tree_keeps_image_and_allows_retry(self):
        (self.root / 'README.md').write_text('Uncommitted user edit')
        with self.assertRaises(ValueError):
            self.captures.ingest(JPEG)
        self.assertEqual((self.root / 'README.md').read_text(), 'Uncommitted user edit')
        self.assertEqual(len(list((self.state / 'images').glob('*'))), 1)
        # Restore only our synthetic test edit, then retry the same image.
        (self.root / 'README.md').write_text('# Synthetic fixture\n')
        self.assertTrue(self.captures.ingest(JPEG)['synced'])

    def test_ocr_failure_is_retryable(self):
        self.captures.ocr = lambda path: (_ for _ in ()).throw(ValueError('Synthetic OCR failure'))
        with self.assertRaises(ValueError):
            self.captures.ingest(JPEG)
        self.assertEqual(json.loads(next((self.state / 'jobs').glob('*')).read_text())['status'], 'retry_needed')
        self.captures.ocr = lambda path: LINES
        self.assertTrue(self.captures.ingest(JPEG)['synced'])

    def test_ambiguous_or_low_confidence_stays_unknown(self):
        fields = extract(LINES + [dict(text='合計 200,000円', confidence=.99), dict(text='納期限 令和8年12月1日', confidence=.99)])
        self.assertIsNone(fields['amount_due'])
        self.assertIsNone(fields['due_date'])
        self.assertIsNone(extract([dict(text='納付金額 999円', confidence=.1)])['amount_due'])
        self.assertIsNone(extract([dict(text='納付金額 1234567890', confidence=.99)])['amount_due'])

    def test_native_japanese_confidence_stays_reviewable(self):
        fields = extract([dict(text=line['text'], confidence=.5) for line in LINES])
        self.assertEqual(fields['category'], '住民税')
        self.assertEqual(fields['due_date'], '2026-11-02')
        self.assertTrue(fields['needs_review'])

    def test_invalid_file_and_body_limit(self):
        with self.assertRaises(ValueError):
            self.captures.ingest(b'<html>Not an image</html>')
        token = self.login()
        response = self.client.post('/capture', headers={'Origin': self.origin, 'X-Capture-CSRF': token, 'Content-Length': str(MAX_IMAGE+1)}, content=b'')
        self.assertEqual(response.status_code, 413)

    def test_auth_rate_limit_and_logout(self):
        for _ in range(5):
            self.client.post('/login', headers={'Origin': self.origin}, data={'key': 'wrong'})
        self.assertEqual(self.client.post('/login', headers={'Origin': self.origin}, data={'key': 'wrong'}).status_code, 429)
        other = TestClient(create_app(self.captures, self.origin, 'key'), base_url=self.origin)
        other.post('/login', headers={'Origin': self.origin}, data={'key': 'key'})
        page = other.get('/')
        token = json.loads(re.search(r'const csrf=("[^"]+")', page.text)[1])
        response = other.post('/logout', headers={'Origin': self.origin}, data={'csrf': token}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertNotIn('const csrf=', other.get('/').text)

    def test_matching_confirmed_obligation_is_flagged(self):
        from datetime import datetime
        from uuid import uuid4
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()
        data = dict(id=str(uuid4()), type='obligation', updated_at=today,
            sources=[dict(reference='Synthetic confirmed notice', captured_at=today)],
            category='住民税', fiscal_year=2026, amount_due='50000', opening_paid='0',
            amount_paid='0', amount_remaining='50000', currency='JPY', due_date='2026-11-02',
            status='unpaid', payments=[])
        self.store.put('finance/taxes/confirmed.md', '---\n' + yaml.safe_dump(data, allow_unicode=True) + '---\n# Synthetic\n', '')
        result = self.captures.ingest(JPEG)
        self.assertEqual(result['possible_duplicate_of'], 'finance/taxes/confirmed.md')

    def test_recover_after_record_commit_before_job_update(self):
        result = self.captures.ingest(JPEG)
        job = next((self.state / 'jobs').glob('*.json'))
        job.unlink()
        head = self.git('rev-parse', 'HEAD')
        recovered = self.captures.ingest(JPEG)
        self.assertTrue(recovered['recovered'])
        self.assertTrue(recovered['synced'])
        self.assertEqual(result['id'], recovered['id'])
        self.assertEqual(head, self.git('rev-parse', 'HEAD'))

    def test_failed_push_preserves_local_record(self):
        self.git('remote', 'set-url', '--push', 'origin', str(self.root / 'missing-origin'))
        result = self.captures.ingest(JPEG)
        self.assertFalse(result['synced'])
        self.assertTrue((self.root / result['path']).is_file())
        self.assertFalse(self.captures.ingest(JPEG)['synced'])

    def test_stream_limit_without_content_length(self):
        token = self.login()
        chunks = (b'x' * 1024 * 1024 for _ in range(16))
        response = self.client.post('/capture', headers={'Origin': self.origin, 'X-Capture-CSRF': token}, content=chunks)
        self.assertEqual(response.status_code, 413)

    def test_frontend_script_is_valid_javascript(self):
        self.login()
        page = self.client.get('/')
        script = re.search(r'<script>(.*?)</script>', page.text, re.S)[1]
        script_file = self.state / 'page.js'
        script_file.write_text(script)
        process = subprocess.run(['node', '--check', str(script_file)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == '__main__':
    unittest.main()
