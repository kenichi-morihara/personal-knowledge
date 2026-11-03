"""Regression tests for safe local saving, cancellation, reassignment and AI Tasks."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_management as fixture
from management import Management, document, today
from mcp_store import Store
from task_sync import TaskError, TasksClient, TaskSync
from validate import validate


class FakeTasks:
    configured = True

    def __init__(self):
        self.tasks = {}
        self.keys = {}
        self.fail = False
        self.lose_response = False
        self.conflict = False

    def request(self, method, path, data=None, key=None):
        if self.fail:
            raise TaskError('Synthetic offline')
        if method == 'POST':
            if key in self.keys:
                task_id, initial = self.keys[key]
                if data != initial:
                    raise TaskError('Idempotency payload changed')
            else:
                task_id = str(uuid4())
                self.keys[key] = (task_id, dict(data))
                self.tasks[task_id] = dict(data, id=task_id, version=1)
            if self.lose_response:
                self.lose_response = False
                raise TaskError('Synthetic lost response after create')
            return {'task': dict(self.tasks[task_id])}
        raise AssertionError(method)

    def list(self, page=0):
        return {'tasks': [dict(t) for t in self.tasks.values()][page*50:(page+1)*50], 'next_page': None}

    def get(self, task_id):
        if self.fail:
            raise TaskError('Synthetic offline')
        return dict(self.tasks[task_id])

    def update(self, task_id, payload):
        if self.fail or self.conflict:
            raise TaskError('Synthetic update conflict')
        task = self.tasks[task_id]
        if payload['version'] != task['version']:
            raise TaskError('Version conflict')
        task.update(payload)
        task['version'] += 1
        return dict(task)


class PaymentRegressionTest(unittest.TestCase):
    git = fixture.ManagementTest.git
    notice = fixture.ManagementTest.notice
    create_notice = fixture.ManagementTest.create_notice
    receipt = fixture.ManagementTest.receipt
    manager = fixture.ManagementTest.manager

    def setUp(self):
        fixture.ManagementTest.setUp(self)
        self.store.auto_sync = False

    def payload(self, oid, **extra):
        return dict(sha256=self.store.fetch(oid)['sha256'], operation_id=str(uuid4()), **extra)

    def manual(self, oid, **extra):
        return self.payload(oid, amount='1000', paid_at=today(), method='口座振替', source='本人が確認した架空取引',
                            evidence_type='statement', confirmed=True, **extra)

    def validate_local(self):
        with TemporaryDirectory() as directory:
            self.store.materialize(Path(directory))
            self.assertEqual(validate(Path(directory))[1], [])

    def test_different_photos_cannot_race_into_duplicate_bills(self):
        a,p1 = self.notice(fixture.JPEG+b'A')
        b,p2 = self.notice(fixture.JPEG+b'B')
        barrier = Barrier(2)
        def confirm(args):
            barrier.wait()
            return self.manager().review(*args)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, [(a['id'],p1),(b['id'],p2)]))
        self.assertEqual(sum(r.get('needs_duplicate_review', False) for r in results), 1)
        self.assertEqual(len(self.store.list_obligations('open')['obligations']), 1)
        self.assertEqual(len(self.manager().overview()['pending']), 1)
        self.validate_local()

    def test_generic_mcp_write_cannot_bypass_duplicate_guard(self):
        oid = self.create_notice()
        record = self.manager().get(oid)
        data = dict(record['data'], id=str(uuid4()))
        with self.assertRaisesRegex(ValueError, 'Similar obligation'):
            self.store.put('finance/taxes/duplicate.md', document(data,'# Duplicate\n'), '')

    def test_cancel_and_reopen_preserve_amount_and_history(self):
        oid = self.create_notice()
        p = self.payload(oid,action='cancel',reason='重複を本人確認')
        self.manager().lifecycle(oid,p)
        self.assertTrue(self.manager().lifecycle(oid,p)['already_recorded'])
        self.assertEqual(self.store.list_obligations('open')['obligations'], [])
        self.assertEqual(len(self.store.list_obligations('cancelled')['obligations']), 1)
        data = self.manager().get(oid)['data']
        self.assertEqual(data['amount_due'], '50000')
        with self.assertRaises(ValueError):
            self.manager().pay(oid,self.manual(oid))
        self.manager().lifecycle(oid,self.payload(oid,action='reopen',reason='別請求と再確認'))
        self.assertEqual(len(self.store.list_obligations('open')['obligations']), 1)
        self.assertEqual(len(self.manager().get(oid)['data']['lifecycle_events']), 2)
        self.manager().pay(oid,self.manual(oid))
        with self.assertRaises(ValueError):
            self.manager().lifecycle(oid,self.payload(oid,action='cancel',reason='誤った取消'))
        self.validate_local()

    def test_reassign_same_receipt_photo_and_restore_dismissed(self):
        first = self.create_notice()
        a,p = self.notice(fixture.JPEG+b'second',installment='第4期')
        second = self.manager().review(a['id'],p)['id']
        payment = self.receipt(first)
        uploaded = self.manager().capture_result(fixture.JPEG+b'receipt','receipt',second)
        self.assertTrue(uploaded['association_conflict'])
        capture_id = payment['capture_id']
        self.manager().review(capture_id,dict(sha256=payment['capture_sha256'],action='dismiss',reason='対象の選び間違い'))
        self.manager().reassign(capture_id,self.payload(capture_id,kind='receipt',target_id=second,reason='正しい第4期へ変更'))
        self.assertNotIn('管理対象外。', self.store.fetch(capture_id)['text'])
        payment.update(sha256=self.store.fetch(second)['sha256'],capture_sha256=self.store.fetch(capture_id)['sha256'])
        self.manager().pay(second,payment)
        self.assertEqual(self.manager().get(first)['data']['amount_paid'],'0')
        self.assertEqual(self.manager().get(second)['data']['amount_paid'],'20000')
        with self.assertRaises(ValueError):
            self.manager().reassign(capture_id,self.payload(capture_id,kind='receipt',target_id=first,reason='確定後の変更'))
        self.validate_local()

    def test_manual_payment_requires_evidence_and_is_retry_safe(self):
        oid=self.create_notice()
        p=self.manual(oid)
        invalid=dict(p,source='')
        with self.assertRaises(ValueError):self.manager().pay(oid,invalid)
        self.manager().pay(oid,p)
        self.assertTrue(self.manager().pay(oid,p)['already_recorded'])
        data=self.manager().get(oid)['data']
        self.assertEqual(data['amount_remaining'],'49000')
        self.assertEqual(len(data['payments']),1)
        self.assertNotIn('image_sha256',data['payments'][0])
        self.validate_local()

    def test_unrelated_edits_and_offline_saves_survive_restart(self):
        (self.root/'README.md').write_text('User staged edit\n')
        self.git('add','README.md')
        oid=self.create_notice()
        self.manager().pay(oid,self.manual(oid))
        before=self.git('rev-parse','HEAD')
        with patch.object(self.store,'git',side_effect=ValueError('Synthetic offline')):
            self.assertFalse(self.store.sync()['synced'])
        restarted=Store(self.root,self.store.state,auto_sync=False)
        self.assertEqual(restarted.parse(restarted.fetch(oid)['text'])['amount_remaining'],'49000')
        self.assertEqual(self.git('rev-parse','HEAD'),before)
        self.assertTrue(restarted.sync()['synced'])
        self.assertEqual(self.git('diff','--cached','--name-only'),'README.md')
        self.assertEqual(self.git('show','HEAD:README.md'),'# Synthetic fixture')
        self.assertEqual((self.root/'README.md').read_text(),'User staged edit\n')
        self.assertEqual(restarted.sync_status()['pending_records'],0)

    def test_sync_never_pushes_unrelated_user_commits(self):
        (self.root/'README.md').write_text('User committed edit\n')
        self.git('add','README.md');self.git('commit','-m','User work not yet approved for push')
        before=self.git('rev-parse','origin/main')
        oid=self.create_notice()
        self.assertFalse(self.store.sync()['synced'])
        self.assertEqual(self.git('rev-parse','origin/main'),before)
        self.assertEqual(self.manager().get(oid)['data']['amount_due'],'50000')

    def test_network_export_does_not_block_local_writes(self):
        oid=self.create_notice()
        entered,release=Event(),Event()
        original=self.store.git
        def slow(*args):
            if args[0]=='fetch':
                entered.set();release.wait(timeout=10)
                raise ValueError('Offline')
            return original(*args)
        with patch.object(self.store,'git',side_effect=slow):
            worker=Thread(target=self.store.sync);worker.start()
            self.assertTrue(entered.wait(5))
            self.manager().pay(oid,self.manual(oid))
            self.assertEqual(self.manager().get(oid)['data']['amount_paid'],'1000')
            release.set();worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_task_retry_deadline_payment_cancel_and_manual_done(self):
        oid=self.create_notice()
        client=FakeTasks();sync=TaskSync(self.store,client)
        client.lose_response=True
        self.assertGreater(sync.sync()['pending'],0)
        r=self.manager().get(oid)
        self.manager().amend(oid,self.payload(oid,reason='納期限を訂正',confirmed=True,fields=dict(r['data'],due_date='2026-12-25')))
        self.assertEqual(sync.sync()['pending'],0)
        self.assertEqual(len(client.tasks),1)
        task=next(iter(client.tasks.values()))
        self.assertEqual(task['due_date'],'2026-12-25')
        with self.assertRaises(TaskError):sync.update_task(task['id'],{'status':'done','version':task['version']})
        task['status']='done';task['version']+=1
        sync.sync()
        self.assertEqual(task['status'],'review')
        self.assertEqual(self.manager().get(oid)['data']['amount_paid'],'0')
        p=self.manual(oid);p['amount']='50000'
        self.manager().pay(oid,p);sync.sync();self.assertEqual(task['status'],'done')
        payment=self.manager().get(oid)['data']['payments'][0]
        self.manager().correct(oid,self.payload(oid,payment_id=payment['id'],reason='実績取消を確認'))
        self.manager().lifecycle(oid,self.payload(oid,action='cancel',reason='請求取消を確認'))
        client.fail=True;self.assertGreater(sync.sync()['pending'],0)
        client.fail=False;self.assertEqual(sync.sync()['pending'],0)
        self.assertEqual(task['status'],'cancelled')

    def test_task_conflict_and_new_local_update_are_not_lost(self):
        oid=self.create_notice();client=FakeTasks();sync=TaskSync(self.store,client)
        sync.sync();r=self.manager().get(oid)
        self.manager().amend(oid,self.payload(oid,reason='期限訂正',confirmed=True,fields=dict(r['data'],due_date='2026-12-25')))
        client.conflict=True;self.assertGreater(sync.sync()['pending'],0)
        client.conflict=False;self.assertEqual(sync.sync()['pending'],0)
        self.assertEqual(len(client.tasks),1)

    def test_local_transaction_rolls_back_all_related_records(self):
        r,p=self.notice()
        with self.store.database() as db:
            db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON pending_records WHEN NEW.path LIKE 'finance/taxes/captures/%' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.Error):self.manager().review(r['id'],p)
        self.assertEqual(self.store.list_obligations()['obligations'],[])
        self.assertEqual(self.manager().get(r['id'])['data']['capture_status'],'needs_review')
        with self.store.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM task_links').fetchone()[0],0)
            db.execute('DROP TRIGGER synthetic_failure')
        self.manager().review(r['id'],p)
        self.assertEqual(len(self.store.list_obligations()['obligations']),1)

    def test_http_new_actions_require_auth_and_use_task_backend(self):
        from app import create_app
        from starlette.testclient import TestClient
        client=FakeTasks()
        self.client=TestClient(create_app(self.captures,self.origin,'test-only-owner-key',tasks=client),base_url=self.origin,follow_redirects=False)
        for action in ('lifecycle','reassign','task','sync'):
            self.assertEqual(self.client.post('/api/'+action,json={}).status_code,401)
        token=fixture.ManagementTest.login(self)
        headers={'Origin':self.origin,'X-Capture-CSRF':token}
        for action in ('lifecycle','reassign','task','sync'):
            self.assertEqual(self.client.post('/api/'+action,headers={'Origin':self.origin},json={}).status_code,403)
        oid=self.create_notice();sync=TaskSync(self.store,client);sync.sync()
        tasks=self.client.get('/api/tasks').json()['tasks'];task=tasks[0]
        blocked=self.client.post('/api/task?id='+task['id'],headers=headers,json={'version':task['version'],'status':'done'})
        self.assertEqual(blocked.status_code,409)
        p=self.manual(oid);p['amount']='50000'
        response=self.client.post('/api/pay?id='+oid,headers=headers,json=p)
        self.assertEqual(response.status_code,200,response.text)
        sync.sync()
        self.assertEqual(self.client.get('/api/tasks').json()['tasks'][0]['status'],'done')
        response=self.client.post('/api/sync',headers=headers,json={})
        self.assertFalse(response.json()['sync_requested'])

    def test_new_desired_state_during_http_is_not_acknowledged_as_old(self):
        oid=self.create_notice();client=FakeTasks();sync=TaskSync(self.store,client)
        sync.sync()
        r=self.manager().get(oid)
        self.manager().amend(oid,self.payload(oid,reason='First deadline change',confirmed=True,fields=dict(r['data'],due_date='2026-12-01')))
        original=client.get;changed=[]
        def simultaneous(task_id):
            if not changed:
                changed.append(True);r=self.manager().get(oid)
                self.manager().amend(oid,self.payload(oid,reason='Second deadline change',confirmed=True,fields=dict(r['data'],due_date='2026-12-25')))
            return original(task_id)
        with patch.object(client,'get',side_effect=simultaneous):
            self.assertGreater(sync.sync()['pending'],0)
        self.assertEqual(sync.sync()['pending'],0)
        self.assertEqual(next(iter(client.tasks.values()))['due_date'],'2026-12-25')

    def test_tasks_configuration_rejects_credentials_redirect_targets(self):
        for origin in ('http://attacker.invalid','https://user:pass@example.invalid','https://example.invalid/path','http://127.0.0.1:8787'):
            with self.assertRaises(TaskError):TasksClient(origin)
        self.assertTrue(TasksClient('http://127.0.0.1:8787',allow_local=True).configured)
        self.assertFalse(TasksClient().configured)


if __name__=='__main__':unittest.main()
