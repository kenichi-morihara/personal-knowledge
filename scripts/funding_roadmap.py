"""Local funding-plan editor. Reads v2 without mutation; explicit saves write validated v3 JSON."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DB = Path('designs/funding-roadmap/db.json')
ASSETS = ROOT / 'designs/funding-roadmap/editor'
NS = UUID('b9a8c768-7ff9-49ec-918e-9d33dd0ba23e')
MAX_BYTES = 1024 * 1024


def stable(name):
    return str(uuid5(NS, name))


def yen(value):
    if value is None:
        return None
    result = Decimal(str(value)) * 10000
    if not result.is_finite() or result != result.to_integral_value() or result < 0:
        raise ValueError('旧金額を整数の円に変換できません')
    return int(result)


def migrate(raw):
    if raw.get('version') == 3:
        return deepcopy(raw)
    if raw.get('version') != 2:
        raise ValueError('対応していないDBバージョンです')
    result = deepcopy(raw)
    result.update(version=3, revision=0, currency='JPY', start_date='2026-10-25',
                  opening_balance=300000, monthly_rules=[], overrides=[])
    # The old opening balance lives only in the demo, not the file; retain its explicit assumption.
    result['opening_balance_source'] = '旧画面の試算用仮残高30万円。銀行残高ではない。'
    monthly = result.pop('monthly', {})
    rules = [('income','給与（本人のみ）','income','income_start','income_end'),
             ('living','生活費（食費＋雑費・内訳未確認）','expense','income_start','income_end'),
             ('rent','家賃（光熱込み）','expense','income_start','rent_until'),
             ('canada','カナダ生活費（内訳未確認）','expense','canada_start','canada_end')]
    defaults = {'income_start':'2026-11','income_end':'2027-08','rent_until':'2027-04',
                'canada_start':'2027-09','canada_end':'2027-11'}
    for key, label, flow, start, end in rules:
        result['monthly_rules'].append(dict(id=stable('monthly:'+key), label=label, direction=flow,
            amount=yen(monthly.get(key)), start_month=monthly.get(start, defaults[start]),
            end_month=monthly.get(end, defaults[end]), status='estimate', note='',
            source=raw.get('source','旧DB'), updated_at=raw.get('updated_at')))
    for event in result['events']:
        amount = yen(event.pop('amount_wan', None))
        event['legacy_total'] = amount
        event['zero_cost'] = amount == 0 and event.get('key') == 'canada-living'
        # A zero-cost checkpoint is not a second copy of the recurring Canadian living expenses.
        if amount == 0 and event.get('key') == 'canada-living':
            event['items'] = []
        else:
            event['items'] = [dict(id=stable('item:'+event['id']), label='内訳未確認', amount=amount,
                status='estimate' if amount is not None else 'unknown', note=event.get('note',''),
                source=event.get('source','旧DB'), updated_at=event.get('updated_at'), unallocated=True)]
    return result


def validate(data):
    if not isinstance(data, dict) or data.get('version') != 3 or data.get('currency') != 'JPY':
        raise ValueError('version=3、通貨JPYのDBが必要です')
    seen = set()

    def identity(row):
        if not isinstance(row, dict):
            raise ValueError('項目の形式が不正です')
        ident = row.get('id')
        try:
            UUID(ident)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('各項目には固定UUIDが必要です') from None
        if ident in seen:
            raise ValueError('IDが重複しています')
        seen.add(ident)

    def amount(value):
        if value is not None and (type(value) is not int or not 0 <= value <= 10**12):
            raise ValueError('金額は0以上の整数の円、未確認はnullにしてください')

    def text(value, required=False):
        if not isinstance(value, str) or len(value) > 3000 or (required and not value.strip()):
            raise ValueError('項目名・メモの形式を確認してください')

    def month(value):
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', value):
            raise ValueError('月はYYYY-MM形式で指定してください')
        if not '2000-01' <= value <= '2100-12':
            raise ValueError('対象月は2000〜2100年にしてください')

    def day(value, nullable=True):
        if value is None and nullable:
            return
        try:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError('日付はYYYY-MM-DD、未定はnullにしてください') from None

    def line(row):
        identity(row)
        text(row.get('label'), True)
        amount(row.get('amount'))
        if row.get('status') not in ('unknown','estimate','confirmed'):
            raise ValueError('確認状態が不正です')
        if row.get('amount') is None and row.get('status') == 'confirmed':
            raise ValueError('金額未確認の項目を確認済みにできません')
        for key in ('note','source'):
            text(row.get(key,''))
        day(row.get('updated_at'), False)

    day(data.get('start_date'), False)
    day(data.get('updated_at'), False)
    amount(data.get('opening_balance'))
    if type(data.get('revision')) is not int or data['revision'] < 0:
        raise ValueError('更新番号が不正です')
    for key in ('events','monthly_rules','overrides'):
        if not isinstance(data.get(key), list) or len(data[key]) > 500:
            raise ValueError('項目数は各500件以内にしてください')
    for event in data['events']:
        identity(event)
        text(event.get('title'), True)
        day(event.get('date'))
        if event.get('date') is not None and event['date'] < data['start_date']:
            raise ValueError('イベント予定日はスタート日以降にしてください')
        if type(event.get('zero_cost', False)) is not bool:
            raise ValueError('費用なしの指定が不正です')
        if not isinstance(event.get('items'), list) or len(event['items']) > 200:
            raise ValueError('イベント内訳は200件以内にしてください')
        for item in event['items']:
            line(item)
    rule_ids = set()
    for rule in data['monthly_rules']:
        line(rule)
        rule_ids.add(rule['id'])
        if rule.get('direction') not in ('income','expense'):
            raise ValueError('収入／支出を指定してください')
        month(rule.get('start_month'))
        month(rule.get('end_month'))
        if rule['start_month'] > rule['end_month']:
            raise ValueError('終了月は開始月以降にしてください')
    targets = set()
    for override in data['overrides']:
        identity(override)
        if override.get('rule_id') not in rule_ids:
            raise ValueError('月別変更の元項目がありません')
        month(override.get('month'))
        key = (override['rule_id'],override['month'])
        if key in targets:
            raise ValueError('同じ項目・月の変更が重複しています')
        targets.add(key)
        amount(override.get('amount'))
        text(override.get('note',''))
        text(override.get('source',''))
        if 'updated_at' in override:
            day(override['updated_at'], False)


class Conflict(ValueError):
    pass


class PlanStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / DB

    def git(self, *args):
        result = subprocess.run(['git','-C',str(self.root),*args],capture_output=True,text=True,timeout=30)
        if result.returncode:
            raise ValueError('Gitの状態を確認してください。保存済みのデータは上書きしません')
        return result.stdout.strip()

    @contextmanager
    def locked(self):
        gitdir = Path(self.git('rev-parse','--absolute-git-dir'))
        with (gitdir/'funding-editor.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            yield

    def read(self):
        raw = self.path.read_bytes()
        data = migrate(json.loads(raw))
        validate(data)
        return data, hashlib.sha256(raw).hexdigest()

    def save(self, data, expected):
        validate(data)
        with self.locked():
            previous, actual = self.read()
            if not secrets.compare_digest(expected, actual):
                raise Conflict('ほかの画面かAgentが更新しました。再読み込みしてください。入力中の内容は画面に残っています')
            if data['revision'] != previous['revision']:
                raise Conflict('更新番号が変わっています。再読み込みしてください')
            if self.git('status','--porcelain','--',str(DB)):
                raise Conflict('db.jsonに未コミットの変更があります。Macで保存してから再試行してください')
            # Reject editing somebody else's branch. Do not fetch, merge, or push in this local editor.
            if self.git('branch','--show-current') != 'main':
                raise Conflict('保存はmainブランチで行ってください')
            updated = deepcopy(data)
            updated['revision'] = previous['revision'] + 1
            updated['updated_at'] = datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()
            body = (json.dumps(updated,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
            original = self.path.read_bytes()
            # Check the exact file immediately before replacement, including direct Agent edits.
            if hashlib.sha256(original).hexdigest() != expected:
                raise Conflict('保存直前にDBが更新されました。再読み込みしてください')
            with tempfile.NamedTemporaryFile(dir=self.path.parent,delete=False) as file:
                temp = Path(file.name)
                file.write(body)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp,self.path)
            try:
                # --only commits this DB without staging somebody else's edits.
                self.git('diff','--check','--',str(DB))
                self.git('commit','--only','-m','Update funding plan item breakdown','--',str(DB))
            except ValueError:
                self.path.write_bytes(original)
                raise
            return updated, hashlib.sha256(body).hexdigest(), self.git('rev-parse','--short','HEAD')


def serve(root, port):
    store = PlanStore(root)
    origin = f'http://127.0.0.1:{port}'
    csrf = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def send(self,status,body,content_type='application/json; charset=utf-8',**headers):
            if isinstance(body,dict):
                body=json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for key,value in headers.items():self.send_header(key,value)
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            if self.headers.get('Host') != f'127.0.0.1:{port}':
                self.send(403,{'error':'接続先ホストを確認してください'})
                return False
            return True

        def do_GET(self):
            if not self.allowed():return
            if self.path == '/api/plan':
                try:
                    data,etag=store.read()
                    self.send(200,dict(plan=data,etag=etag,csrf=csrf))
                except (ValueError,OSError):self.send(500,{'error':'DBを読み込めません。Macで形式を確認してください'})
                return
            files={'/':('index.html','text/html; charset=utf-8'),'/editor.js':('editor.js','text/javascript; charset=utf-8'),'/editor.css':('editor.css','text/css; charset=utf-8'),'/model.mjs':('model.mjs','text/javascript; charset=utf-8')}
            if self.path not in files:
                self.send(404,{'error':'見つかりません'})
                return
            name,mime=files[self.path]
            self.send(200,(ASSETS/name).read_bytes(),mime)

        def do_PUT(self):
            if not self.allowed():return
            if self.path != '/api/plan':return self.send(404,{'error':'見つかりません'})
            if self.headers.get('Origin') != origin or not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):
                return self.send(403,{'error':'この画面を開き直してください'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0 < size <= MAX_BYTES:return self.send(413,{'error':'保存サイズの上限は1MBです'})
                raw=self.rfile.read(size)
                data=json.loads(raw)
                updated,etag,commit=store.save(data,self.headers.get('If-Match',''))
                self.send(200,dict(plan=updated,etag=etag,commit=commit,remote_synced=False))
            except Conflict as exc:self.send(409,{'error':str(exc)})
            except (ValueError,TypeError,KeyError):self.send(400,{'error':'保存できません。入力値・DB・Gitの状態を確認してください'})
            except OSError:self.send(500,{'error':'ファイルに保存できませんでした'})
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'Funding breakdown editor: {origin} (local only; Ctrl+C to stop)',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8863)
    args=parser.parse_args()
    serve(ROOT,args.port)
