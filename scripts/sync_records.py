"""Inspect/export local records without exposing credentials or financial contents."""
import argparse
import json
from pathlib import Path
import tempfile

from mcp_store import Store
from task_sync import TaskSync
from validate import validate

ROOT = Path(__file__).resolve().parent.parent
STATE = Path.home() / 'Library/Application Support/personal-knowledge-mcp'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['sync', 'status', 'check', 'tasks-connect-local'])
    args = parser.parse_args()
    store = Store(ROOT, STATE, auto_sync=False)
    if args.command == 'tasks-connect-local':
        path = STATE / 'ai-tasks-config.json'
        path.write_text(json.dumps({'origin': 'http://127.0.0.1:8787', 'allow_local': True}) + '\n')
        path.chmod(0o600)
        print('AI Tasksのローカル接続先を設定しました。サーバーを再起動してください。')
    elif args.command == 'sync':
        print(json.dumps({'git': store.sync(), 'tasks': TaskSync(store).sync()}, ensure_ascii=False))
    elif args.command == 'check':
        with tempfile.TemporaryDirectory() as directory:
            store.materialize(Path(directory))
            count, errors = validate(Path(directory))
        if errors:
            parser.exit(1, '\n'.join(errors)+'\n')
        print(f'OK: {count} records including locally saved pending changes')
    else:
        print(json.dumps({'git': store.sync_status(), 'tasks': TaskSync(store).status()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
