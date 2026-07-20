#!/usr/bin/env python3
"""
/test 指令：测试渠道或模型的可用性
"""
from __future__ import annotations
import base64
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import httpx as _httpx
except ImportError:
    _httpx = None

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

from core.model_manager import ModelManager

_HERE = Path(__file__).resolve().parent
_FONT_DIR = _HERE / "fonts"
_IMAGES_DIR = _HERE.parent / "data" / "images"

# ── 字体 ──
_FONT_CACHE = {}

def _get_font(size: int = 18):
    if not _HAS_PIL:
        return None
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for name in ("JetBrainsMono.ttf", "DejaVuSansMono.ttf"):
        p = _FONT_DIR / name
        if p.exists():
            try:
                f = ImageFont.truetype(str(p), size)
                _FONT_CACHE[size] = f
                return f
            except Exception:
                pass
    f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f

# ── 模型列表 ──

def _collect_all_models(mm: ModelManager) -> list:
    """收集所有模型，每个包含 channel_name, upstream_name, model_id。"""
    result = []
    for ch in (mm.config.get("channels") or []):
        ch_name = str(ch.get("name") or "").strip()
        for m in (ch.get("models") or []):
            up_name = str(m.get("upstream") or "").strip()
            mid = str(m.get("model_id") or "").strip()
            if up_name and mid:
                result.append({"channel": ch_name, "upstream": up_name, "model_id": mid})
    return result

# ── 匹配 ──

def _match_channel(mm: ModelManager, arg: str) -> Optional[dict]:
    channels = mm.config.get("channels") or []
    s = arg.strip()
    if len(s) == 1 and s.isalpha():
        idx = ord(s.upper()) - ord("A")
        if 0 <= idx < len(channels):
            return channels[idx]
    sl = s.lower()
    for ch in channels:
        if str(ch.get("name") or "").strip().lower() == sl:
            return ch
    return None

def _match_model(mm: ModelManager, arg: str) -> Optional[dict]:
    """匹配模型，返回 {"upstream": dict, "model_id": str, "channel_name": str, "upstream_name": str} 或 None。"""
    all_models = _collect_all_models(mm)
    s = arg.strip()

    if s.isdigit():
        idx = int(s) - 1
        if 0 <= idx < len(all_models):
            m = all_models[idx]
            up = mm._find_upstream(m["upstream"])
            if up:
                return {"upstream": up, "model_id": m["model_id"],
                        "channel_name": m["channel"], "upstream_name": m["upstream"]}

    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            ch_part, up_part, md_part = parts[0].strip(), parts[1].strip(), parts[2].strip()
            for m in all_models:
                if (m["channel"].lower() == ch_part.lower() and
                    m["upstream"].lower() == up_part.lower() and
                    m["model_id"].lower() == md_part.lower()):
                    up = mm._find_upstream(m["upstream"])
                    if up:
                        return {"upstream": up, "model_id": m["model_id"],
                                "channel_name": m["channel"], "upstream_name": m["upstream"]}
        if len(parts) == 2:
            up_part, md_part = parts[0].strip(), parts[1].strip()
            for m in all_models:
                if (m["upstream"].lower() == up_part.lower() and
                    m["model_id"].lower() == md_part.lower()):
                    up = mm._find_upstream(m["upstream"])
                    if up:
                        return {"upstream": up, "model_id": m["model_id"],
                                "channel_name": m["channel"], "upstream_name": m["upstream"]}
        parts_lower = [p.strip().lower() for p in parts]
        for m in all_models:
            full = f'{m["channel"]}/{m["upstream"]}/{m["model_id"]}'.lower()
            if all(p in full for p in parts_lower):
                up = mm._find_upstream(m["upstream"])
                if up:
                    return {"upstream": up, "model_id": m["model_id"],
                            "channel_name": m["channel"], "upstream_name": m["upstream"]}

    sl = s.lower()
    for m in all_models:
        if sl in m["model_id"].lower():
            up = mm._find_upstream(m["upstream"])
            if up:
                return {"upstream": up, "model_id": m["model_id"],
                        "channel_name": m["channel"], "upstream_name": m["upstream"]}
    return None

# ── 测试请求 ──

