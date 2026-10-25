"""Synthetic plans and temporary Git repositories only."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('funding_editor',ROOT/'scripts/funding_roadmap.py')
editor=importlib.util.module_from_spec(spec)
spec.loader.exec_module(editor)


def fixture():
    return dict(version=2,updated_at='2026-10-25',source='Synthetic plan',monthly=dict(
        income=20,living=5,rent=3,rent_until='2027-04',canada=10,canada_start='2027-09',
        canada_end='2027-11',income_start='2026-11',income_end='2027-08',unit='万円'),events=[
        dict(id=str(uuid4()),type='funding_event',date='2026-11-30',title='Synthetic event',amount_wan=12,
             source='Synthetic source',updated_at='2026-10-25',confirmed=False,note='Unconfirmed breakdown',key='example'),
        dict(id=str(uuid4()),type='funding_event',date='2027-11-30',title='Checkpoint',amount_wan=0,
             source='Synthetic source',updated_at='2026-10-25',confirmed=False,note='Recurring costs',key='canada-living')])


class ModelTest(unittest.TestCase):
    def test_migration_preserves_total_ids_and_sources(self):
        old=fixture();new=editor.migrate(old);editor.validate(new)
        self.assertEqual(old['version'],2)
        self.assertEqual(new['events'][0]['id'],old['events'][0]['id'])
        self.assertEqual(new['events'][0]['items'][0]['amount'],120000)
        self.assertEqual(new['events'][0]['source'],'Synthetic source')
        self.assertTrue(new['events'][0]['items'][0]['unallocated'])
        self.assertEqual(new['events'][1]['items'],[])
        self.assertEqual(new['monthly_rules'][0]['amount'],200000)
        self.assertEqual(editor.migrate(new),new)
        self.assertEqual(editor.migrate(old),new)

    def test_null_amount_and_negative_or_fractional_amount(self):
        data=editor.migrate(fixture());item=data['events'][0]['items'][0]
        item.update(amount=None,status='unknown');editor.validate(data)
        for wrong in (-1,0.1,True,float('nan')):
            item['amount']=wrong
            with self.assertRaises(ValueError):editor.validate(data)
        item.update(amount=None,status='confirmed')
        with self.assertRaises(ValueError):editor.validate(data)

    def test_invalid_dates_and_periods(self):
        data=editor.migrate(fixture())
        data['monthly_rules'][0]['end_month']='2026-01'
        with self.assertRaises(ValueError):editor.validate(data)
        data=editor.migrate(fixture());data['events'][0]['date']='2026-02-30'
        with self.assertRaises(ValueError):editor.validate(data)
        data['events'][0]['date']='2026-01-01'
        with self.assertRaises(ValueError):editor.validate(data)

    def test_duplicate_ids_and_month_overrides(self):
        data=editor.migrate(fixture());rule=data['monthly_rules'][0]
        data['overrides']=[dict(id=str(uuid4()),rule_id=rule['id'],month='2026-11',amount=0,note='No income')]
        editor.validate(data)
        data['overrides'].append(dict(data['overrides'][0],id=str(uuid4())))
        with self.assertRaises(ValueError):editor.validate(data)
        data['overrides']=[];data['events'][0]['items'][0]['id']=rule['id']
        with self.assertRaises(ValueError):editor.validate(data)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.git('init','-b','main');self.git('config','user.email','test@example.invalid');self.git('config','user.name','Synthetic test')
        self.path=self.root/editor.DB;self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(fixture()))
        (self.root/'unrelated.txt').write_text('Original')
        self.git('add','.');self.git('commit','-m','Synthetic fixture')
        self.store=editor.PlanStore(self.root)

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.root),*args],text=True,stderr=subprocess.DEVNULL).strip()

    def test_read_does_not_migrate_disk(self):
        raw=self.path.read_bytes();self.store.read();self.assertEqual(raw,self.path.read_bytes())

    def test_save_and_preserve_unrelated_staged_edits(self):
        (self.root/'unrelated.txt').write_text('Another agent edit');self.git('add','unrelated.txt')
        data,etag=self.store.read();data['events'][0]['items'][0]['note']='Edited'
        saved,new_etag,_=self.store.save(data,etag)
        self.assertEqual(saved['version'],3);self.assertEqual(saved['revision'],1);self.assertNotEqual(etag,new_etag)
        self.assertEqual(self.git('diff','--cached','--name-only'),'unrelated.txt')
        self.assertEqual(self.git('show','HEAD:unrelated.txt'),'Original')
        self.assertEqual(self.git('show','--format=','--name-only','HEAD'),str(editor.DB))

    def test_concurrent_save_is_rejected(self):
        data,etag=self.store.read();self.store.save(data,etag)
        with self.assertRaises(editor.Conflict):self.store.save(data,etag)

    def test_uncommitted_db_is_not_overwritten(self):
        data,etag=self.store.read();self.path.write_text(self.path.read_text()+'\n')
        current=self.path.read_bytes()
        with self.assertRaises(editor.Conflict):self.store.save(data,etag)
        self.assertEqual(self.path.read_bytes(),current)

    def test_http_origin_csrf_and_conflict(self):
        import socket, sys, time, urllib.error, urllib.request
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        code=f"import sys;sys.path.insert(0,{str(ROOT/'scripts')!r});from funding_roadmap import serve;from pathlib import Path;serve(Path({str(self.root)!r}),{port})"
        process=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            origin=f'http://127.0.0.1:{port}'
            for _ in range(50):
                try:
                    with urllib.request.urlopen(origin+'/api/plan',timeout=1) as response: initial=json.load(response)
                    break
                except OSError:time.sleep(.05)
            else:self.fail('Test HTTP server did not start')
            payload=json.dumps(initial['plan']).encode()
            request=urllib.request.Request(origin+'/api/plan',data=payload,method='PUT')
            with self.assertRaises(urllib.error.HTTPError) as rejected:urllib.request.urlopen(request)
            self.assertEqual(rejected.exception.code,403)
            headers={'Origin':origin,'X-CSRF-Token':initial['csrf'],'If-Match':initial['etag'],'Content-Type':'application/json'}
            with urllib.request.urlopen(urllib.request.Request(origin+'/api/plan',data=payload,headers=headers,method='PUT')) as response:saved=json.load(response)
            self.assertEqual(saved['plan']['revision'],1)
            self.assertFalse(saved['remote_synced'])
            with self.assertRaises(urllib.error.HTTPError) as conflict:urllib.request.urlopen(urllib.request.Request(origin+'/api/plan',data=payload,headers=headers,method='PUT'))
            self.assertEqual(conflict.exception.code,409)
            with self.assertRaises(urllib.error.HTTPError) as host:urllib.request.urlopen(urllib.request.Request(origin+'/api/plan',headers={'Host':'attacker.invalid'}))
            self.assertEqual(host.exception.code,403)
        finally:
            process.terminate();process.wait(timeout=5)

    def test_failed_commit_restores_db_and_preserves_index(self):
        data,etag=self.store.read();original=self.path.read_bytes()
        hook=self.root/'.git/hooks/pre-commit';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
        with self.assertRaises(ValueError):self.store.save(data,etag)
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(self.git('status','--porcelain'),'')


if __name__=='__main__':unittest.main()
