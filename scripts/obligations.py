"""Shared arithmetic: immutable payments with explicit, append-only void corrections."""
def effective_payments(data):
    voided = {c['payment_id'] for c in data.get('payment_corrections', [])}
    return [p for p in data.get('payments', []) if p['id'] not in voided]
