#!/usr/bin/env python3
"""LiveAi Setup Wizard — 首次运行 / 自我纠错引导程序。"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
# ANSI 终端样式
# ═══════════════════════════════════════════════════════════════════

_R = '\033[0m'
_B = '\033[1m'
_D = '\033[2m'

def _c(code: str, text: str) -> str:
    return f'\033[{code}m{text}{_R}'

def _dim(s): return _c('2', s)
def _bold(s): return _c('1', s)
def _cyan(s): return _c('36', s)
def _green(s): return _c('32', s)
def _yellow(s): return _c('33', s)
def _red(s): return _c('31', s)
def _blue(s): return _c('34', s)
def _white(s): return _c('37', s)
def _gray(s): return _c('90', s)

def _clear():
    os.system('cls' if sys.platform == 'win32' else 'clear')

def _hrule(title: str = ''):
    if title:
        print(f"\n{_dim('──')} {_bold(title)} {_dim('─' * max(2, 55 - len(title)))}")
    else:
        print(_dim('─' * 60))

# ═══════════════════════════════════════════════════════════════════
# 配置文件路径
# ═══════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / 'config.yaml'
EXAMPLE_PATH = ROOT / 'config.yaml.example'


def _load_yaml(path: Path) -> dict:
    import yaml
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_yaml(path: Path, data: dict):
    import yaml
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _has_config() -> bool:
    return CONFIG_PATH.exists()


def _get_nested(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d


def _set_nested(d: dict, value, *keys):
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


# ═══════════════════════════════════════════════════════════════════
# 终端输入工具
# ═══════════════════════════════════════════════════════════════════

def _get_key() -> str:
    """读取单个按键，支持方向键。"""
    if sys.platform == 'win32':
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b'\xe0', b'\x00'):
            ch2 = msvcrt.getch()
            mapping = {b'H': 'UP', b'P': 'DOWN', b'K': 'LEFT', b'M': 'RIGHT'}
            return mapping.get(ch2, '?')
        if ch == b'\r': return 'ENTER'
        if ch == b'\t': return 'TAB'
        if ch == b'\x03': raise KeyboardInterrupt()
        if ch == b'\x08': return 'BACKSPACE'
        return ch.decode('utf-8', errors='replace')
    else:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = os.read(fd, 1)
            if ch == b'\x1b':
                # 可能是方向键序列
                ch2 = os.read(fd, 1)
                if ch2 == b'[':
                    ch3 = os.read(fd, 1)
                    mapping = {b'A': 'UP', b'B': 'DOWN', b'C': 'RIGHT', b'D': 'LEFT'}
                    return mapping.get(ch3, '?')
                return 'ESC'
            if ch in (b'\r', b'\n'):
                return 'ENTER'
            if ch == b'\t':
                return 'TAB'
            if ch == b'\x03':
                raise KeyboardInterrupt()
            if ch == b'\x7f':
                return 'BACKSPACE'
            return ch.decode('utf-8', errors='replace')
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _press_any_key():
    print(f"\n{_dim('按任意键继续...')}", end='', flush=True)
    _get_key()
    print()


def _input_str(prompt: str, default: str = '', password: bool = False) -> str:
    """带默认值的输入。"""
    default_str = f" {_dim(f'[{default}]')}" if default else ''
    print(f"  {_cyan('?')} {prompt}{default_str}", end='', flush=True)
    print(f" {_dim('>')} ", end='', flush=True)
    value = input().strip()
    return value if value else default


# ═══════════════════════════════════════════════════════════════════
# 模型选择器（方向键导航）
# ═══════════════════════════════════════════════════════════════════

_PAGE_SIZE = 10


def _select_from_list(title: str, items: list[str], allow_custom: bool = True) -> str | None:
    """方向键选择列表项。返回选中的字符串，或 None 表示取消。"""
    if not items:
        print(f"\n  {_yellow('⚠')} 未能获取到模型列表，请手动输入。")
        return None

    idx = 0
    scroll = 0  # 窗口起始位置

    def _redraw():
        # 清屏
        sys.stderr.write('\033[2J\033[H')
        sys.stderr.flush()
        print(f"  {_cyan('?')} {title}")
        if allow_custom:
            print(f"  {_dim('↑↓ 选择  │  Enter 确认  │  Tab 自定义输入  │  Esc 返回')}")
        else:
            print(f"  {_dim('↑↓ 选择  │  Enter 确认')}")
        print()

        end = min(scroll + _PAGE_SIZE, len(items))
        for i in range(scroll, end):
            prefix = _cyan('❯') if i == idx else ' '
            item_display = f" {items[i]}"
            if i == idx:
                line = f"  {prefix} {_bold(_white(item_display))}"
            else:
                line = f"  {prefix} {_dim(item_display)}"
            sys.stderr.write(line + '\n')

        if len(items) > _PAGE_SIZE:
            progress = f"  {scroll + 1}-{end} / {len(items)}"
            sys.stderr.write(f"\n  {_dim(progress)}\n")
        sys.stderr.flush()

    # 首次绘制
    sys.stderr.write('\033[?1049h')  # 切换到 alternate buffer
    sys.stderr.flush()
    _redraw()

    while True:
        key = _get_key()
        if key == 'UP':
            idx = (idx - 1) % len(items)
            if idx < scroll:
                scroll = idx
            elif idx >= scroll + _PAGE_SIZE:
                scroll = idx - _PAGE_SIZE + 1
        elif key == 'DOWN':
            if idx == len(items) - 1:
                idx = 0
                scroll = 0
            else:
                idx += 1
                if idx >= scroll + _PAGE_SIZE:
                    scroll = idx - _PAGE_SIZE + 1
        elif key == 'ENTER':
            sys.stderr.write('\033[?1049l')
            sys.stderr.flush()
            return items[idx]
        elif key == 'TAB' and allow_custom:
            sys.stderr.write('\033[?1049l')
            sys.stderr.flush()
            return '__CUSTOM__'
        elif key == 'ESC':
            sys.stderr.write('\033[?1049l')
            sys.stderr.flush()
            return None
        elif key and len(key) == 1 and key.isprintable():
            # 快速跳转：搜索以该字母开头的项
            lower = key.lower()
            for i, item in enumerate(items):
                if item.lower().startswith(lower):
                    idx = i
                    scroll = max(0, min(idx, len(items) - _PAGE_SIZE))
                    break
        _redraw()


# ═══════════════════════════════════════════════════════════════════
# API 探测
# ═══════════════════════════════════════════════════════════════════

_DEFAULT_TIMEOUT = 15


def _http_get(url: str, api_key: str = '', timeout: int = _DEFAULT_TIMEOUT) -> tuple[int, dict | None]:
    """发送 GET 请求，返回 (status_code, json_body_or_None)。"""
    ctx = ssl.create_default_context()
    headers = {'User-Agent': 'LiveAi-Setup/1.0', 'Accept': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return -1, None  # 网络不通


def _check_http_health(http_url: str, token: str = '') -> bool:
    """检查 Napcat HTTP 是否存活。"""
    code, _ = _http_get(http_url.rstrip('/') + '/get_login_info', token)
    return code == 200


def _detect_base_url(user_url: str, api_key: str) -> tuple[str, str]:
    """
    防呆：自动探测正确的 base_url 和 messages_path。
    返回 (base_url, messages_path)。
    """
    url = user_url.rstrip('/')

    # 已包含 /v1 → 用 OpenAI 路径
    if url.endswith('/v1'):
        return url, '/chat/completions'

    # 尝试清单模型的各种路径组合
    candidates = [
        (url, '/v1/models'),      # 标准: base + /v1/models
        (url, '/models'),         # 简化: base + /models
    ]

    for base, path in candidates:
        code, data = _http_get(f'{base}{path}', api_key, timeout=10)
        if code == 200 and data:
            messages_path = '/chat/completions'
            print(f"  {_green('✔')} 模型列表接口可用: {_dim(base + path)}")
            return base, messages_path

    print(f"  {_yellow('⚠')} 未能自动探测模型列表，保留原始 URL")
    return url, '/chat/completions'


def _fetch_models_from_api(base_url: str, api_key: str) -> list[str]:
    """从 API 获取模型列表。"""
    models = []

    # 尝试各种 models 端点
    paths = ['/models', '/v1/models']
    for path in paths:
        code, data = _http_get(f'{base_url.rstrip("/")}{path}', api_key, timeout=10)
        if code != 200 or not data:
            continue
        raw = data.get('data') or data.get('models') or []
        for item in raw:
            if isinstance(item, dict):
                mid = item.get('id') or item.get('name') or item.get('model') or ''
                if mid:
                    models.append(str(mid))
            elif isinstance(item, str):
                models.append(item)
        if models:
            break

    # 去重排序
    seen = set()
    unique = []
    for m in models:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    unique.sort()
    return unique


# ═══════════════════════════════════════════════════════════════════
# 步骤
# ═══════════════════════════════════════════════════════════════════


def _print_banner():
    _clear()
    print()
    print(f"    {_bold(_cyan('╭'))}{_dim('────────────────────────────────────────────────')}{_bold(_cyan('╮'))}")
    print(f"    {_bold(_cyan('│'))}          {_bold('LiveAi')} {_dim('Bot')}  {_dim('—')}  安装 & 配置向导              {_bold(_cyan('│'))}")
    print(f"    {_bold(_cyan('│'))}          {_dim('v1.0  ·  first-run  ·  self-healing')}            {_bold(_cyan('│'))}")
    print(f"    {_bold(_cyan('╰'))}{_dim('────────────────────────────────────────────────')}{_bold(_cyan('╯'))}")
    print()
    print(f"  {_dim('这个向导会帮你配置 OneBot 连接和 AI 模型。')}")
    print(f"  {_dim('配置完成后会生成 config.yaml，之后可随时修改。')}")
    print()


def _step_onebot(config: dict) -> dict:
    """步骤 1：配置 OneBot (Napcat) 连接。"""
    napcat = config.get('napcat', {})
    ws_url = napcat.get('ws_url', '')
    http_url = napcat.get('http_url', '')
    token = napcat.get('http_access_token', '')
    self_id = napcat.get('self_id', 0)

    is_placeholder = (
        not ws_url
        or 'localhost' in str(ws_url)
        or 'your_token' in str(ws_url)
        or str(self_id) in ('0', '1234567890')
    )

    if _has_config() and not is_placeholder:
        _hrule('OneBot 连接')
        print(f"  {_green('✔')} OneBot 已配置")
        print(f"  {_dim('WS:')} {ws_url}")
        print(f"  {_dim('HTTP:')} {http_url}")
        print(f"  {_dim('QQ:')} {self_id}")
        return config

    _hrule('步骤 1：OneBot 连接配置')

    ws_url = _input_str('WebSocket 地址 (含 access_token)', ws_url or 'ws://localhost:3001/ws')
    http_url = _input_str('HTTP API 地址', http_url or 'http://localhost:8080')
    token = _input_str('HTTP Access Token (可留空)', token)
    self_id_str = _input_str('机器人 QQ 号', str(self_id) if self_id else '')
    self_id = int(self_id_str) if self_id_str.isdigit() else 0

    config['napcat'] = {
        'ws_url': ws_url,
        'http_url': http_url,
        'http_access_token': token,
        'self_id': self_id,
    }

    # 测试连接
    print()
    print(f"  {_cyan('…')} 正在测试 OneBot 连接...")
    attempt = 0
    while True:
        attempt += 1
        if _check_http_health(http_url, token):
            print(f"  {_green('✔')} OneBot 连接成功！")
            break
        else:
            print(f"  {_yellow('⚠')} 第 {attempt} 次连接失败，10 秒后重试... (Ctrl+C 跳过)")
            try:
                time.sleep(10)
            except KeyboardInterrupt:
                print()
                print(f"  {_yellow('⚠')} 已跳过连接测试，配置已保存。之后可重新运行本向导。")
                break

    return config


def _step_api(config: dict, profile_key: str, label: str):
    """步骤 2：配置单个 AI 模型档位。"""
    ai = config.setdefault('ai', {})
    prefix = '' if profile_key == 'model' else f'{profile_key}_'

    base_url_key = f'{prefix}base_url' if profile_key != 'model' else 'model_base_url'
    api_key_key = f'{prefix}api_key' if profile_key != 'model' else 'api_key'
    model_key = f'{prefix}model_name' if profile_key != 'model' else 'model_name'
    msg_path_key = f'{prefix}messages_path' if profile_key != 'model' else 'model_messages_path'

    current_url = ai.get(base_url_key, '')
    current_key = ai.get(api_key_key, '')
    current_model = ai.get(model_key, '')

    is_placeholder = (
        not current_url
        or not current_key
        or 'sk-your-' in str(current_key)
    )

    if _has_config() and not is_placeholder:
        print(f"  {_green('✔')} [{label}] 已配置: {_dim(current_url)}  →  {_dim(current_model)}")
        return config

    _hrule(f'步骤 2：AI 模型配置 [{label}]')
    print(f"  {_dim('（按回车使用方括号内的默认值）')}")

    base_url = _input_str('API 地址', current_url or 'https://api.deepseek.com/anthropic')
    api_key = _input_str('API Key', current_key or '', password=True)

    # 防呆：探测 /v1
    if '/v1' not in base_url:
        print(f"  {_cyan('…')} 正在自动探测 URL 格式...")
        time.sleep(0.3)
        base_url, msg_path = _detect_base_url(base_url, api_key)
        ai[msg_path_key] = msg_path
    else:
        ai[msg_path_key] = '/messages' if base_url.endswith('/v1') else '/v1/messages'

    ai[base_url_key] = base_url
    ai[api_key_key] = api_key

    # 尝试拉取模型列表
    print()
    print(f"  {_cyan('…')} 正在获取可用模型列表...")
    models = _fetch_models_from_api(base_url, api_key)

    if models and len(models) > 0:
        print(f"  {_green('✔')} 找到 {len(models)} 个模型")
        time.sleep(0.5)
        chosen = _select_from_list(f'请为 [{label}] 选择模型', models, allow_custom=True)
        if chosen == '__CUSTOM__':
            chosen = _input_str('请输入自定义模型名', current_model or models[0] if models else '')
        if chosen:
            ai[model_key] = chosen
            print(f"  {_green('✔')} [{label}] 模型: {_bold(chosen)}")
        else:
            ai[model_key] = current_model or (models[0] if models else '')
            print(f"  {_yellow('⚠')} 使用当前配置: {ai[model_key]}")
    else:
        # 模型列表获取失败，手动输入
        model = _input_str('未获取到模型列表，请手动输入模型名', current_model or 'deepseek-v4-flash')
        ai[model_key] = model
        print(f"  {_green('✔')} [{label}] 模型: {_bold(model)}")

    return config


def _step_admin(config: dict) -> dict:
    """步骤 3：管理员和主人配置。"""
    ai = config.setdefault('ai', {})

    admin = ai.get('admin_qq', 0)
    master = ai.get('master_qq', 0)

    is_default = str(admin) in ('0', '123456789')

    if _has_config() and not is_default:
        print(f"  {_green('✔')} 管理员QQ: {admin}  │  主人QQ: {master}")
        return config

    _hrule('步骤 3：管理员配置')

    admin = int(_input_str('管理员 QQ 号', str(admin) if admin else '') or 0)
    master = int(_input_str('主人 QQ 号（与管理员相同可留空）', str(master) if master else str(admin)) or admin)

    ai['admin_qq'] = admin
    ai['master_qq'] = master
    return config


def _step_github(config: dict) -> dict:
    """步骤 4：GitHub Token。"""
    ai = config.setdefault('ai', {})

    token = ai.get('github_api_token', '')
    is_placeholder = not token or 'ghp_your_' in str(token)

    if _has_config() and not is_placeholder:
        print(f"  {_green('✔')} GitHub Token: {_dim(token[:4] + '****' + token[-4:] if len(token) > 8 else '****')}")
        return config

    _hrule('步骤 4：GitHub 自动更新 (可选)')

    print(f"  {_dim('GitHub Token 用于每天自动检查更新，留空则不启用。')}")
    token = _input_str('GitHub Personal Access Token (可留空)', token or '')

    if token:
        ai['github_api_token'] = token
        ai['auto_update_enabled'] = True
        print(f"  {_green('✔')} 自动更新已启用")
    else:
        ai['auto_update_enabled'] = False
        print(f"  {_yellow('⚠')} 已跳过，自动更新未启用")

    return config


# ═══════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════

def main():
    _print_banner()

    # 加载已有配置
    config = _load_yaml(CONFIG_PATH) if _has_config() else _load_yaml(EXAMPLE_PATH)

    # 步骤 1：OneBot
    config = _step_onebot(config)

    # 步骤 2：AI 模型（优先配置主档位 flash + claude）
    config = _step_api(config, 'model', 'Flash (默认)')
    config = _step_api(config, 'claude', 'Claude')

    # 询问是否配置更多模型
    _hrule()
    more = _input_str('是否配置更多模型档位？(Pro / Opus / Vision)', 'n').lower()
    if more in ('y', 'yes', '是', '1'):
        config = _step_api(config, 'pro', 'Pro')
        config = _step_api(config, 'opus', 'Opus')
        config = _step_api(config, 'vision', 'Vision')

    # 步骤 3：管理员
    config = _step_admin(config)

    # 步骤 4：GitHub
    config = _step_github(config)

    # 保存
    _hrule()
    _save_yaml(CONFIG_PATH, config)

    print()
    print(f"  {_green('✔')} 配置已保存到 {_bold('config.yaml')}")
    print(f"  {_dim('随时可重新运行:')} python setup.py {_dim('进行修改')}")
    print()
    print(f"  {_cyan('▶')} 现在可以启动项目了：{_bold('python main.py')}")
    print()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {_yellow('⚠')} 已取消。配置可能不完整。")
        sys.exit(0)
    except Exception as e:
        print(f"\n  {_red('✘')} 出错: {e}")
        sys.exit(1)
