"""Read-only production audit. Never imports the production application or opens a queue."""
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(r'D:\UserData\Desktop\NL')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    paths = subprocess.check_output(['git', '-C', str(SOURCE), 'ls-files', '-z']).decode().split('\0')
    paths = [p for p in paths if p] + ['config/gift_groups.local.json']
    hashes = {p: digest(SOURCE / p) for p in paths if (SOURCE / p).is_file()}
    evidence = []
    counts = Counter()
    for path in sorted((SOURCE / 'data/registration_jobs').glob('*.json')):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            counts['unreadable'] += 1
            continue
        for row, item in zip(job.get('rows', []), job.get('items', [])):
            counts[item.get('state', 'unknown')] += 1
            raw = item.get('evidence', {})
            receipt = raw.get('receipt', {}) or {}
            if item.get('state') != 'failed' and not item.get('review_result'):
                continue
            # Pseudonyms allow timeline correlation without exposing orders/recipients.
            key = json.dumps([row.get('order_no'), sorted(row.get('gift_ids', []))], ensure_ascii=False)
            evidence.append({
                'task_alias': hashlib.sha256(key.encode()).hexdigest()[:12],
                'execution_alias': hashlib.sha256(str(item.get('execution_id', '')).encode()).hexdigest()[:12],
                'created_at': job.get('created_at'), 'updated_at': item.get('updated_at'),
                'state': item.get('state'), 'submission_status': item.get('submission_status'),
                'stage_details': [e.get('detail', '') for e in item.get('events', [])],
                'events': [{'at': e.get('at'), 'state': e.get('state')} for e in item.get('events', [])],
                'baseline_count': len(raw.get('before_snapshot', {}).get('records', [])),
                'submit_clicked': raw.get('submit_clicked') is True,
                'receipt_accepted': receipt.get('accepted'),
                'receipt_id_count': len(receipt.get('created_record_ids', [])),
                'receipt_reference_present': bool(receipt.get('reference')),
                'verified_count': len(raw.get('verified_record_ids', [])),
                'review_success': item.get('review_result', {}).get('success'),
            })
    output = ROOT / '.tmp/baseline'
    output.mkdir(parents=True, exist_ok=True)
    manifest = {'source': str(SOURCE), 'commit': subprocess.check_output(
        ['git', '-C', str(SOURCE), 'rev-parse', 'HEAD']).decode().strip(), 'sha256': hashes}
    (output / 'source-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (output / 'status-audit.json').write_text(json.dumps({'counts': counts, 'cases': evidence}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'counts': counts, 'case_count': len(evidence), 'files_hashed': len(hashes)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
