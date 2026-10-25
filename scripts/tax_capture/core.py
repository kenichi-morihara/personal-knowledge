"""Conservative OCR extraction and durable tax-notice capture. Never records a payment."""
from datetime import date, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import unicodedata
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

import yaml

NAMESPACE = UUID('063690c7-1e98-4ee1-988f-b5e532c96044')
MAX_IMAGE = 15 * 1024 * 1024


def private_write(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw)
    os.replace(temporary, path)
    path.chmod(0o600)


def parse_date(text):
    era = re.search(r'(令和|平成)\s*(元|\d{1,2})年\s*(\d{1,2})月\s*(\d{1,2})日', text)
    normal = re.search(r'\b(20\d{2})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?', text)
    if era:
        year = (2018 if era[1] == '令和' else 1988) + (1 if era[2] == '元' else int(era[2]))
        month, day = int(era[3]), int(era[4])
    elif normal:
        year, month, day = map(int, normal.groups())
    else:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def extract(lines):
    # No document text is interpreted as instructions; only allowlisted fields leave local storage.
    texts = [unicodedata.normalize('NFKC', line['text']).strip() for line in lines
             if isinstance(line, dict) and isinstance(line.get('text'), str) and line.get('confidence', 0) >= .50]
    combined = '\n'.join(texts)
    categories = []
    for name, pattern in (
        ('住民税', r'住民税|市民税|県民税|都民税|特別区民税|道民税|府民税'),
        ('所得税', r'所得税'), ('消費税', r'消費税'), ('個人事業税', r'個人事業税'),
        ('国民健康保険', r'国民健康保険|国保'), ('国民年金', r'国民年金')):
        if re.search(pattern, combined):
            categories.append(name)
    category = categories[0] if len(categories) == 1 else None
    amounts, deadlines, years = set(), set(), set()
    for index, text in enumerate(texts):
        # Look only at explicitly labelled amounts, never OCR-only bare account/reference numbers.
        if re.search(r'本期.*(?:納付|納税)|納付(?:すべき)?(?:金額|額)|納税額|合計(?:金額)?', text):
            nearby = text + (' ' + texts[index + 1] if index + 1 < len(texts) else '')
            for found in re.finditer(r'(?:[¥￥]\s*([\d,]+)|([\d,]+)\s*円)', nearby):
                number = (found[1] or found[2]).replace(',', '')
                if number.isdigit() and 0 < int(number) < 10**10:
                    amounts.add(str(int(number)))
        if re.search(r'納付期限|納期限', text):
            for candidate in texts[index:index + 2]:
                parsed = parse_date(candidate)
                if parsed:
                    deadlines.add(parsed)
        for match in re.finditer(r'(令和|平成)\s*(元|\d{1,2})年度', text):
            years.add((2018 if match[1] == '令和' else 1988) + (1 if match[2] == '元' else int(match[2])))
        years.update(int(m[1]) for m in re.finditer(r'\b(20\d{2})年度', text))
    result = dict(category=category, amount_due=next(iter(amounts)) if len(amounts) == 1 else None,
                  currency='JPY' if len(amounts) == 1 else None,
                  due_date=next(iter(deadlines)) if len(deadlines) == 1 else None,
                  fiscal_year=next(iter(years)) if len(years) == 1 else None)
    result['needs_review'] = True
    result['ambiguities'] = [name for name, values in [('category', categories), ('amount_due', amounts), ('due_date', deadlines), ('fiscal_year', years)] if len(values) != 1]
    return result