async def _test_request(upstream: dict, model_id: str, timeout: float = 12.0) -> dict:
    base_url = str(upstream.get("base_url") or "").strip().rstrip("/")
    api_key = str(upstream.get("api_key") or "").strip()
    messages_path = str(upstream.get("messages_path") or "").strip() or "/v1/messages"
    is_openai = "/chat/completions" in messages_path

    headers = {"Content-Type": "application/json"}
    if is_openai:
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["anthropic-version"] = "2023-06-01"
        if api_key:
            headers["x-api-key"] = api_key

    payload = {"model": model_id, "max_tokens": 10,
               "messages": [{"role": "user", "content": "hi"}], "stream": False}
    url = f"{base_url}{messages_path}"

    if _httpx is None:
        return {"success": False, "elapsed_ms": 0, "error": "httpx 未安装"}

    start = time.perf_counter()
    try:
        async with _httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code < 400:
            return {"success": True, "elapsed_ms": elapsed_ms, "status": resp.status_code}
        body_text = (resp.text or "")[:200]
        return {"success": False, "elapsed_ms": elapsed_ms,
                "error": f"HTTP {resp.status_code}: {body_text}"}
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {"success": False, "elapsed_ms": elapsed_ms, "error": str(exc)[:200]}

# ── 图片渲染 ──

def _render_image(lines: list, title: str = "Test Result") -> Optional[str]:
    """用 PIL 渲染代码风格图片，保存到 data/images/，返回路径。PIL 不可用时返回 None。"""
    if not _HAS_PIL:
        return None
    font = _get_font(18)
    if font is None:
        return None
    try:
        tmp = Image.new("RGB", (10, 10))
        tmpd = ImageDraw.Draw(tmp)
        lh = 24
        max_w = max((tmpd.textlength(ln, font=font) for ln in lines), default=200) + 80
        ih = lh * len(lines) + 60
        img = Image.new("RGB", (int(max_w), ih), (22, 22, 33))
        draw = ImageDraw.Draw(img)
        draw.text((30, 15), title, font=font, fill=(255, 255, 255))
        draw.line([(30, 42), (int(max_w) - 30, 42)], fill=(70, 70, 90), width=1)
        for i, ln in enumerate(lines):
            y = 52 + i * lh
            if any(tag in ln for tag in ("\u274c", "\u274e", "\u2717", "\u2716", "失败", "[FAIL]", "[X]")):
                color = (255, 130, 130)
            elif any(tag in ln for tag in ("\u2705", "\u2714", "[OK]")):
                color = (130, 255, 130)
            else:
                color = (210, 210, 210)
            draw.text((30, y), ln, font=font, fill=color)
        _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = _IMAGES_DIR / f"test_result_{ts}.png"
        img.save(str(fp), "PNG")
        return str(fp)
    except Exception:
        return None

# ── 发送辅助 ──

def _send_multi(bot, message, lines: list, title: str = "Test Result"):
    """多条结果：优先图片（base64 编码，避免远程 Napcat 无法访问本地文件），回退到文本。"""
    if len(lines) <= 1:
        bot.send_text(message.chat_type, message.chat_id, "\n".join(lines))
        return
    fp = _render_image(lines, title)
    if fp:
        try:
            with open(fp, "rb") as f:
                b64data = base64.b64encode(f.read()).decode("ascii")
            resp = bot.send_image(message.chat_type, message.chat_id, f"base64://{b64data}")
            if isinstance(resp, dict) and resp.get("status") != "ok":
                raise RuntimeError(resp.get("message", "图片发送失败"))
        except Exception:
            bot.send_text(message.chat_type, message.chat_id,
                          "[图片发送失败，回退到文本]\n" + "\n".join(lines))
    else:
        bot.send_text(message.chat_type, message.chat_id,
                      "[图片渲染不可用]\n" + "\n".join(lines))

# ── 主入口 ──

async def handle_test_command(message, cleaned: str, mm: ModelManager, bot):
    """处理 /test 指令，message 为 ChatMessage，bot 为 NapcatBot。"""
    import shlex
    try:
        parts = shlex.split(cleaned)
    except ValueError as exc:
        bot.send_text(message.chat_type, message.chat_id, f"指令解析失败: {exc}")
        return

    if len(parts) == 1:
        bot.send_text(message.chat_type, message.chat_id, _cmd_list(mm))
        return

    arg = parts[1].strip()

    if arg.lower() == "all":
        await _cmd_all_channels(mm, bot, message)
        return
    if arg.lower() == "alls":
        await _cmd_all_models(mm, bot, message)
        return

    ch = _match_channel(mm, arg)
    if ch is not None:
        await _cmd_test_channel(mm, ch, bot, message)
        return

    mi = _match_model(mm, arg)
    if mi is not None:
        await _cmd_test_one_model(mm, mi, bot, message)
        return

    bot.send_text(message.chat_type, message.chat_id,
                  f"未找到匹配的渠道或模型: {arg}\n输入 /test 查看列表。")

