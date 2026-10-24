"""Run an authenticated MCP server through a temporary Cloudflare HTTPS URL."""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from mcp_auth import owner_key
from mcp_server import ROOT, STATE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8777)
    parser.add_argument('--stop', action='store_true', help='Stop the running temporary MCP and tunnel')
    args = parser.parse_args()
    if args.stop:
        info = STATE / 'connection.json'
        if not info.exists():
            print('No running connection recorded')
            return
        pid = int(json.loads(info.read_text())['pid'])
        process = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
        started = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True).stdout.strip()
        recorded = json.loads(info.read_text()).get('started_at')
        if process.returncode or 'scripts/start_mcp.py' not in process.stdout or not recorded or recorded != started:
            raise RuntimeError('Recorded process no longer matches; inspect connection.json locally')
        os.kill(pid, signal.SIGTERM)
        print('Stop requested')
        return
    # Refuse to tunnel another service already bound to the intended port.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', args.port))
    owner_key(STATE)
    messages = queue.Queue()
    children = []
    log = (STATE / 'tunnel.log').open('w')

    def cleanup(*_):
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
        log.close()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        tunnel = subprocess.Popen(['cloudflared', 'tunnel', '--url', f'http://127.0.0.1:{args.port}',
            '--no-autoupdate', '--protocol', 'http2'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        children.append(tunnel)

        def read_tunnel():
            for line in tunnel.stdout:
                log.write(line)
                log.flush()
                found = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if found:
                    messages.put(found.group())

        threading.Thread(target=read_tunnel, daemon=True).start()
        origin = messages.get(timeout=45)
        server_log = (STATE / 'server.log').open('w')
        server = subprocess.Popen([sys.executable, str(ROOT / 'scripts/mcp_server.py'), '--public-url', origin,
            '--port', str(args.port)], cwd=ROOT, stdout=server_log, stderr=subprocess.STDOUT)
        children.append(server)
        import urllib.request
        for _ in range(50):
            if server.poll() is not None:
                raise RuntimeError('Server failed to start; inspect server.log')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/health', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError('Server did not become ready')
        connection = {'url': origin + '/mcp', 'authentication': 'OAuth', 'pid': os.getpid(), 'port': args.port}
        connection['started_at'] = subprocess.check_output(['ps', '-p', str(os.getpid()), '-o', 'lstart='], text=True).strip()
        (STATE / 'connection.json').write_text(json.dumps(connection, indent=2) + '\n')
        print(json.dumps(connection), flush=True)
        print(f'Owner key file (never commit or send to chat): {STATE / "owner-key.txt"}', flush=True)
        print('Keep this process and Mac running. Ctrl-C stops both server and tunnel.', flush=True)
        while tunnel.poll() is None and server.poll() is None:
            time.sleep(1)
        raise RuntimeError('Tunnel or MCP server stopped; restart to get a new URL')
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == '__main__':
    main()
