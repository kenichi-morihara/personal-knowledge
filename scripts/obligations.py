"""Shared arithmetic: immutable payments with explicit, append-only void corrections."""
def effective_payments(data):
    voided = {c['payment_id'] for c in data.get('payment_corrections', [])}
    return [p for p in data.get('payments', []) if p['id'] not in voided]


def duplicate_candidates(data, records):
    result = []
    for row in records:
        if row.get('type') != 'obligation' or row.get('lifecycle') == 'cancelled' or row.get('id') == data.get('id'):
            continue
        if row.get('category') != data.get('category') or row.get('fiscal_year') != data.get('fiscal_year'):
            continue
        if row.get('issuer') not in (None, data.get('issuer')):
            continue
        if not row.get('installment') or not data.get('installment') or row.get('installment') == data.get('installment'):
            result.append({k: row.get(k) for k in ('id', 'category', 'issuer', 'installment', 'amount_due')})
    return result
