"""从 gifts-分组名.txt 增量更新礼品：仅按班牛商品列表第一行名称；默认预览，确认后才写入。"""
import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gift_registry.catalog_import import plan_txt_import, apply_txt_plan
from gift_registry.catalog_maintenance import BoundCatalog, encoded


def _print_grouped(entries, label):
    """按原因合并显示，避免 191 个编码逐行刷屏；报告里仍有逐项明细。"""
    if not entries:
        return
    groups = {}
    for entry in entries:
        reason = (entry.get('reason') or '').strip() or '名称未核实'
        groups.setdefault(reason, []).append(str(entry.get('code', '')))
    for reason, codes in groups.items():
        shown = '、'.join(codes[:8]) + (' 等' if len(codes) > 8 else '')
        print('%s（%d 项）：%s' % (label, len(codes), reason))
        print('  涉及编码：' + shown)


def _print_summary(result):
    print('编码 %d 个；目录现有 %d 项。' % (result.get('code_count', 0),
                                          result.get('catalog_count_before', 0)))
    verified = result.get('verified_names') or {}
    for code in result.get('added_codes') or []:
        print('新增 %s：%s' % (code, verified.get(code, '')))
    for entry in result.get('diff') or []:
        before, after = entry.get('before') or {}, entry.get('after') or {}
        parts = []
        if before.get('name') != after.get('name'):
            parts.append('名称 "%s" -> "%s"' % (before.get('name', ''), after.get('name', '')))
        if entry.get('groups_before') != entry.get('groups_after'):
            parts.append('分组 %s -> %s' % ('、'.join(entry.get('groups_before') or []) or '无',
                                            '、'.join(entry.get('groups_after') or []) or '无'))
        if parts:
            print('更新 %s：%s' % (entry['code'], '；'.join(parts)))
    _print_grouped(result.get('name_pending'), '名称待核实（保留原名，仅更新分组）')
    _print_grouped(result.get('pending'), '阻止应用')
    if not result.get('diff') and not result.get('added_codes'):
        print('配置已是最新，无需写入。')


def _apply(tool, result, report_path):
    result['application'] = apply_txt_plan(tool, result)
    result['dry_run'] = False
    tool._write(report_path, encoded(result))
    if result.get('diff') or result.get('added_codes'):
        print('已写入 config/gifts.json 与 config/gift_groups.local.json。')
    else:
        print('配置已是最新；本次仅记录核实报告，未改动配置文件。')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='*', type=Path, help='默认读取本项目 config/gifts-*.txt')
    parser.add_argument('--apply', action='store_true', help='核实成功后备份并应用整批变更')
    parser.add_argument('--interactive', action='store_true', help='预览差异后，主机输入 Y 才写入')
    parser.add_argument('--offline', action='store_true', help='仅为已有编码添加分组，不刷新名称、不新增礼品')
    parser.add_argument('--headless', action='store_true', help='隐藏查询浏览器；验证码需使用 login.bat 处理')
    parser.add_argument('--login-wait', type=int, default=300, metavar='SECONDS',
                        help='班牛未登录时等待人工登录的秒数，0 表示不等待（默认 300，仅可见浏览器）')
    args = parser.parse_args(argv)
    if args.apply and args.interactive:
        parser.error('--apply 与 --interactive 不能同时使用')
    reader = None
    if not args.offline:
        from gift_registry.product_reader import SeleniumProductReader
        # 按主机要求只核对班牛平台：以每个编码商品列表第一行的商品名称作为目录名称。
        reader = SeleniumProductReader(headless=args.headless, platforms=('banniu',),
            login_wait_seconds=0 if args.headless else args.login_wait,
            progress=lambda message: print(message, file=sys.stderr, flush=True))
    tool = BoundCatalog(ROOT, reader=reader)
    paths = args.files or sorted((ROOT / 'config').glob('gifts-*.txt'))
    report_path = tool.work / 'reports' / ('txt-' + uuid.uuid4().hex + '.json')
    try:
        result = plan_txt_import(tool, paths, offline=args.offline)
        # 完整差异先落盘；写入层仍检查计划摘要、原文件摘要与 revision。
        tool._write(report_path, encoded(result))
        _print_summary(result)
        print('报告：' + str(report_path))
        if not result['applicable']:
            print('存在新编码未取得班牛商品名称，未写入任何配置；请查看报告 pending。', file=sys.stderr)
            return 2
        if args.apply:
            _apply(tool, result, report_path)
            return 0
        if args.interactive:
            try:
                answer = input('确认按以上差异写入 config/gifts.json 与 config/gift_groups.local.json？输入 Y 确认：').strip().lower()
            except EOFError:
                answer = ''
            if answer not in ('y', 'yes', '是'):
                print('已取消，未写入任何配置。')
                return 3
            _apply(tool, result, report_path)
            return 0
        print('预览完成，未写入；加 --apply 或 --interactive 才应用。')
        return 0
    except (ValueError, OSError, UnicodeError) as exc:
        # 不打印 WebDriver 或认证异常原文，避免会话信息进入日志。
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else '输入文件、配置或工作目录不可用，请检查 UTF-8 编码和文件权限'
        tool._write(report_path, encoded({'applicable': False, 'error': message}))
        print(message + '\n报告：' + str(report_path), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
