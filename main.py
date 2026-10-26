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
    parser.add_argument('--enable-lan', action='store_true', help='保存并启用局域网共享配置')
    parser.add_argument('--disable-lan', action='store_true', help='恢复仅本机访问，下次启动生效')
    parser.add_argument('--lan-info', action='store_true', help='在本机桌面显示共享地址及独立访问密码')
    parser.add_argument('--simplify-lan-password', action='store_true', help='将旧局域网长密码改为六位数字，重启工作台后生效')
    parser.add_argument('--lan-ip', help='本机局域网 IPv4 地址')
    parser.add_argument('--lan-subnet', help='允许访问的本机局域网子网，例如 192.168.1.0/24')
    parser.add_argument('--domain', help='局域网访问域名，不包含协议或端口')
    args = parser.parse_args()
    if sum((args.enable_lan, args.disable_lan, args.lan_info, args.simplify_lan_password, args.login, args.status)) > 1:
        parser.error('配置、信息查看、登录及状态选项不能同时使用')
    if any((args.lan_ip, args.lan_subnet, args.domain)) and not args.enable_lan:
        parser.error('--lan-ip、--lan-subnet 和 --domain 只能与 --enable-lan 同时使用')
    from gift_registry.network import LanAccessStore, NetworkSettings, load_network, save_network, show_lan_info
    if args.simplify_lan_password:
        LanAccessStore().simplify()
        print('已设置六位数字访问密码；重启工作台后生效，使用 --lan-info 在主机查看。')
        return
    if args.enable_lan:
        if not all((args.lan_ip, args.lan_subnet, args.domain)):
            parser.error('--enable-lan 必须同时填写 --lan-ip、--lan-subnet 和 --domain')
        settings = NetworkSettings(True, args.lan_ip, args.lan_subnet, args.domain)
        LanAccessStore().ensure()
        save_network(settings)
        print('局域网配置已保存，重新启动工作台后生效；使用 --lan-info 在主机桌面查看访问密码。')
        return
    if args.disable_lan:
        save_network(NetworkSettings())
        print('已保存仅本机访问配置，重新启动工作台后生效。')
        return
    if args.lan_info:
        show_lan_info(load_network(), args.port)
        return
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
        settings = load_network()
        run(port=args.port, open_browser=not args.no_open, network=settings,
            access_password=LanAccessStore().ensure() if settings.enabled else None)


if __name__ == "__main__":
    main()
