"""只读模型可用性探测服务。

接收 ModelManager 解析出的已配置目标，不接受任意 URL/key/prompt，
避免 SSRF 和凭据进入工具日志。
"""

import re
import time

from pack.anthropic_chat_model import AnthropicChatModel
from pack.console_logger import error, info, warn

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

_VALIDATION_TIMEOUT = 15
_FIXED_SYSTEM = 'You are a test assistant.'
_FIXED_PROMPT = 'Reply with the single word: ok'
# 推理模型会把大量 token 消耗在 reasoning_content 上；16 个 token 会被推理全部
# 吃掉，导致 text 为空而被误判“空回复”。这里给足推理预算，让模型能输出最终答案。
_FIXED_MAX_TOKENS = 1024


def _extract_http_status(exc: Exception) -> int | None:
    m = re.search(r'status[=\s:]*(\d{3})', str(exc), re.IGNORECASE)
    return int(m.group(1)) if m else None


def _classify_error(exc: Exception) -> dict:
    """把探测异常映射为结构化分类：category + 真实 HTTP 状态 + 连接阶段。

    返回示例：
      {'category': 'server_error', 'http_status': 502, 'stage': 'response'}
      {'category': 'connection_reset', 'http_status': None, 'stage': 'read'}
    """
    msg = str(exc or '')
    low = msg.lower()
    status = _extract_http_status(exc)
    if status is not None:
        if status == 401:
            return {'category': 'unauthorized', 'http_status': status, 'stage': 'response'}
        if status == 403:
            return {'category': 'forbidden', 'http_status': status, 'stage': 'response'}
        if status == 429:
            return {'category': 'rate_limited', 'http_status': status, 'stage': 'response'}
        if status == 404:
            return {'category': 'not_found', 'http_status': status, 'stage': 'response'}
        if status >= 500:
            return {'category': 'server_error', 'http_status': status, 'stage': 'response'}
        return {'category': 'http_error', 'http_status': status, 'stage': 'response'}
    cls = type(exc).__name__.lower()
    if requests is not None and isinstance(exc, requests.exceptions.ConnectTimeout):
        return {'category': 'connect_timeout', 'http_status': None, 'stage': 'connect'}
    if requests is not None and isinstance(exc, requests.exceptions.SSLError):
        return {'category': 'tls_error', 'http_status': None, 'stage': 'tls'}
    if requests is not None and isinstance(exc, requests.exceptions.ReadTimeout):
        return {'category': 'read_timeout', 'http_status': None, 'stage': 'read'}
    if 'connection reset' in low or 'connection aborted' in low or 'broken pipe' in low or 'eof' in low or 'chunked' in low:
        return {'category': 'connection_reset', 'http_status': None, 'stage': 'read'}
    if 'connect timeout' in low or 'timed out while connecting' in low or cls == 'connecttimeout':
        return {'category': 'connect_timeout', 'http_status': None, 'stage': 'connect'}
    if 'name or service not known' in low or 'getaddrinfo' in low or 'nodename nor servname' in low or 'dns' in low:
        return {'category': 'dns_error', 'http_status': None, 'stage': 'connect'}
    if 'connection refused' in low or 'connect refused' in low:
        return {'category': 'connection_refused', 'http_status': None, 'stage': 'connect'}
    if 'ssl' in low or 'tls' in low or 'handshake' in low or 'certificate' in low or cls == 'sslerror':
        return {'category': 'tls_error', 'http_status': None, 'stage': 'tls'}
    if 'timeout' in low or 'timed out' in low:
        return {'category': 'timeout', 'http_status': None, 'stage': 'read'}
    if '空内容' in msg or 'empty' in low:
        return {'category': 'empty_reply', 'http_status': None, 'stage': 'response'}
    return {'category': 'protocol_error', 'http_status': status, 'stage': 'unknown'}


def _probe(cfg: dict) -> dict:
    client = AnthropicChatModel(
        base_url=cfg['base_url'],
        api_key=cfg['api_key'],
        model_name=cfg['model_name'],
        messages_path=cfg['messages_path'],
        request_timeout=_VALIDATION_TIMEOUT,
    )
    t0 = time.perf_counter()
    try:
        reply = client.complete(
            system_blocks=_FIXED_SYSTEM,
            messages=[{'role': 'user', 'content': _FIXED_PROMPT}],
            tools=None,
            temperature=0,
            max_tokens=_FIXED_MAX_TOKENS,
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        ok = reply is not None and bool(reply.text or reply.tool_calls)
        return {
            'ok': ok,
            'channel': cfg['channel_name'],
            'upstream': cfg['upstream_name'],
            'model_id': cfg['model_name'],
            'display_name': cfg['display_name'],
            'elapsed_ms': elapsed_ms,
            'error': None if ok else 'empty_reply',
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        classified = _classify_error(exc)
        warn(f'[ModelValidation] {cfg["display_name"]} -> {classified}')
        return {
            'ok': False,
            'channel': cfg['channel_name'],
            'upstream': cfg['upstream_name'],
            'model_id': cfg['model_name'],
            'display_name': cfg['display_name'],
            'elapsed_ms': elapsed_ms,
            'error': classified['category'],
            'http_status': classified['http_status'],
            'stage': classified['stage'],
        }


class ModelValidationService:
    def __init__(self, model_manager):
        self._mm = model_manager

    def validate_model(self, channel: str, upstream: str, model_id: str) -> dict:
        """精确验证单个 channel+upstream+model_id 组合。"""
        cfg = self._mm.resolve_exact_model(channel, upstream, model_id)
        if cfg is None:
            return {
                'ok': False,
                'channel': channel,
                'upstream': upstream,
                'model_id': model_id,
                'display_name': f'{upstream}/{model_id}',
                'elapsed_ms': 0,
                'error': 'not_configured',
            }
        info(f'[ModelValidation] probing {cfg["display_name"]}')
        return _probe(cfg)

    def validate_channel(self, channel_name: str) -> list[dict]:
        """逐个验证渠道内所有模型并汇总，不修改任何渠道游标。"""
        models = self._mm.resolve_channel_models(channel_name)
        if not models:
            return [{'ok': False, 'channel': channel_name, 'error': 'channel_not_found'}]
        results = []
        for cfg in models:
            info(f'[ModelValidation] probing {cfg["display_name"]}')
            results.append(_probe(cfg))
        return results
