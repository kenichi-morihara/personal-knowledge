"""Separate local tax-capture service. Never restart or reconfigure the MCP server."""
import argparse
from functools import partial
import json
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from mcp_store import Store
from core import Captures, native_ocr, private_write
from app import create_app

STATE = Path.home() / 'Library' / 'Application Support' / 'personal-knowledge-capture'
MCP_STATE = Path.home() / 'Library' / 'Application Support' / 'personal-knowledge-mcp'


def key():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    path = STATE / 'capture-key.txt'
    if not path.exists():
        # Dedicated upload credential, never the MCP owner key.
        private_write(path, (secrets.token_urlsafe(32) + '\n').encode())
    return path.read_text().strip()


def setup():
    key()
    binary = STATE / 'bin/ocr'
    binary.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    subprocess.run(['xcrun', 'swiftc', '-O', str(HERE / 'ocr.swift'), '-o', str(binary)], check=True)
    print('Local OCR and dedicated capture key are ready. Key has not been displayed.')


def start(port):
    if not (STATE / 'bin/ocr').exists():
        raise ValueError('先に make capture-setup を実行してください。')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
    key()
    messages, children = queue.Queue(), []
    logs = []

    def cleanup():
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        (STATE / 'connection.json').unlink(missing_ok=True)
        for log in logs:
            log.close()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        tunnel = subprocess.Popen(['cloudflared', 'tunnel', '--url', f'http://127.0.0.1:{port}', '--no-autoupdate', '--protocol', 'http2'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        children.append(tunnel)
        private_write(STATE / 'tunnel.log', b'')
        tunnel_log = (STATE / 'tunnel.log').open('a')
        logs.append(tunnel_log)

        def reader():
            for line in tunnel.stdout:
                tunnel_log.write(line)
                tunnel_log.flush()
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if match:
                    messages.put(match.group())
        threading.Thread(target=reader, daemon=True).start()
        origin = messages.get(timeout=45)
        private_write(STATE / 'server.log', b'')
        log = (STATE / 'server.log').open('a')
        logs.append(log)
        server = subprocess.Popen([sys.executable, str(HERE / 'run.py'), 'serve', '--origin', origin, '--port', str(port)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        children.append(server)
        for _ in range(50):
            if server.poll() is not None:
                raise ValueError('撮影サーバーを起動できませんでした。撮影用server.logを確認してください。')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(.1)
        else:
            raise ValueError('撮影サーバーの起動を確認できませんでした。')
        info = dict(url=origin, pid=os.getpid(), port=port,
            started_at=subprocess.check_output(['ps', '-p', str(os.getpid()), '-o', 'lstart='], text=True).strip())
        private_write(STATE / 'connection.json', json.dumps(info).encode())
        print(f'Android capture URL: {origin}', flush=True)
        print('Login key: make capture-key-copy (do not send it to AI chats)', flush=True)
        print('Keep this Mac and process running. Ctrl+C stops this capture service only.', flush=True)
        while server.poll() is None and tunnel.poll() is None:
            time.sleep(1)
        raise ValueError('撮影接続が停止しました。再起動時はURLが変わります。')
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'start', 'serve', 'key-copy', 'status', 'stop', 'pending'])
    parser.add_argument('--origin')
    parser.add_argument('--port', type=int, default=8780)
    args = parser.parse_args()
    try:
        if args.command == 'setup':
            setup()
        elif args.command == 'key-copy':
            subprocess.run(['pbcopy'], input=key().encode(), check=True)
            print('撮影用キーをコピーしました。撮影ページのログインにだけ使ってください。')
        elif args.command == 'pending':
            store = Store(ROOT, MCP_STATE)
            with store.locked():
                records = [dict(id=data['id'], path=path, tax_category=data.get('tax_category'),
                    amount_due=data.get('amount_due'), due_date=data.get('due_date'),
                    possible_duplicate_of=data.get('possible_duplicate_of'))
                    for path, _, data in store.records() if data.get('capture_status') == 'needs_review']
            print(json.dumps(records, ensure_ascii=False, indent=2))
        elif args.command in ('status', 'stop'):
            path = STATE / 'connection.json'
            if not path.exists():
                print('撮影接続は起動していません。make capture-start で起動できます。')
                return
            info = json.loads(path.read_text())
            pid = str(info['pid'])
            process = subprocess.run(['ps', '-p', pid, '-o', 'command='], capture_output=True, text=True)
            started = subprocess.run(['ps', '-p', pid, '-o', 'lstart='], capture_output=True, text=True).stdout.strip()
            if process.returncode or 'tax_capture/run.py' not in process.stdout or started != info['started_at']:
                raise ValueError('起動記録とプロセスが一致しません。Macで確認してください。')
            if args.command == 'stop':
                os.kill(int(pid), signal.SIGTERM)
                print('撮影専用の接続に停止を依頼しました。')
            else:
                print(info['url'])
        elif args.command == 'start':
            start(args.port)
        else:
            if not args.origin:
                raise ValueError('--origin is required')
            captures = Captures(STATE, Store(ROOT, MCP_STATE), partial(native_ocr, STATE / 'bin/ocr'))
            import uvicorn
            uvicorn.run(create_app(captures, args.origin, key(), local_port=args.port), host='127.0.0.1', port=args.port, log_level='warning', access_log=False)
    except (ValueError, OSError, subprocess.CalledProcessError, queue.Empty) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
