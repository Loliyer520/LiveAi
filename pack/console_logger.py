"""Minimal console logging — Claude Code style."""

import sys
import os
from datetime import datetime

# ── ANSI ───────────────────────────────────────────────────────────
_R = '\033[0m'
_B = '\033[1m'
_D = '\033[2m'
_colors = {'red': '31', 'green': '32', 'yellow': '33', 'blue': '34', 'cyan': '36', 'gray': '90'}

def _s(text: str, color: str = '', bold: bool = False, dim: bool = False) -> str:
    p = ''
    if bold: p += _B
    if dim:  p += _D
    if color and color in _colors: p += f'\033[{_colors[color]}m'
    return f'{p}{text}{_R}'

def _w(msg: str):
    sys.stderr.write(msg + '\n')
    sys.stderr.flush()

# Windows ANSI 支持
if sys.platform == 'win32':
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


def info(msg: str):
    _w(msg)


def ok(msg: str):
    _w(f"  {_s('✓', 'green')}  {msg}")


def warn(msg: str):
    _w(f"  {_s('⚠', 'yellow')}  {_D}{msg}{_R}")


def error(msg: str):
    ts = datetime.now().strftime('%H:%M:%S')
    _w(f"  {_s('✘', 'red')}  {msg}  {_s(ts, 'gray')}")


def debug(msg: str):
    if os.getenv('DEBUG'):
        _w(f"  {_s('·', 'gray')}  {_D}{msg}{_R}")


def section(title: str):
    _w(f"\n{_s(title, bold=True)}")


def blank():
    _w('')


def banner(lines: list[str], width: int = 62):
    """绘制 Claude Code 风格的启动面板。

    lines 每项可以是 (text, style) 或纯字符串。
    style: 'bold', 'dim', 'green', 'cyan', 'yellow', 'gray'
    """
    top = f"  {_s('╭───', 'gray')} LiveAi Bot {_s('─' * (width - 19), 'gray')}{_s('╮', 'gray')}"
    bottom = f"  {_s('╰' + '─' * (width - 2) + '╯', 'gray')}"

    _w(top)
    for line in lines:
        text = line if isinstance(line, str) else line[0]
        style = '' if isinstance(line, str) else (line[1] if len(line) > 1 else '')
        styled = _s(text, style)
        _w(f"  {_s('│', 'gray')}  {styled}{' ' * (width - 4 - _visible_len(text))}{_s('│', 'gray')}")
    _w(bottom)


def _visible_len(text: str) -> int:
    """估算去除 ANSI 后的可见长度。"""
    import re
    clean = re.sub(r'\033\[[0-9;]*m', '', text)
    # 粗略处理中文全角
    length = 0
    for ch in clean:
        if '一' <= ch <= '鿿' or '　' <= ch <= '〿' or '＀' <= ch <= '￯':
            length += 2
        else:
            length += 1
    return length
