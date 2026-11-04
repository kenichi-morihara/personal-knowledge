"""Transport-independent validated SDK. Auth, timeouts and redirects belong to transport."""
import json
from pathlib import Path
import re
from urllib.parse import quote, urlencode
from jsonschema import Draft202012Validator, FormatChecker

CONTRACT = json.loads(Path(__file__).with_name('contract.json').read_text())
VALIDATORS = {name: Draft202012Validator(schema, format_checker=FormatChecker())
              for name, schema in CONTRACT['schemas'].items()}


class ContractError(ValueError):
    """Invalid client input or incompatible server response; never includes payloads."""


def validate(name, value):
    errors = list(VALIDATORS[name].iter_errors(value))
    if errors:
        raise ContractError('AI Tasks API contract mismatch: ' + name)
    return value


class ContractClient:
    def __init__(self, transport):
        self.transport = transport

    def call(self, operation, *, task_id=None, data=None, query=None, key=None):
        op = CONTRACT['operations'][operation]
        path = op['path']
        if '{id}' in path:
            if not isinstance(task_id, str) or not task_id:
                raise ContractError('Task/collection ID required')
            path = path.replace('{id}', quote(task_id, safe=''))
        if op.get('idempotent') and (not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', key)):
            raise ContractError('Invalid idempotency key')
        if 'body' in op:
            validate(op['body'], data)
        if 'query' in op:
            query = {} if query is None else query
            validate(op['query'], query)
            if query:
                path += '?' + urlencode(query)
        return validate(op['response'], self.transport(op['method'], path, data, key))
