"""Payment lifecycle tests: isolated Git origins and synthetic OCR only."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_tax_capture as fixture
JPEG, LINES = fixture.JPEG, fixture.LINES
from core import extract, extract_receipt
from management import Management, today
from validate import validate


class ManagementTest(unittest.TestCase):
    setUp = fixture.CaptureTest.setUp
    git = fixture.CaptureTest.git
    login = fixture.CaptureTest.login

    def manager(self):
        return Management(self.captures)

    def notice(self, raw=JPEG, **fields):
        r = self.captures.ingest(raw)
        fetched = self.store.fetch(r['id'])
        payload = dict(sha256=fetched['sha256'], action='create', confirmed_unpaid=True, independently_payable=True,
                       fields=dict(category='住民税', issuer='架空市', fiscal_year=2026, installment='第3期',
                                   target_period='2025年所得', amount_due='50000', due_date='2026-11-02'))
        payload['fields'].update(fields)
        return r, payload

    def create_notice(self, **fields):
        r, p = self.notice(**fields)
        return self.manager().review(r['id'], p)['id']

    def receipt(self, oid, raw=JPEG+b'receipt'):
        self.captures.ocr = lambda image: [dict(text=t, confidence=.99) for t in (
            '領収金額 20,000円', '領収日 令和8年11月1日')]
        result = self.manager().capture_result(raw, 'receipt', oid)
        rec = self.store.fetch(result['id'])
        return dict(sha256=self.store.fetch(oid)['sha256'], capture_id=result['id'], capture_sha256=rec['sha256'],
                    amount='20000', paid_at='2026-11-01', method='コンビニ', confirmed=True)

    def test_notice_atomic_and_idempotent(self):
        r,p=self.notice()
        before=int(self.git('rev-list','--count','HEAD'))
        result=self.manager().review(r['id'],p)
        self.assertTrue(result['synced'])
        self.assertEqual(int(self.git('rev-list','--count','HEAD')),before+1)
        self.assertEqual(len(self.git('show','--pretty=','--name-only','HEAD').splitlines()),2)
        self.assertEqual(self.manager().get(r['id'])['data']['capture_status'],'reviewed')
        oid=result['id']
        self.assertEqual(self.manager().get(oid)['data']['amount_remaining'],'50000')
        self.assertTrue(self.manager().review(r['id'],p)['already_recorded'])
        self.assertEqual(len(self.store.list_obligations()['obligations']),1)
        self.assertEqual(validate(self.root)[1],[])

    def test_draft_annual_and_duplicate_review(self):
        r,p=self.notice()
        p['action']='draft';p['fields']['amount_due']=''
        self.manager().review(r['id'],p)
        self.assertEqual(self.store.list_obligations()['obligations'],[])
        p['action']='create';p['sha256']=self.store.fetch(r['id'])['sha256'];p['fields']['amount_due']='50000'
        p['independently_payable']=False
        with self.assertRaises(ValueError):self.manager().review(r['id'],p)
        p['independently_payable']=True
        oid=self.manager().review(r['id'],p)['id']
        second,p2=self.notice(JPEG+b'photo2')
        result=self.manager().review(second['id'],p2)
        self.assertTrue(result['needs_duplicate_review'])
        self.assertEqual(len(self.store.list_obligations()['obligations']),1)
        result=self.manager().review(second['id'],dict(sha256=p2['sha256'],action='link',target_id=oid))
        self.assertEqual(len(self.manager().get(oid)['data']['image_refs']),2)
        self.assertEqual(validate(self.root)[1],[])

    def test_receipt_partial_paid_void_and_retry(self):
        oid=self.create_notice()
        p=self.receipt(oid)
        self.manager().pay(oid,p)
        data=self.manager().get(oid)['data']
        self.assertEqual((data['status'],data['amount_remaining']),('partial','30000'))
        self.assertTrue(self.manager().pay(oid,p)['already_recorded'])
        p2=self.receipt(oid,JPEG+b'receipt2');p2['amount']='30000'
        self.manager().pay(oid,p2)
        data=self.manager().get(oid)['data'];self.assertEqual(data['status'],'paid')
        correction=dict(sha256=self.store.fetch(oid)['sha256'],operation_id='synthetic-operation',payment_id=data['payments'][0]['id'],reason='金額の転記誤り')
        self.manager().correct(oid,correction)
        data=self.manager().get(oid)['data']
        self.assertEqual(len(data['payments']),2)
        self.assertEqual(data['amount_remaining'],'20000')
        self.assertTrue(self.manager().correct(oid,correction)['already_recorded'])
        self.assertEqual(validate(self.root)[1],[])
        self.assertEqual(len(self.manager().overview()['pending']),0)

    def test_rephotographed_receipt_duplicate_and_overpayment(self):
        oid=self.create_notice()
        p=self.receipt(oid);self.manager().pay(oid,p)
        p2=self.receipt(oid,JPEG+b'other_receipt_photo')
        self.assertTrue(self.manager().pay(oid,p2)['needs_duplicate_review'])
        p2.update(distinct_confirmed=True,distinct_reason='異なる取引番号を本人確認')
        self.manager().pay(oid,p2)
        p3=self.receipt(oid,JPEG+b'third')
        p3['amount']='10001'
        with self.assertRaises(ValueError):self.manager().pay(oid,p3)
        self.assertEqual(self.manager().get(oid)['data']['amount_remaining'],'10000')
        self.assertEqual(self.manager().get(p3['capture_id'])['data']['capture_status'],'needs_review')

    def test_stale_sha_and_invalid_inputs(self):
        oid=self.create_notice()
        p=self.receipt(oid)
        p['sha256']='stale'
        with self.assertRaises(ValueError):self.manager().pay(oid,p)
        p['sha256']=self.store.fetch(oid)['sha256'];p['amount']='-3'
        with self.assertRaises(ValueError):self.manager().pay(oid,p)
        p['amount']='20000';p['confirmed']=False
        with self.assertRaises(ValueError):self.manager().pay(oid,p)
        p['confirmed']=True;p['paid_at']='2099-01-01'
        with self.assertRaises(ValueError):self.manager().pay(oid,p)
        self.assertEqual(self.manager().get(oid)['data']['payments'],[])

    def test_atomic_rollback_and_unsynced(self):
        r,p=self.notice()
        real_git=self.store.git
        def fail_commit(*args):
            if args[0]=='commit':raise ValueError('synthetic commit failure')
            return real_git(*args)
        with patch.object(self.store,'git',side_effect=fail_commit):
            with self.assertRaises(ValueError):self.manager().review(r['id'],p)
        self.assertEqual(self.store.list_obligations()['obligations'],[])
        self.assertEqual(self.manager().get(r['id'])['data']['capture_status'],'needs_review')
        self.assertEqual(self.git('status','--porcelain'),'')
        self.git('remote','set-url','--push','origin',str(self.root/'unavailable'))
        result=self.manager().review(r['id'],p)
        self.assertFalse(result['synced'])
        self.assertFalse(self.manager().overview()['sync']['synced'])
        self.assertTrue(self.manager().review(r['id'],p)['already_recorded'])

    def test_amend_preserves_history_and_optimistic_lock(self):
        oid = self.create_notice()
        before = self.manager().get(oid)
        payload = dict(sha256=before['sha256'], operation_id='amend-test', reason='用紙の金額を再確認',
                       confirmed=True, fields=dict(before['data'], amount_due='60000'))
        result = self.manager().amend(oid, payload)
        self.assertTrue(result['synced'])
        data = self.manager().get(oid)['data']
        self.assertEqual(data['amount_remaining'], '60000')
        self.assertEqual(data['obligation_corrections'][0]['previous']['amount_due'], '50000')
        self.assertTrue(self.manager().amend(oid, payload)['already_recorded'])
        payload.update(operation_id='another', reason='stale')
        with self.assertRaises(ValueError):
            self.manager().amend(oid, payload)
        self.assertEqual(validate(self.root)[1], [])

    def test_payment_replacement_keeps_original_and_receipt(self):
        oid = self.create_notice()
        payload = self.receipt(oid)
        self.manager().pay(oid, payload)
        record = self.manager().get(oid)
        old = record['data']['payments'][0]
        result = self.manager().correct(oid, dict(sha256=record['sha256'], operation_id='replacement-test',
            payment_id=old['id'], reason='金額を領収書に合わせて訂正',
            replacement=dict(amount='15000', paid_at='2026-11-01', method='現金')))
        self.assertTrue(result['synced'])
        data = self.manager().get(oid)['data']
        self.assertEqual(data['amount_remaining'], '35000')
        self.assertEqual(data['payments'][0], old)
        self.assertEqual(data['payments'][1]['image_sha256'], old['image_sha256'])
        self.assertEqual(validate(self.root)[1], [])

    def test_two_writers_only_one_payment_commits(self):
        from concurrent.futures import ThreadPoolExecutor
        oid = self.create_notice()
        p1 = self.receipt(oid, JPEG+b'concurrent1')
        p2 = self.receipt(oid, JPEG+b'concurrent2')
        def save(payload):
            try:
                return self.manager().pay(oid, payload)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, (p1, p2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.manager().get(oid)['data']['payments']), 1)
        self.assertEqual(len(self.manager().overview()['pending']), 1)
        self.assertEqual(validate(self.root)[1], [])

    def test_buckets_and_filter(self):
        dates=[('2026-11-02','overdue'),('2026-11-03','soon'),('2026-12-03','soon'),('2026-12-04','later'),(None,'undated')]
        for index,(due,bucket) in enumerate(dates):
            r,p=self.notice(JPEG+str(index).encode(),due_date=due,installment=f'第{index+1}期')
            self.manager().review(r['id'],p)
        with patch('mcp_store.datetime') as dt:
            dt.now.return_value=datetime(2026,11,3,tzinfo=ZoneInfo('Asia/Tokyo'))
            rows=self.store.list_obligations('open')['obligations']
            self.assertEqual([r['bucket'] for r in rows],[b for _,b in dates])
            self.assertEqual(len(self.store.list_obligations(due_before='2026-11-03')['obligations']),2)

    def test_ocr_manual_fallback_and_labeled_candidates(self):
        self.captures.ocr=lambda image: (_ for _ in ()).throw(ValueError('unreadable'))
        r=self.manager().capture_result(JPEG,'notice',None)
        self.assertTrue(r['record']['data']['ocr_failed'])
        self.assertEqual(self.store.list_obligations()['obligations'],[])
        lines=[dict(text=t,confidence=.99) for t in ['請求元：架空市','令和8年度 住民税 第3期','納期限 令和8年11月2日']]
        self.assertEqual(extract(lines)['issuer'],'架空市')
        self.assertEqual(extract(lines)['installment'],'第3期')
        self.assertEqual(extract_receipt([dict(text='領収日 令和8年11月1日',confidence=.99)])['paid_at'],'2026-11-01')
        self.assertIsNone(extract_receipt([dict(text='納期限 2026年11月2日',confidence=.99)])['paid_at'])

    def test_http_auth_csrf_images_and_lifecycle(self):
        self.assertEqual(self.client.get('/api/overview').status_code,401)
        self.assertEqual(self.client.get('/api/image?id='+'a'*64).status_code,401)
        token=self.login();headers={'Origin':self.origin,'X-Capture-CSRF':token}
        r=self.client.post('/capture?kind=notice',headers=headers,content=JPEG).json()
        record=r['record']
        self.assertEqual(self.client.get('/api/image?id='+record['data']['image_sha256']).status_code,200)
        self.assertIn("img-src 'self'",self.client.get('/').headers['content-security-policy'])
        self.assertEqual(self.client.get('/api/image?id=../secret').status_code,409)
        self.assertEqual(self.client.get('/api/image?id='+'a'*64).status_code,409)
        self.assertEqual(self.client.post('/api/review?id='+r['id'],json={}).status_code,403)
        payload=self.notice()[1]
        response=self.client.post('/api/review?id='+r['id'],headers=headers,json=payload)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(self.client.get('/api/overview').json()['obligations']),1)
        script=re.search(r'<script>(.*?)</script>',self.client.get('/').text,re.S)[1]
        dest=self.state/'web.js';dest.write_text(script)
        result=subprocess.run(['node','--check',str(dest)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
