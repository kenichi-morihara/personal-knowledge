"""Owner-only Android capture page; separate from the live MCP service."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from starlette.routing import Route

from core import MAX_IMAGE
from management import Management

COOKIE = 'tax_capture_session'


def create_app(captures, origin, owner_key, local_port=8780, background_sync=False, tasks=None):
    parsed = urlparse(origin)
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1')) or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Public URL must be an HTTPS origin (HTTP loopback for tests only)')
    origin = origin.rstrip('/')
    secure = parsed.scheme == 'https'
    failure_times = []
    active_upload = False
    management = Management(captures, tasks)
    workers = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal workers
        if background_sync:
            from sync_runtime import SyncWorkers
            workers = SyncWorkers(management.store, management.tasks)
        try:
            yield
        finally:
            if workers:
                workers.close()

    def sign(value):
        return hmac.new(owner_key.encode(), value.encode(), hashlib.sha256).hexdigest()

    def authenticated(request):
        cookie = request.cookies.get(COOKIE, '')
        try:
            token, expires, signature = cookie.split('.')
            if len(token) != 32 or int(expires) < time.time() or int(expires) > time.time() + 31*86400:
                return False
            return hmac.compare_digest(signature, sign(token + '.' + expires))
        except (ValueError, TypeError):
            return False

    def same_origin(request):
        return request.headers.get('origin') == origin and request.headers.get('sec-fetch-site') not in ('cross-site', 'none')

    def csrf(request):
        return sign('csrf:' + request.cookies.get(COOKIE, ''))

    def page(body):
        return HTMLResponse('''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#143c32"><title>支払い管理</title><link rel="manifest" href="/manifest.webmanifest"><style>
:root{font:17px system-ui,sans-serif;color:#193d34;background:#f3f6f2}*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0}main{max-width:560px;margin:auto;padding:32px 22px}h1{font-size:28px;margin:12px 0}p{line-height:1.65}.card{background:white;border:1px solid #dce4dc;border-radius:18px;padding:24px;margin:24px 0}button,.camera{display:block;width:100%;border:0;border-radius:12px;padding:18px;background:#194d3f;color:white;text-align:center;font:inherit;font-weight:650;cursor:pointer}input[type=password]{width:100%;padding:14px;margin:12px 0 20px;border:1px solid #aabbb2;border-radius:8px;font:inherit}small{display:block;line-height:1.7;color:#52685e}.secondary{background:#e8eee8;color:#193d34;margin-top:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.7}button:disabled{opacity:.5}footer{font-size:13px;color:#52685e}a{color:#194d3f}</style><main><small>PERSONAL KNOWLEDGE</small><h1>支払い管理</h1>''' + body + '''<footer>撮影画像・OCR全文はMacに保管。Gitには抽出候補と出典を保存します。Macとこの接続の稼働中に利用できます。</footer></main></html>''')

    async def home(request):
        if not authenticated(request):
            return page('''<p>税金・国保・年金の納付書を、あなたのナレッジに記録します。</p><section class="card"><form method="post" action="/login"><label for="key">撮影用キーでログイン</label><input id="key" name="key" type="password" required autocomplete="current-password"><button>ログイン</button></form><p><small>Macで <code>make capture-key-copy</code> を実行してキーを取得してください。キーをAIのチャットに貼る必要はありません。</small></p></section>''')
        source = (Path(__file__).parent / 'web.html').read_text()
        return HTMLResponse(source.replace('__CSRF_JSON__', json.dumps(csrf(request))))

    async def login(request):
        if not same_origin(request):
            return JSONResponse({'error': '接続元を確認してください。'}, status_code=403)
        now = time.time()
        failure_times[:] = [stamp for stamp in failure_times if now - stamp < 60]
        if len(failure_times) >= 5:
            return JSONResponse({'error': '1分待ってからログインしてください。'}, status_code=429)
        raw = await request.body()
        if len(raw) > 2048:
            return JSONResponse({'error': '入力が長すぎます。'}, status_code=413)
        from urllib.parse import parse_qs
        key = parse_qs(raw.decode(errors='replace')).get('key', [''])[0]
        if not secrets.compare_digest(key, owner_key):
            failure_times.append(now)
            return page('<p>撮影用キーを確認してください。</p><a href="/">ログイン画面に戻る</a>')
        prefix = secrets.token_hex(16) + '.' + str(int(now + 30*86400))
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(COOKIE, prefix + '.' + sign(prefix), max_age=30*86400, secure=secure, httponly=True, samesite='strict', path='/')
        return response

    async def logout(request):
        if not authenticated(request) or not same_origin(request):
            return JSONResponse({'error': 'ログインしてください。'}, status_code=403)
        form = await request.form()
        if not secrets.compare_digest(str(form.get('csrf', '')), csrf(request)):
            return JSONResponse({'error': '画面を開き直してください。'}, status_code=403)
        response = RedirectResponse('/', status_code=303)
        response.delete_cookie(COOKIE, path='/', secure=secure, httponly=True, samesite='strict')
        return response

    async def capture(request):
        nonlocal active_upload
        if not authenticated(request):
            return JSONResponse({'error': 'ログイン画面を開き直してください。'}, status_code=401)
        if not same_origin(request) or not secrets.compare_digest(request.headers.get('x-capture-csrf', ''), csrf(request)):
            return JSONResponse({'error': '画面を開き直してください。'}, status_code=403)
        if active_upload:
            return JSONResponse({'error': '別の写真を処理中です。少し待って再試行してください。'}, status_code=409)
        active_upload = True
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_IMAGE:
                    return JSONResponse({'error': '画像は15MB以下にしてください。'}, status_code=413)
            result = await run_in_threadpool(management.capture_result, bytes(raw), request.query_params.get('kind', 'notice'), request.query_params.get('target'))
            return JSONResponse(result)
        except ValueError:
            # Internal Git exceptions contain no secrets, but do not return local paths/details.
            return JSONResponse({'error': '記録できませんでした。Macの作業中変更・Git同期状態、画像形式を確認して再試行してください。'}, status_code=409)
        except Exception:
            return JSONResponse({'error': '処理を完了できませんでした。写真を保管し、Macで状態を確認してください。'}, status_code=500)
        finally:
            active_upload = False

    async def api(request):
        if not authenticated(request):
            return JSONResponse({'error': 'ログイン画面を開き直してください。'}, status_code=401)
        if request.method == 'POST' and (not same_origin(request) or not secrets.compare_digest(request.headers.get('x-capture-csrf', ''), csrf(request))):
            return JSONResponse({'error': '画面を開き直してください。'}, status_code=403)
        try:
            operation = request.path_params['operation']
            record_id = request.query_params.get('id')
            if request.method == 'GET':
                if operation == 'overview':
                    return JSONResponse(await run_in_threadpool(management.overview))
                if operation == 'tasks':
                    if not management.tasks.client.configured:
                        return JSONResponse({'configured': False, 'tasks': [], 'next_page': None})
                    page_number = int(request.query_params.get('page', '0'))
                    report = await run_in_threadpool(management.tasks.client.list, page_number)
                    return JSONResponse(dict(report, configured=True))
                if operation == 'record':
                    return JSONResponse(await run_in_threadpool(management.get, record_id))
                if operation == 'image':
                    path = await run_in_threadpool(management.image, record_id or '')
                    return FileResponse(path, media_type='image/jpeg' if path.suffix == '.jpg' else 'image/png')
            else:
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 64 * 1024:
                        return JSONResponse({'error': '入力が長すぎます。'}, status_code=413)
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError('入力を確認してください。')
                if operation == 'sync':
                    if workers:
                        workers.request()
                    return JSONResponse({'sync_requested': workers is not None, 'sync': management.store.sync_status(), 'tasks': management.tasks.status()})
                if operation == 'task':
                    result = await run_in_threadpool(management.tasks.update_task, record_id, payload)
                    return JSONResponse({'task': result})
                handlers = {'lifecycle': management.lifecycle, 'reassign': management.reassign, 'review': management.review, 'pay': management.pay, 'correct': management.correct, 'amend': management.amend}
                if operation in handlers:
                    return JSONResponse(await run_in_threadpool(handlers[operation], record_id, payload))
            return JSONResponse({'error': '対象がありません。'}, status_code=404)
        except (ValueError, TypeError, KeyError) as error:
            message = str(error) if isinstance(error, ValueError) else '入力内容を確認してください。'
            return JSONResponse({'error': message}, status_code=409)
        except Exception:
            return JSONResponse({'error': '処理できませんでした。Macの保存・Git同期状態を確認してください。'}, status_code=500)

    async def manifest(request):
        return JSONResponse({'name': '支払い管理', 'short_name': '支払い', 'start_url': '/', 'display': 'standalone', 'background_color': '#f3f6f2', 'theme_color': '#143c32'})

    async def health(request):
        return JSONResponse({'status': 'ok'})

    app = Starlette(lifespan=lifespan, routes=[Route('/', home), Route('/login', login, methods=['POST']), Route('/logout', logout, methods=['POST']), Route('/capture', capture, methods=['POST']), Route('/api/{operation}', api, methods=['GET', 'POST']), Route('/manifest.webmanifest', manifest), Route('/health', health)])

    async def boundary(request: Request, call_next):
        # Check Host even though the process binds to loopback behind a tunnel.
        if request.headers.get('host') not in {parsed.netloc, f'127.0.0.1:{local_port}'}:
            return JSONResponse({'error': 'Invalid host'}, status_code=400)
        length = request.headers.get('content-length')
        if length and (not length.isdigit() or int(length) > MAX_IMAGE):
            return JSONResponse({'error': '画像は15MB以下にしてください。'}, status_code=413)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' blob:; manifest-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response
    app.add_middleware(BaseHTTPMiddleware, dispatch=boundary)
    return app
