"""荣耀版身份和独立服务入口。"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EDITION = 'honor'
APP_NAME = '荣耀礼赠工作台'
DEFAULT_PORT = 8767
BANNIU_URL = 'https://work.bytenew.com/app.html#/applet/appNew/projectNew/58231/27715'
# 实际工作表与字段核对完成后才允许真实登记；不会沿用原项目的字段身份。
BANNIU_VERIFIED = True
BANNIU_TITLE = '聚水谭赠品补发—荣耀'
BANNIU_BOARD_FRAGMENT = '/applet/appNew/boardNew/58231/268616793655'
BANNIU_BOARD_TITLE = '赠品补发看板——荣耀'


def original_installation_path(path):
    """允许当前独立荣耀目录；拒绝原项目其它路径和 U 盘。"""
    path = Path(path).resolve()
    if path == PROJECT_ROOT or path.is_relative_to(PROJECT_ROOT):
        return False
    return any(path == root.resolve() or path.is_relative_to(root.resolve())
               for root in (Path(r'D:\UserData\Desktop\NL'), Path(r'E:\gifts')))
