"""Generated schema validation, transport failures and durable collection selection."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'scripts'))
from task_sync import TasksClient, TaskError, configured_collection, queue_obligation
from vendor.ai_tasks_contract import CONTRACT_SHA256
from vendor.ai_tasks_contract.runtime import ContractError, validate

class TasksContractTests(unittest.TestCase):
    def test_generated_digest_and_schema(self):
        contract = Path(__file__).resolve().parents[1]/'scripts/vendor/ai_tasks_contract/contract.json'
        self.assertEqual(hashlib.sha256(contract.read_bytes()).hexdigest(), CONTRACT_SHA256)
        for data in [{'title':'x','due_date':'2026-02-30'}, {'title':'x','status':'unknown'}, {'title':'x','extra':True}]:
            with self.assertRaises(ContractError): validate('CreateTask',data)
        with self.assertRaises(ContractError): validate('ListResponse',{'tasks':[{'id':'incomplete'}],'next_page':None})
        validate('UpdateTask',{'version':1,'status':'review'})

    def test_collection_query_and_invalid_page(self):
        client=TasksClient('http://127.0.0.1:8787',allow_local=True,collection_id='payments')
        with patch.object(client.sdk,'transport',return_value={'tasks':[],'next_page':None}) as send:
            client.list()
            self.assertIn('collection_id=payments',send.call_args.args[1])
            client.list(collection_id='other')
            self.assertIn('collection_id=other',send.call_args.args[1])
            with self.assertRaises(TaskError): client.list(page=True)
            with self.assertRaises(TaskError): client.list(page=100000)
        with patch.object(client.sdk,'transport',return_value={'tasks':[{}],'next_page':None}):
            with self.assertRaises(TaskError) as error: client.list()
            self.assertEqual(error.exception.code,'contract_mismatch')

    def test_durable_creation_collection_and_original_payload(self):
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE task_links(record_id TEXT PRIMARY KEY,create_payload TEXT,desired TEXT)')
        data={'id':'synthetic','category':'tax','status':'unpaid'}
        queue_obligation(db,data,'https://example.test','payments')
        original=db.execute('SELECT create_payload FROM task_links').fetchone()[0]
        self.assertEqual(json.loads(original)['collection_id'],'payments')
        queue_obligation(db,dict(data,due_date='2027-01-01'),'https://example.test','other')
        self.assertEqual(db.execute('SELECT create_payload FROM task_links').fetchone()[0],original)
        self.assertNotIn('collection_id',json.loads(db.execute('SELECT desired FROM task_links').fetchone()[0]))
        db.close()

    def test_configuration(self):
        with TemporaryDirectory() as directory, patch.dict('os.environ',{},clear=True):
            state=Path(directory)
            self.assertEqual(configured_collection(state),'default')
            (state/'ai-tasks-config.json').write_text(json.dumps({'collection_id':'payments'}))
            self.assertEqual(configured_collection(state),'payments')
            (state/'ai-tasks-config.json').write_text('{broken')
            self.assertIsNone(configured_collection(state,strict=False))
            with self.assertRaises(TaskError): configured_collection(state)
