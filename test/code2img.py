#!/usr/bin/env python3
"""
code2img —— 代码块转高颜值图片（原型）

方案：Pygments（词法分析 + 语法高亮 token）+ Pillow（绘制）
效果：圆角窗口 + macOS 红黄绿按钮 + 渐变背景 + 深色主题 + 行号 + 合适内边距/字体
无浏览器依赖，纯 Python。

用法：
    from code2img import render_code_to_image
    render_code_to_image(code_text, language="python", out_path="out.png")

也可命令行直接跑内置示例：
    python code2img.py
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from pygments import lex
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound
from pygments.token import Token


HERE = Path(__file__).resolve().parent
FONT_DIR = HERE / "fonts"

# 图片默认输出目录：项目 data/images/。
# 转图默认落这里，发图工具（send_local_image）也只从这里读，形成闭环。
# HERE 是 test/，其上一级即项目根。
IMAGES_DIR = (HERE.parent / "data" / "images").resolve()

# ── 字体候选（按优先级）────────────────────────────────────────────
_FONT_REGULAR_CANDIDATES = ["JetBrainsMono.ttf", "DejaVuSansMono.ttf"]
_FONT_BOLD_CANDIDATES = ["DejaVuSansMono-Bold.ttf", "JetBrainsMono.ttf"]
_FONT_ITALIC_CANDIDATES = ["JetBrainsMono-Italic.ttf", "JetBrainsMono.ttf"]

# ── 缺字回退字体路径（CJK / Emoji / 特殊符号）────────────────────────
# 运行时按顺序探测，找到的第一个可用字体即作为回退字体。
_FALLBACK_FONT_PATHS = [
    # Linux: Noto CJK / Emoji (apt install fonts-noto-cjk fonts-noto-color-emoji)
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
    # Linux: WenQuanYi (apt install fonts-wqy-microhei)
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    # Linux: DroidSans fallback
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # Linux: AR PL fonts (apt install fonts-arphic-uming)
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    # macOS
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Windows
    "C:/Windows/Fonts/seguiemj.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    # 项目自带（可手动放入 core/fonts/）
]

# 字形支持缓存：避免重复调用 getmask()
_char_support_cache: dict[tuple, bool] = {}


def _font_has_char(font, char: str) -> bool:
    """检测字体是否包含某个字符的字形（结果缓存）。"""
    key = (id(font), char)
    cached = _char_support_cache.get(key)
    if cached is not None:
        return cached
    try:
        mask = font.getmask(char)
        ok = mask is not None and mask.size[0] > 0
    except Exception:
        ok = False
    _char_support_cache[key] = ok
    return ok


def _discover_fallback_fonts(font_size: int) -> list:
    """探测系统/项目中可用的缺字回退字体，返回 ImageFont 列表。"""
    found = []
    for path_str in _FALLBACK_FONT_PATHS:
        p = Path(path_str)
        if p.exists():
            try:
                f = ImageFont.truetype(str(p), font_size)
                found.append(f)
            except Exception:
                pass
    # 同时扫描项目 fonts 目录下的额外字体文件
    for name in sorted(FONT_DIR.glob("*")):
        if name.suffix.lower() in (".ttf", ".otf", ".ttc") and name.name not in {
            "JetBrainsMono.ttf", "JetBrainsMono-Italic.ttf",
            "DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf",
        }:
            try:
                f = ImageFont.truetype(str(name), font_size)
                found.append(f)
            except Exception:
                pass
    return found


def _draw_text_with_fallback(
    draw, xy: tuple, text: str,
    primary_font, fallback_fonts: list,
    fill, measure_draw=None,
) -> float:
    """绘制文本段，对主字体不支持的字形自动回退。

    返回绘制后的 x 坐标（用于光标前进）。
    大部分代码段只有 ASCII，走快速路径整段一次绘制。
    """
    x, y = xy

    # 快速路径：该段所有唯一字符都在主字体中
    unique_chars = set(text)
    needs_fallback = any(not _font_has_char(primary_font, ch) for ch in unique_chars)

    if not needs_fallback:
        draw.text((x, y), text, font=primary_font, fill=fill)
        md = measure_draw or draw
        return x + md.textlength(text, font=primary_font)

    # 慢路径：逐字绘制，遇缺字则尝试回退字体
    md = measure_draw or draw
    for ch in text:
        if _font_has_char(primary_font, ch):
            draw.text((x, y), ch, font=primary_font, fill=fill)
            x += md.textlength(ch, font=primary_font)
        else:
            rendered = False
            for fb in fallback_fonts:
                if _font_has_char(fb, ch):
                    draw.text((x, y), ch, font=fb, fill=fill)
                    x += md.textlength(ch, font=fb)
                    rendered = True
                    break
            if not rendered:
                # 没有可用回退字体 → 用主字体绘制（仍会是豆腐块，但不中断渲染）
                draw.text((x, y), ch, font=primary_font, fill=fill)
                x += md.textlength(ch, font=primary_font)
    return x


def _pick_font(candidates: list[str]) -> Path:
    for name in candidates:
        p = FONT_DIR / name
        if p.exists():
            return p
    raise FileNotFoundError(f"未找到可用字体，检查目录：{FONT_DIR}")


# ── 深色主题配色（参考 carbon 的 "Seti / One Dark" 风格）──────────────
THEME = {
    "bg_gradient_top": (40, 44, 92),      # 外层渐变背景（上）
    "bg_gradient_bottom": (28, 30, 58),   # 外层渐变背景（下）
    "window_bg": (30, 33, 39),            # 窗口体背景
    "titlebar_bg": (30, 33, 39),          # 标题栏背景（与窗口同色，靠按钮区分）
    "line_number": (99, 109, 131),        # 行号颜色
    "default_text": (171, 178, 191),      # 默认文字
    "shadow": (0, 0, 0, 90),              # 窗口投影
    "btn_red": (255, 95, 86),
    "btn_yellow": (255, 189, 46),
    "btn_green": (39, 201, 63),
}

# token -> 颜色（One Dark 风格）
TOKEN_COLORS = {
    Token.Keyword: (198, 120, 221),
    Token.Keyword.Constant: (209, 154, 102),
    Token.Keyword.Namespace: (198, 120, 221),
    Token.Name.Function: (97, 175, 239),
    Token.Name.Class: (229, 192, 123),
    Token.Name.Builtin: (86, 182, 194),
    Token.Name.Decorator: (229, 192, 123),
    Token.Name.Namespace: (229, 192, 123),
    Token.Name.Exception: (229, 192, 123),
    Token.Name.Tag: (224, 108, 117),
    Token.Name.Attribute: (209, 154, 102),
    Token.String: (152, 195, 121),
    Token.String.Doc: (127, 132, 142),
    Token.String.Escape: (86, 182, 194),
    Token.Number: (209, 154, 102),
    Token.Operator: (86, 182, 194),
    Token.Operator.Word: (198, 120, 221),
    Token.Comment: (127, 132, 142),
    Token.Comment.Single: (127, 132, 142),
    Token.Comment.Multiline: (127, 132, 142),
    Token.Punctuation: (171, 178, 191),
    Token.Text: (171, 178, 191),
    Token.Literal: (152, 195, 121),
    Token.Generic.Deleted: (224, 108, 117),
    Token.Generic.Inserted: (152, 195, 121),
}

ITALIC_TOKENS = {Token.Comment, Token.Comment.Single, Token.Comment.Multiline, Token.String.Doc}


def _color_for_token(ttype):
    """沿 token 继承链找最匹配的颜色。"""
    t = ttype
    while t is not None:
        if t in TOKEN_COLORS:
            return TOKEN_COLORS[t]
        t = t.parent
    return THEME["default_text"]


def _is_italic_token(ttype):
    t = ttype
    while t is not None:
        if t in ITALIC_TOKENS:
            return True
        t = t.parent
    return False


def _get_lexer(code: str, language: str | None):
    if language:
        try:
            return get_lexer_by_name(language, stripnl=False)
        except ClassNotFound:
            pass
    try:
        return guess_lexer(code)
    except ClassNotFound:
        return get_lexer_by_name("text", stripnl=False)


def _tokenize_lines(code: str, lexer):
    """把代码按行拆成 [(text, ttype), ...] 的列表，每行一个 segment 列表。"""
    lines: list[list[tuple[str, object]]] = [[]]
    for ttype, value in lex(code, lexer):
        parts = value.split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                lines.append([])
            if part:
                lines[-1].append((part, ttype))
    # 去掉末尾可能的空行（保留内容行）
    while len(lines) > 1 and not lines[-1]:
        lines.pop()
    return lines


def _vertical_gradient(size, top_rgb, bottom_rgb):
    """生成竖直渐变背景。"""
    w, h = size
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * t)
        g = int(top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * t)
        b = int(top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * t)
        base.putpixel((0, y), (r, g, b))
    return base.resize((w, h))


def render_code_to_image(
    code: str,
    language: str | None = None,
    out_path: str | os.PathLike = "code.png",  # 相对名默认落到 data/images/
    *,
    font_size: int = 30,
    show_line_numbers: bool = True,
    title: str | None = None,
    scale: int = 2,
) -> str:
    """
    把一段代码渲染成高颜值 PNG 图片。

    参数：
        code               代码文本
        language           语言标识（python/js/go...），None 则自动猜测
        out_path           输出 PNG 路径
        font_size          代码字号（逻辑像素）
        show_line_numbers  是否显示行号
        title              窗口标题栏文字（如文件名），None 则不显示文字
        scale              超采样倍数（越大越清晰，2 即足够）

    返回：输出文件的绝对路径
    """
    # 用高分辨率绘制再缩小，保证清晰（抗锯齿）
    fs = font_size * scale

    font = ImageFont.truetype(str(_pick_font(_FONT_REGULAR_CANDIDATES)), fs)
    font_italic = ImageFont.truetype(str(_pick_font(_FONT_ITALIC_CANDIDATES)), fs)

    # 加载缺字回退字体（CJK / Emoji 等）
    fallback_fonts = _discover_fallback_fonts(fs)

    # 等宽字体：用单字符宽度估算
    tmp_img = Image.new("RGB", (10, 10))
    tmp_draw = ImageDraw.Draw(tmp_img)
    char_w = tmp_draw.textlength("M", font=font)
    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * 1.35)

    lexer = _get_lexer(code, language)
    lines = _tokenize_lines(code, lexer)
    n_lines = len(lines)

    # 布局参数（都乘 scale）
    pad = 44 * scale                    # 窗口外的渐变留白
    win_pad_x = 32 * scale              # 窗口内左右内边距
    win_pad_top = 60 * scale            # 标题栏高度
    win_pad_bottom = 32 * scale
    radius = 16 * scale

    ln_digits = len(str(n_lines))
    ln_width = int(char_w * (ln_digits + 2)) if show_line_numbers else 0

    # 计算最长行像素宽度
    max_line_px = 0
    for segs in lines:
        text = "".join(s[0] for s in segs)
        max_line_px = max(max_line_px, int(tmp_draw.textlength(text, font=font)))

    code_area_w = ln_width + max_line_px
    win_w = win_pad_x * 2 + code_area_w
    win_h = win_pad_top + win_pad_bottom + line_h * n_lines

    img_w = win_w + pad * 2
    img_h = win_h + pad * 2

    # 背景渐变
    bg = _vertical_gradient((img_w, img_h), THEME["bg_gradient_top"], THEME["bg_gradient_bottom"]).convert("RGBA")

    # 窗口投影
    shadow = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    soff = 10 * scale
    sdraw.rounded_rectangle(
        [pad, pad + soff, pad + win_w, pad + win_h + soff],
        radius=radius, fill=THEME["shadow"],
    )
    try:
        from PIL import ImageFilter
        shadow = shadow.filter(ImageFilter.GaussianBlur(12 * scale))
    except Exception:
        pass
    bg = Image.alpha_composite(bg, shadow)

    # 窗口体
    draw = ImageDraw.Draw(bg)
    draw.rounded_rectangle(
        [pad, pad, pad + win_w, pad + win_h],
        radius=radius, fill=THEME["window_bg"],
    )

    # macOS 三个按钮
    btn_r = 7 * scale
    btn_y = pad + win_pad_top // 2
    btn_x = pad + 26 * scale
    gap = 22 * scale
    for i, color in enumerate([THEME["btn_red"], THEME["btn_yellow"], THEME["btn_green"]]):
        cx = btn_x + i * gap
        draw.ellipse([cx - btn_r, btn_y - btn_r, cx + btn_r, btn_y + btn_r], fill=color)

    # 标题文字（居中）
    if title:
        tw = draw.textlength(title, font=font)
        draw.text(
            (pad + (win_w - tw) / 2, btn_y - (ascent + descent) / 2),
            title, font=font, fill=THEME["line_number"],
        )

    # 绘制代码
    x0 = pad + win_pad_x
    y0 = pad + win_pad_top
    for idx, segs in enumerate(lines):
        y = y0 + idx * line_h
        cx = x0
        if show_line_numbers:
            num = str(idx + 1).rjust(ln_digits)
            draw.text((x0, y), num, font=font, fill=THEME["line_number"])
            cx = x0 + ln_width
        for text, ttype in segs:
            color = _color_for_token(ttype)
            f = font_italic if _is_italic_token(ttype) else font
            cx = _draw_text_with_fallback(draw, (cx, y), text, f, fallback_fonts, color, tmp_draw)

    # 超采样缩小
    if scale > 1:
        bg = bg.resize((img_w // scale, img_h // scale), Image.LANCZOS)

    # 相对路径 / 纯文件名 → 落到项目 data/images/；绝对路径按原样。
    _op = Path(out_path)
    if not _op.is_absolute():
        _op = IMAGES_DIR / _op
    out_path = _op.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(out_path, "PNG")
    return str(out_path)


if __name__ == "__main__":
    sample = '''import asyncio
from dataclasses import dataclass


@dataclass
class Message:
    """一条聊天消息。"""
    user: str
    text: str
    at: float = 0.0


async def handle(msg: Message) -> str:
    # 简单回显，实际会走 AI 推理
    if not msg.text:
        return "（空消息）"
    reply = f"收到 {msg.user}: {msg.text!r}"
    await asyncio.sleep(0.1)
    return reply


if __name__ == "__main__":
    m = Message(user="alice", text="hello world", at=1234.5)
    print(asyncio.run(handle(m)))
'''
    out = render_code_to_image(
        sample,
        language="python",
        out_path="sample_output.png",  # 落到 data/images/
        title="message.py",
    )
    print(f"已生成图片：{out}")
