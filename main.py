"""礼赠自动登记工具：表格工作台与平台登录助手。"""

import argparse
import json

from gift_registry import __version__
from gift_registry.config import DATA_DIR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--status", action="store_true", help="仅查看状态，不输出会话凭据")
    parser.add_argument("--login", action="store_true", help="打开平台登录助手")
    parser.add_argument("--port", type=int, default=8765, help="本地前端端口，默认 8765")
    parser.add_argument("--no-open", action="store_true", help="启动前端但不自动打开浏览器")
    args = parser.parse_args()
    if args.status:
        path = DATA_DIR / "status.json"
        print(path.read_text(encoding="utf-8") if path.exists() else json.dumps(
            {"message": "尚未启动登录助手"}, ensure_ascii=False))
        return
    if args.login:
        from gift_registry.app import run
        run()
    else:
        from gift_registry.frontend_server import run
        run(port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
