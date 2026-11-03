"""Bound, non-executing catalog maintenance protocol. No registration imports.

The model-facing route accepts JSON operations only. Root/work directories and
the platform adapter are supplied by the trusted launcher, never by JSON input.
"""
import base64
import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .catalog_transaction import catalog_access, state_directory, _no_links, sha256
from .gift_catalog import validate_gift

FILES = ('gifts.json', 'gift_groups.local.json')
PROTECTED = (Path(r'D:\UserData\Desktop\NL'), Path(r'E:\gifts'))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class UnconfiguredProductReader:
    """Explicit capability gap; selectors/URLs are not guessed from order pages."""
    def query(self, codes, profile_directory):
        return [{'platform': platform, 'queried_code': code, 'status': 'blocked',
                 'observed_at': timestamp(), 'matches': [],
                 'reason': '需要独立 Selenium 登录并核对实际商品查询入口、完整编码搜索及结果列；未配置真实查询适配器'}
                for code in codes for platform in ('banniu', 'jushuitan')]


def validate_pair(catalog, groups):
    if not isinstance(catalog, list) or not 0 < len(catalog) <= 10000:
        raise ValueError('目录必须为 1 至 10000 项的列表')
    codes = set()
    for gift in catalog:
        stored = gift
        if isinstance(gift, dict) and isinstance(gift.get('aliases'), list):
            stored = {**gift, 'aliases': [a for a in gift['aliases'] if not (isinstance(a, str) and not a.strip())]}
        validated = validate_gift(stored)
        if validated['id'] != gift['id'] or validated['name'] != gift['name'] or gift['id'] in codes:
            raise ValueError('商品编码必须唯一且保持原字符串，名称不能有首尾空白')
        codes.add(gift['id'])
    if (not isinstance(groups, dict) or groups.get('version') != 1
            or type(groups.get('revision')) is not int or groups['revision'] < 0
            or not isinstance(groups.get('groups'), list) or len(groups['groups']) > 100
            or not isinstance(groups.get('memberships'), dict)):
        raise ValueError('分组 schema 无效')
    ids, names = set(), set()
    for group in groups['groups']:
        if (not isinstance(group, dict) or not isinstance(group.get('id'), str) or not group['id']
                or len(group['id']) > 80 or group['id'] in ids or not isinstance(group.get('name'), str)
                or not 0 < len(group['name'].strip()) <= 40 or group['name'] in names):
            raise ValueError('分组 ID 或名称无效/重复')
        ids.add(group['id']); names.add(group['name'])
    for code, members in groups['memberships'].items():
        if code not in codes:
            raise ValueError('分组引用了目录中不存在的编码：' + code + '；保留原配置，请主机核查目录/分组版本')
        if (not isinstance(members, list) or len(set(members)) != len(members)
                or any(member not in ids for member in members)):
            raise ValueError('分组交叉引用无效')


