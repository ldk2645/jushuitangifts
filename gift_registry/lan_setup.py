"""本机 LAN 快速维护；不更改 DNS、访问密码或业务记录。"""
import argparse
import json

from .network import NetworkSettings, load_network, save_network
from .network_monitor import NetworkProbeError, probe_network


def refresh_network():
    previous = load_network()
    if not previous.enabled:
        raise ValueError('尚未启用局域网共享，请先完成局域网配置。')
    connections = probe_network()
    if not connections:
        raise ValueError('没有检测到可共享的物理内网，请先连接 Wi-Fi 或网线。')
    if len(connections) == 1:
        selected = connections[0]
    else:
        for index, item in enumerate(connections, 1):
            print(f"{index}. {item['name']}  {item['ip']}  {item['subnet']}")
        answer = input('选择用于局域网共享的网络编号：').strip()
        if not answer.isascii() or not answer.isdecimal() or not 1 <= int(answer) <= len(connections):
            raise ValueError('未选择有效网络，配置没有改变。')
        selected = connections[int(answer) - 1]
    current = NetworkSettings(True, selected['ip'], selected['subnet'], previous.domain)
    if current != previous:
        save_network(current)
        print('已更新共享 IP/子网。请等待当前登记完成后正常重启工作台，使新配置生效。')
    else:
        print('共享 IP/子网已是当前值，无需重写配置。')
    print(f'当前内网 IP：{current.lan_ip}；子网：{current.subnet}')
    if current.domain:
        print(f'请确认 Cloudflare 中 {current.domain} 的 A 记录为 {current.lan_ip}（仅 DNS）。')
    return current


def diagnose_network():
    """只读检查入口；不生成密码，不保存配置，不触发防火墙更新。"""
    settings = load_network()
    connections = probe_network()
    print('只读检查：不会修改配置、DNS、hosts 或防火墙。')
    print(f'已保存的共享配置：{settings.lan_ip or "未启用"}；子网：{settings.subnet or "未启用"}')
    if settings.domain:
        print(f'已保存的域名：{settings.domain}')
    if not connections:
        raise ValueError('没有检测到可共享的物理内网，请先连接 Wi-Fi 或网线。')
    for index, item in enumerate(connections, 1):
        matches = settings.enabled and settings.lan_ip == item['ip'] and settings.subnet == item['subnet']
        print(f'{index}. {item["name"]}  {item["ip"]}  {item["subnet"]}  ' + ('与配置一致' if matches else '与配置不同'))
    print('网络检测完成；此检查不代表其他设备已通过防火墙或 DNS 连通主机。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='输出已保存配置，供防火墙脚本校验')
    mode.add_argument('--diagnose', action='store_true', help='只读检查实际网络和已保存配置')
    args = parser.parse_args()
    try:
        if args.check:
            settings = load_network()
            if not settings.enabled:
                raise ValueError('LAN configuration is disabled')
            print(json.dumps(vars(settings), ensure_ascii=True))
        elif args.diagnose:
            diagnose_network()
        else:
            refresh_network()
    except (NetworkProbeError, ValueError) as exc:
        print(f'局域网配置步骤失败：{exc}')
        raise SystemExit(1) from None
    except (EOFError, KeyboardInterrupt):
        print('已取消网络选择，未更新配置。')
        raise SystemExit(1) from None
    except OSError:
        print('无法读写本机局域网配置，请检查配置文件及目录权限。')
        raise SystemExit(1) from None
    except Exception:
        print('局域网配置步骤失败，未进入防火墙更新。请核对本机配置和网络连接。')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
