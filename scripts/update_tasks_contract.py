"""Explicitly vendor the upstream generated SDK; --check detects cross-repo drift."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', required=True, type=Path, help='personal-ops/ai-tasks checkout')
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
source = args.source / 'api/python/ai_tasks_contract'
target = Path(__file__).resolve().parent / 'vendor/ai_tasks_contract'
files = ('__init__.py', 'runtime.py', 'contract.json')
for name in files:
    content = (source / name).read_bytes()
    if args.check:
        if not (target / name).is_file() or (target / name).read_bytes() != content:
            raise SystemExit('AI Tasks SDK drift: run scripts/update_tasks_contract.py --source PATH')
    else:
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(content)
print('AI Tasks SDK matches upstream' if args.check else 'AI Tasks SDK updated')
