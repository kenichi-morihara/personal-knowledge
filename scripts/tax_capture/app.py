"""Owner-only Android capture page; separate from the live MCP service."""
import hashlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from core import MAX_IMAGE

COOKIE = 'tax_capture_session'


def create_app(captures, origin, owner_key, local_port=8780):
    parsed = urlparse(origin)
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1')) or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Public URL must be an HTTPS origin (HTTP loopback for tests only)')
    origin = origin.rstrip('/')
    secure = parsed.scheme == 'https'
    failure_times = []
    active_upload = False

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
        return HTMLResponse('''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#143c32"><title>納付書を撮る</title><link rel="manifest" href="/manifest.webmanifest"><style>
:root{font:17px system-ui,sans-serif;color:#193d34;background:#f3f6f2}*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0}main{max-width:560px;margin:auto;padding:32px 22px}h1{font-size:28px;margin:12px 0}p{line-height:1.65}.card{background:white;border:1px solid #dce4dc;border-radius:18px;padding:24px;margin:24px 0}button,.camera{display:block;width:100%;border:0;border-radius:12px;padding:18px;background:#194d3f;color:white;text-align:center;font:inherit;font-weight:650;cursor:pointer}input[type=password]{width:100%;padding:14px;margin:12px 0 20px;border:1px solid #aabbb2;border-radius:8px;font:inherit}small{display:block;line-height:1.7;color:#52685e}.secondary{background:#e8eee8;color:#193d34;margin-top:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.7}button:disabled{opacity:.5}footer{font-size:13px;color:#52685e}a{color:#194d3f}</style><main><small>PERSONAL KNOWLEDGE</small><h1>納付書を撮る</h1>''' + body + '''<footer>撮影画像・OCR全文はMacに保管。Gitには抽出候補と出典を保存します。Macとこの接続の稼働中に利用できます。</footer></main></html>''')

    async def home(request):
        if not authenticated(request):
            return page('''<p>税金・国保・年金の納付書を、あなたのナレッジに記録します。</p><section class="card"><form method="post" action="/login"><label for="key">撮影用キーでログイン</label><input id="key" name="key" type="password" required autocomplete="current-password"><button>ログイン</button></form><p><small>Macで <code>make capture-key-copy</code> を実行してキーを取得してください。キーをAIのチャットに貼る必要はありません。</small></p></section>''')
        token = json.dumps(csrf(request))
        return page('''<p>撮影後、自動で読み取り・記録します。<br>金額や納期限は「要確認」で残ります。</p><section class="card"><label class="camera" for="camera">カメラで納付書を撮る</label><input id="camera" type="file" accept="image/jpeg,image/png" capture="environment" hidden><button id="choose" class="secondary" type="button">保存済みの写真を選ぶ</button><input id="gallery" type="file" accept="image/jpeg,image/png" hidden><p><small>15MB以下のJPEG・PNG。用紙全体を明るい場所で、1枚ずつ撮影してください。</small></p><pre id="result" role="status" aria-live="polite"></pre><button id="retry" hidden>同じ写真で再試行</button></section><small>Chromeのメニュー →「ホーム画面に追加」で、撮影への入口を置けます。納付書の撮影だけで「支払済み」にはしません。</small><form method="post" action="/logout"><input type="hidden" name="csrf" value="''' + html.escape(csrf(request), quote=True) + '''"><button class="secondary">ログアウト</button></form><script>
const csrf=''' + token + r''';const out=document.querySelector('#result'),retry=document.querySelector('#retry');let current=null,busy=false;
async function send(file){if(busy||!file)return;current=file;retry.hidden=true;if(file.size>15728640){out.textContent='画像は15MB以下にしてください。';return;}busy=true;document.querySelector('#choose').disabled=true;out.textContent='読み取り・記録中です… このページを開いたままお待ちください。';try{const r=await fetch('/capture',{method:'POST',headers:{'X-Capture-CSRF':csrf},body:file,credentials:'same-origin'});const data=await r.json();if(!r.ok)throw new Error(data.error||'記録できませんでした');const f=data.fields;out.textContent=(data.duplicate?'同じ写真は記録済みです。':'撮影記録を保存しました（要確認）。')+'\n税目候補：'+(f.category||'未確認')+'\n金額候補：'+(f.amount_due?f.amount_due+'円':'未確認')+'\n年度候補：'+(f.fiscal_year||'未確認')+'\n納期限候補：'+(f.due_date||'未確認')+'\n記録：'+data.path+'\nID：'+data.id+'\n'+(data.synced?'GitHubにも保存済み':'Macに保存済み。GitHubへの同期はMacで確認してください。')+(data.possible_duplicate_of?'\n似た請求があります。二重登録に注意して確認してください。':'');current=null;}catch(e){out.textContent=e.message+'\n写真はMacに保存されている場合があります。接続を確認して再試行してください。';retry.hidden=false;}finally{busy=false;document.querySelector('#choose').disabled=false;}}
document.querySelector('#choose').onclick=()=>document.querySelector('#gallery').click();for(const id of ['camera','gallery'])document.getElementById(id).onchange=e=>send(e.target.files[0]);retry.onclick=()=>send(current);
</script>''')

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
            result = await run_in_threadpool(captures.ingest, bytes(raw))
            return JSONResponse(result)
        except ValueError as exc:
            # Internal Git exceptions contain no secrets, but do not return local paths/details.
            return JSONResponse({'error': '記録できませんでした。Macの作業中変更・Git同期状態、画像形式を確認して再試行してください。'}, status_code=409)
        except Exception:
            return JSONResponse({'error': '処理を完了できませんでした。写真を保管し、Macで状態を確認してください。'}, status_code=500)
        finally:
            active_upload = False

    async def manifest(request):
        return JSONResponse({'name': '納付書を撮る', 'short_name': '納付書', 'start_url': '/', 'display': 'standalone', 'background_color': '#f3f6f2', 'theme_color': '#143c32'})

    async def health(request):
        return JSONResponse({'status': 'ok'})

    app = Starlette(routes=[Route('/', home), Route('/login', login, methods=['POST']), Route('/logout', logout, methods=['POST']), Route('/capture', capture, methods=['POST']), Route('/manifest.webmanifest', manifest), Route('/health', health)])

    async def boundary(request: Request, call_next):
        # Check Host even though the process binds to loopback behind a tunnel.
        if request.headers.get('host') not in {parsed.netloc, f'127.0.0.1:{local_port}'}:
            return JSONResponse({'error': 'Invalid host'}, status_code=400)
        length = request.headers.get('content-length')
        if length and (not length.isdigit() or int(length) > MAX_IMAGE):
            return JSONResponse({'error': '画像は15MB以下にしてください。'}, status_code=413)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; manifest-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response
    app.add_middleware(BaseHTTPMiddleware, dispatch=boundary)
    return app
