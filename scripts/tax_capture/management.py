"""Owner-confirmed payment management over the shared Git record store."""
from datetime import date, datetime
from decimal import Decimal
import hashlib
from functools import wraps
import re
from uuid import uuid5
from zoneinfo import ZoneInfo

import yaml

from core import NAMESPACE
from obligations import effective_payments, duplicate_candidates
from task_sync import TaskSync

CATEGORIES = ('住民税', '所得税', '消費税', '個人事業税', '国民健康保険', '国民年金')


def today():
    return datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()


def document(data, body):
    return '---\n' + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + '---\n' + body


def short(value, name, required=False):
    if value is None or value == '':
        if required:
            raise ValueError(f'{name}を入力してください。')
        return None
    if not isinstance(value, str) or len(value) > 200 or '\n' in value or '\r' in value or any(ord(c) < 32 for c in value):
        raise ValueError(f'{name}は200文字以内の1行で入力してください。')
    return value.strip() or (short(None, name, required))


def money(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}', value) or int(value) <= 0:
        raise ValueError('金額は1円以上の整数で入力してください。')
    return str(int(value))


def calendar(value, name, optional=False):
    if not value and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{name}を入力してください。')
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError(f'{name}はYYYY-MM-DDで入力してください。') from None
    return value


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.store.locked():
            result = method(self, *args, **kwargs)
        if self.store.auto_sync and result.get('changed'):
            result.update(self.store.sync())
        return result
    return call