class Captures:
    def __init__(self, state, store, ocr):
        self.state, self.store, self.ocr = Path(state), store, ocr
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)

    def ingest(self, raw):
        if not raw or len(raw) > MAX_IMAGE:
            raise ValueError('画像は15MB以下のJPEGまたはPNGにしてください。')
        extension = '.jpg' if raw.startswith(b'\xff\xd8\xff') else '.png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else None
        if not extension:
            raise ValueError('JPEGまたはPNGの画像を選んでください。')
        digest = hashlib.sha256(raw).hexdigest()
        # A durable lock serializes captures; the shared Store has its own record lock.
        with (self.state / 'capture.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            job_path = self.state / 'jobs' / (digest + '.json')
            job = json.loads(job_path.read_text()) if job_path.exists() else {}
            if job.get('status') == 'recorded':
                return dict(job['result'], duplicate=True)
            image = self.state / 'images' / (digest + extension)
            private_write(image, raw)
            job.update(image_sha256=digest, status='processing')
            private_write(job_path, json.dumps(job).encode())
            try:
                # OCR output stays on this Mac. Never put the full text or photograph in Git.
                lines = self.ocr(image)
                if not isinstance(lines, list) or len(lines) > 5000:
                    raise ValueError('画像の文字認識結果を確認できませんでした。')
                private_write(self.state / 'ocr' / (digest + '.json'), json.dumps(lines, ensure_ascii=False).encode())
                fields = extract(lines)
                normalized = ''.join(unicodedata.normalize('NFKC', line.get('text', '')) for line in lines)
                fingerprint = hashlib.sha256(re.sub(r'\s+', '', normalized).encode()).hexdigest() if normalized.strip() else None
                similar = None
                signature = (fields['category'], fields['amount_due'], fields['due_date'], fields['fiscal_year'])
                for previous in sorted((self.state / 'jobs').glob('*.json')):
                    if previous == job_path:
                        continue
                    other = json.loads(previous.read_text())
                    if other.get('status') != 'recorded':
                        continue
                    other_fields = other['result']['fields']
                    other_signature = tuple(other_fields.get(k) for k in ('category', 'amount_due', 'due_date', 'fiscal_year'))
                    if (fingerprint and other.get('fingerprint') == fingerprint) or (all(x is not None for x in signature) and signature == other_signature):
                        similar = other['result']['path']
                        break
                # Flag matching confirmed obligations too; never create a second debt here.
                if similar is None and all(value is not None for value in signature):
                    with self.store.locked():
                        for name, _, record in self.store.records():
                            if record.get('type') != 'obligation':
                                continue
                            existing_signature = (record.get('category'), record.get('amount_due'), record.get('due_date'), record.get('fiscal_year'))
                            if signature == existing_signature:
                                similar = name
                                break
                now = datetime.now(ZoneInfo('Asia/Tokyo'))
                record_id = str(uuid5(NAMESPACE, digest))
                path = f'finance/taxes/captures/{digest}.md'
                meta = dict(id=record_id, type='note', updated_at=now.date().isoformat(),
                    sources=[dict(reference=f'Androidから本人が送信した納付書画像。SHA-256: {digest}。原画像・OCR全文はMacの撮影用ローカル保存領域。',captured_at=now.date().isoformat())],
                    capture_status='needs_review', image_sha256=digest, captured_at=now.isoformat(),
                    tax_category=fields['category'], fiscal_year=fields['fiscal_year'],
                    amount_due=fields['amount_due'], currency=fields['currency'], due_date=fields['due_date'],
                    amount_paid=None, amount_remaining=None, possible_duplicate_of=similar)
                body = '# 納付書の撮影記録（要確認）\n\n'
                for label, key in [('税目候補', 'category'), ('請求額候補（円）', 'amount_due'), ('課税年度候補', 'fiscal_year'), ('納期限候補', 'due_date')]:
                    value = fields[key]
                    body += f'- {label}：{value if value is not None else "未確認"}\n'
                body += '\nOCRの候補であり、まだ確定債務ではない。支払い済みとは扱わない。原画像と照合し、年額・期別・既納額・納期限を確認してから請求原本へ反映する。\n'
                if similar:
                    # Data-only path is generated by this service, never OCR text.
                    body += f'\n重複の可能性：{similar}。同じ請求を二重計上しない。\n'
                body += '\n確認後の請求原本：未登録。\n'
                content = '---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n' + body
                # Deterministic UUID/path permits retry after an interrupted response or Git push failure.
                target = self.store.root / path
                if target.exists():
                    existing = yaml.safe_load(target.read_text().split('---\n', 2)[1])
                    if existing.get('id') != record_id or existing.get('image_sha256') != digest:
                        raise ValueError('既存記録との競合があります。Macで確認してください。')
                    # Recover only a committed record; never acknowledge an unrelated local edit.
                    committed = self.store.git('show', f'HEAD:{path}')
                    if committed != target.read_text().strip():
                        raise ValueError('記録に未保存の変更があります。Macで確認してください。')
                    commit = self.store.git('log', '-1', '--format=%H', '--', path)
                    try:
                        self.store.git('fetch', '--quiet', 'origin', 'main')
                        synced = self.store.git('rev-list', '--count', f'origin/main..{commit}') == '0'
                    except ValueError:
                        synced = False
                    result = dict(id=record_id, path=path, commit=commit, synced=synced, recovered=True)
                else:
                    result = self.store.put(path, content, '')
                result.update(fields=fields, possible_duplicate_of=similar, duplicate=False)
                job.update(status='recorded', fingerprint=fingerprint, result=result)
                private_write(job_path, json.dumps(job, ensure_ascii=False).encode())
                return result
            except Exception:
                job['status'] = 'retry_needed'
                private_write(job_path, json.dumps(job).encode())
                raise


def native_ocr(binary, image):
    result = subprocess.run([str(binary), str(image)], capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError('画像を読み取れませんでした。明るい場所で、用紙全体を撮り直してください。')
    if len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError('読み取り結果が大きすぎます。納付書1枚ずつ撮影してください。')
    return json.loads(result.stdout)
