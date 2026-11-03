"""Restricted record access. No arbitrary file, shell or credential tool is exposed."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
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
    def __init__(self, root, state):
        self.root = Path(root).resolve()
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('Git operation failed; inspect local Git state before retrying')
        return result.stdout.strip()

    @contextmanager
    def locked(self):
        with (self.state / 'records.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
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

    def records(self):
        paths = self.git('ls-files', '-z', '--', *DOMAINS).split('\0')
        for name in paths:
            if not name or not name.endswith('.md') or PurePosixPath(name).name == 'README.md':
                continue
            path = self.path(name)
            if path.stat().st_size > MAX_BYTES:
                raise ValueError('Record exceeds size limit')
            content = path.read_text()
            data = self.parse(content)
            yield name, content, data

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
                            'path': name, 'sha256': hashlib.sha256(content.encode()).hexdigest()}
        raise ValueError('Record not found; search for its UUID first')

    def sync_status(self):
        # Compare cached remote refs only; the UI labels this as the last known remote state.
        with self.locked():
            try:
                dirty = bool(self.git('status', '--porcelain'))
                ahead = int(self.git('rev-list', '--count', 'origin/main..HEAD'))
                behind = int(self.git('rev-list', '--count', 'HEAD..origin/main'))
                return {'synced': not dirty and ahead == 0 and behind == 0,
                        'local_changes': dirty, 'ahead': ahead, 'behind': behind,
                        'remote_checked': False}
            except ValueError:
                return {'synced': False, 'remote_checked': False}

    def put(self, path, content, expected_sha256):
        return self.put_many([dict(path=path, content=content, expected_sha256=expected_sha256)])

    def put_many(self, changes):
        """Validate and commit a related group atomically; all writers use the same lock."""
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
        with self.locked():
            if self.git('status', '--porcelain'):
                raise ValueError('Working tree has local changes; finish or preserve them before MCP writes')
            if self.git('branch', '--show-current') != 'main':
                raise ValueError('MCP writes require the main branch')
            self.git('fetch', '--quiet', 'origin', 'main')
            if self.git('rev-list', '--count', 'HEAD..origin/main') != '0':
                raise ValueError('Remote has new commits; fast-forward the checkout locally, then fetch the record again')
            before = {}
            for change, target, data in prepared:
                old = target.read_text() if target.exists() else None
                before[change['path']] = old
                actual = hashlib.sha256(old.encode()).hexdigest() if old is not None else ''
                if change['expected_sha256'] != actual:
                    raise ValueError('Record changed or already exists; fetch again before updating')
                if old is not None:
                    previous = self.parse(old)
                    if data['id'] != previous['id'] or data.get('type') != previous.get('type'):
                        raise ValueError('Record UUID and type cannot change')
                    if previous.get('type') == 'balance':
                        raise ValueError('Balances are append-only snapshots; create a new UUID and path')
                    if previous.get('type') == 'obligation':
                        for field in ('payments', 'payment_corrections', 'obligation_corrections'):
                            history = previous.get(field, [])
                            if data.get(field, [])[:len(history)] != history:
                                raise ValueError('Existing payment history is append-only; correct it with a reason')
            with tempfile.TemporaryDirectory() as directory:
                draft = Path(directory)
                for name in self.git('ls-files', '-z').split('\0'):
                    if not name:
                        continue
                    src = self.root / name
                    if src.is_symlink():
                        raise ValueError('Tracked symlinks require local review')
                    dst = draft / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                for change, _, _ in prepared:
                    candidate = draft / change['path']
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    candidate.write_text(change['content'])
                try:
                    _, errors = validate(draft)
                except Exception:
                    raise ValueError('Record validation failed; check metadata types and YAML') from None
                if errors:
                    raise ValueError('Validation failed: ' + '; '.join(errors[:10]))
            if all(before[c['path']] == c['content'] for c, _, _ in prepared):
                return {'path': changes[0]['path'], 'changed': False, 'commit': self.git('rev-parse', 'HEAD')}
            if self.git('status', '--porcelain'):
                raise ValueError('Working tree changed during validation; retry after local review')
            paths = [c['path'] for c, _, _ in prepared]
            try:
                for change, target, _ in prepared:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, delete=False) as staged:
                        staged.write(change['content'])
                        temporary = Path(staged.name)
                    os.replace(temporary, target)
                self.git('diff', '--check', '--', *paths)
                self.git('add', '--', *paths)
                self.git('commit', '-m', f'Update knowledge record {prepared[0][2]["id"]}', '--', *paths)
            except Exception:
                self.git('reset', '--quiet', 'HEAD', '--', *paths)
                for change, target, _ in prepared:
                    old = before[change['path']]
                    if old is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.write_text(old)
                raise
            commit = self.git('rev-parse', 'HEAD')
            try:
                self.git('push', 'origin', 'HEAD:main')
                synced = True
            except ValueError:
                synced = False
            return {'id': prepared[0][2]['id'], 'path': paths[0], 'paths': paths,
                    'changed': True, 'commit': commit, 'synced': synced,
                    'message': 'Saved and pushed' if synced else 'Saved locally; push failed. Sync locally before relying on GitHub.'}

    def list_obligations(self, status=None, due_before=None):
        from datetime import date, timedelta
        if status not in (None, 'open', 'unknown', 'unpaid', 'partial', 'paid'):
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
                if status == 'open' and data.get('status') not in ('unpaid', 'partial', 'unknown'):
                    continue
                if status not in (None, 'open') and data.get('status') != status:
                    continue
                due = data.get('due_date')
                if due_before is not None and (not due or due > due_before):
                    continue
                bucket = ('paid' if data.get('status') == 'paid' else 'unknown' if data.get('status') == 'unknown'
                          else 'undated' if not due else 'overdue' if due < today.isoformat()
                          else 'soon' if due <= (today + timedelta(days=30)).isoformat() else 'later')
                rows.append(dict(data, path=path, sha256=hashlib.sha256(content.encode()).hexdigest(),
                                 url=self.url(path), bucket=bucket))
        return {'as_of': today.isoformat(), 'timezone': 'Asia/Tokyo',
                'obligations': sorted(rows, key=lambda r: (r.get('due_date') or '9999', r['id']))}
