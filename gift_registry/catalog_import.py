"""将 gifts-分组名.txt 合并进现有目录：仅按班牛商品列表第一行名称更新，复用受限维护器的事务写入。"""
import re
from pathlib import Path

from .catalog_maintenance import FILES, normalize_name
from .catalog_transaction import catalog_access, sha256
from .gift_catalog import validate_gift

PLATFORMS = ['banniu']  # 主机要求：目录名称只以班牛商品列表第一行的商品名称为准。


def _display_name(existing, observed, key):
    """展示名称：保留已存在的等价名称，否则优先聚水潭原文，再取空白最少的平台原文。"""
    if isinstance(existing, dict) and normalize_name(existing.get('name', '')) == key:
        return existing['name']
    for platform in ('jushuitan', 'banniu'):
        raws = observed.get(platform, {}).get(key, set())
        if raws:
            return min(raws, key=lambda raw: (sum(character.isspace() for character in raw), raw))
    return key  # 调用方已确认所有要求平台都有唯一名称，正常不可达。


def read_code_file(path):
    path = Path(path)
    match = re.fullmatch(r'gifts-(.+)\.txt', path.name, re.IGNORECASE)
    if not match:
        raise ValueError('文件名必须为 gifts-分组名.txt')
    group = match[1]
    if group != group.strip() or not 0 < len(group) <= 40 or any(ord(c) < 32 for c in group):
        raise ValueError('文件名中的分组名称必须为 1 至 40 个字符，不能有首尾空白')
    if path.stat().st_size > 2_000_000:
        raise ValueError('TXT 文件过大')
    codes, seen, duplicates = [], set(), []
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        code = line.strip()
        if not code:
            continue
        if any(c.isspace() for c in code):
            raise ValueError(f'第 {number} 行应只包含一个完整商品编码，不能附带名称或空格')
        validate_gift({'id': code, 'name': '编码格式校验'})
        if code in seen:
            duplicates.append({'line': number, 'code': code})
        else:
            seen.add(code)
            codes.append(code)
    if not codes or len(codes) > 10000:
        raise ValueError('TXT 必须包含 1 至 10000 个完整商品编码')
    return {'file': path.name, 'group': group, 'codes': codes, 'duplicates': duplicates}


def plan_txt_import(tool, paths, *, offline=False):
    """只规划；新编码未核实会阻止整批，已有编码未核实名称时保留原名、仅更新分组。"""
    sources = [read_code_file(path) for path in paths]
    if not sources:
        raise ValueError('未找到 config/gifts-*.txt')
    codes = list(dict.fromkeys(code for source in sources for code in source['codes']))
    tool._codes(codes)
    with catalog_access(tool.config):
        raw, docs = tool._read_pair()
    before = {name: sha256(raw[name]) for name in FILES}
    catalog, groups = docs[FILES[0]], docs[FILES[1]]
    old = {gift['id']: gift for gift in catalog}
    evidence_ids, names, pending, name_pending = [], {}, [], []
    if offline:
        for code in codes:
            if code not in old:
                pending.append({'code': code, 'reason': '离线模式无法取得新编码名称，请使用默认联网更新'})
            else:
                names[code] = old[code]['name']
    else:
        evidence_ids = [tool.query(codes)['evidence_id']]
        for code in codes:
            observed, issues = tool._verified_names(code, evidence_ids, PLATFORMS)
            unique = {key for values in observed.values() for key in values}
            if issues or set(observed) != set(PLATFORMS) or len(unique) != 1:
                item = {'code': code, 'reason': '班牛未取得该编码第一行商品名称',
                        'observed': {platform: sorted({raw_name for raw_names in values.values()
                                                       for raw_name in raw_names})
                                     for platform, values in observed.items()}}
                if code in old:
                    name_pending.append(item)  # 已有编码只更新分组，保留原名等待人工核实。
                else:
                    pending.append({**item, 'reason': item['reason'] + '；新编码无名称不新增'})
                continue
            names[code] = _display_name(old.get(code), observed, unique.pop())
    summary = {'sources': sources, 'code_count': len(codes), 'catalog_count_before': len(catalog),
               'offline': offline, 'evidence_ids': evidence_ids, 'pending': pending,
               'name_pending': name_pending, 'verified_names': names,
               'applicable': False, 'dry_run': True, 'before_sha256': before}
    if pending:
        return summary
    by_name = {group['name']: group['id'] for group in groups['groups']}
    creates, assignments = [], {code: [] for code in codes}
    for source in sources:
        name = source['group']
        if name not in by_name:
            identifier = 'txt-' + sha256(name.encode('utf-8'))[:20]
            if identifier in {group['id'] for group in groups['groups']}:
                raise ValueError('新分组 ID 冲突，请在主机分组管理中先创建该名称')
            by_name[name] = identifier
            creates.append({'id': identifier, 'name': name})
        for code in source['codes']:
            if by_name[name] not in assignments[code]:
                assignments[code].append(by_name[name])
    added = [code for code in codes if code not in old]
    changes = []
    for code in codes:
        change = {'code': code, 'groups': {'mode': 'add', 'ids': assignments[code]}}
        if code in old and names.get(code) and names[code] != old[code]['name']:
            change['name'] = names[code]
        changes.append(change)
    request = {
        'platforms': PLATFORMS,
        'evidence_ids': evidence_ids, 'create_groups': creates, 'allow_add_codes': added,
        'add_items': [{'id': code, 'name': names[code], 'aliases': []} for code in added],
        'changes': changes,
    }
    # 网络查询可能持续数分钟；期间发生目录编辑时不能用旧快照覆盖新的决定。
    with catalog_access(tool.config):
        latest, _ = tool._read_pair()
        if {name: sha256(latest[name]) for name in FILES} != before:
            raise ValueError('商品查询期间配置已变化，请重新运行更新')
        plan = tool.plan(request)
    return {**summary, **plan, 'added_codes': added,
            'catalog_count_after': len(catalog) + len(added),
            'changed_codes': [entry['code'] for entry in plan['diff']]}


def apply_txt_plan(tool, result):
    if not result['applicable']:
        raise ValueError('存在新编码未取得班牛商品名称，未更新任何配置；请查看报告中的 pending')
    tool.approve(result['plan_id'])
    return tool.apply(result['plan_id'])
