"""Restricted record access. No arbitrary file, shell or credential tool is exposed."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import sqlite3
import threading
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml

from validate import DOMAINS, validate

MAX_BYTES = 128 * 1024
SECRET = re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN .*PRIVATE KEY-----|sk-[A-Za-z0-9_-]{24,}')


class Store:
    def __init__(self, root, state, auto_sync=True):
        self.root = Path(root).resolve()
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.auto_sync = auto_sync
        self._local = threading.local()
        with self.database() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pending_records(path TEXT PRIMARY KEY, content TEXT NOT NULL, base_content TEXT);
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_links(record_id TEXT PRIMARY KEY, task_id TEXT, create_payload TEXT NOT NULL,
                    desired TEXT NOT NULL, acknowledged TEXT, last_error TEXT);
            """)
        (self.state / 'records.sqlite3').chmod(0o600)

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.state / 'records.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('Git operation failed; inspect local Git state before retrying')
        return result.stdout.strip()

    @contextmanager
    def locked(self):
        # A business operation can call fetch/put without releasing the shared process lock.
        if getattr(self._local, 'depth', 0):
            self._local.depth += 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        with (self.state / 'records.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._local.depth = 1
            try:
                yield
            finally:
                self._local.depth = 0
                fcntl.flock(lock, fcntl.LOCK_UN)

    def path(self, name):
        p = PurePosixPath(name)
        if (p.is_absolute() or len(p.parts) < 2 or p.parts[0] not in DOMAINS
                or p.suffix != '.md' or p.name == 'README.md'
                or any(part.startswith('.') or part in ('..', '') for part in p.parts)
                or str(p) != name or '\\' in name):
            raise ValueError('Only record Markdown paths in the five knowledge domains are allowed')
        target = self.root.joinpath(*p.parts)
        for part in [target, *target.parents]:
            if part == self.root:
                break
            if part.is_symlink():
                raise ValueError('Symlinks are not allowed')
        if not target.resolve().is_relative_to(self.root):
            raise ValueError('Invalid record path')
        return target

    def committed(self, path):
        exists = subprocess.run(['git', '-C', str(self.root), 'cat-file', '-e', f'HEAD:{path}'], capture_output=True)
        if exists.returncode:
            return None
        return subprocess.check_output(['git', '-C', str(self.root), 'show', f'HEAD:{path}'], text=True)

    def texts(self):
        texts = {}
        paths = self.git('ls-tree', '-r', '--name-only', '-z', 'HEAD', '--', *DOMAINS).split('\0')
        for name in paths:
            if name.endswith('.md') and PurePosixPath(name).name != 'README.md':
                self.path(name)
                texts[name] = self.committed(name)
        with self.database() as db:
            texts.update({r['path']: r['content'] for r in db.execute('SELECT path,content FROM pending_records')})
        return texts

    def records(self):
        for name, content in sorted(self.texts().items()):
            yield name, content, self.parse(content)

    @staticmethod
    def parse(content):
        if not content.startswith('---\n') or len(content.encode()) > MAX_BYTES:
            raise ValueError('Record requires YAML frontmatter and must be at most 128 KiB')
        try:
            data = yaml.safe_load(content.split('---\n', 2)[1])
            if not isinstance(data, dict) or not isinstance(data.get('id'), str):
                raise ValueError
            UUID(data['id'])
            if data.get('template'):
                raise ValueError
        except (ValueError, IndexError, yaml.YAMLError):
            raise ValueError('Invalid record metadata') from None
        return data

    def url(self, name):
        return f'https://github.com/kenichi-morihara/personal-knowledge/blob/main/{name}'

    def search(self, query):
        if not query.strip() or len(query) > 500:
            raise ValueError('Provide a search phrase of 1–500 characters')
        terms = query.casefold().split()
        results = []
        with self.locked():
            for name, content, data in self.records():
                if all(term in (name + '\n' + content).casefold() for term in terms):
                    title = next((line[2:] for line in content.splitlines() if line.startswith('# ')), name)
                    results.append({'id': data['id'], 'title': title, 'url': self.url(name)})
                if len(results) == 20:
                    break
        return {'results': results}

    def fetch(self, record_id):
        with self.locked():
            for name, content, data in self.records():
                if data['id'] == record_id:
                    title = next((line[2:] for line in content.splitlines() if line.startswith('# ')), name)
                    return {'id': record_id, 'title': title, 'text': content, 'url': self.url(name),
                            'path': name, 'sha256': hashlib.sha256(content.encode()).hexdigest(), 'pending_sync': self.record_pending(name)}
        raise ValueError('Record not found; search for its UUID first')

    def record_pending(self, path):
        with self.database() as db:
            return db.execute('SELECT 1 FROM pending_records WHERE path=?', (path,)).fetchone() is not None

    def materialize(self, directory):
        """A consistent view for validators and the read-only ledger, including the local queue."""
        with self.locked():
            for name in self.git('ls-files', '-z').split('\0'):
                if not name:
                    continue
                src = self.root / name
                if src.is_symlink():
                    raise ValueError('Tracked symlinks require local review')
                if src.is_file():
                    dst = directory / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
            for name, content in self.texts().items():
                dst = directory / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(content)

    def sync_status(self):
        with self.locked(), self.database() as db:
            pending = db.execute('SELECT count(*) FROM pending_records').fetchone()[0]
            error = db.execute("SELECT value FROM state WHERE key='sync_error'").fetchone()
            try:
                ahead = int(self.git('rev-list', '--count', 'origin/main..HEAD'))
                behind = int(self.git('rev-list', '--count', 'HEAD..origin/main'))
            except ValueError:
                ahead, behind = 0, 0
                if not error:
                    error = ('GitHubの同期状態を確認できません。',)
            return {'synced': not pending and not ahead and not behind and not error,
                    'pending_records': pending, 'ahead': ahead, 'behind': behind,
                    'error': error[0] if error else None, 'remote_checked': False}

    def put(self, path, content, expected_sha256):
        return self.put_many([dict(path=path, content=content, expected_sha256=expected_sha256)])

    def put_many(self, changes):
        """Durable local transaction. GitHub availability and unrelated code edits do not block saves."""
        if not changes or len(changes) > 10 or len({c['path'] for c in changes}) != len(changes):
            raise ValueError('Provide 1–10 distinct record paths')
        prepared = []
        today = datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()
        for change in changes:
            target = self.path(change['path'])
            data = self.parse(change['content'])
            if SECRET.search(change['content']):
                raise ValueError('Possible credential detected; do not store secrets in records')
            if data.get('updated_at') != today:
                raise ValueError(f'updated_at must be the current Asia/Tokyo date: {today}')
            prepared.append((change, target, data))
        nested = bool(getattr(self._local, 'depth', 0))
        with self.locked():
            texts = self.texts()
            before_texts = dict(texts)
            for change, target, data in prepared:
                old = texts.get(change['path'])
                actual = hashlib.sha256(old.encode()).hexdigest() if old is not None else ''
                if actual != change['expected_sha256']:
                    raise ValueError('Record changed or already exists; fetch again before updating')
                committed = self.committed(change['path'])
                if target.exists() and target.read_text() not in (committed, old, change['content']):
                    raise ValueError('This record has local changes outside the application; preserve and reconcile them first')
                if old is not None:
                    previous = self.parse(old)
                    if data['id'] != previous['id'] or data.get('type') != previous.get('type'):
                        raise ValueError('Record UUID and type cannot change')
                    if previous.get('type') == 'balance':
                        raise ValueError('Balances are append-only snapshots; create a new UUID and path')
                    if previous.get('type') == 'obligation':
                        for field in ('payments', 'payment_corrections', 'obligation_corrections', 'lifecycle_events'):
                            history = previous.get(field, [])
                            if data.get(field, [])[:len(history)] != history:
                                raise ValueError('Existing payment history is append-only; correct it with a reason')
                if data.get('type') == 'obligation':
                    from obligations import duplicate_candidates
                    identity = ('category', 'issuer', 'fiscal_year', 'target_period', 'installment', 'lifecycle')
                    changed_identity = old is None or any(self.parse(old).get(k) != data.get(k) for k in identity)
                    if changed_identity and duplicate_candidates(data, [self.parse(t) for n, t in texts.items() if n != change['path']]) and not data.get('duplicate_review_reason'):
                        raise ValueError('Similar obligation exists; review it and record a reason before adding a distinct bill')
                texts[change['path']] = change['content']
            with tempfile.TemporaryDirectory() as directory:
                draft = Path(directory)
                for name in self.git('ls-files', '-z').split('\0'):
                    if not name:
                        continue
                    src = self.root / name
                    if src.is_symlink():
                        raise ValueError('Tracked symlinks require local review')
                    if src.is_file():
                        dst = draft / name
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dst)
                for name, content in texts.items():
                    dst = draft / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_text(content)
                try:
                    _, errors = validate(draft)
                except Exception:
                    raise ValueError('Record validation failed; check metadata types and YAML') from None
                if errors:
                    raise ValueError('Validation failed: ' + '; '.join(errors[:10]))
            changed = False
            with self.database() as db:
                for change, _, data in prepared:
                    # Preserve the original Git base across multiple queued local edits.
                    old = before_texts.get(change['path'])
                    if old == change['content']:
                        continue
                    changed = True
                    db.execute('INSERT INTO pending_records(path,content,base_content) VALUES (?,?,?) ON CONFLICT(path) DO UPDATE SET content=excluded.content',
                               (change['path'], change['content'], self.committed(change['path'])))
                    if data.get('type') == 'obligation':
                        from task_sync import queue_obligation, configured_collection
                        queue_obligation(db, data, self.url(change['path']), configured_collection(self.state, strict=False))
        result = {'id': prepared[0][2]['id'], 'path': changes[0]['path'], 'paths': [c['path'] for c in changes],
                  'changed': changed, 'saved_locally': True, 'synced': False, 'commit': None}
        if self.auto_sync and not nested:
            result.update(self.sync())
        return result

    def sync(self):
        """Best-effort export of local transactions; network I/O never holds the record lock."""
        with (self.state / 'sync.lock').open('a') as mutex:
            try:
                fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'synced': False}
            try:
                self.git('fetch', '--quiet', 'origin', 'main')
                with self.locked(), self.database() as db:
                    if self.git('branch', '--show-current') != 'main':
                        raise ValueError('Sync requires the main branch; local records are safe')
                    if self.git('rev-list', '--count', 'HEAD..origin/main') != '0':
                        raise ValueError('Remote has new commits; fast-forward locally before exporting queued records')
                    for revision in self.git('rev-list', 'origin/main..HEAD').splitlines():
                        subject = self.git('show', '-s', '--format=%s', revision)
                        exported = self.git('diff-tree', '--no-commit-id', '--name-only', '-r', revision).splitlines()
                        if (subject != 'Save locally confirmed knowledge records' or not exported
                                or any(PurePosixPath(name).parts[0] not in DOMAINS or not name.endswith('.md') for name in exported)):
                            raise ValueError('Unpushed commits require local review; application records remain saved')
                    pending = list(db.execute('SELECT * FROM pending_records ORDER BY path'))
                    paths = [r['path'] for r in pending]
                    before = {}
                    for row in pending:
                        target = self.path(row['path'])
                        head = self.committed(row['path'])
                        actual = target.read_text() if target.exists() else None
                        if head not in (row['base_content'], row['content']) or actual not in (head, row['content']):
                            raise ValueError('Queued record conflicts with local or committed edits; reconcile on the Mac')
                        before[row['path']] = actual
                    try:
                        for row in pending:
                            target = self.path(row['path'])
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, delete=False) as out:
                                out.write(row['content'])
                                temporary = Path(out.name)
                            os.replace(temporary, target)
                        if paths:
                            self.git('diff', '--check', '--', *paths)
                            self.git('add', '--', *paths)
                            if self.git('diff', '--cached', '--name-only', '--', *paths):
                                self.git('commit', '--only', '-m', 'Save locally confirmed knowledge records', '--', *paths)
                    except Exception:
                        if paths:
                            self.git('reset', '--quiet', 'HEAD', '--', *paths)
                        for row in pending:
                            target = self.path(row['path'])
                            if target.exists() and target.read_text() == row['content']:
                                if before[row['path']] is None:
                                    target.unlink()
                                else:
                                    target.write_text(before[row['path']])
                        raise
                    db.execute('DELETE FROM pending_records')
                    exported_commit = self.git('rev-parse', 'HEAD')
                self.git('push', 'origin', exported_commit + ':main')
                with self.database() as db:
                    db.execute("DELETE FROM state WHERE key='sync_error'")
                status = self.sync_status()
                return dict(status, commit=self.git('rev-parse', 'HEAD'))
            except (ValueError, OSError, subprocess.SubprocessError):
                with self.database() as db:
                    db.execute("INSERT OR REPLACE INTO state(key,value) VALUES ('sync_error',?)", ('GitHubへの同期は未完了です。MacのGit状態・接続を確認してください。',))
                return {'synced': False, 'saved_locally': True}

    def list_obligations(self, status=None, due_before=None):
        from datetime import date, timedelta
        if status not in (None, 'open', 'unknown', 'unpaid', 'partial', 'paid', 'cancelled'):
            raise ValueError('Invalid obligation status')
        if due_before is not None:
            if date.fromisoformat(due_before).isoformat() != due_before:
                raise ValueError('due_before requires YYYY-MM-DD')
        today = datetime.now(ZoneInfo('Asia/Tokyo')).date()
        rows = []
        with self.locked():
            for path, content, data in self.records():
                if data.get('type') != 'obligation':
                    continue
                cancelled = data.get('lifecycle') == 'cancelled'
                if status == 'cancelled' and not cancelled:
                    continue
                if status not in (None, 'cancelled') and cancelled:
                    continue
                if status == 'open' and data.get('status') not in ('unpaid', 'partial', 'unknown'):
                    continue
                if status not in (None, 'open', 'cancelled') and data.get('status') != status:
                    continue
                due = data.get('due_date')
                if due_before is not None and (not due or due > due_before):
                    continue
                bucket = ('cancelled' if cancelled else 'paid' if data.get('status') == 'paid' else 'unknown' if data.get('status') == 'unknown'
                          else 'undated' if not due else 'overdue' if due < today.isoformat()
                          else 'soon' if due <= (today + timedelta(days=30)).isoformat() else 'later')
                rows.append(dict(data, path=path, sha256=hashlib.sha256(content.encode()).hexdigest(),
                                 url=self.url(path), bucket=bucket))
        return {'as_of': today.isoformat(), 'timezone': 'Asia/Tokyo',
                'obligations': sorted(rows, key=lambda r: (r.get('due_date') or '9999', r['id']))}