class Management:
    def __init__(self, captures, tasks=None):
        self.captures = captures
        self.store = captures.store
        self.tasks = TaskSync(self.store, tasks)

    def get(self, record_id):
        record = self.store.fetch(record_id)
        record['data'] = self.store.parse(record['text'])
        return record

    def overview(self):
        result = self.store.list_obligations()
        with self.store.locked():
            result['pending'] = [dict(data, path=path, sha256=hashlib.sha256(content.encode()).hexdigest())
                                 for path, content, data in self.store.records()
                                 if data.get('capture_status') == 'needs_review']
        result['sync'] = self.store.sync_status()
        result['tasks'] = self.tasks.status()
        return result

    def image(self, digest):
        if not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('画像がありません。')
        # Only images linked from tracked records may be served, never arbitrary local files.
        with self.store.locked():
            linked = any(data.get('image_sha256') == digest or digest in data.get('image_refs', [])
                         or any(p.get('image_sha256') == digest for p in data.get('payments', []))
                         for _, _, data in self.store.records())
        if linked:
            for suffix in ('.jpg', '.png'):
                image = self.captures.state / 'images' / (digest + suffix)
                if image.is_file() and not image.is_symlink():
                    return image
        raise ValueError('画像がありません。')

    def change(self, record, data, body=None):
        data['updated_at'] = today()
        if body is None and data.get('capture_status') in ('reviewed', 'dismissed'):
            body = '# 撮影記録の確認結果\n\n' + ('確認済み。区分: ' + str(data.get('review_kind')) + '。原本UUID: ' + str(data.get('confirmed_record_id')) if data['capture_status'] == 'reviewed' else '管理対象外。理由: ' + str(data.get('review_reason'))) + '\n'
        return dict(path=record['path'], expected_sha256=record['sha256'],
                    content=document(data, body if body is not None else record['text'].split('---\n', 2)[2]))

    def checked(self, record_id, sha, kind):
        record = self.get(record_id)
        if record['data'].get('type') != kind:
            raise ValueError('対象の記録を確認してください。')
        if record['sha256'] != sha:
            raise ValueError('別の画面またはAIで更新されました。画面を開き直してください。')
        return record

    def capture_result(self, raw, kind, target):
        if kind == 'receipt':
            if self.get(target)['data'].get('type') != 'obligation':
                raise ValueError('支払い対象を選んでください。')
        result = self.captures.ingest(raw, kind=kind, obligation_id=target, allow_unreadable=True)
        record = self.get(result['id'])
        return dict(result, record=record, synced=self.store.sync_status()['synced'],
                    association_conflict=(record['data'].get('capture_kind', 'notice') != kind or (kind == 'receipt' and record['data'].get('target_obligation_id') != target)))

    def candidates(self, data):
        return duplicate_candidates(data, self.store.list_obligations()['obligations'])

    @serialized
    def review(self, capture_id, payload):
        original = self.get(capture_id)
        note = original['data']
        if note.get('capture_status') in ('reviewed', 'dismissed'):
            return {'changed': False, 'id': note.get('confirmed_record_id'), 'already_recorded': True,
                    'synced': self.store.sync_status()['synced']}
        record = self.checked(capture_id, payload.get('sha256'), 'note')
        note = record['data']
        if not note.get('image_sha256'):
            raise ValueError('撮影記録を選んでください。')
        action = payload.get('action')
        if action == 'dismiss':
            note.update(capture_status='dismissed', review_reason=short(payload.get('reason'), '理由', True))
            return self.store.put_many([self.change(record, note)])
        if action == 'link':
            target = self.get(payload.get('target_id'))
            if target['data'].get('type') != 'obligation':
                raise ValueError('関連づける請求を選んでください。')
            note.update(capture_status='reviewed', confirmed_record_id=target['id'], review_kind='related_document')
            data = target['data']
            data['image_refs'] = list(dict.fromkeys([*data.get('image_refs', []), note['image_sha256']]))
            result = self.store.put_many([self.change(record, note), self.change(target, data)])
            return dict(result, id=target['id'])
        fields = payload.get('fields', {})
        if not isinstance(fields, dict):
            raise ValueError('入力内容を確認してください。')
        allowed = ('category', 'issuer', 'target_period', 'installment', 'fiscal_year', 'amount_due', 'due_date')
        draft = {k: fields.get(k) for k in allowed}
        if action == 'draft':
            # Draft fields remain a note, never a payable obligation.
            note['review_draft'] = draft
            return self.store.put_many([self.change(record, note)])
        if note.get('capture_kind') == 'receipt':
            raise ValueError('領収書は支払いの確認画面から登録してください。')
        if action != 'create':
            raise ValueError('確認操作を選んでください。')
        if payload.get('confirmed_unpaid') is not True or payload.get('independently_payable') is not True:
            raise ValueError('未払いと、この用紙単独で払えることを確認してください。年額通知は関連資料として保存してください。')
        if draft['category'] not in CATEGORIES:
            raise ValueError('税目を選んでください。')
        amount = money(draft['amount_due'])
        year = draft.get('fiscal_year')
        if year not in (None, ''):
            if isinstance(year, bool) or not re.fullmatch(r'20\d{2}', str(year)):
                raise ValueError('年度は西暦の4桁で入力してください。')
            year = int(year)
        else:
            year = None
        data = dict(id=str(uuid5(NAMESPACE, 'obligation:' + note['image_sha256'])), type='obligation',
                    updated_at=today(), sources=[dict(reference=f'本人が納付書画像と未払いを確認。画像SHA-256: {note["image_sha256"]}', captured_at=today())],
                    category=draft['category'], issuer=short(draft['issuer'], '請求元', True),
                    target_period=short(draft['target_period'], '対象期間'), installment=short(draft['installment'], '期別'),
                    fiscal_year=year, currency='JPY', amount_due=amount, opening_paid='0', amount_paid='0', amount_remaining=amount,
                    due_date=calendar(draft['due_date'], '納期限', True), status='unpaid', payments=[],
                    image_refs=[note['image_sha256']])
        candidates = self.candidates(data)
        if candidates and payload.get('distinct_confirmed') is not True:
            return {'needs_duplicate_review': True, 'candidates': candidates}
        if candidates:
            data['duplicate_review_reason'] = short(payload.get('distinct_reason'), '別請求と確認した理由', True)
        note.update(capture_status='reviewed', confirmed_record_id=data['id'], review_kind='obligation')
        path = f'finance/taxes/obligations/{data["id"]}.md'
        result = self.store.put_many([dict(path=path, content=document(data, '# 納付書の請求\n\n本人が用紙を確認して登録。\n'), expected_sha256=''), self.change(record, note)])
        return dict(result, id=data['id'])

    @staticmethod
    def recalculate(data):
        paid = Decimal(data['opening_paid']) + sum((Decimal(p['amount']) for p in effective_payments(data)), Decimal(0))
        remaining = Decimal(data['amount_due']) - paid
        if remaining < 0:
            raise ValueError('請求額を超える支払いになります。対象・金額・登録済み履歴を確認してください。')
        data.update(amount_paid=str(paid), amount_remaining=str(remaining),
                    status='paid' if remaining == 0 else 'partial' if paid > 0 else 'unpaid')

    @serialized
    def pay(self, obligation_id, payload):
        receipt = None
        if payload.get('capture_id'):
            receipt = self.get(payload['capture_id'])
            note = receipt['data']
            if note.get('capture_status') == 'reviewed':
                if note.get('confirmed_record_id') != obligation_id or note.get('review_kind') != 'payment':
                    raise ValueError('この画像は別の用途で登録済みです。')
                return {'already_recorded': True, 'id': obligation_id, 'synced': self.store.sync_status()['synced']}
            receipt = self.checked(receipt['id'], payload.get('capture_sha256'), 'note')
            note = receipt['data']
            if note.get('capture_kind') != 'receipt' or note.get('target_obligation_id') != obligation_id or note.get('capture_status') != 'needs_review':
                raise ValueError('対象請求の領収書を確認し、必要なら紐づけ先を変更してください。')
            payment_id = str(uuid5(NAMESPACE, 'payment:' + note['image_sha256']))
            source = f'本人確認済み領収書 SHA-256: {note["image_sha256"]}'
            evidence_type = 'receipt'
        else:
            operation = short(payload.get('operation_id'), '登録ID', True)
            payment_id = str(uuid5(NAMESPACE, 'manual-payment:' + operation))
            existing = self.get(obligation_id)['data']
            if any(p['id'] == payment_id for p in existing.get('payments', [])):
                return {'already_recorded': True, 'id': obligation_id, 'synced': self.store.sync_status()['synced']}
            evidence_type = payload.get('evidence_type')
            if evidence_type not in ('statement', 'owner_report'):
                raise ValueError('取引明細または本人申告を選んでください。')
            source = short(payload.get('source'), '支払い確認の根拠', True)
        record = self.checked(obligation_id, payload.get('sha256'), 'obligation')
        data = record['data']
        if data.get('lifecycle') == 'cancelled':
            raise ValueError('取消済みの請求へは記録できません。先に請求を再開してください。')
        if payload.get('confirmed') is not True:
            raise ValueError('実際の支払い・対象・金額・日付を確認してください。')
        if data.get('currency') != 'JPY' or data.get('opening_paid') is None or data.get('amount_due') is None:
            raise ValueError('先に請求額・既納額・通貨を確認してください。')
        paid_at = calendar(payload.get('paid_at'), '支払日')
        if paid_at > today():
            raise ValueError('支払日は未来日にできません。')
        amount = money(payload.get('amount'))
        payment = dict(id=payment_id, amount=amount, paid_at=paid_at, source=source,
                       method=short(payload.get('method'), '支払い方法'), evidence_type=evidence_type)
        if receipt:
            payment['image_sha256'] = note['image_sha256']
        possible = [p for p in effective_payments(data) if p['amount'] == amount and p['paid_at'] == paid_at]
        if possible and payload.get('distinct_confirmed') is not True:
            return {'needs_duplicate_review': True, 'candidates': possible}
        if possible:
            payment['duplicate_review_reason'] = short(payload.get('distinct_reason'), '別支払いと確認した理由', True)
        data['payments'].append(payment)
        self.recalculate(data)
        changes = [self.change(record, data)]
        if receipt:
            note.update(capture_status='reviewed', confirmed_record_id=obligation_id, review_kind='payment')
            changes.append(self.change(receipt, note))
        return dict(self.store.put_many(changes), id=obligation_id)

    @serialized
    def correct(self, obligation_id, payload):
        record = self.get(obligation_id)
        data = record['data']
        operation = short(payload.get('operation_id'), '訂正ID', True)
        correction_id = str(uuid5(NAMESPACE, 'correction:' + operation))
        if any(c['id'] == correction_id for c in data.get('payment_corrections', [])):
            return {'already_recorded': True, 'id': obligation_id, 'synced': self.store.sync_status()['synced']}
        record = self.checked(obligation_id, payload.get('sha256'), 'obligation')
        data = record['data']
        if payload.get('payment_id') not in {p['id'] for p in effective_payments(data)}:
            raise ValueError('訂正する有効な支払いを選んでください。')
        reason = short(payload.get('reason'), '訂正理由', True)
        data.setdefault('payment_corrections', []).append(dict(id=correction_id, action='void', payment_id=payload['payment_id'], reason=reason, corrected_at=today()))
        replacement = payload.get('replacement')
        if replacement is not None:
            if not isinstance(replacement, dict):
                raise ValueError('訂正値を確認してください。')
            original = next(p for p in data['payments'] if p['id'] == payload['payment_id'])
            paid_at = calendar(replacement.get('paid_at'), '支払日')
            if paid_at > today():
                raise ValueError('支払日は未来日にできません。')
            payment = dict(original, id=str(uuid5(NAMESPACE, 'replacement:' + operation)),
                           amount=money(replacement.get('amount')), paid_at=paid_at,
                           method=short(replacement.get('method'), '支払い方法'), correction_reason=reason)
            candidates = [p for p in effective_payments(data) if p['amount'] == payment['amount'] and p['paid_at'] == paid_at]
            if candidates and payload.get('distinct_confirmed') is not True:
                return {'needs_duplicate_review': True, 'candidates': candidates}
            if candidates:
                payment['duplicate_review_reason'] = short(payload.get('distinct_reason'), '別支払いと確認した理由', True)
            data['payments'].append(payment)
        self.recalculate(data)
        return self.store.put_many([self.change(record, data)])

    @serialized
    def amend(self, obligation_id, payload):
        record = self.get(obligation_id)
        data = record['data']
        operation = short(payload.get('operation_id'), '訂正ID', True)
        correction_id = str(uuid5(NAMESPACE, 'amend:' + operation))
        if any(c['id'] == correction_id for c in data.get('obligation_corrections', [])):
            return {'already_recorded': True, 'id': obligation_id, 'synced': self.store.sync_status()['synced']}
        record = self.checked(obligation_id, payload.get('sha256'), 'obligation')
        data = record['data']
        if data.get('lifecycle') == 'cancelled':
            raise ValueError('取消済みの請求は先に再開してください。')
        fields = payload.get('fields', {})
        if not isinstance(fields, dict) or payload.get('confirmed') is not True:
            raise ValueError('用紙と内容を確認してください。')
        if data.get('currency') not in (None, 'JPY'):
            raise ValueError('この画面は日本円の請求を対象にしています。')
        reason = short(payload.get('reason'), '訂正理由', True)
        before = {k: data.get(k) for k in ('category', 'issuer', 'fiscal_year', 'target_period', 'installment', 'amount_due', 'opening_paid', 'due_date')}
        if fields.get('category') not in CATEGORIES:
            raise ValueError('税目を選んでください。')
        amount = money(fields.get('amount_due'))
        opening = fields.get('opening_paid')
        if not isinstance(opening, str) or not re.fullmatch(r'[0-9]{1,10}', opening):
            raise ValueError('開始時点の既納額は確認済みの整数を入力してください。未払いと確認した場合だけ0にしてください。')
        year = fields.get('fiscal_year')
        if year not in (None, '') and (isinstance(year, bool) or not re.fullmatch('20[0-9]{2}', str(year))):
            raise ValueError('年度は西暦4桁で入力してください。')
        data.update(category=fields['category'], issuer=short(fields.get('issuer'), '請求元', True),
                    fiscal_year=int(year) if year not in (None, '') else None,
                    target_period=short(fields.get('target_period'), '対象期間'), installment=short(fields.get('installment'), '期別'),
                    amount_due=amount, opening_paid=str(int(opening)), currency='JPY', due_date=calendar(fields.get('due_date'), '納期限', True))
        self.recalculate(data)
        candidates = [c for c in self.candidates(data) if c['id'] != obligation_id]
        if candidates and payload.get('distinct_confirmed') is not True:
            return {'needs_duplicate_review': True, 'candidates': candidates}
        if candidates:
            data['duplicate_review_reason'] = short(payload.get('distinct_reason'), '別請求と確認した理由', True)
        data.setdefault('obligation_corrections', []).append(dict(id=correction_id, corrected_at=today(), reason=reason, previous=before))
        data['sources'].append(dict(reference='本人が用紙と照合して訂正: ' + reason, captured_at=today()))
        return self.store.put_many([self.change(record, data)])

    @serialized
    def lifecycle(self, obligation_id, payload):
        record = self.get(obligation_id)
        data = record['data']
        operation = short(payload.get('operation_id'), '操作ID', True)
        event_id = str(uuid5(NAMESPACE, 'lifecycle:' + operation))
        if any(e['id'] == event_id for e in data.get('lifecycle_events', [])):
            return {'already_recorded': True, 'id': obligation_id, 'synced': self.store.sync_status()['synced']}
        record = self.checked(obligation_id, payload.get('sha256'), 'obligation')
        data = record['data']
        action = payload.get('action')
        if action not in ('cancel', 'reopen'):
            raise ValueError('取消または再開を選んでください。')
        reason = short(payload.get('reason'), '理由', True)
        if action == 'cancel':
            if data.get('amount_paid') != '0':
                raise ValueError('支払い記録がある・既納額が不明な請求は取り消せません。実績を確認して先に訂正してください。')
            data['lifecycle'] = 'cancelled'
        else:
            data['lifecycle'] = 'active'
            candidates = self.candidates(data)
            if candidates and payload.get('distinct_confirmed') is not True:
                return {'needs_duplicate_review': True, 'candidates': candidates}
            if candidates:
                data['duplicate_review_reason'] = short(payload.get('distinct_reason'), '別請求と確認した理由', True)
        data.setdefault('lifecycle_events', []).append(dict(id=event_id, action=action, reason=reason, recorded_at=today()))
        return self.store.put_many([self.change(record, data)])

    @serialized
    def reassign(self, capture_id, payload):
        record = self.get(capture_id)
        note = record['data']
        operation = short(payload.get('operation_id'), '操作ID', True)
        event_id = str(uuid5(NAMESPACE, 'reassign:' + operation))
        if any(e['id'] == event_id for e in note.get('association_events', [])):
            return {'already_recorded': True, 'id': capture_id, 'synced': self.store.sync_status()['synced']}
        record = self.checked(capture_id, payload.get('sha256'), 'note')
        note = record['data']
        if note.get('capture_status') == 'reviewed' or not note.get('image_sha256'):
            raise ValueError('確定済みの写真は付け替えできません。記録済みの支払いを先に確認してください。')
        reason = short(payload.get('reason'), '付け替え理由', True)
        kind = payload.get('kind')
        target = payload.get('target_id')
        if kind not in ('notice', 'receipt'):
            raise ValueError('書類の種類を選んでください。')
        if kind == 'receipt':
            obligation = self.get(target)['data']
            if obligation.get('type') != 'obligation' or obligation.get('lifecycle') == 'cancelled':
                raise ValueError('有効な支払い対象を選んでください。')
        else:
            target = None
        note.setdefault('association_events', []).append(dict(id=event_id, reason=reason, recorded_at=today(),
            previous_kind=note.get('capture_kind', 'notice'), previous_target=note.get('target_obligation_id')))
        note.update(capture_kind=kind, target_obligation_id=target, capture_status='needs_review')
        note.pop('review_draft', None)
        body = '# 確認待ちの撮影記録\n\n本人が書類の種類・対象を変更して再確認中。変更理由: ' + reason + '\nまだ請求・支払いを確定した記録ではありません。\n'
        return self.store.put_many([self.change(record, note, body)])
