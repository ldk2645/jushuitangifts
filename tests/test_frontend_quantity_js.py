"""正式回归：前端复制/填充/结构化粘贴的数量传递（Node 执行真实 frontend/app.js）。

`.tmp/codex-review-quantity-20261105.cjs` 的隔离复现已转为
`tests/frontend_quantity_checks.cjs`：Node 用 vm 从真实 frontend/app.js 截取函数执行，
不复制实现源码。本文件把它接入 unittest 自动发现；node 不可用时跳过。
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "tests" / "frontend_quantity_checks.cjs"


def find_node():
    executable = shutil.which("node") or shutil.which("node.exe")
    return executable


class FrontendQuantityRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = find_node()
        if not cls.node:
            raise unittest.SkipTest("未找到 node，跳过 frontend/app.js 数量回归（真实函数由 Node vm 加载）")

    def run_node(self, *arguments, timeout=180):
        return subprocess.run(
            [self.node, *arguments], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)

    def test_app_js_syntax(self):
        result = self.run_node("--check", "frontend/app.js")
        self.assertEqual(result.returncode, 0, f"frontend/app.js 语法检查失败：\n{result.stdout}\n{result.stderr}")

    def test_quantity_checks_pass_on_real_frontend_source(self):
        self.assertTrue(CHECK_SCRIPT.is_file(), f"缺少 {CHECK_SCRIPT}")
        result = self.run_node(str(CHECK_SCRIPT))
        output = f"{result.stdout}\n{result.stderr}"
        self.assertEqual(result.returncode, 0, f"数量回归失败：\n{output}")

        summary = None
        for line in result.stdout.splitlines():
            if line.startswith("SUMMARY "):
                summary = json.loads(line[len("SUMMARY "):])
                break
        self.assertIsNotNone(summary, f"未找到 SUMMARY 行：\n{output}")
        self.assertEqual(summary["failed"], 0, f"失败用例数不为 0：\n{output}")
        self.assertEqual(summary["passed"], summary["total"], f"通过数不等于总数：\n{output}")
        # 防止用例被悄悄删减到失去覆盖意义（当前 39 项，至少保留 30 项）。
        self.assertGreaterEqual(summary["total"], 30, f"数量回归用例数过少：{summary}")
        self.assertIn("SOURCE ", result.stdout)

        # 第 5 节“必须通过”清单中的关键场景必须各自有独立用例，不能被合并删除。
        for keyword in ("原复现", "100 次", "99 件", "表头行", "旧 4 列", "同名不同编码",
                        "撤销恢复", "两个 SKU", "结构化粘贴", "筛选时填充", "显式数量映射"):
            self.assertIn(keyword, result.stdout, f"输出中缺少关键用例：{keyword}")

    def test_ui_documents_plain_text_quantity_limitation(self):
        """纯文本/Excel 往返会丢失多件数量，界面必须写清楚限制（第 5 节扩展检查）。"""
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn("data-gift-registry-range", (ROOT / "frontend" / "app.js").read_text(encoding="utf-8"))
        self.assertIn("同一礼品多件仅在工作台内复制/粘贴时保留数量", html)
        self.assertIn("Excel 纯文本不含数量", html)
        self.assertIn("会按每种 1 件记录", (ROOT / "frontend" / "app.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