# ── 子命令 ──

def _cmd_list(mm: ModelManager) -> str:
    channels = mm.config.get("channels") or []
    ch_parts = []
    for i, ch in enumerate(channels):
        if i < 26:
            ch_parts.append(f"{chr(ord('A') + i)}:{ch.get('name', '')}")
    ch_text = "  ".join(ch_parts) if ch_parts else "(无渠道)"

    models = _collect_all_models(mm)
    mdl_parts = []
    for i, m in enumerate(models):
        mdl_parts.append(f'{i + 1}:{m["channel"]}/{m["upstream"]}/{m["model_id"]}')
    mdl_text = "  ".join(mdl_parts) if mdl_parts else "(无模型)"

    return f"渠道: {ch_text}\n模型: {mdl_text}\n输入 /test <编号或名称> 测试"

async def _cmd_test_channel(mm: ModelManager, channel: dict, bot, message):
    ch_name = str(channel.get("name") or "")
    strategy = str(channel.get("strategy") or "fallback")
    models = channel.get("models") or []

    errors = []
    for m in models:
        up_name = str(m.get("upstream") or "").strip()
        mid = str(m.get("model_id") or "").strip()
        up = mm._find_upstream(up_name)
        if not up:
            errors.append(f'[X] {ch_name}/{up_name}/{mid}: 上游不存在')
            continue
        r = await _test_request(up, mid)
        if r["success"]:
            bot.send_text(message.chat_type, message.chat_id,
                          f'[OK] {ch_name} -> {up_name}/{mid}\n延迟: {r["elapsed_ms"]}ms')
            return
        errors.append(f'[X] {ch_name}/{up_name}/{mid}: {r["error"]}')

    header = f'[FAIL] 渠道 [{ch_name}] 全部失败 (策略: {strategy})'
    all_lines = [header] + errors
    _send_multi(bot, message, all_lines, title=f"Test [{ch_name}]")

async def _cmd_test_one_model(mm: ModelManager, mi: dict, bot, message):
    up = mi["upstream"]
    mid = mi["model_id"]
    ch_name = mi["channel_name"]
    up_name = mi["upstream_name"]
    r = await _test_request(up, mid)
    full_name = f"{ch_name}/{up_name}/{mid}"
    if r["success"]:
        bot.send_text(message.chat_type, message.chat_id,
                      f'[OK] {full_name}\n延迟: {r["elapsed_ms"]}ms')
    else:
        bot.send_text(message.chat_type, message.chat_id,
                      f'[FAIL] {full_name}\n{r["error"]}')

async def _cmd_all_channels(mm: ModelManager, bot, message):
    channels = mm.config.get("channels") or []
    if not channels:
        bot.send_text(message.chat_type, message.chat_id, "没有渠道可测试。")
        return

    lines = []
    for ch in channels:
        ch_name = str(ch.get("name") or "")
        strategy = str(ch.get("strategy") or "fallback")
        models = ch.get("models") or []
        success = False
        best = ""
        for m in models:
            up_name = str(m.get("upstream") or "").strip()
            mid = str(m.get("model_id") or "").strip()
            up = mm._find_upstream(up_name)
            if not up:
                continue
            r = await _test_request(up, mid)
            if r["success"]:
                success = True
                best = f" -> {up_name}/{mid} {r['elapsed_ms']}ms"
                break
        if success:
            lines.append(f"[OK] {ch_name}{best}")
        else:
            lines.append(f"[FAIL] {ch_name} [{strategy}]")

    _send_multi(bot, message, lines, title="Test All Channels")

async def _cmd_all_models(mm: ModelManager, bot, message):
    models = _collect_all_models(mm)
    if not models:
        bot.send_text(message.chat_type, message.chat_id, "没有模型可测试。")
        return

    lines = []
    for m in models:
        up = mm._find_upstream(m["upstream"])
        if not up:
            lines.append(f'[X] {m["channel"]}/{m["upstream"]}/{m["model_id"]}: 上游不存在')
            continue
        r = await _test_request(up, m["model_id"])
        full = f'{m["channel"]}/{m["upstream"]}/{m["model_id"]}'
        if r["success"]:
            lines.append(f"[OK] {full} - {r['elapsed_ms']}ms")
        else:
            lines.append(f"[FAIL] {full} - {r['error']}")

    _send_multi(bot, message, lines, title="Test All Models")
