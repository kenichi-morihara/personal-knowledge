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

    def put(self, path, content, expected_sha256):
        target = self.path(path)
        data = self.parse(content)
        if SECRET.search(content):
            raise ValueError('Possible credential detected; do not store secrets in records')
        today = datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()
        if data.get('updated_at') != today:
            raise ValueError(f'updated_at must be the current Asia/Tokyo date: {today}')
        with self.locked():
            if self.git('status', '--porcelain'):
                raise ValueError('Working tree has local changes; finish or preserve them before MCP writes')
            if self.git('branch', '--show-current') != 'main':
                raise ValueError('MCP writes require the main branch')
            # Never merge or overwrite local edits. An upstream update requires a local fast-forward.
            self.git('fetch', '--quiet', 'origin', 'main')
            if self.git('rev-list', '--count', 'HEAD..origin/main') != '0':
                raise ValueError('Remote has new commits; fast-forward the checkout locally, then fetch the record again')
            before = target.read_text() if target.exists() else None
            actual = hashlib.sha256(before.encode()).hexdigest() if before is not None else ''
            if expected_sha256 != actual:
                raise ValueError('Record changed or already exists; fetch again before updating')
            if before is not None:
                previous = self.parse(before)
                if data['id'] != previous['id'] or data.get('type') != previous.get('type'):
                    raise ValueError('Record UUID and type cannot change')
                if previous.get('type') == 'balance':
                    raise ValueError('Balances are append-only snapshots; create a new UUID and path')
                if previous.get('type') == 'obligation':
                    old_payments = previous.get('payments', [])
                    if data.get('payments', [])[:len(old_payments)] != old_payments:
                        raise ValueError('Existing payment history is append-only; correct it locally with a reason')
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
                candidate = draft / path
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text(content)
                try:
                    _, errors = validate(draft)
                except Exception:
                    raise ValueError('Record validation failed; check metadata types and YAML') from None
                if errors:
                    raise ValueError('Validation failed: ' + '; '.join(errors[:10]))
            if before == content:
                return {'path': path, 'changed': False, 'commit': self.git('rev-parse', 'HEAD')}
            if self.git('status', '--porcelain'):
                raise ValueError('Working tree changed during validation; retry after local review')
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, delete=False) as staged:
                staged.write(content)
                temporary = Path(staged.name)
            os.replace(temporary, target)
            try:
                self.git('diff', '--check', '--', path)
                self.git('add', '--', path)
                self.git('commit', '-m', f'Update knowledge record {data["id"]}', '--', path)
            except Exception:
                self.git('reset', '--quiet', 'HEAD', '--', path)
                if before is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_text(before)
                raise
            commit = self.git('rev-parse', 'HEAD')
            try:
                self.git('push', 'origin', 'HEAD:main')
                synced = True
            except ValueError:
                synced = False
            return {'id': data['id'], 'path': path, 'changed': True, 'commit': commit, 'synced': synced,
                    'message': 'Saved and pushed' if synced else 'Saved locally; push failed. Sync locally before relying on GitHub.'}
