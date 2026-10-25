"""隔离 Chrome 验证班牛列表 DOM 读取；仅加载本地合成 HTML，不访问真实平台。"""

import sys
import tempfile
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gift_registry.banniu_receipt import _LIST, complete_task_rows
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site


ORDER_NO = "00012345678901234567890"
ROW = {"task_id": "000321", "order_no": ORDER_NO, "created_at": "2026-01-01 00:00:00"}
EMPTY_TEXT = "呀~暂时还没有内容呢~"


def table(*, empty=False, rows=(), message=None, message_style="", table_style=""):
    records = "".join(
        '<tr rowid="{}"><td colid="order"><span class="worksheetColumn">{}</span></td>'
        '<td colid="created"><span class="worksheetColumn">{}</span></td></tr>'.format(
            escape(row["task_id"]), escape(row["order_no"]), escape(row["created_at"]))
        for row in rows
    )
    empty_message = (f'<div class="w-vxeTable-empty" style="{message_style}">{escape(message)}</div>'
                     if message is not None else "")
    return (
        f'<div class="vxe-table{" is--empty" if empty else ""}" style="{table_style}">'
        '<table class="vxe-table--header"><thead><tr>'
        '<th colid="order">订单号</th><th colid="created">创建时间</th>'
        '</tr></thead></table>'
        f'<table class="vxe-table--body"><tbody>{records}</tbody></table>{empty_message}</div>'
    )


def pager(count, style=""):
    return f'<div class="el-pagination__total" style="{style}">共 {count} 条</div>'


def main():
    normal = table(rows=(ROW,)) + pager(1)
    explicit_empty = table(empty=True, message=EMPTY_TEXT)
    empty_result = {"pager": "共 0 条", "rows": []}
    cases = [
        ("normal-record", normal, {"pager": "共 1 条", "rows": [ROW]}),
        ("explicit-empty-without-pager", explicit_empty, empty_result),
        ("explicit-empty-with-hidden-pager", explicit_empty + pager(0, "display:none"), empty_result),
        ("missing-pager-on-nonempty-table", table(rows=(ROW,)), None),
        ("empty-text-mismatch", table(empty=True, message="加载失败"), None),
        ("empty-marker-with-existing-row", table(empty=True, rows=(ROW,), message=EMPTY_TEXT), None),
        ("empty-without-empty-class", table(message=EMPTY_TEXT), None),
        ("hidden-empty-message", table(empty=True, message=EMPTY_TEXT, message_style="display:none"), None),
        ("empty-while-loading", explicit_empty + '<div class="el-loading-mask">加载中</div>', None),
        ("normal-while-loading", normal + '<div class="el-loading-mask">加载中</div>', None),
        ("multiple-visible-tables", explicit_empty + table(empty=True, message=EMPTY_TEXT), None),
        ("hidden-other-table", explicit_empty + table(rows=(ROW,), table_style="display:none"), empty_result),
    ]
    temporary_root = ROOT / ".tmp"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="banniu-list-test-", dir=temporary_root) as directory:
        folder = Path(directory)
        html_path = folder / "fixture.html"
        site = Site("banniu-list-test", "班牛列表隔离验收", html_path.as_uri(), ())
        driver = None
        try:
            driver = create_driver(site, headless=True, profile=folder / "profile")
            for name, body, expected in cases:
                html_path.write_text(
                    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                    'style-src \'unsafe-inline\'">'
                    '<title>Local synthetic Banniu list test</title>'
                    '<style>.vxe-table{min-height:20px}.el-loading-mask{min-height:20px}</style>'
                    f'</head><body>{body}</body></html>', encoding="utf-8")
                driver.get(site.url)
                actual = driver.execute_script(_LIST)
                assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"
                if expected is not None:
                    assert complete_task_rows(actual, ORDER_NO) == expected["rows"], name
            print(f"PASS: {len(cases)} isolated Chrome DOM cases; normal and explicit empty results accepted; "
                  "missing pager, ambiguous empty state, existing rows, loading and multiple tables rejected. "
                  "Only local synthetic HTML was loaded; no real platform or user profile was used.")
        finally:
            if driver is not None:
                driver.quit()


if __name__ == "__main__":
    main()
