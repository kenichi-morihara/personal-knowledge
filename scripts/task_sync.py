"""AI Tasks is the ToDo backend; financial truth never comes from a task's status."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from vendor.ai_tasks_contract import Client as ContractClient, STATUSES, CreateResponse, ListResponse, TaskDetail, Task, UpdateTask
from vendor.ai_tasks_contract.runtime import ContractError, validate


class TaskError(ValueError):
    def __init__(self, message, *, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TasksClient:
    def __init__(self, origin=None, client_id=None, client_secret=None, allow_local=False, collection_id='default'):
        self.origin = (origin or '').rstrip('/')
        if not isinstance(collection_id, str) or not 1 <= len(collection_id) <= 128:
            raise TaskError('AI Tasksの分類IDを確認してください。')
        self.collection_id = collection_id
        self.sdk = ContractClient(self._transport)
        self.headers = {'Content-Type': 'application/json'}
        self.configured = bool(self.origin)
        if not self.configured:
            return
        parsed = urlparse(self.origin)
        local = allow_local and self.origin == 'http://127.0.0.1:8787'
        if (parsed.scheme != 'https' and not local) or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise TaskError('AI_TASKS_URLはHTTPS originか、明示的に許可したローカル8787番を指定してください。')
        if not local:
            if not client_id or not client_secret:
                raise TaskError('AI Tasksのサービス認証設定がありません。')
            self.headers.update({'CF-Access-Client-Id': client_id, 'CF-Access-Client-Secret': client_secret})

    @classmethod
    def environment(cls, state=None):
        config = {}
        if state is not None:
            path = Path(state) / 'ai-tasks-config.json'
            if path.is_file():
                config = json.loads(path.read_text())
        if not isinstance(config, dict):
            raise TaskError('AI Tasksの接続設定を確認してください。')
        origin = os.environ.get('AI_TASKS_URL', config.get('origin'))
        local = os.environ.get('AI_TASKS_ALLOW_LOCAL') == '1' if 'AI_TASKS_ALLOW_LOCAL' in os.environ else config.get('allow_local') is True
        return cls(origin, os.environ.get('CF_ACCESS_CLIENT_ID'), os.environ.get('CF_ACCESS_CLIENT_SECRET'), local, os.environ.get('AI_TASKS_COLLECTION_ID', config.get('collection_id', 'default')))

    def _transport(self, method, path, data=None, key=None):
        if not self.configured:
            raise TaskError('AI Tasks未接続です。バックエンドの接続先をMacで設定してください。')
        headers = dict(self.headers)
        if key:
            headers['Idempotency-Key'] = key
        request = Request(self.origin + path, method=method, headers=headers,
                          data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None)
        try:
            with build_opener(NoRedirect).open(request, timeout=8) as response:
                if 'application/json' not in response.headers.get('Content-Type', ''):
                    raise TaskError('AI Tasksの認証・API応答を確認してください。', code='non_json_response')
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise TaskError('AI Tasksの応答サイズを確認してください。', code='invalid_response')
                return json.loads(raw)
        except HTTPError as exc:
            code = 'http_error'
            try:
                if 'application/json' in exc.headers.get('Content-Type', ''):
                    error = validate('ErrorResponse', json.loads(exc.read(65536)))
                    code = error['error']['code']
            except (ValueError, KeyError, TypeError):
                code = 'invalid_error_response'
            finally:
                exc.close()
            raise TaskError(f'AI Tasksへの接続または更新に失敗しました（HTTP {exc.code}）。', code=code, status=exc.code) from None
        except TaskError:
            raise
        except (URLError, OSError, ValueError):
            raise TaskError('AI Tasksに接続できません。記録はMacに保存済みで、後から再試行できます。', code='request_failed') from None

    def request(self, method, path, data=None, key=None) -> CreateResponse:
        # Compatibility for the durable outbox: creation still uses its original payload.
        if method != 'POST' or path != '':
            raise TaskError('未対応のAI Tasks操作です。')
        try:
            payload = dict(data)
            return self.sdk.createTask(payload, key)
        except ContractError as exc:
            raise TaskError(str(exc), code='contract_mismatch') from None

    def collections(self):
        try:
            return dict(self.sdk.listCollections(), selected_collection_id=self.collection_id)
        except ContractError as exc:
            raise TaskError(str(exc), code='contract_mismatch') from None

    def list(self, page: int = 0, collection_id: str | None = None) -> ListResponse:
        try:
            return self.sdk.listTasks({'page': page, 'collection_id': collection_id or self.collection_id})
        except ContractError as exc:
            raise TaskError(str(exc), code='contract_mismatch') from None

    def get(self, task_id: str) -> TaskDetail:
        try:
            return self.sdk.getTask(task_id)['task']
        except ContractError as exc:
            raise TaskError(str(exc), code='contract_mismatch') from None

    def update(self, task_id: str, payload: UpdateTask) -> Task:
        try:
            return self.sdk.updateTask(task_id, payload)['task']
        except ContractError as exc:
            raise TaskError(str(exc), code='contract_mismatch') from None


def configured_collection(state, *, strict=True):
    path = Path(state) / 'ai-tasks-config.json'
    try:
        config = json.loads(path.read_text()) if path.is_file() else {}
        value = os.environ.get('AI_TASKS_COLLECTION_ID', config.get('collection_id', 'default'))
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise TaskError('AI Tasksの分類IDを確認してください。')
        return value
    except (OSError, ValueError, AttributeError):
        if strict:
            raise TaskError('AI Tasksの分類設定を確認してください。') from None
        # Preserve local financial saving even when the independent task config is broken.
        return None


def desired_task(data, url):
    return dict(title=('支払い: ' + ' · '.join(str(data[k]) for k in ('category', 'issuer', 'fiscal_year', 'installment') if data.get(k)))[:200],
                description=f'請求ID: {data["id"]}\n原本: {url}\nToDoの完了は支払いの証明ではありません。支払い管理画面で実績を確認してください。',
                acceptance='本人が実際の支払いを確認し、請求原本の残額が0になる。',
                due_date=data.get('due_date'), status='cancelled' if data.get('lifecycle') == 'cancelled' else 'done' if data.get('status') == 'paid' else 'pending')


def queue_obligation(db, data, url, collection_id="default"):
    payload = json.dumps(desired_task(data, url), ensure_ascii=False, sort_keys=True)
    initial_data = desired_task(data, url)
    if collection_id is not None:
        initial_data['collection_id'] = collection_id
    initial = json.dumps(initial_data, ensure_ascii=False, sort_keys=True)
    db.execute('INSERT INTO task_links(record_id,create_payload,desired) VALUES (?,?,?) ON CONFLICT(record_id) DO UPDATE SET desired=excluded.desired',
               (data['id'], initial, payload))


class TaskSync:
    def __init__(self, store, client=None):
        self.store = store
        self.config_error = None
        try:
            self.client = client or TasksClient.environment(store.state)
        except (TaskError, OSError, ValueError):
            self.client = TasksClient()
            self.config_error = 'AI Tasksの接続設定を確認してください。請求・支払いはMacに保存できます。'

    def status(self):
        with self.store.database() as db:
            rows = list(db.execute('SELECT record_id,task_id,desired,acknowledged,last_error FROM task_links'))
        return {'configured': self.client.configured, 'error': self.config_error,
                'pending': sum(r['desired'] != r['acknowledged'] or bool(r['last_error']) for r in rows),
                'links': [dict(record_id=r['record_id'], task_id=r['task_id'], error=r['last_error'],
                               synced=r['desired'] == r['acknowledged'] and not r['last_error']) for r in rows]}

    def sync(self):
        if not self.client.configured:
            return self.status()
        with self.store.locked(), self.store.database() as db:
            for path, _, data in self.store.records():
                if data.get('type') == 'obligation':
                    queue_obligation(db, data, self.store.url(path), getattr(self.client, 'collection_id', 'default'))
        # Independent of record writes. Never hold the record lock over HTTP.
        with (self.store.state / 'tasks-sync.lock').open('a') as mutex:
            import fcntl
            try:
                fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self.status()
            with self.store.database() as db:
                jobs = [dict(r) for r in db.execute('SELECT * FROM task_links ORDER BY record_id')]
            for job in jobs:
                try:
                    task_id = job['task_id']
                    if task_id is None:
                        created = self.client.request('POST', '', json.loads(job['create_payload']), 'payment_' + job['record_id'])
                        task_id = created['task']['id']
                        with self.store.database() as db:
                            db.execute('UPDATE task_links SET task_id=? WHERE record_id=?', (task_id, job['record_id']))
                    current = self.client.get(task_id)
                    desired = json.loads(job['desired'])
                    if desired['status'] == 'pending':
                        # Keep ordinary progress; manually completed unpaid tasks need review.
                        desired['status'] = 'review' if current['status'] in ('done', 'cancelled') else current['status']
                    patch = {key: value for key, value in desired.items() if current.get(key) != value}
                    if patch:
                        self.client.update(task_id, dict(patch, version=current['version']))
                    with self.store.database() as db:
                        db.execute('UPDATE task_links SET acknowledged=?, last_error=NULL WHERE record_id=? AND desired=?',
                                   (job['desired'], job['record_id'], job['desired']))
                except (TaskError, KeyError, TypeError):
                    with self.store.database() as db:
                        db.execute('UPDATE task_links SET last_error=? WHERE record_id=?',
                                   ('ToDoへの反映は未完了です。接続・更新競合を確認して再試行してください。', job['record_id']))
        return self.status()

    def update_task(self, task_id, payload):
        status = payload.get('status')
        version = payload.get('version')
        if status not in STATUSES or type(version) is not int or version < 1:
            raise TaskError('ToDoの状態・更新番号を確認してください。')
        with self.store.database() as db:
            linked = db.execute('SELECT record_id FROM task_links WHERE task_id=?', (task_id,)).fetchone()
        if linked and status == 'done':
            record = self.store.fetch(linked['record_id'])
            if self.store.parse(record['text']).get('status') != 'paid':
                raise TaskError('このToDoは支払い管理画面で実際の支払いを記録して完了してください。')
        return self.client.update(task_id, {'version': version, 'status': status})
