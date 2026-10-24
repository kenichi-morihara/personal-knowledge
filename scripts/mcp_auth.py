"""Single-owner OAuth for a temporary Mac-hosted server.

The SDK implements DCR, PKCE, client authentication and OAuth endpoints.
Grants live only in RAM, and all expire on restart. The owner key lives outside Git.
"""
from collections import deque
import hashlib
import hmac
import os
from html import escape
from pathlib import Path
import secrets
import time
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
    RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPES = ['knowledge:read', 'knowledge:write', 'offline_access']


def owner_key(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    path = state / 'owner-key.txt'
    if not path.exists():
        with open(path, 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            stream.write(secrets.token_urlsafe(32) + '\n')
    path.chmod(0o600)
    key = path.read_text().strip()
    if len(key) < 32:
        raise ValueError('Owner key must contain at least 32 characters')
    return key


class OwnerOAuth:
    def __init__(self, origin, key):
        self.origin = origin.rstrip('/')
        self.issuer = self.origin + '/'
        self.resource = self.origin + '/mcp'
        self.key_digest = hashlib.sha256(key.encode()).digest()
        self.clients, self.pending, self.codes, self.access, self.refresh = ({ } for _ in range(5))
        self.attempts = deque()

    def clean(self):
        now = time.time()
        for store in (self.pending, self.codes, self.access, self.refresh):
            for key, value in list(store.items()):
                expiry = value['expires_at'] if isinstance(value, dict) else value.expires_at
                if expiry is not None and expiry < now:
                    del store[key]
        while self.attempts and self.attempts[0] < now - 60:
            self.attempts.popleft()

    @staticmethod
    def fingerprint(token):
        return hashlib.sha256(token.encode()).hexdigest()

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        if len(self.clients) >= 100:
            raise RegistrationError('invalid_client_metadata', 'Registration limit reached; restart the server')
        for redirect in client_info.redirect_uris:
            url = urlparse(str(redirect))
            local = url.hostname in ('localhost', '127.0.0.1')
            if (url.username or url.password or url.fragment or (not local and
                    (url.scheme != 'https' or url.hostname not in ('chatgpt.com', 'chat.openai.com', 'claude.ai', 'claude.com') or url.port not in (None, 443)))):
                raise RegistrationError('invalid_redirect_uri', 'Only ChatGPT, Claude and loopback callback URLs are allowed')
            if local and url.scheme not in ('http', 'https'):
                raise RegistrationError('invalid_redirect_uri')
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params):
        self.clean()
        scopes = params.scopes or ['knowledge:read', 'knowledge:write']
        if not set(scopes) <= set(SCOPES) or 'knowledge:read' not in scopes:
            raise AuthorizeError('invalid_scope')
        if params.resource not in (None, self.resource):
            raise AuthorizeError('invalid_request', 'Unknown resource')
        if len(self.pending) >= 100:
            raise AuthorizeError('temporarily_unavailable')
        params.scopes = scopes
        nonce = secrets.token_urlsafe(32)
        self.pending[nonce] = {'client': client, 'params': params, 'expires_at': time.time() + 600}
        return self.origin + '/consent?request=' + nonce

    async def consent(self, request: Request):
        self.clean()
        nonce = request.query_params.get('request', '')
        pending = self.pending.get(nonce)
        if pending is None:
            return Response('Authorization request expired; reconnect from your AI app.', status_code=400)
        headers = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                   'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
                   'Content-Security-Policy': "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}
        if request.method == 'GET':
            csrf = secrets.token_urlsafe(32)
            pending['csrf'] = csrf
            client = pending['client']
            response = HTMLResponse(f'''<!doctype html><html lang="ja"><meta charset="utf-8">
<title>Personal Knowledge 接続</title><h1>Personal Knowledgeへの接続</h1>
<p>接続先：{escape(client.client_name or client.client_id)}</p>
<p>戻り先：{escape(str(pending['params'].redirect_uri))}</p>
<p>許可する権限：{escape(' '.join(pending['params'].scopes))}</p>
<p>自分で開始した接続だけ許可してください。所有者キーはこのMacのローカルファイルにあります。</p>
<form method="post"><input type="hidden" name="csrf" value="{csrf}">
<label>所有者キー <input type="password" name="owner_key" required autocomplete="off" maxlength="128"></label>
<button type="submit">接続を許可</button></form></html>''', headers=headers)
            response.set_cookie('pk_consent', csrf, secure=self.origin.startswith('https://'), httponly=True, samesite='lax', max_age=600, path='/consent')
            return response
        if request.headers.get('origin') != self.origin:
            return Response('Invalid origin', status_code=403, headers=headers)
        form = await request.form(max_fields=3, max_files=0)
        csrf = form.get('csrf', '')
        if (not isinstance(csrf, str) or not csrf or
                not hmac.compare_digest(csrf, pending.get('csrf', '')) or
                not hmac.compare_digest(csrf, request.cookies.get('pk_consent', ''))):
            return Response('Invalid consent request', status_code=403, headers=headers)
        if len(self.attempts) >= 5:
            return Response('Too many attempts. Wait one minute.', status_code=429, headers=headers)
        self.attempts.append(time.time())
        entered = form.get('owner_key', '')
        if not isinstance(entered, str) or not hmac.compare_digest(hashlib.sha256(entered.encode()).digest(), self.key_digest):
            return Response('Invalid owner key. Return to the authorization page and retry.', status_code=403, headers=headers)
        params = pending['params']
        code = secrets.token_urlsafe(32)
        self.codes[self.fingerprint(code)] = AuthorizationCode(
            code='', scopes=params.scopes, expires_at=time.time() + 120,
            client_id=pending['client'].client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource, subject='owner')
        del self.pending[nonce]
        response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state, iss=self.issuer), status_code=303, headers=headers)
        response.delete_cookie('pk_consent', path='/consent')
        return response

    async def load_authorization_code(self, client, authorization_code):
        self.clean()
        value = self.codes.get(self.fingerprint(authorization_code))
        if value and value.client_id == client.client_id:
            return value.model_copy(update={'code': authorization_code})
        return None

    def issue(self, client_id, scopes):
        self.clean()
        if len(self.access) >= 200 or len(self.refresh) >= 200:
            raise TokenError('invalid_grant', 'Grant limit reached; restart the server')
        access = secrets.token_urlsafe(32)
        self.access[self.fingerprint(access)] = AccessToken(token='', client_id=client_id, scopes=scopes,
            expires_at=int(time.time()) + 3600, resource=self.resource, subject='owner')
        refresh = None
        if 'offline_access' in scopes:
            refresh = secrets.token_urlsafe(32)
            self.refresh[self.fingerprint(refresh)] = RefreshToken(token='', client_id=client_id, scopes=scopes,
                expires_at=int(time.time()) + 86400 * 7, resource=self.resource, subject='owner')
        return OAuthToken(access_token=access, expires_in=3600, refresh_token=refresh, scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        self.clean()
        code = self.codes.pop(self.fingerprint(authorization_code.code), None)
        if not code or code.client_id != client.client_id:
            raise TokenError('invalid_grant')
        return self.issue(client.client_id, code.scopes)

    async def load_refresh_token(self, client, refresh_token):
        self.clean()
        value = self.refresh.get(self.fingerprint(refresh_token))
        if value and value.client_id == client.client_id:
            return value.model_copy(update={'token': refresh_token})
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.clean()
        value = self.refresh.pop(self.fingerprint(refresh_token.token), None)
        if not value or value.client_id != client.client_id or not set(scopes) <= set(value.scopes):
            raise TokenError('invalid_grant')
        # Revoke earlier access tokens for this client when its refresh token rotates.
        for key, token in list(self.access.items()):
            if token.client_id == client.client_id:
                del self.access[key]
        return self.issue(client.client_id, scopes)

    async def load_access_token(self, token):
        self.clean()
        value = self.access.get(self.fingerprint(token))
        return value.model_copy(update={'token': token}) if value else None

    async def revoke_token(self, token):
        for store in (self.access, self.refresh):
            for key, value in list(store.items()):
                if value.client_id == token.client_id:
                    del store[key]
