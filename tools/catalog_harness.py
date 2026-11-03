"""Standalone JSON CLI / restricted JSONL tool route bound to this checkout."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gift_registry.catalog_maintenance import BoundCatalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdio', action='store_true', help='One JSON request/reply per line; no shell/Python/file-write tools')
    parser.add_argument('--approve', metavar='PLAN_ID', help='Host-only explicit approval after reviewing the exact plan')
    args = parser.parse_args()
    service = BoundCatalog(ROOT)
    if args.approve:
        print(json.dumps(service.approve(args.approve), ensure_ascii=False)); return
    requests = sys.stdin if args.stdio else [sys.stdin.read()]
    for raw in requests:
        try:
            if len(raw) > 2_000_000: raise ValueError('请求过大')
            result = {'ok': True, 'result': service.handle(json.loads(raw))}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            # Never expose platform exceptions/URLs or raw inputs to model logs.
            result = {'ok': False, 'error': str(exc) if isinstance(exc, ValueError) else '请求、配置或维护工作目录无效，请主机核查'}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
