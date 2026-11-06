"""Create a separate v1.2-compatible data copy without discarding any new history.

Never overwrites the source or deploys. Use only after normal shutdown/queue drain.
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path


def adapt(value):
    if isinstance(value, list):
        for item in value: adapt(item)
    elif isinstance(value, dict):
        if {'id', 'order_no', 'gift', 'gift_ids'} <= value.keys():
            value.setdefault('change_address', False)
            for key in ('recipient_name', 'phone', 'address'): value.setdefault(key, '')
        for item in value.values(): adapt(item)


def prepare(source, destination):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError('回滚输出必须是源数据外的全新目录，不能覆盖已有文件')
    for path in [source, destination, *destination.parents, *source.rglob('*')]:
        if path.is_symlink() or path.is_junction(): raise ValueError('回滚复制禁止链接/junction')
    if not source.is_dir() or (source / 'server.pid').exists():
        raise ValueError('源数据不存在或工作台仍有运行标记；请正常停机并排空队列后再准备')
    for root in (Path(r'D:\UserData\Desktop\NL'), Path(r'E:\gifts')):
        if destination == root or destination.is_relative_to(root): raise ValueError('本工具不部署或写入生产目录/U 盘')
    before = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in source.rglob('*') if p.is_file()}
    shutil.copytree(source, destination)
    changed = []
    signatures = {}
    documents = {}
    # Only row-bearing JSON files are adapted in the COPY. Evidence/identities remain intact.
    for path in destination.rglob('*.json'):
        document = json.loads(path.read_text(encoding='utf-8'))
        documents[path] = document
        original = json.dumps(document, ensure_ascii=False, sort_keys=True)
        adapt(document)
        for index, row in enumerate(document.get('rows', []) if isinstance(document, dict) else []):
            stable = json.dumps([row['id'], row['order_no'].strip(), sorted(set(row.get('gift_ids', []))),
                                 row.get('change_address', False), row.get('gift_unmatched', [])], ensure_ascii=False, separators=(',', ':'))
            legacy = json.dumps([row['id'], row['order_no'], row['gift'], row.get('gift_ids', []), row.get('change_address', False),
                                 row.get('recipient_name', ''), row.get('phone', ''), row.get('address', '')], ensure_ascii=False, separators=(',', ':'))
            # Match v1.2's latest display binding, independent of filesystem enumeration order.
            key = hashlib.sha256(stable.encode()).hexdigest()
            items = document.get('items', [])
            item = items[index] if index < len(items) else {}
            priority = ('registration_jobs' in path.parts,
                        item.get('display_updated_at', item.get('updated_at', document.get('created_at', ''))),
                        path.as_posix())
            if key not in signatures or priority >= signatures[key][0]:
                signatures[key] = (priority, hashlib.sha256(legacy.encode()).hexdigest())
        if json.dumps(document, ensure_ascii=False, sort_keys=True) != original:
            path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
            changed.append(path.relative_to(destination).as_posix())
    manual = destination / 'manual_status.json'
    if manual in documents:
        document = documents[manual]
        converted = False
        for mark in [*document.get('events', []), *document.get('marks', {}).values()]:
            if mark.get('signature') in signatures:
                old = mark['signature']
                mark.update(v130_signature=old, signature=signatures[old][1]); converted = True
        if converted:
            manual.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
            if 'manual_status.json' not in changed: changed.append('manual_status.json')
    after_source = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in source.rglob('*') if p.is_file()}
    if before != after_source:
        raise ValueError('复制期间源数据发生变化，输出不可用于回滚；请重新停机备份后生成')
    manifest = {'source': str(source), 'source_sha256': before, 'adapted_files': changed,
                'preserved': 'all files, execution IDs, task keys, attempts, submission flags, evidence, manual marks and reviews',
                'deployment_performed': False}
    (destination.parent / (destination.name + '-manifest.json')).write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True); parser.add_argument('--out', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.out), ensure_ascii=False, indent=2))
