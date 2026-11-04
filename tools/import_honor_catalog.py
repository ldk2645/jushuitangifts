"""从用户提供的 Markdown 礼品清单导入目录；不导入 example 中的订单。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gift_registry.gift_catalog import validate_gift
from gift_registry.sessions import atomic_write


def parse_catalog(text):
    active, found, gifts, seen = False, False, [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower() == 'gifts list:':
            active = found = True
            continue
        if line.lower() == 'example:':
            break
        if not active or not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError('礼品行必须包含完整编码和名称')
        gift = validate_gift({'id': parts[0], 'name': parts[1], 'aliases': []})
        if gift['id'] in seen:
            raise ValueError('清单有重复编码，未导入')
        seen.add(gift['id'])
        gifts.append(gift)
    if not found or not gifts:
        raise ValueError('未找到 gifts list 礼品清单')
    return gifts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'config' / 'gifts.json')
    args = parser.parse_args()
    gifts = parse_catalog(args.source.read_text(encoding='utf-8-sig'))
    atomic_write(args.output, json.dumps(gifts, ensure_ascii=False, indent=2).encode('utf-8'))
    print(json.dumps({'gift_count': len(gifts), 'unique_codes': len({g['id'] for g in gifts}),
                      'imported_orders': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
