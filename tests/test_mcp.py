"""Exercise OAuth and the real MCP HTTP boundary using synthetic temporary Git repos."""
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from mcp_auth import owner_key
from mcp_server import create_app, create_server
from mcp_store import Store


class MCPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root, self.state = base / 'repo', base / 'state'
        self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.bare = base / 'origin.git'
        subprocess.run(['git', 'init', '--bare', str(self.bare)], check=True, capture_output=True)
        self.git('remote', 'add', 'origin', str(self.bare))
        for domain in ('notes', 'finance', 'profile', 'projects', 'sales', 'templates'):
            (self.root / domain).mkdir()
        for name in ('AGENTS.md', 'finance/README.md', 'sales/README.md', 'templates/note.md'):
            (self.root / name).write_text('# Test rules\n')
        self.id = str(uuid4())
        self.data = dict(id=self.id, type='note', updated_at=self.today(),
            sources=[dict(reference='Synthetic fixture', captured_at=self.today())])
        (self.root / 'notes/test.md').write_text(self.content())
        self.git('add', '.')
        self.git('commit', '-m', 'Synthetic fixture')
        self.git('push', '-u', 'origin', 'main')
        self.store = Store(self.root, self.state)
        self.origin = 'http://127.0.0.1:8777'
        self.server, self.auth = create_server(self.root, self.state, self.origin)
        self.client = TestClient(create_app(self.server, self.auth), base_url=self.origin, follow_redirects=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL, text=True).strip()

    @staticmethod
    def today():
        return datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()

    def content(self, body='Synthetic Canada plan'):
        return '---\n' + yaml.safe_dump(self.data) + '---\n# ' + body + '\n'

    def authorize(self, scopes='knowledge:read knowledge:write offline_access', key=None):
        registered = self.client.post('/register', json={'client_name': 'Test ChatGPT',
            'redirect_uris': ['https://chatgpt.com/connector_platform/oauth_redirect'],
            'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
            'token_endpoint_auth_method': 'none', 'scope': scopes})
        self.assertEqual(registered.status_code, 201, registered.text)
        client_id = registered.json()['client_id']
        verifier = 'a' * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        authorized = self.client.get('/authorize', params={'client_id': client_id, 'response_type': 'code',
            'redirect_uri': 'https://chatgpt.com/connector_platform/oauth_redirect', 'scope': scopes,
            'code_challenge': challenge, 'code_challenge_method': 'S256', 'state': 'owner-request',
            'resource': self.origin + '/mcp'})
        self.assertEqual(authorized.status_code, 302, authorized.text)
        consent_url = authorized.headers['location']
        page = self.client.get(consent_url)
        # Native HTML form POSTs use Origin:null under no-referrer. Do not reintroduce it.
        self.assertEqual(page.headers['referrer-policy'], 'same-origin')
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        approved = self.client.post(consent_url, headers={'Origin': self.origin}, data={'csrf': csrf, 'owner_key': key or owner_key(self.state)})
        return client_id, verifier, approved, consent_url, csrf

    def tokens(self, scopes='knowledge:read knowledge:write offline_access'):
        client_id, verifier, approved, _, _ = self.authorize(scopes)
        self.assertEqual(approved.status_code, 303, approved.text)
        parsed = parse_qs(urlparse(approved.headers['location']).query)
        self.assertEqual(parsed['state'], ['owner-request'])
        self.assertEqual(parsed['iss'], [self.client.get('/.well-known/oauth-authorization-server').json()['issuer']])
        form = dict(grant_type='authorization_code', client_id=client_id, code=parsed['code'][0], code_verifier=verifier,
            redirect_uri='https://chatgpt.com/connector_platform/oauth_redirect', resource=self.origin + '/mcp')
        token = self.client.post('/token', data=form)
        self.assertEqual(token.status_code, 200, token.text)
        return token.json(), form

    def rpc(self, token, method, params):
        return self.client.post('/mcp', headers={'Authorization': 'Bearer ' + token,
            'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})

    def test_unauthenticated_requests_are_blocked(self):
        response = self.client.post('/mcp', json={})
        self.assertEqual(response.status_code, 401)
        self.assertIn('resource_metadata', response.headers['www-authenticate'])
        metadata = self.client.get('/.well-known/oauth-authorization-server').json()
        self.assertIn('S256', metadata['code_challenge_methods_supported'])
        resource = self.client.get('/.well-known/oauth-protected-resource/mcp').json()
        self.assertEqual(resource['resource'], self.origin + '/mcp')

    def test_owner_auth_and_csrf_required(self):
        _, _, denied, consent_url, csrf = self.authorize(key='incorrect')
        self.assertEqual(denied.status_code, 403)
        cross_origin = self.client.post(consent_url, headers={'Origin': 'https://evil.invalid'}, data={'csrf': csrf, 'owner_key': owner_key(self.state)})
        self.assertEqual(cross_origin.status_code, 403)
        null_origin = self.client.post(consent_url, headers={'Origin': 'null'}, data={'csrf': csrf, 'owner_key': owner_key(self.state)})
        self.assertEqual(null_origin.status_code, 403)
        bad_csrf = self.client.post(consent_url, headers={'Origin': self.origin}, data={'csrf': 'wrong', 'owner_key': owner_key(self.state)})
        self.assertEqual(bad_csrf.status_code, 403)

    def test_oauth_search_fetch_and_replay_rejected(self):
        token, form = self.tokens()
        self.assertEqual(self.client.post('/token', data=form).status_code, 400)
        initialized = self.rpc(token['access_token'], 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(initialized.status_code, 200, initialized.text)
        tools = self.rpc(token['access_token'], 'tools/list', {}).json()['result']['tools']
        self.assertEqual({t['name'] for t in tools}, {'search', 'fetch', 'get_template', 'get_guidance', 'put_record'})
        found = self.rpc(token['access_token'], 'tools/call', {'name': 'search', 'arguments': {'query': 'Canada'}}).json()['result']
        self.assertEqual(json.loads(found['content'][0]['text'])['results'][0]['id'], self.id)
        fetched = self.rpc(token['access_token'], 'tools/call', {'name': 'fetch', 'arguments': {'id': self.id}}).json()['result']
        self.assertIn('sha256', json.loads(fetched['content'][0]['text']))

    def test_pkce_invalid_and_refresh_rotation(self):
        token, form = self.tokens()
        new = self.client.post('/token', data=dict(grant_type='refresh_token', client_id=form['client_id'], refresh_token=token['refresh_token']))
        self.assertEqual(new.status_code, 200, new.text)
        self.assertEqual(self.rpc(token['access_token'], 'tools/list', {}).status_code, 401)
        self.assertEqual(self.client.post('/token', data=dict(grant_type='refresh_token', client_id=form['client_id'], refresh_token=token['refresh_token'])).status_code, 400)
        client_id, verifier, approved, _, _ = self.authorize()
        code = parse_qs(urlparse(approved.headers['location']).query)['code'][0]
        wrong = self.client.post('/token', data=dict(grant_type='authorization_code', client_id=client_id, code=code,
            code_verifier='b' * 64, redirect_uri='https://chatgpt.com/connector_platform/oauth_redirect'))
        self.assertEqual(wrong.status_code, 400)

    def test_read_only_scope_cannot_write(self):
        token, _ = self.tokens('knowledge:read')
        response = self.rpc(token['access_token'], 'tools/call', {'name': 'put_record', 'arguments': {
            'path': 'notes/new.md', 'content': self.content(), 'expected_sha256': ''}}).json()['result']
        self.assertTrue(response['isError'])
        self.assertFalse((self.root / 'notes/new.md').exists())

    def test_create_update_stale_hash_and_push(self):
        fetched = self.store.fetch(self.id)
        result = self.store.put('notes/test.md', self.content('Updated plan'), fetched['sha256'])
        self.assertTrue(result['synced'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'origin/main'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.store.put('notes/test.md', self.content('Stale'), fetched['sha256'])
        self.data['id'] = str(uuid4())
        result = self.store.put('notes/new.md', self.content('New record'), '')
        self.assertTrue(result['synced'])
        self.assertEqual(len(self.store.search('record')['results']), 1)

    def test_invalid_record_never_changes_working_tree(self):
        before = self.git('rev-parse', 'HEAD')
        self.data['sources'] = []
        with self.assertRaisesRegex(ValueError, 'Validation failed'):
            self.store.put('notes/test.md', self.content(), self.store.fetch(self.id)['sha256'])
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_paths_dirty_tree_and_secret_rejected(self):
        for path in ('../README.md', 'notes/../README.md', '.git/config', 'AGENTS.md', 'notes/.secret.md'):
            with self.assertRaises(ValueError):
                self.store.put(path, self.content(), '')
        (self.root / 'notes/link.md').symlink_to(self.root / 'AGENTS.md')
        with self.assertRaises(ValueError):
            self.store.put('notes/link.md', self.content(), '')
        (self.root / 'notes/link.md').unlink()
        (self.root / 'local.txt').write_text('user work')
        with self.assertRaisesRegex(ValueError, 'local changes'):
            self.store.put('notes/new.md', self.content(), '')
        with self.assertRaisesRegex(ValueError, 'credential'):
            self.store.put('notes/new.md', self.content('ghp_' + 'x' * 30), '')

    def test_authenticated_tool_write(self):
        token, _ = self.tokens()
        self.data['id'] = str(uuid4())
        result = self.rpc(token['access_token'], 'tools/call', {'name': 'put_record', 'arguments': {
            'path': 'notes/via-mcp.md', 'content': self.content('Created via MCP'), 'expected_sha256': ''}}).json()['result']
        self.assertFalse(result.get('isError', False), result)
        self.assertTrue(json.loads(result['content'][0]['text'])['synced'])
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_invalid_redirect_is_rejected(self):
        result = self.client.post('/register', json={'redirect_uris': ['https://evil.invalid/callback'],
            'grant_types': ['authorization_code'], 'response_types': ['code'], 'token_endpoint_auth_method': 'none'})
        self.assertEqual(result.status_code, 400)

    def test_expired_token_and_revocation(self):
        token, form = self.tokens()
        self.auth.access[self.auth.fingerprint(token['access_token'])].expires_at = 0
        self.assertEqual(self.rpc(token['access_token'], 'tools/list', {}).status_code, 401)
        renewed = self.client.post('/token', data=dict(grant_type='refresh_token', client_id=form['client_id'], refresh_token=token['refresh_token'])).json()
        revoke = self.client.post('/revoke', data=dict(client_id=form['client_id'], token=renewed['access_token'], token_type_hint='access_token'))
        self.assertEqual(revoke.status_code, 200)
        self.assertEqual(self.rpc(renewed['access_token'], 'tools/list', {}).status_code, 401)

    def test_existing_payment_history_cannot_be_removed(self):
        self.id = str(uuid4())
        payment = dict(id=str(uuid4()), amount='100', paid_at=self.today(), source='Synthetic receipt')
        self.data.update(id=self.id, type='obligation', currency='JPY', amount_due='300', opening_paid='0',
            amount_paid='100', amount_remaining='200', status='partial', payments=[payment])
        self.store.put('finance/tax.md', self.content('Tax'), '')
        original = self.store.fetch(self.id)
        self.data.update(payments=[], amount_paid='0', amount_remaining='300', status='unpaid')
        with self.assertRaisesRegex(ValueError, 'append-only'):
            self.store.put('finance/tax.md', self.content('Tax'), original['sha256'])

    def test_upstream_changes_require_local_sync(self):
        before = self.git('rev-parse', 'HEAD')
        # Simulate a different writer advancing main without touching this working tree.
        commit = subprocess.check_output(['git', '-C', str(self.root), 'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'Other writer'], text=True).strip()
        subprocess.run(['git', '--git-dir', str(self.bare), 'fetch', str(self.root), commit], check=True, capture_output=True)
        subprocess.run(['git', '--git-dir', str(self.bare), 'update-ref', 'refs/heads/main', commit], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, 'Remote has new commits'):
            self.store.put('notes/test.md', self.content('Overwrite'), self.store.fetch(self.id)['sha256'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)

    def test_balance_snapshots_append_only(self):
        self.id = str(uuid4())
        self.data.update(id=self.id, type='balance', amount='1000', currency='JPY', account_id='synthetic',
            institution='Test bank', observed_at=self.today() + 'T12:00:00+09:00')
        self.store.put('finance/balance.md', self.content('Snapshot'), '')
        with self.assertRaisesRegex(ValueError, 'append-only'):
            self.store.put('finance/balance.md', self.content('Replaced'), self.store.fetch(self.id)['sha256'])


if __name__ == '__main__':
    unittest.main()