class BoundCatalog:
    def __init__(self, root, *, reader=None):
        self.root = _no_links(Path(root)).resolve()
        for protected in PROTECTED:
            if self.root == protected.resolve() or self.root.is_relative_to(protected.resolve()):
                raise ValueError('开发阶段禁止以生产目录或 U 盘为维护目标')
        self.config = _no_links(self.root / 'config')
        self.work = _no_links(state_directory(self.config))
        self.reader = reader or UnconfiguredProductReader()
        self._paths()  # Resolve and inspect before creating any work files.

    def _paths(self):
        targets = {name: _no_links(self.config / name) for name in FILES}
        if any(path.resolve().parent != self.config.resolve() for path in targets.values()):
            raise ValueError('目标路径逃逸')
        return targets

    def _work_file(self, kind, identifier):
        if not re.fullmatch(r'[a-f0-9]{64}', identifier):
            raise ValueError('计划或证据 ID 无效')
        return _no_links(self.work / kind / (identifier + '.json'))

    def _write(self, path, content):
        _no_links(path)
        self.work.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.work / ('swap-' + uuid.uuid4().hex)
        try:
            with temporary.open('xb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_pair(self):
        raw = {name: path.read_bytes() for name, path in self._paths().items()}
        docs = {name: json.loads(value) for name, value in raw.items()}
        validate_pair(docs[FILES[0]], docs[FILES[1]])
        return raw, docs

    @staticmethod
    def _codes(value):
        if (not isinstance(value, list) or not value or len(value) > 10000
                or any(not isinstance(c, str) or not c or c != c.strip() or len(c) > 80 for c in value)
                or len(value) != len(set(value))):
            raise ValueError('需要明确、不重复的完整商品编码列表')
        return value

    def query(self, codes):
        codes = self._codes(codes)
        # Independent profile directory; no cookies, DPAPI or account files are loaded.
        observations = self.reader.query(codes, self.work / 'browser')
        document = {'version': 1, 'codes': codes, 'observations': observations, 'at': timestamp()}
        identifier = sha256(encoded(document))
        with catalog_access(self.config):
            self._write(self._work_file('evidence', identifier), encoded(document))
        return {'evidence_id': identifier, **document}

    def _verified_names(self, code, evidence_ids):
        observed = {}
        reasons = []
        for identifier in evidence_ids:
            raw = self._work_file('evidence', identifier).read_bytes()
            document = json.loads(raw)
            if sha256(encoded(document)) != identifier:
                raise ValueError('查询证据摘要不匹配')
            for entry in document['observations']:
                if entry.get('queried_code') != code:
                    continue
                platform = entry.get('platform')
                matches = entry.get('matches', [])
                if (platform not in ('banniu', 'jushuitan') or entry.get('status') != 'exact'
                        or not matches or any(m.get('code') != code or not isinstance(m.get('name'), str) or not m['name'] for m in matches)):
                    reasons.append('平台未找到、认证/页面不完整或编码不一致')
                    continue
                names = {m['name'] for m in matches}
                if len(names) != 1:
                    reasons.append('同编码结果名称冲突'); continue
                observed.setdefault(platform, set()).update(names)
        if any(len(names) != 1 for names in observed.values()):
            reasons.append('多次观察名称冲突')
        return observed, reasons

    def plan(self, request):
        changes = request.get('changes', [])
        additions = request.get('add_items', [])
        evidence_ids = request.get('evidence_ids', [])
        group_creates = request.get('create_groups', [])
        if any(not isinstance(value, list) for value in (changes, additions, evidence_ids, group_creates)):
            raise ValueError('变更、证据和新增项必须为列表')
        if len(changes) + len(additions) > 10000 or len(evidence_ids) > 100:
            raise ValueError('维护请求过大')
        with catalog_access(self.config):
            raw, docs = self._read_pair()
            candidate = deepcopy(docs)
            catalog, groups = candidate[FILES[0]], candidate[FILES[1]]
            by_code = {g['id']: g for g in catalog}
            pending = []
            explicit = request.get('allow_add_codes', [])
            if not isinstance(explicit, list) or set(explicit) != {g.get('id') for g in additions if isinstance(g, dict)}:
                raise ValueError('新增编码需通过 allow_add_codes 单独明确授权')
            for gift in additions:
                if not isinstance(gift, dict) or set(gift) - {'id', 'name', 'aliases'}:
                    raise ValueError('新增商品只接受 id/name/aliases')
                validate_gift(gift)
                if gift['id'] in by_code:
                    raise ValueError('新增编码已经存在')
                names, issues = self._verified_names(gift['id'], evidence_ids)
                if issues or set(names) != {'banniu', 'jushuitan'} or any(v != {gift['name']} for v in names.values()):
                    pending.append({'code': gift['id'], 'reason': '新增名称尚未由两平台精确核实'}); continue
                catalog.append(deepcopy(gift)); by_code[gift['id']] = catalog[-1]
            for group in group_creates:
                if not isinstance(group, dict) or set(group) != {'id', 'name'}:
                    raise ValueError('新分组必须显式给出 id/name')
                groups['groups'].append(deepcopy(group))
            seen = set()
            for change in changes:
                if not isinstance(change, dict) or set(change) - {'code', 'name', 'aliases_add', 'groups'}:
                    raise ValueError('变更含未授权字段；禁止路径、编码替换、删除或任意代码')
                code = change.get('code')
                if code not in by_code or code in seen:
                    raise ValueError('变更编码必须存在且不重复')
                seen.add(code)
                old = by_code[code]
                proposed = deepcopy(old)
                valid_aliases = [a for a in old.get('aliases', []) if not (isinstance(a, str) and not a.strip())]
                names, issues = self._verified_names(code, evidence_ids)
                if 'name' in change and change['name'] != old['name']:
                    if issues or set(names) != {'banniu', 'jushuitan'} or any(v != {change['name']} for v in names.values()):
                        pending.append({'code': code, 'reason': '两平台名称缺失或不一致，保留旧名称', 'observed': {p: sorted(n) for p, n in names.items()}})
                        continue
                    proposed['name'] = change['name']
                    proposed['aliases'] = list(dict.fromkeys([*valid_aliases, old['name']]))
                aliases = change.get('aliases_add', [])
                if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
                    raise ValueError('别名格式无效')
                verified = {name for values in names.values() for name in values} | {old['name'], *old.get('aliases', [])}
                if issues or any(alias not in verified for alias in aliases):
                    pending.append({'code': code, 'reason': '别名缺少精确平台核实依据，保留旧配置'}); continue
                if aliases:
                    proposed['aliases'] = list(dict.fromkeys([*valid_aliases, *proposed.get('aliases', []), *aliases]))
                    proposed['aliases'] = [a for a in proposed['aliases'] if a.strip()]
                # Historical blank aliases stay unchanged unless this item explicitly changes names/aliases.
                validate_gift({**proposed, 'aliases': [a for a in proposed.get('aliases', []) if a.strip()]})
                old.clear(); old.update(proposed)
                if 'groups' in change:
                    assignment = change['groups']
                    if not isinstance(assignment, dict) or set(assignment) != {'mode', 'ids'}:
                        raise ValueError('分组变更需明确 mode 和 ids')
                    ids = assignment['ids']; mode = assignment['mode']
                    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or mode not in ('add', 'remove', 'replace'):
                        raise ValueError('分组变更格式无效')
                    if any(i not in {g['id'] for g in groups['groups']} for i in ids):
                        raise ValueError('指定分组不存在')
                    previous = groups['memberships'].get(code, [])
                    updated = ([*previous, *ids] if mode == 'add' else
                               [i for i in previous if i not in ids] if mode == 'remove' else ids)
                    updated = list(dict.fromkeys(updated))
                    if updated: groups['memberships'][code] = updated
                    else: groups['memberships'].pop(code, None)
            if groups != docs[FILES[1]]:
                groups['revision'] += 1
            validate_pair(catalog, groups)
            # All-or-nothing plan: unresolved evidence cannot silently apply a subset.
            diff = []
            old_codes = {g['id']: g for g in docs[FILES[0]]}
            for gift in catalog:
                old = old_codes.get(gift['id'])
                old_members = docs[FILES[1]]['memberships'].get(gift['id'], [])
                new_members = groups['memberships'].get(gift['id'], [])
                if gift != old or old_members != new_members:
                    diff.append({'code': gift['id'], 'before': old, 'after': gift,
                                 'groups_before': old_members, 'groups_after': new_members})
            after = {name: (encoded(candidate[name]) if candidate[name] != docs[name] else raw[name]) for name in FILES}
            plan = {'version': 1, 'root': str(self.root), 'at': timestamp(),
                    'before': {n: sha256(raw[n]) for n in FILES},
                    'after': {n: sha256(after[n]) for n in FILES},
                    'contents': {n: base64.b64encode(after[n]).decode() for n in FILES},
                    'group_revision': docs[FILES[1]]['revision'], 'pending': pending, 'diff': diff,
                    'groups_before': docs[FILES[1]]['groups'], 'groups_after': groups['groups']}
            identifier = sha256(encoded(plan))
            self._write(self._work_file('plans', identifier), encoded(plan))
            return {'plan_id': identifier, 'dry_run': True, 'applicable': not pending,
                    'diff': diff, 'pending': pending, 'before_sha256': plan['before'], 'after_sha256': plan['after'],
                    'groups_before': plan['groups_before'], 'groups_after': plan['groups_after']}

    def _plan(self, identifier):
        plan = json.loads(self._work_file('plans', identifier).read_bytes())
        if sha256(encoded(plan)) != identifier or plan['root'] != str(self.root) or set(plan['contents']) != set(FILES):
            raise ValueError('计划内容、绑定目标或摘要已变化')
        if plan['pending']:
            raise ValueError('计划含待核实事项，禁止应用')
        docs = {n: json.loads(base64.b64decode(plan['contents'][n], validate=True)) for n in FILES}
        validate_pair(docs[FILES[0]], docs[FILES[1]])
        return plan

    def approve(self, identifier):
        """Host-only CLI entry; not exposed by handle()/stdio tool routes."""
        with catalog_access(self.config):
            plan = self._plan(identifier)
            self._write(self._work_file('approvals', identifier), encoded({'plan_id': identifier, 'after': plan['after'], 'at': timestamp()}))
        return {'approved_plan_id': identifier}

    def _replace_target(self, path, content):
        if path not in self._paths().values():
            raise ValueError('写入目标不在固定白名单')
        self._write(path, content)

    def _recover_locked(self):
        journal_path = self.work / 'transaction.json'
        if not journal_path.exists(): return {'recovered': False}
        journal = json.loads(journal_path.read_bytes())
        if journal.get('state') != 'pending': return {'recovered': False, 'state': journal.get('state')}
        targets = self._paths()
        if set(journal['backups']) != set(FILES): raise ValueError('恢复日志白名单无效')
        # Refuse to overwrite any out-of-band edit.
        for name, path in targets.items():
            if sha256(path.read_bytes()) not in (journal['before'][name], journal['after'][name]):
                raise ValueError('恢复时检测到外部修改，保持阻塞并等待主机核查')
        for name, path in targets.items():
            before = base64.b64decode(journal['backups'][name], validate=True)
            if sha256(before) != journal['before'][name]: raise ValueError('恢复备份摘要不匹配')
            self._replace_target(path, before)
        journal.update(state='rolled_back', recovered_at=timestamp())
        self._write(journal_path, encoded(journal))
        return {'recovered': True, 'state': 'rolled_back'}

    def recover(self):
        with catalog_access(self.config, recovery=True): return self._recover_locked()

    def apply(self, identifier):
        with catalog_access(self.config):
            plan = self._plan(identifier)
            approval_path = self._work_file('approvals', identifier)
            if not approval_path.exists(): raise ValueError('缺少主机对具体差异计划的批准；默认 dry-run')
            approval = json.loads(approval_path.read_bytes())
            if approval.get('plan_id') != identifier or approval.get('after') != plan['after']: raise ValueError('批准不匹配当前计划')
            raw, docs = self._read_pair()
            current = {n: sha256(raw[n]) for n in FILES}
            audit = self._work_file('applied', identifier)
            if audit.exists() and current == plan['after']:
                return {'applied': True, 'idempotent': True, 'plan_id': identifier}
            if current != plan['before'] or docs[FILES[1]]['revision'] != plan['group_revision']:
                raise ValueError('预览后文件摘要或分组 revision 已变化，请重新预览与审核')
            journal = {'state': 'pending', 'plan_id': identifier, 'before': current, 'after': plan['after'],
                       'backups': {n: base64.b64encode(raw[n]).decode() for n in FILES}, 'at': timestamp()}
            self._write(self.work / 'transaction.json', encoded(journal))
            try:
                for name, path in self._paths().items():
                    if plan['before'][name] != plan['after'][name]:
                        self._replace_target(path, base64.b64decode(plan['contents'][name], validate=True))
                self._write(audit, encoded({'plan_id': identifier, 'after': plan['after'], 'at': timestamp()}))
                journal.update(state='committed', completed_at=timestamp())
                self._write(self.work / 'transaction.json', encoded(journal))
            except Exception:
                self._recover_locked()
                raise ValueError('写入失败，已恢复原配置；请核查后重新预览') from None
            return {'applied': True, 'idempotent': False, 'plan_id': identifier}

    def handle(self, request):
        if not isinstance(request, dict): raise ValueError('JSON 输入必须为对象')
        operation = request.get('op', 'plan')
        allowed = {'query': {'op', 'codes'}, 'plan': {'op', 'changes', 'add_items', 'allow_add_codes', 'evidence_ids', 'create_groups'},
                   'apply': {'op', 'plan_id'}, 'recover': {'op'}, 'status': {'op'}}
        if operation not in allowed or set(request) - allowed[operation]:
            raise ValueError('不支持的操作/字段；不接受路径、脚本、命令或权限修改')
        if operation == 'query': return self.query(request.get('codes'))
        if operation == 'plan': return self.plan(request)
        if operation == 'apply': return self.apply(request.get('plan_id', ''))
        if operation == 'recover': return self.recover()
        with catalog_access(self.config):
            raw, docs = self._read_pair()
            return {'target': str(self.root), 'writable_configs': list(FILES), 'codes': [g['id'] for g in docs[FILES[0]]],
                    'sha256': {n: sha256(raw[n]) for n in FILES}, 'group_revision': docs[FILES[1]]['revision'],
                    'platform_reader': type(self.reader).__name__}
