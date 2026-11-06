"""Client-declared input scope and server-owned row selection snapshots.

Group preferences never enter task identity. Unchanged selections survive catalog
edits; newly added codes require the exact catalog/group generation seen by the
client. Copied row IDs cannot inherit another row's selection authority.
"""
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from .catalog_transaction import catalog_access


class SelectionConflict(ValueError):
    pass


def catalog_version(gifts):
    return hashlib.sha256(json.dumps(gifts, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def label_matches(row, gifts):
    """Read legacy display text without fuzzy matching or guessing another code."""
    by_code = {g['id']: g for g in gifts}
    remaining = {row.get('gift', '')}
    for index, code in enumerate(row.get('gift_ids', [])):
        gift = by_code.get(code, {})
        labels = {code, gift.get('name', ''), *gift.get('aliases', [])} - {''}
        prefixes = {(('、' if index else '') + label) for label in labels}
        remaining = {text[len(prefix):] for text in remaining for prefix in prefixes if text.startswith(prefix)}
    return '' in remaining and bool(row.get('gift_ids'))


def migrate_legacy_selections(rows, gifts, jobs):
    """In-memory compatibility only. No historical file or display-name snapshot is rewritten."""
    unresolved = [row for row in rows if 'gift_selection' not in row]
    proven = set()
    for path in jobs._paths() if unresolved else []:
        job = jobs._load(path)
        for old in job['rows']:
            if label_matches(old, job.get('gift_catalog', [])):
                proven.add((old.get('order_no', '').strip(), tuple(sorted(set(old.get('gift_ids', [])))), old.get('gift', '')))
    for row in unresolved:
        key = (row['order_no'].strip(), tuple(sorted(set(row['gift_ids']))), row['gift'])
        confirmed = label_matches(row, gifts) or key in proven
        row['gift_unmatched'] = [] if confirmed or not row['gift'].strip() else [row['gift']]
        row['gift_selection'] = {'source': 'legacy', 'gift_ids': list(row['gift_ids']),
                                 'label': row['gift'], 'confirmed': confirmed}


class SelectionPolicy:
    def __init__(self, catalog, groups):
        self.catalog, self.groups = catalog, groups

    def context(self):
        with catalog_access(self.catalog.path.parent):
            gifts, groups = self.catalog.read(), self.groups.read()
        return gifts, groups, catalog_version(gifts)

    def validate(self, rows, previous):
        gifts, groups, version = self.context()
        known = {g['id']: g for g in gifts}
        group_ids = {g['id'] for g in groups['groups']}
        old_rows = {r['id']: r for r in previous}
        for row in rows:
            old = old_rows.get(row['id'], {})
            ids = row['gift_ids']
            old_ids = old.get('gift_ids', [])
            added = set(ids) - set(old_ids)
            # Snapshot is owned by the server: no client can forge a grandfathered selection.
            if set(ids) == set(old_ids) and old:
                row['gift_selection'] = deepcopy(old.get('gift_selection', {
                    'source': 'legacy', 'gift_ids': old_ids, 'label': old.get('gift', '')}))
                row['gift_selection']['gift_ids'] = list(ids)
                continue
            scope = row.get('selection_scope')
            if ids and (added or not old):
                if not isinstance(scope, dict):
                    raise SelectionConflict('新增或更改礼品需要声明当前礼品分组，请刷新后重新选择')
                if (set(scope) != {'group_id', 'group_revision', 'catalog_version'}
                        or type(scope.get('group_revision')) is not int
                        or not isinstance(scope.get('catalog_version'), str)):
                    raise SelectionConflict('礼品分组声明格式无效')
                group = scope.get('group_id')
                if not isinstance(group, str) or group not in {'', '__ungrouped', *group_ids}:
                    raise SelectionConflict('所选礼品分组已失效，请重新选择；已有订单保留')
                if scope.get('catalog_version') != version or scope.get('group_revision') != groups['revision']:
                    raise SelectionConflict('目录或分组已更新，请刷新并重新选择礼品分组')
                for code in added if old else ids:
                    memberships = groups['memberships'].get(code, [])
                    if code not in known or (group and not (not memberships if group == '__ungrouped' else group in memberships)):
                        raise SelectionConflict('当前分组未找到，请切换分组；本次礼品选择未保存')
            prior = deepcopy(old.get('gift_selection', {}))
            origins = {code: deepcopy(prior.get('origins', {}).get(code, {'source': 'legacy'})) for code in ids if code in old_ids}
            for code in added:
                origins[code] = {**deepcopy(scope or {}), 'selected_at': datetime.now(timezone.utc).isoformat()}
            entries = prior.get('events', [])
            if scope and added:
                entries = [*entries, {'gift_ids': sorted(added), **deepcopy(scope)}][-100:]
            row['gift_selection'] = {'source': 'confirmed', 'gift_ids': list(ids),
                'label': '、'.join(known[i]['name'] for i in ids if i in known), 'events': entries, 'origins': origins}
        return rows
