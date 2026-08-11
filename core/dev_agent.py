import asyncio
import difflib
import fnmatch
import importlib
import inspect
import json
import os
import posixpath
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from pack.anthropic_chat_model import AnthropicChatModel
from pack.github_service import GitHubService
from pack.console_logger import error, warn
from core.logger import get_bot_logger, CAT_API, CAT_AGENT
from core.config import SSHProfileConfig

MAX_ITERATIONS = 100
MAX_FILE_BYTES = 300_000
MAX_FILE_OPERATION_BYTES = 5_000_000
MAX_FILE_CHUNK_BYTES = 200_000
# lines 模式流式读取允许的最大文件字节数（内存有界，仅线性扫描计数）
MAX_STREAM_LINE_FILE_BYTES = 256 * 1024 * 1024
SSH_TRANSFER_DEFAULT_CHUNK_BYTES = 2 * 1024 * 1024
SSH_TRANSFER_MAX_CHUNK_BYTES = 16 * 1024 * 1024
SSH_TRANSFER_DEFAULT_TIMEOUT_SECONDS = 120
SSH_CONNECT_VALIDATE_TIMEOUT_SECONDS = 30
SSH_PATH_PROBE_TIMEOUT_SECONDS = 45
SSH_LIST_TIMEOUT_SECONDS = 45
SSH_TEXT_READ_TIMEOUT_SECONDS = 60
MAX_CONTEXT_CHARS = 120_000
HISTORY_SUMMARY_TRIGGER_MESSAGES = 120
HISTORY_SUMMARY_KEEP_RECENT_MESSAGES = 60
HISTORY_SUMMARY_KEEP_HEAD_MESSAGES = 1
HISTORY_SUMMARY_MAX_ENTRIES = 8
TOOL_RESULT_TRIM_KEEP_RECENT_ROUNDS = 10
AGENT_TODO_ITEM_LIMIT = 64
AGENT_NOTE_ITEM_LIMIT = 64
AGENT_STATE_RENDER_LIMIT = 12
DENYLIST_PREFIXES = ('.env', 'data/msgs', 'data/state')
# 递归搜索时跳过的目录名：体积大、几乎不含人写代码，扫进去只会拖慢并挤占结果预算。
FIND_SKIP_DIR_NAMES = frozenset({
    '.git', '.hg', '.svn', 'node_modules', '__pycache__', '.venv', 'venv',
    'env', '.mypy_cache', '.pytest_cache', '.ruff_cache', 'dist', 'build',
    '.idea', '.vscode', '.next', 'target', 'site-packages', '.tox',
})
FIND_MAX_SCAN_FILES = 20_000
FIND_CONTENT_MAX_FILE_BYTES = 2_000_000
FIND_DEFAULT_MAX_RESULTS = 40
FIND_MAX_RESULTS_CAP = 200
API_MAX_RETRIES = 3
API_RETRY_BASE_DELAY = 1.2
API_RETRY_MAX_DELAY = 8.0
SHELL_DEFAULT_TIMEOUT_SECONDS = 20
SSH_SHELL_DEFAULT_TIMEOUT_SECONDS = 60
SHELL_DEFAULT_BACKGROUND_TIMEOUT_SECONDS = 600
SHELL_MAX_TIMEOUT_SECONDS = 3600
SHELL_MAX_OUTPUT_CHARS = 12000
SHELL_DEFAULT_TAIL_LINES = 80
SHELL_MAX_TAIL_LINES = 200
_BLOCKING_RUNNER = None


def _diag_scope_for_ssh(profile: SSHProfileConfig | None) -> str:
    if profile is None:
        return 'ssh:(unknown)'
    profile_id = str(getattr(profile, 'profile_id', '') or '').strip() or '(unknown)'
    return f'ssh:{profile_id}'


def _diag_text(value, limit: int = 200) -> str:
    text = str(value or '').replace('\r', '\\r').replace('\n', '\\n')
    if len(text) > limit:
        return text[:limit] + '...'
    return text


def _log_ssh_diag(profile: SSHProfileConfig | None, level: str, message: str) -> None:
    logger = get_bot_logger()
    scope = _diag_scope_for_ssh(profile)
    try:
        if level == 'error':
            logger.error(CAT_AGENT, scope, message)
        elif level == 'warn':
            logger.warn(CAT_AGENT, scope, message)
        else:
            logger.info(CAT_AGENT, scope, message)
    except Exception:
        pass


def _debug_report_ssh_timeout(hypothesis_id: str, location: str, msg: str, data: dict | None = None, *, run_id: str = 'post-fix') -> None:
    # #region debug-point DBG:report
    try:
        _p = os.path.join(_project_root(), '.dbg', 'ssh-agent-timeout.env')
        _u, _s = 'http://127.0.0.1:7777/event', 'ssh-agent-timeout'
        try:
            with open(_p, 'r', encoding='utf-8') as _f:
                _c = _f.read()
            _u = next((line.split('=', 1)[1] for line in _c.splitlines() if line.startswith('DEBUG_SERVER_URL=')), _u)
            _s = next((line.split('=', 1)[1] for line in _c.splitlines() if line.startswith('DEBUG_SESSION_ID=')), _s)
        except Exception:
            pass
        __import__('urllib.request').request.urlopen(
            __import__('urllib.request').request.Request(
                _u,
                data=json.dumps(
                    {
                        'sessionId': _s,
                        'runId': run_id,
                        'hypothesisId': str(hypothesis_id or ''),
                        'location': str(location or ''),
                        'msg': f'[DEBUG] {msg}',
                        'data': data or {},
                        'ts': int(time.time() * 1000),
                    }
                ).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            ),
            timeout=1.5,
        ).read()
    except Exception:
        pass
    # #endregion


def _debug_report_ssh_agent_runtime(hypothesis_id: str, location: str, msg: str, data: dict | None = None, *, run_id: str = 'pre-fix') -> None:
    # #region debug-point C:ssh-agent-runtime-report
    try:
        _p = os.path.join(_project_root(), '.dbg', 'ssh-agent-report-timeout.env')
        _u, _s = 'http://127.0.0.1:7777/event', 'ssh-agent-report-timeout'
        try:
            with open(_p, 'r', encoding='utf-8') as _f:
                _c = _f.read()
            _u = next((line.split('=', 1)[1] for line in _c.splitlines() if line.startswith('DEBUG_SERVER_URL=')), _u)
            _s = next((line.split('=', 1)[1] for line in _c.splitlines() if line.startswith('DEBUG_SESSION_ID=')), _s)
        except Exception:
            pass
        __import__('urllib.request').request.urlopen(
            __import__('urllib.request').request.Request(
                _u,
                data=json.dumps(
                    {
                        'sessionId': _s,
                        'runId': run_id,
                        'hypothesisId': str(hypothesis_id or ''),
                        'location': str(location or ''),
                        'msg': f'[DEBUG] {msg}',
                        'data': data or {},
                        'ts': int(time.time() * 1000),
                    }
                ).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            ),
            timeout=1.5,
        ).read()
    except Exception:
        pass
    # #endregion


def set_blocking_runner(runner) -> None:
    global _BLOCKING_RUNNER
    if runner is not None and not callable(runner):
        raise TypeError('runner must be callable or None')
    _BLOCKING_RUNNER = runner


async def _run_blocking(func, /, *args, **kwargs):
    if _BLOCKING_RUNNER is not None:
        return await _BLOCKING_RUNNER(func, *args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)


# 只有明确列入白名单、无副作用且参数之间不依赖的只读工具才允许并发。
# 未知工具和未来新增工具默认串行，避免把潜在写操作误判为只读。
PARALLEL_LOCAL_READ_TOOLS = frozenset({
    'list_local_files',
    'find_in_project',
    'read_local_file',
    'read_local_file_chunk',
    'search_local_file',
    'query_local_file_json',
})
PARALLEL_GITHUB_READ_TOOLS = frozenset({
    'github_search_code',
    'github_read_file',
    'github_list_repos',
    'github_search_repos',
    'github_list_branches',
    'github_list_pull_requests',
    'github_list_issues',
    'github_list_commits',
    'github_get_commit',
})
PARALLEL_READ_TOOLS = PARALLEL_LOCAL_READ_TOOLS | PARALLEL_GITHUB_READ_TOOLS
LOCAL_WRITE_TOOLS = frozenset({
    'replace_local_file_text',
    'replace_local_file_lines',
    'insert_local_file_lines',
    'delete_local_file_lines',
    'replace_local_file_regex',
    'apply_unified_diff_to_file',
    'edit_local_file',
})
GITHUB_WRITE_TOOLS = frozenset({
    'github_create_or_update_file',
    'github_delete_file',
    'github_create_branch',
    'github_create_tag',
    'github_create_pull_request',
    'github_merge_pull_request',
    'github_close_pull_request',
    'github_create_issue',
    'github_add_issue_comment',
    'github_close_issue',
})
READ_ONLY_AGENT_TOOLS = frozenset(PARALLEL_READ_TOOLS | {'shell_status', 'shell_stop', 'shell_list', 'shell_exec'})
SSH_TRANSFER_START_TOOLS = frozenset({'ssh_download_file', 'ssh_upload_file'})
SSH_TRANSFER_CONTROL_TOOLS = frozenset({'ssh_transfer_status', 'ssh_transfer_cancel', 'ssh_transfer_list'})
READ_ONLY_AGENT_TOOLS = frozenset(READ_ONLY_AGENT_TOOLS | SSH_TRANSFER_CONTROL_TOOLS)

# 常驻 agent 的三个显式沟通出口。原先"汇报/提问/完成"都挤在同一个纯文本通道上：
# 输出纯文本一律被判成"在等上级答复"，想顺手说一句进展就会被误挂成 waiting，
# 完成还得靠模型记得手写 [[AGENT_DONE]] 标记。拆成工具后语义由调用决定，不靠猜。
RESIDENT_AGENT_COMM_TOOLS = (
    {
        'name': 'report_progress',
        'description': (
            '向上级汇报当前进展，然后继续干活（不会中断你的回合，也不会进入等待）。'
            '适合阶段性完成、发现重要情况、方向有调整时主动同步。'
            '注意：这个工具不会等来上级答复，需要上级拍板请用 ask_supervisor。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'text': {'type': 'string', 'description': '要汇报的进展内容，聚焦做了什么、结果如何、下一步'},
            },
            'required': ['text'],
        },
    },
    {
        'name': 'ask_supervisor',
        'description': (
            '向上级提问并停下等待答复（本回合结束，状态转 waiting，收到回复后从原上下文继续）。'
            '指令有歧义、缺关键信息、要做有风险/难回滚的操作前用这个。'
            '只是想同步进展不要用它，否则会白等一轮。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'question': {'type': 'string', 'description': '要问的问题，讲清背景和卡点'},
                'options': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': '可选，给上级的候选方案，便于快速拍板',
                },
                'recommendation': {'type': 'string', 'description': '可选，你倾向的方案及理由'},
            },
            'required': ['question'],
        },
    },
    {
        'name': 'finish_task',
        'description': (
            '本轮任务真正干完时调用，提交最终汇报并进入待命（状态转 idle）。'
            '调用它就等于打了完成标记，不需要再手写 [[AGENT_DONE]]。'
            '失败、受阻、还需要上级拍板都不算完成，那些情况请用 ask_supervisor。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'summary': {'type': 'string', 'description': '最终汇报：做了什么、动了哪些文件、验证结果'},
                'follow_up': {'type': 'string', 'description': '可选，遗留问题或建议的后续动作'},
            },
            'required': ['summary'],
        },
    },
)
RESIDENT_AGENT_COMM_TOOL_NAMES = frozenset(tool['name'] for tool in RESIDENT_AGENT_COMM_TOOLS)

# 只读模式下 shell_exec 允许的白名单命令（只读、无副作用的诊断/查询类命令）。
# 判定规则（运算符只在“独立 token”层面识别，不再整串一刀切拦截）：
# - 命令可按 ; 或 && 或 || 分段，段间可用 | 管道连接，每段/每侧首命令必须在白名单内；
# - 引号内的同类字符不算运算符：grep -E 'a|b'、grep 'a\|b'、grep ";" 均放行；
# - 硬禁区：后台 &、写入重定向 > / >> / >& / |&、heredoc(<<)、进程替换 <(、子 shell ( )、
#   $()/反引号、.. 路径穿越；
# - 单 < 输入重定向放行（只读无副作用），其后一个词是文件名不作命令校验；
# - find 的 -exec/-execdir/-delete/-ok、sed 的 -i、awk 函数调用写法禁止。
READ_ONLY_SHELL_COMMANDS = frozenset({
    'ls', 'cat', 'grep', 'egrep', 'fgrep', 'head', 'tail', 'wc',
    'stat', 'du', 'find', 'diff', 'diff3', 'file', 'echo', 'printf', 'env',
    'printenv', 'pwd', 'whoami', 'id', 'uname', 'date', 'basename', 'dirname',
    'realpath', 'readlink', 'nl', 'sed', 'awk', 'sort', 'uniq', 'cut', 'tr',
    'od', 'xxd', 'base64', 'strings', 'which', 'sha256sum', 'md5sum', 'cksum',
    'df', 'true', 'false', 'zcat', 'bzcat', 'xzcat',
})
# 出现即拒绝的“可写/逃逸”片段（整串硬扫：$() 在双引号内同样执行，反引号同理）
READ_ONLY_SHELL_HARD_BLOCKED_FRAGMENTS = ('$(', '`', '..')
# 允许的分段/连接运算符（独立 token，引号感知）
_READ_ONLY_SHELL_SEPARATORS = {';', '|', '&&'}
# 禁止的运算符（独立 token，引号感知）
_READ_ONLY_SHELL_BLOCKED_OPERATORS = {'&', '>', '>>', '>&', '|&', '<<', '<>', '<(', '(', ')'}
# find 的破坏性参数
READ_ONLY_SHELL_FIND_BLOCKED_ARGS = {'-exec', '-execdir', '-delete', '-ok'}


def _strip_shell_token_quotes(tok: str) -> str:
    """posix=False 分词会保留引号，去掉首尾成对引号后再做参数比对（bash 里 '-i' 等同 -i）。"""
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
        return tok[1:-1]
    return tok


def _is_read_only_shell_command(command: str) -> tuple[bool, str]:
    """校验 shell 命令是否为只读白名单命令。返回 (是否允许, 拒绝原因)。

    用 shlex punctuation_chars 做“引号感知”分词：运算符只在引号外成为独立 token，
    引号内的 | ; > < ( ) 等是 grep 模式/参数，不是 shell 语法。
    """
    raw = str(command or '')
    if not raw.strip():
        return False, '命令为空'
    if any(fragment in raw for fragment in READ_ONLY_SHELL_HARD_BLOCKED_FRAGMENTS):
        return False, '命令含 $()/反引号/.. 等逃逸写法，只读模式禁止'
    try:
        tokens = list(shlex.shlex(raw, posix=False, punctuation_chars=';&|<>()'))
    except ValueError as exc:
        return False, f'命令无法解析: {exc}'
    commands: list[list[str]] = []
    cur: list[str] = []
    skip_target = False  # < 之后的一个词是重定向目标（文件名），不是命令
    for tok in tokens:
        if tok in _READ_ONLY_SHELL_SEPARATORS:
            if cur:
                commands.append(cur)
                cur = []
            skip_target = False
            continue
        if tok in _READ_ONLY_SHELL_BLOCKED_OPERATORS:
            return False, f'命令含禁止运算符 {tok!r}，只读模式禁止'
        if tok == '<':
            skip_target = True
            continue
        if skip_target:
            skip_target = False
            continue
        cur.append(tok)
    if cur:
        commands.append(cur)
    for cmd_words in commands:
        first = _strip_shell_token_quotes(cmd_words[0]).lstrip('/').split('/')[-1]
        if first not in READ_ONLY_SHELL_COMMANDS:
            return False, f'命令 {first!r} 不在只读白名单内'
        unquoted = [_strip_shell_token_quotes(w) for w in cmd_words]
        if first == 'find' and any(arg in READ_ONLY_SHELL_FIND_BLOCKED_ARGS for arg in unquoted):
            return False, 'find 的 -exec/-delete 等破坏性参数被禁止'
        if first == 'sed' and any(arg in ('-i', '--in-place') for arg in unquoted):
            return False, 'sed 的 -i 原地修改被禁止'
        if first == 'awk' and any('(' in w for w in cmd_words):
            return False, 'awk 含函数调用写法被禁止（可能逃逸执行）'
    return True, ''

# 稳定优先：本地读取适度并发；GitHub 单独限流，降低 API 限流和网络抖动风险。
LOCAL_READ_CONCURRENCY = 4
GITHUB_READ_CONCURRENCY = 2
MAX_PARALLEL_READ_SUB_BATCH = 8

# 结果预算只影响回填给模型/持久化的文本，不改变工具实际执行。
# 编辑和 shell 保留更高单项预算并采用头尾保留，避免丢失 diff 结尾和诊断错误。
MAX_TOOL_BATCH_RESULT_CHARS = 80_000
TOOL_RESULT_LIMITS = {
    'list_local_files': 16_000,
    'find_in_project': 24_000,
    'search_local_file': 24_000,
    'read_local_file': 36_000,
    'read_local_file_chunk': 36_000,
    'query_local_file_json': 36_000,
    'github_search_code': 20_000,
    'github_read_file': 36_000,
    'shell_exec': 36_000,
    'shell_status': 24_000,
    'ssh_transfer_status': 24_000,
    'ssh_transfer_list': 24_000,
}
DEFAULT_READ_RESULT_LIMIT = 24_000
DEFAULT_SERIAL_RESULT_LIMIT = 60_000
MIN_TOOL_RESULT_CHARS = 2_000


class RetryableAPIError(RuntimeError):
    pass


def _is_retryable_api_error(exc: Exception) -> bool:
    if isinstance(exc, RetryableAPIError):
        return True
    message = f'{type(exc).__name__}: {exc}'.lower()
    keywords = (
        'timeout', 'timed out', 'connection reset', 'connection aborted', 'connection refused',
        'temporarily unavailable', 'temporary failure', 'temporarily overloaded',
        'overloaded', 'rate limit', '429', '500', '502', '503', '504',
        'service unavailable', 'bad gateway', 'gateway timeout',
        'remoteprotocolerror', 'apiconnectionerror', 'read error', 'network',
        'socket', 'ssl', 'eof', 'server error',
        '空内容', 'empty content', 'empty response',
    )
    return any(keyword in message for keyword in keywords)


def _retry_sleep_seconds(attempt: int) -> float:
    base = API_RETRY_BASE_DELAY * (2 ** max(0, attempt - 1))
    jitter = random.uniform(0, 0.35)
    return min(API_RETRY_MAX_DELAY, base + jitter)


def _call_with_retry(label: str, fn, max_retries: int = API_MAX_RETRIES):
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries or not _is_retryable_api_error(exc):
                raise
            delay = _retry_sleep_seconds(attempt)
            try:
                get_bot_logger().warn(CAT_API, '', f'API 重试 {label} attempt={attempt}/{max_retries} delay={delay:.1f}s error={exc}')
            except Exception:
                pass
            warn(f'[DevAgent] {label} 失败，第 {attempt}/{max_retries} 次重试前等待 {delay:.1f}s: {exc}')
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f'{label} 失败，且未捕获到具体异常。')


def _complete_with_valid_response(
    model,
    system_prompt,
    messages,
    tools,
    temperature,
    max_tokens,
    *,
    require_content: bool = False,
):
    """调用模型并显式初始化响应，避免协议/异常分支读取未绑定局部变量。"""
    response = None
    response = model.complete(system_prompt, messages, tools, None, temperature, max_tokens)
    if response is None or (require_content and not (response.tool_calls or response.text)):
        suffix = '（空内容）' if require_content else ''
        raise RetryableAPIError(f'模型没有返回有效响应{suffix}')
    return response


def _render_messages_for_summary(messages: list[dict], limit: int = MAX_CONTEXT_CHARS) -> str:
    """把 agent 上下文压平成可读文本，供总结模型或兜底摘要使用。"""
    lines: list[str] = []
    for msg in messages or []:
        role = str(msg.get('role') or '')
        content = msg.get('content')
        if isinstance(content, str):
            lines.append(f'[{role}] {content}')
            continue
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    lines.append(f'[{role}] {block}')
                    continue
                block_type = block.get('type')
                if block_type == 'text':
                    lines.append(f'[{role}] {block.get("text") or ""}')
                elif block_type == 'tool_use':
                    tool_input = json.dumps(block.get('input') or {}, ensure_ascii=False)
                    lines.append(f'[{role} 调用工具] {block.get("name") or ""} 参数={tool_input}')
                elif block_type == 'tool_result':
                    result_text = block.get('content')
                    if not isinstance(result_text, str):
                        result_text = json.dumps(result_text, ensure_ascii=False)
                    lines.append(f'[工具结果] {result_text}')
                else:
                    lines.append(f'[{role}] {json.dumps(block, ensure_ascii=False)}')
            continue
        if content is not None:
            lines.append(f'[{role}] {content}')
    rendered = '\n'.join(lines)
    if len(rendered) <= limit:
        return rendered
    head = rendered[: limit // 2]
    tail = rendered[-(limit // 2):]
    return f'{head}\n\n……（中间上下文过长已省略）……\n\n{tail}'


def _plan_history_compaction(
    messages: list[dict],
    trigger_messages: int = HISTORY_SUMMARY_TRIGGER_MESSAGES,
    keep_recent_messages: int = HISTORY_SUMMARY_KEEP_RECENT_MESSAGES,
    keep_head_messages: int = HISTORY_SUMMARY_KEEP_HEAD_MESSAGES,
) -> tuple[list[dict], list[dict]]:
    """当消息条数过大时，返回（待总结历史，保留消息）。"""
    items = [dict(message) for message in (messages or [])]
    if len(items) <= max(1, int(trigger_messages or 0)):
        return [], items
    keep_recent = max(1, int(keep_recent_messages or 1))
    keep_head = max(0, int(keep_head_messages or 0))
    if len(items) <= keep_head + keep_recent:
        return [], items
    summary_slice_end = len(items) - keep_recent
    # 不要把 assistant 的 tool_use 和下一条 user 的 tool_result 切开。
    # 否则压缩后保留下来的近期上下文会以孤立 tool_result 开头，
    # OpenAI/Anthropic 兼容上游会因找不到对应 tool_use 而直接报 400。
    while summary_slice_end > keep_head:
        current = items[summary_slice_end]
        previous = items[summary_slice_end - 1]
        current_blocks = current.get('content') if isinstance(current, dict) else None
        previous_blocks = previous.get('content') if isinstance(previous, dict) else None
        current_is_tool_result = (
            isinstance(current, dict)
            and current.get('role') == 'user'
            and isinstance(current_blocks, list)
            and any(isinstance(block, dict) and block.get('type') == 'tool_result' for block in current_blocks)
        )
        previous_has_tool_use = (
            isinstance(previous, dict)
            and previous.get('role') == 'assistant'
            and isinstance(previous_blocks, list)
            and any(isinstance(block, dict) and block.get('type') == 'tool_use' for block in previous_blocks)
        )
        if not (current_is_tool_result and previous_has_tool_use):
            break
        summary_slice_end -= 1
    removable = [dict(message) for message in items[keep_head:summary_slice_end]]
    if not removable:
        return [], items
    kept = [dict(message) for message in items[:keep_head]]
    kept.extend(dict(message) for message in items[summary_slice_end:])
    return removable, kept


def _build_history_summary_fallback(messages: list[dict]) -> str:
    rendered = _render_messages_for_summary(messages, limit=8_000).strip()
    if not rendered:
        return '本段历史没有可提取的有效内容。'
    if len(rendered) > 2_000:
        rendered = rendered[:2_000] + '\n……（后续细节已省略）'
    return (
        '自动压缩的历史摘要：\n'
        f'{rendered}\n'
        '请以后续保留的近期上下文、todo 列表和备注为准继续执行。'
    )


async def _summarize_history_chunk(model, removed_messages: list[dict]) -> str:
    if model is None:
        return _build_history_summary_fallback(removed_messages)
    rendered = _render_messages_for_summary(removed_messages, limit=MAX_CONTEXT_CHARS)
    prompt = (
        '下面是一段即将从 agent 实时上下文中移除的较早历史，请把它压缩成一段后续可继续工作的摘要。\n'
        '要求：\n'
        '1. 不要总结 system prompt，也不要改写或覆盖原始任务指令。\n'
        '2. 只总结这段历史里已经发生的事实：做过的检查、改过的文件、运行过的命令、发现的问题、未完成事项、风险、待验证点。\n'
        '3. 不要编造成果，不要输出新的行动指令。\n'
        '4. 用中文，简洁但保留工程关键信息。\n\n'
        f'历史内容：\n------8<------\n{rendered}\n------8<------'
    )
    try:
        reply = await _run_blocking(
            model.complete,
            '你是 agent 的上下文压缩助手，只做客观历史摘要，不调用任何工具，不重写任务目标。',
            [{'role': 'user', 'content': prompt}],
            None,
            None,
            0.2,
            1024,
        )
    except Exception:
        return _build_history_summary_fallback(removed_messages)
    summary = (reply.text if reply else '').strip()
    return summary or _build_history_summary_fallback(removed_messages)


def _append_history_summary(
    summaries: list[dict],
    summary_text: str,
    *,
    max_entries: int = HISTORY_SUMMARY_MAX_ENTRIES,
) -> list[dict]:
    text = str(summary_text or '').strip()
    entries = [dict(item) for item in (summaries or []) if isinstance(item, dict)]
    if not text:
        return entries
    entries.append({'summary': text, 'created_at': time.time()})
    keep = max(1, int(max_entries or 1))
    if len(entries) > keep:
        older = entries[: len(entries) - keep + 1]
        merged = '\n'.join(
            f"- {str(item.get('summary') or '').strip()}"
            for item in older
            if str(item.get('summary') or '').strip()
        ).strip()
        entries = [{'summary': f'更早历史摘要汇总：\n{merged}', 'created_at': time.time()}] + entries[-(keep - 1):]
    return entries


def _clip_agent_state_text(value: str, limit: int = 220) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + '…'


def _render_agent_state_prompt(
    history_summaries: list[dict] | None,
    todo_items: list[dict] | None,
    notes: list[dict] | None,
) -> str:
    parts: list[str] = []
    summary_entries = [dict(item) for item in (history_summaries or []) if isinstance(item, dict)]
    if summary_entries:
        lines = []
        for index, item in enumerate(summary_entries[-AGENT_STATE_RENDER_LIMIT:], start=1):
            text = _clip_agent_state_text(item.get('summary') or '', limit=320)
            if text:
                lines.append(f'{index}. {text}')
        if lines:
            parts.append('历史摘要（系统自动压缩生成，不替代原始任务）:\n' + '\n'.join(lines))

    todo_entries = [dict(item) for item in (todo_items or []) if isinstance(item, dict)]
    if todo_entries:
        lines = []
        for item in todo_entries[-AGENT_STATE_RENDER_LIMIT:]:
            todo_id = str(item.get('todo_id') or '无ID')
            status = str(item.get('status') or 'pending')
            content = _clip_agent_state_text(item.get('content') or '', limit=220)
            lines.append(f'- [{status}] {todo_id}: {content}')
        if lines:
            parts.append(
                'Todo 列表（需主动维护，避免遗漏多步任务）:\n'
                + '\n'.join(lines)
            )

    note_entries = [dict(item) for item in (notes or []) if isinstance(item, dict)]
    if note_entries:
        lines = []
        for item in note_entries[-AGENT_STATE_RENDER_LIMIT:]:
            note_id = str(item.get('note_id') or '无ID')
            content = _clip_agent_state_text(item.get('content') or '', limit=220)
            lines.append(f'- {note_id}: {content}')
        if lines:
            parts.append(
                '备注（不能被总结稀释的长期重点，如安全警告、禁区、踩坑记录）:\n'
                + '\n'.join(lines)
            )

    if not parts:
        return ''
    return '\n\n[系统维护状态]\n' + '\n\n'.join(parts)


def _build_capability_matrix(read_only: bool = False, ssh_enabled: bool = False, resident: bool = False) -> str:
    """下发当前会话的能力矩阵：模式、工具边界、结果上限与续读约定。

    工具 schema 只说明“每个工具是什么”，不说明“本会话里哪些被限制、结果多大
    会被截断、截断后怎么续读”。模型靠试错才知道，浪费大量轮次。这里在任务
    开始时一次性说清楚。
    """
    lines = [
        '【当前能力矩阵（系统生成，请严格按此执行，不要尝试矩阵外操作）】',
        f'- 模式：{"只读" if read_only else "可写"}。',
    ]
    if read_only:
        allow_list = sorted(READ_ONLY_SHELL_COMMANDS)
        lines.append(f'- shell_exec 仅允许白名单内只读命令：{", ".join(allow_list)}（sed 禁止 -i，find 禁止 -exec/-delete）。')
        lines.append('- 白名单命令可用 ; / && / || / | 组合（每段命令都须在白名单内）；禁止：后台任务、写入重定向(>)、heredoc(<<)、修改文件、写 GitHub、启动 SSH 传输。')
        lines.append('- 建议：查文件用 read/search/query_local_file_json，跑只读诊断用白名单 shell。')
    else:
        lines.append('- 可执行 shell 与文件读写；写操作前先读再改，尽量用 search 定位行号。')
    lines.append(
        '- 结果上限：read 类/JSON 查询/shell 结果约 36K 字符，搜索 24K 字符；'
        '超出会被头尾截断并附续读提示，按提示续读即可，不要反复从头试。'
    )
    lines.append(
        '- 大文件读取：read_local_file_chunk 的 lines 模式按行流式读取并返回总行数/行号范围；'
        'bytes 模式返回对齐后的 offset_bytes/read_bytes 作为精确续读基准。'
    )
    lines.append(
        '- JSON 文件取少量字段优先用 query_local_file_json（如 $.config.timeout），不要整文件读取浪费上下文。'
    )
    lines.append('- shell 输出统一 UTF-8 解码；中文与控制字符会被保留/清洗，不会导致命令被误判失败。')
    lines.append(
        '- 定位未知文件用 find_in_project 一次搞定（name_pattern 找文件名、content_query 找内容），'
        '不要多轮 list_local_files 下钻，也不要用 shell find/grep 替代。'
    )
    if resident:
        lines.append(
            '- 与上级沟通有三个专用出口：report_progress（同步进展、不中断不等待）、'
            'ask_supervisor（提问并挂起等答复）、finish_task（任务完成并待命）。'
            '状态由你调用哪个决定，不要靠纯文本表达这三件事，也不需要手写 [[AGENT_DONE]]。'
        )
    if ssh_enabled:
        lines.append('- 当前作用于远程服务器：本地文件工具映射到远端；大文件用 ssh_transfer_* 管理传输。')
    return '\n'.join(lines)


def _normalize_todo_status(status: str) -> str:
    value = str(status or '').strip().lower()
    return value if value in {'pending', 'in_progress', 'completed', 'blocked'} else 'pending'


def _apply_todo_write(todo_items: list[dict] | None, tool_input: dict) -> str:
    items = todo_items if isinstance(todo_items, list) else []
    tool_input = tool_input or {}
    action = str(tool_input.get('action') or 'list').strip().lower()
    now = time.time()
    if action == 'add':
        content = str(tool_input.get('content') or '').strip()
        if not content:
            return 'todo 新增失败：content 不能为空。'
        item = {
            'todo_id': tool_input.get('todo_id') or f"todo_{uuid.uuid4().hex[:8]}",
            'content': content,
            'status': _normalize_todo_status(tool_input.get('status') or 'pending'),
            'created_at': now,
            'updated_at': now,
        }
        items.append(item)
        del items[:-AGENT_TODO_ITEM_LIMIT]
        return f"已新增 todo {item['todo_id']} [{item['status']}]: {item['content']}"
    if action == 'update':
        todo_id = str(tool_input.get('todo_id') or '').strip()
        if not todo_id:
            return 'todo 更新失败：todo_id 不能为空。'
        for item in items:
            if str(item.get('todo_id') or '') != todo_id:
                continue
            if tool_input.get('content') is not None:
                content = str(tool_input.get('content') or '').strip()
                if not content:
                    return 'todo 更新失败：content 不能为空字符串。'
                item['content'] = content
            if tool_input.get('status') is not None:
                item['status'] = _normalize_todo_status(tool_input.get('status'))
            item['updated_at'] = now
            return f"已更新 todo {todo_id} [{item.get('status') or 'pending'}]: {item.get('content') or ''}"
        return f'todo 更新失败：没有找到 todo_id={todo_id}。'
    if action == 'remove':
        todo_id = str(tool_input.get('todo_id') or '').strip()
        if not todo_id:
            return 'todo 删除失败：todo_id 不能为空。'
        before = len(items)
        items[:] = [item for item in items if str(item.get('todo_id') or '') != todo_id]
        return f'已删除 todo {todo_id}。' if len(items) != before else f'todo 删除失败：没有找到 todo_id={todo_id}。'
    if action != 'list':
        return 'todo 操作失败：action 只能是 add / update / remove / list。'
    if not items:
        return '当前没有 todo。'
    lines = [f'当前 todo 共 {len(items)} 条：']
    for item in items[-AGENT_STATE_RENDER_LIMIT:]:
        lines.append(
            f"- [{item.get('status') or 'pending'}] {item.get('todo_id') or '无ID'}: {item.get('content') or ''}"
        )
    return '\n'.join(lines)


def _apply_note_write(notes: list[dict] | None, tool_input: dict) -> str:
    items = notes if isinstance(notes, list) else []
    tool_input = tool_input or {}
    action = str(tool_input.get('action') or 'list').strip().lower()
    now = time.time()
    if action == 'add':
        content = str(tool_input.get('content') or '').strip()
        if not content:
            return '备注新增失败：content 不能为空。'
        item = {
            'note_id': tool_input.get('note_id') or f"note_{uuid.uuid4().hex[:8]}",
            'content': content,
            'created_at': now,
            'updated_at': now,
        }
        items.append(item)
        del items[:-AGENT_NOTE_ITEM_LIMIT]
        return f"已新增备注 {item['note_id']}: {item['content']}"
    if action == 'update':
        note_id = str(tool_input.get('note_id') or '').strip()
        content = str(tool_input.get('content') or '').strip()
        if not note_id or not content:
            return '备注更新失败：note_id 和 content 都不能为空。'
        for item in items:
            if str(item.get('note_id') or '') != note_id:
                continue
            item['content'] = content
            item['updated_at'] = now
            return f'已更新备注 {note_id}: {content}'
        return f'备注更新失败：没有找到 note_id={note_id}。'
    if action == 'remove':
        note_id = str(tool_input.get('note_id') or '').strip()
        if not note_id:
            return '备注删除失败：note_id 不能为空。'
        before = len(items)
        items[:] = [item for item in items if str(item.get('note_id') or '') != note_id]
        return f'已删除备注 {note_id}。' if len(items) != before else f'备注删除失败：没有找到 note_id={note_id}。'
    if action != 'list':
        return '备注操作失败：action 只能是 add / update / remove / list。'
    if not items:
        return '当前没有备注。'
    lines = [f'当前备注共 {len(items)} 条：']
    for item in items[-AGENT_STATE_RENDER_LIMIT:]:
        lines.append(f"- {item.get('note_id') or '无ID'}: {item.get('content') or ''}")
    return '\n'.join(lines)


async def _notify_run_finished(on_finished, payload: dict) -> None:
    if on_finished is None:
        return
    try:
        result = on_finished(dict(payload))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        error(f'[DevAgent] 结束回调触发失败: {exc}')


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_safe_path(project_root: str, relative_path: str) -> str | None:
    relative_path = (relative_path or '').strip()
    if not relative_path or os.path.isabs(relative_path):
        return None
    normalized = os.path.normpath(relative_path)
    if normalized == '..' or normalized.startswith('..' + os.sep):
        return None
    normalized_slashes = normalized.replace('\\', '/')
    for prefix in DENYLIST_PREFIXES:
        if normalized_slashes == prefix or normalized_slashes.startswith(prefix + '/'):
            return None
    project_root = os.path.realpath(project_root)
    resolved = os.path.realpath(os.path.join(project_root, normalized))
    try:
        common = os.path.commonpath([resolved, project_root])
    except ValueError:
        return None
    if common != project_root:
        return None
    return resolved


def _normalize_agent_cwd_spec(cwd: str) -> str | None:
    cwd = str(cwd or '').strip()
    if not cwd or cwd == '.':
        return '/'
    if cwd in {'/', '~'}:
        return cwd

    if cwd.startswith('~/'):
        prefix = '~/'
        tail = cwd[2:]
    elif cwd.startswith('/'):
        prefix = '/'
        tail = cwd[1:]
    else:
        prefix = '~/'
        tail = cwd

    normalized = os.path.normpath(str(tail or '').strip())
    if normalized in {'', '.'}:
        return prefix[:-1] if prefix.endswith('/') else prefix
    if normalized.startswith('..') or os.path.isabs(normalized):
        return None
    return prefix + normalized.replace('\\', '/')


def _normalize_repo_relative_path(path: str) -> str | None:
    text = str(path or '').strip()
    if not text:
        return ''
    normalized = os.path.normpath(text.lstrip('/\\'))
    if normalized in {'', '.'}:
        return ''
    if normalized.startswith('..') or os.path.isabs(normalized):
        return None
    normalized_slashes = normalized.replace('\\', '/')
    for prefix in DENYLIST_PREFIXES:
        if normalized_slashes == prefix or normalized_slashes.startswith(prefix + '/'):
            return None
    return normalized_slashes


def _inject_backup_text(result_text: str, backup_path: str) -> str:
    result_text = str(result_text or '')
    marker = '\n应用 diff:\n'
    if marker in result_text:
        head, tail = result_text.split(marker, 1)
        return f'{head}\n备份文件: {backup_path}{marker}{tail}'
    return f'{result_text}\n备份文件: {backup_path}'


def _human_bytes(value: int | float) -> str:
    size = float(max(0, value or 0))
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f'{int(size)} {units[unit_index]}'
    return f'{size:.1f} {units[unit_index]}'


def _local_text_tool_on_temp(
    path: str,
    original_text: str,
    runner,
    *,
    create_input_file: bool = True,
) -> tuple[str, str | None]:
    relative_path = _normalize_repo_relative_path(path)
    if relative_path is None or not relative_path:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝访问。', None
    with tempfile.TemporaryDirectory(prefix='ssh_agent_edit_') as tmp:
        resolved = os.path.join(tmp, relative_path.replace('/', os.sep))
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        if create_input_file:
            # newline='' 原始写入，避免 Windows 文本模式把远端 LF 转 CRLF，导致行尾判定失真
            with open(resolved, 'w', encoding='utf-8', newline='') as f:
                f.write(original_text)
        result = str(runner(tmp, relative_path) or '')
        updated_text = None
        if os.path.isfile(resolved):
            try:
                with open(resolved, 'r', encoding='utf-8') as f:
                    updated_text = f.read()
            except Exception:
                updated_text = None
        return result, updated_text


def _ensure_ssh_binary() -> str | None:
    return shutil.which('ssh')


def _ssh_profile_uses_password(profile: SSHProfileConfig) -> bool:
    return bool(str(getattr(profile, 'password', '') or '').strip())


def _load_paramiko_module():
    try:
        return importlib.import_module('paramiko')
    except Exception:
        return None


def _parse_paramiko_target(profile: SSHProfileConfig) -> tuple[str | None, str | None, str | None]:
    target = str(profile.target or '').strip()
    if not target:
        return None, None, 'SSH 目标为空。'
    username = None
    hostname = target
    if '@' in target:
        username, hostname = target.split('@', 1)
        username = str(username or '').strip() or None
        hostname = str(hostname or '').strip()
    if not hostname:
        return None, None, f'密码登录要求 target 至少包含主机名，当前值无效: {target!r}'
    return hostname, username, None


def _resolve_ssh_identity_file(profile: SSHProfileConfig) -> str:
    """解析 SSH profile 实际使用的 identity_file。

    优先取 profile 显式配置的 identity_file；为空且无密码时，从 ~/.ssh/config
    按 hostname 匹配 Host 段解析 IdentityFile（直连 IP 也能匹配 HostName），
    实现“本机 ssh config 配好即可用”的自动发现，避免误用默认密钥（如 GitHub
    的 ~/.ssh/id_ed25519）导致 Permission denied。通配 Host 段不参与匹配。
    """
    explicit = str(getattr(profile, 'identity_file', '') or '').strip()
    if explicit:
        return explicit
    if _ssh_profile_uses_password(profile):
        return ''
    hostname, _username, _err = _parse_paramiko_target(profile)
    if not hostname:
        return ''
    config_path = Path.home() / '.ssh' / 'config'
    try:
        if not config_path.exists():
            return ''
        segments: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for raw_line in config_path.read_text(encoding='utf-8', errors='replace').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(None, 1)
            key = parts[0].lower()
            value = parts[1].strip() if len(parts) > 1 else ''
            if key == 'host':
                if current:
                    segments.append(current)
                current = {'host': value}
            elif key in ('hostname', 'identityfile'):
                current[key] = value
        if current:
            segments.append(current)
        for seg in segments:
            host_pattern = str(seg.get('host') or '').strip()
            if not host_pattern or any(ch in host_pattern for ch in '*?['):
                continue
            hostname_matches = hostname == host_pattern or (
                seg.get('hostname') and hostname == seg['hostname']
            )
            if not hostname_matches:
                continue
            identity = str(seg.get('identityfile') or '').strip()
            if identity:
                return identity
        return ''
    except Exception:
        return ''


def _run_paramiko_command(
    profile: SSHProfileConfig,
    remote_command: str,
    *,
    timeout_seconds: int | None = None,
    input_bytes: bytes | None = None,
    client=None,
) -> tuple[subprocess.CompletedProcess[bytes] | None, str | None]:
    _log_ssh_diag(
        profile,
        'info',
        f'paramiko 执行开始 target={profile.target} timeout={timeout_seconds} cmd={_diag_text(remote_command)}',
    )
    paramiko = _load_paramiko_module()
    if paramiko is None:
        _log_ssh_diag(profile, 'warn', 'paramiko 不可用，无法进行密码 SSH 登录。')
        return None, '当前环境未安装 paramiko，无法使用 SSH 密码登录。'
    hostname, username, target_err = _parse_paramiko_target(profile)
    if target_err:
        return None, target_err
    uses_password = _ssh_profile_uses_password(profile)
    identity_file = os.path.expanduser(_resolve_ssh_identity_file(profile))
    look_for_keys = (not uses_password) or bool(identity_file)
    allow_agent = (not uses_password) or bool(identity_file)
    own_client = client is None
    ssh_client = client or paramiko.SSHClient()
    try:
        if own_client:
            if bool(profile.strict_host_key_checking):
                ssh_client.load_system_host_keys()
                ssh_client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh_client.connect(
                hostname=hostname,
                port=int(profile.port or 22),
                username=username,
                password=str(profile.password or ''),
                key_filename=identity_file or None,
                look_for_keys=look_for_keys,
                allow_agent=allow_agent,
                timeout=timeout_seconds,
                banner_timeout=timeout_seconds,
                auth_timeout=timeout_seconds,
            )
        stdin, stdout, stderr = ssh_client.exec_command(str(remote_command), timeout=timeout_seconds)
        if input_bytes is not None:
            try:
                stdin.channel.sendall(bytes(input_bytes))
            finally:
                try:
                    stdin.channel.shutdown_write()
                except Exception:
                    pass
        channel = stdout.channel
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        deadline = time.time() + timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        _reported_output = False
        while True:
            if channel.recv_ready():
                data = channel.recv(65536)
                if data:
                    stdout_chunks.append(bytes(data))
                    if not _reported_output:
                        _reported_output = True
                        # #region debug-point C:paramiko-first-output
                        _debug_report_ssh_agent_runtime(
                            'C',
                            'core.dev_agent:_run_paramiko_command:first-output',
                            'paramiko command produced stdout before completion',
                            {
                                'profile_id': str(profile.profile_id or ''),
                                'target': str(profile.target or ''),
                                'stdout_bytes': sum(len(chunk) for chunk in stdout_chunks),
                                'stderr_bytes': sum(len(chunk) for chunk in stderr_chunks),
                                'exit_status_ready': bool(channel.exit_status_ready()),
                                'command': _diag_text(remote_command, 300),
                            },
                        )
                        # #endregion
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(65536)
                if data:
                    stderr_chunks.append(bytes(data))
            if channel.exit_status_ready():
                while channel.recv_ready():
                    data = channel.recv(65536)
                    if data:
                        stdout_chunks.append(bytes(data))
                while channel.recv_stderr_ready():
                    data = channel.recv_stderr(65536)
                    if data:
                        stderr_chunks.append(bytes(data))
                # #region debug-point C:paramiko-exit-ready
                _debug_report_ssh_agent_runtime(
                    'C',
                    'core.dev_agent:_run_paramiko_command:exit-ready',
                    'paramiko command reached exit_status_ready',
                    {
                        'profile_id': str(profile.profile_id or ''),
                        'target': str(profile.target or ''),
                        'stdout_bytes': sum(len(chunk) for chunk in stdout_chunks),
                        'stderr_bytes': sum(len(chunk) for chunk in stderr_chunks),
                        'command': _diag_text(remote_command, 300),
                    },
                )
                # #endregion
                exit_code = int(channel.recv_exit_status())
                break
            if deadline is not None and time.time() > deadline:
                # #region debug-point C:paramiko-timeout
                _debug_report_ssh_agent_runtime(
                    'C',
                    'core.dev_agent:_run_paramiko_command:timeout',
                    'paramiko command hit timeout deadline',
                    {
                        'profile_id': str(profile.profile_id or ''),
                        'target': str(profile.target or ''),
                        'stdout_bytes': sum(len(chunk) for chunk in stdout_chunks),
                        'stderr_bytes': sum(len(chunk) for chunk in stderr_chunks),
                        'exit_status_ready': bool(channel.exit_status_ready()),
                        'command': _diag_text(remote_command, 300),
                    },
                )
                # #endregion
                raise TimeoutError(f'paramiko command timed out after {timeout_seconds}s')
            time.sleep(0.05)
        stdout_bytes = b''.join(stdout_chunks)
        stderr_bytes = b''.join(stderr_chunks)
        completed = subprocess.CompletedProcess(
            args=['paramiko', str(profile.target), str(remote_command)],
            returncode=exit_code,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
        )
        _log_ssh_diag(
            profile,
            'info',
            f'paramiko 执行完成 rc={exit_code} stdout_bytes={len(stdout_bytes)} stderr_bytes={len(stderr_bytes)}',
        )
        return completed, None
    except TimeoutError:
        _log_ssh_diag(profile, 'warn', f'paramiko 执行超时 timeout={timeout_seconds} cmd={_diag_text(remote_command)}')
        return None, f'SSH 命令执行超时（{timeout_seconds} 秒）。'
    except Exception as exc:
        _log_ssh_diag(profile, 'error', f'paramiko 执行失败: {exc}')
        return None, f'Paramiko 执行失败: {exc}'
    finally:
        if own_client:
            try:
                ssh_client.close()
            except Exception:
                pass


def _build_ssh_base_args(profile: SSHProfileConfig) -> list[str] | None:
    if _ssh_profile_uses_password(profile):
        return None
    ssh_bin = _ensure_ssh_binary()
    if not ssh_bin:
        return None
    args = [
        ssh_bin,
        '-T',
        '-o',
        'BatchMode=yes',
        '-o',
        'ConnectTimeout=10',
        '-o',
        'ConnectionAttempts=1',
    ]
    if int(profile.port or 22) > 0:
        args.extend(['-p', str(int(profile.port or 22))])
    identity_file = os.path.expanduser(_resolve_ssh_identity_file(profile))
    if identity_file:
        args.extend(['-i', identity_file])
    if not bool(profile.strict_host_key_checking):
        args.extend(['-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null'])
    args.append(str(profile.target))
    return args


def _run_ssh_command(
    profile: SSHProfileConfig,
    remote_command: str,
    *,
    timeout_seconds: int | None = None,
    input_bytes: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[bytes] | None, str | None]:
    if _ssh_profile_uses_password(profile):
        # #region debug-point B:paramiko-branch
        _debug_report_ssh_timeout(
            'B',
            'core.dev_agent:_run_ssh_command:paramiko',
            'ssh command enters paramiko branch',
            {
                'pid': os.getpid(),
                'thread_id': threading.get_ident(),
                'profile_id': str(profile.profile_id or ''),
                'target': str(profile.target or ''),
                'root_dir': str(profile.root_dir or ''),
                'timeout_seconds': timeout_seconds,
                'command': _diag_text(remote_command, 400),
            },
        )
        # #endregion
        return _run_paramiko_command(
            profile,
            remote_command,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
        )
    if _load_paramiko_module() is not None:
        _log_ssh_diag(profile, 'info', f'SSH 命令优先走 paramiko target={profile.target}')
        primary_completed, primary_err = _run_paramiko_command(
            profile,
            remote_command,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
        )
        if primary_err is None and primary_completed is not None:
            return primary_completed, None
        _log_ssh_diag(profile, 'warn', f'paramiko 主路径失败，回退系统 ssh: {primary_err}')
    base_args = _build_ssh_base_args(profile)
    if base_args is None:
        _log_ssh_diag(profile, 'warn', 'ssh 可执行文件不存在，且 paramiko 主路径未成功。')
        return None, primary_err if 'primary_err' in locals() and primary_err else '本机未找到 ssh 可执行文件，无法启动 ssh_agent。'
    started_at = time.time()
    _log_ssh_diag(
        profile,
        'info',
        f'ssh 子进程执行开始 target={profile.target} timeout={timeout_seconds} cmd={_diag_text(remote_command)}',
    )
    # #region debug-point A:ssh-subprocess-start
    _debug_report_ssh_timeout(
        'A',
        'core.dev_agent:_run_ssh_command:start',
        'ssh subprocess starts',
        {
            'pid': os.getpid(),
            'thread_id': threading.get_ident(),
            'profile_id': str(profile.profile_id or ''),
            'target': str(profile.target or ''),
            'root_dir': str(profile.root_dir or ''),
            'port': int(profile.port or 22),
            'identity_file_set': bool(str(profile.identity_file or '').strip()),
            'strict_host_key_checking': bool(profile.strict_host_key_checking),
            'ssh_bin': str(base_args[0] if base_args else ''),
            'base_args': list(base_args or []),
            'timeout_seconds': timeout_seconds,
            'command': _diag_text(remote_command, 400),
        },
    )
    # #endregion
    try:
        completed = subprocess.run(
            base_args + [str(remote_command)],
            input=input_bytes,
            capture_output=True,
            timeout=timeout_seconds,
        )
        _log_ssh_diag(
            profile,
            'info',
            f'ssh 子进程执行完成 rc={completed.returncode} stdout_bytes={len(completed.stdout or b"")} stderr_bytes={len(completed.stderr or b"")}',
        )
        # #region debug-point B:ssh-subprocess-done
        _debug_report_ssh_timeout(
            'B',
            'core.dev_agent:_run_ssh_command:done',
            'ssh subprocess finished',
            {
                'pid': os.getpid(),
                'thread_id': threading.get_ident(),
                'profile_id': str(profile.profile_id or ''),
                'elapsed_ms': int((time.time() - started_at) * 1000),
                'returncode': int(completed.returncode),
                'stdout_bytes': len(completed.stdout or b''),
                'stderr_bytes': len(completed.stderr or b''),
                'command': _diag_text(remote_command, 400),
            },
        )
        # #endregion
        return completed, None
    except subprocess.TimeoutExpired:
        _log_ssh_diag(profile, 'warn', f'ssh 子进程执行超时 timeout={timeout_seconds} cmd={_diag_text(remote_command)}')
        # #region debug-point B:ssh-subprocess-timeout
        _debug_report_ssh_timeout(
            'B',
            'core.dev_agent:_run_ssh_command:timeout',
            'ssh subprocess timed out',
            {
                'pid': os.getpid(),
                'thread_id': threading.get_ident(),
                'profile_id': str(profile.profile_id or ''),
                'elapsed_ms': int((time.time() - started_at) * 1000),
                'timeout_seconds': timeout_seconds,
                'command': _diag_text(remote_command, 400),
            },
        )
        # #endregion
        return None, f'SSH 命令执行超时（{timeout_seconds} 秒）。'
    except Exception as exc:
        _log_ssh_diag(profile, 'error', f'ssh 子进程执行失败: {exc}')
        # #region debug-point B:ssh-subprocess-error
        _debug_report_ssh_timeout(
            'B',
            'core.dev_agent:_run_ssh_command:error',
            'ssh subprocess raised exception',
            {
                'pid': os.getpid(),
                'thread_id': threading.get_ident(),
                'profile_id': str(profile.profile_id or ''),
                'elapsed_ms': int((time.time() - started_at) * 1000),
                'error': str(exc),
                'command': _diag_text(remote_command, 400),
            },
        )
        # #endregion
        return None, f'SSH 命令执行失败: {exc}'


def _run_ssh_command_with_timeout_retry(
    profile: SSHProfileConfig,
    remote_command: str,
    *,
    timeout_seconds: int | None = None,
    input_bytes: bytes | None = None,
    timeout_retries: int = 1,
    retry_delay_seconds: float = 0.6,
) -> tuple[subprocess.CompletedProcess[bytes] | None, str | None]:
    attempts = max(1, int(timeout_retries or 0) + 1)
    last_completed = None
    last_err = None
    for attempt in range(1, attempts + 1):
        completed, err = _run_ssh_command(
            profile,
            remote_command,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
        )
        last_completed = completed
        last_err = err
        is_timeout = isinstance(err, str) and err.startswith('SSH 命令执行超时')
        if not is_timeout or attempt >= attempts:
            return completed, err
        _log_ssh_diag(
            profile,
            'warn',
            f'SSH 只读命令超时，准备重试 attempt={attempt + 1}/{attempts} timeout={timeout_seconds} cmd={_diag_text(remote_command)}',
        )
        time.sleep(max(0.0, float(retry_delay_seconds or 0.0)))
    return last_completed, last_err


def validate_ssh_profile(profile: SSHProfileConfig) -> dict:
    base_args = _build_ssh_base_args(profile)
    uses_password = _ssh_profile_uses_password(profile)
    result = {
        'ok': False,
        'profile_id': str(profile.profile_id or ''),
        'target': str(profile.target or ''),
        'root_dir': str(profile.root_dir or '~'),
        'port': int(profile.port or 22),
        'identity_file': str(profile.identity_file or ''),
        'password_set': uses_password,
        'shell': str(profile.shell or 'bash'),
        'strict_host_key_checking': bool(profile.strict_host_key_checking),
    }
    if base_args is None and not uses_password:
        result['error'] = '本机未找到 ssh 可执行文件。'
        return result
    if uses_password and _load_paramiko_module() is None:
        result['error'] = '当前环境未安装 paramiko，无法使用 SSH 密码登录。'
        return result
    completed, err = _run_ssh_command_with_timeout_retry(
        profile,
        'printf "__SSH_OK__"; command -v pwd >/dev/null 2>&1 && printf "\\n"; pwd',
        timeout_seconds=SSH_CONNECT_VALIDATE_TIMEOUT_SECONDS,
    )
    if err:
        _log_ssh_diag(profile, 'warn', f'SSH profile 校验失败（连接阶段）: {err}')
        result['error'] = err
        return result
    if completed is None:
        result['error'] = 'SSH 验证失败：未获得执行结果。'
        return result
    stdout = completed.stdout.decode('utf-8', errors='replace').strip()
    stderr = completed.stderr.decode('utf-8', errors='replace').strip()
    if completed.returncode != 0 or '__SSH_OK__' not in stdout:
        result['error'] = stderr or stdout or f'SSH 连接失败，exit_code={completed.returncode}'
        return result
    pwd_lines = [line.strip() for line in stdout.splitlines() if line.strip() and line.strip() != '__SSH_OK__']
    remote_pwd = pwd_lines[-1] if pwd_lines else ''
    root_path = _ssh_root_path(profile)
    root_exists = _ssh_path_exists(profile, root_path, 'dir')
    result.update(
        {
            'ok': bool(root_exists),
            'remote_pwd': remote_pwd,
            'root_exists': bool(root_exists),
        }
    )
    if not root_exists:
        _log_ssh_diag(profile, 'warn', f'SSH profile 已连接但根目录不存在 root={root_path} remote_pwd={remote_pwd}')
        result['error'] = f'可连接，但远程根目录不存在: {root_path}'
    else:
        _log_ssh_diag(profile, 'info', f'SSH profile 校验成功 remote_pwd={remote_pwd} root={root_path}')
    return result


def _quote_remote_path(path: str) -> str:
    text = str(path or '').replace('\\', '/')
    if text == '~':
        return '"$HOME"'
    if text.startswith('~/'):
        parts = [segment for segment in text[2:].split('/') if segment not in {'', '.'}]
        expr = '"$HOME"'
        for segment in parts:
            expr += '/' + shlex.quote(segment)
        return expr
    return shlex.quote(text)


def _paramiko_connect(profile: SSHProfileConfig):
    paramiko = _load_paramiko_module()
    if paramiko is None:
        return None, '当前环境未安装 paramiko，无法使用 SSH 密码登录。'
    hostname, username, target_err = _parse_paramiko_target(profile)
    if target_err:
        return None, target_err
    uses_password = _ssh_profile_uses_password(profile)
    identity_file = os.path.expanduser(_resolve_ssh_identity_file(profile))
    look_for_keys = (not uses_password) or bool(identity_file)
    allow_agent = (not uses_password) or bool(identity_file)
    client = paramiko.SSHClient()
    try:
        if bool(profile.strict_host_key_checking):
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=hostname,
            port=int(profile.port or 22),
            username=username,
            password=str(profile.password or ''),
            key_filename=identity_file or None,
            look_for_keys=look_for_keys,
            allow_agent=allow_agent,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )
        return client, None
    except Exception as exc:
        try:
            client.close()
        except Exception:
            pass
        return None, f'Paramiko 连接失败: {exc}'


def _ensure_remote_parent_dir(profile: SSHProfileConfig, remote_path: str) -> str:
    remote_dir = posixpath.dirname(remote_path) or '.'
    completed, err = _run_ssh_command(
        profile,
        f'mkdir -p {_quote_remote_path(remote_dir)}',
        timeout_seconds=30,
    )
    if err:
        return err
    if completed is None or completed.returncode != 0:
        stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
        return f'远程创建目录失败: {stderr or "未知错误"}'
    return ''


def _ssh_root_path(profile: SSHProfileConfig) -> str:
    root = str(profile.root_dir or '~').strip() or '~'
    return root.replace('\\', '/')


def _ssh_remote_path_candidates(profile: SSHProfileConfig, path: str, *, allow_root: bool = True) -> list[str]:
    text = str(path or '').strip().replace('\\', '/')
    root = _ssh_root_path(profile)
    if text in {'', '.'}:
        return [root] if allow_root else []
    if text in {'/', '~'}:
        return [root]

    candidates: list[str] = []

    normalized = _normalize_repo_relative_path(text)
    if normalized is not None:
        rooted = posixpath.normpath(posixpath.join(root, normalized)) if normalized else root
        candidates.append(rooted)

    if text.startswith('~/'):
        candidates.append(text)
    elif text.startswith('/'):
        candidates.append(posixpath.normpath(text))

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate or '')
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _ssh_remote_path(profile: SSHProfileConfig, relative_path: str = '') -> str | None:
    candidates = _ssh_remote_path_candidates(profile, relative_path)
    return candidates[0] if candidates else None


def _ssh_resolve_existing_path(profile: SSHProfileConfig, path: str, kind: str, *, allow_root: bool = True) -> tuple[str | None, list[str]]:
    candidates = _ssh_remote_path_candidates(profile, path, allow_root=allow_root)
    for candidate in candidates:
        if _ssh_path_exists(profile, candidate, kind):
            return candidate, candidates
    return (candidates[0] if candidates else None), candidates


def _ssh_path_exists(profile: SSHProfileConfig, remote_path: str, kind: str) -> bool:
    test_flag = '-d' if kind == 'dir' else '-f'
    completed, err = _run_ssh_command_with_timeout_retry(
        profile,
        f'test {test_flag} {_quote_remote_path(remote_path)}',
        timeout_seconds=SSH_PATH_PROBE_TIMEOUT_SECONDS,
    )
    if err or completed is None:
        return False
    return completed.returncode == 0


def _read_remote_text_file_for_operation(
    profile: SSHProfileConfig,
    path: str,
    *,
    size_limit: int = MAX_FILE_OPERATION_BYTES,
    allow_missing: bool = False,
) -> tuple[str | None, str | None, bool]:
    raw_path = str(path or '').strip()
    if not raw_path:
        return None, '路径不合法、超出允许范围，或命中禁止访问清单，拒绝读取。', False
    remote_path, _candidates = _ssh_resolve_existing_path(profile, raw_path, 'file', allow_root=False)
    if remote_path is None:
        return None, '路径不合法、超出允许范围，或命中禁止访问清单，拒绝读取。', False
    probe_command = (
        f'if [ -f {_quote_remote_path(remote_path)} ]; then '
        f'wc -c < {_quote_remote_path(remote_path)}; '
        f'elif [ -e {_quote_remote_path(remote_path)} ]; then '
        "printf '__NOT_FILE__'; "
        'else '
        "printf '__MISSING__'; "
        'fi'
    )
    completed, err = _run_ssh_command_with_timeout_retry(profile, probe_command, timeout_seconds=SSH_PATH_PROBE_TIMEOUT_SECONDS)
    if err:
        return None, err, False
    if completed is None or completed.returncode != 0:
        stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
        return None, f'远程探测文件失败: {stderr or "未知错误"}', False
    probe_output = completed.stdout.decode('utf-8', errors='replace').strip()
    if probe_output == '__MISSING__':
        if allow_missing:
            return '', None, False
        return None, f'{path} 不是一个文件，或不存在。', False
    if probe_output == '__NOT_FILE__':
        return None, f'{path} 不是一个文件，或不存在。', False
    try:
        size = int(probe_output or '0')
    except ValueError:
        return None, f'远程文件大小探测结果异常: {probe_output!r}', False
    if size > int(size_limit or MAX_FILE_OPERATION_BYTES):
        return None, f'{path} 文件过大（{size} 字节），超过当前操作上限 {int(size_limit or MAX_FILE_OPERATION_BYTES)} 字节。', True
    completed, err = _run_ssh_command_with_timeout_retry(
        profile,
        f'cat -- {_quote_remote_path(remote_path)}',
        timeout_seconds=SSH_TEXT_READ_TIMEOUT_SECONDS,
    )
    if err:
        return None, err, True
    if completed is None or completed.returncode != 0:
        stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
        return None, f'远程读取文件失败: {stderr or "未知错误"}', True
    try:
        return completed.stdout.decode('utf-8'), None, True
    except UnicodeDecodeError:
        return None, f'{path} 不是可读的文本文件（可能是二进制文件）。', True


def _write_remote_text_file(profile: SSHProfileConfig, path: str, content: str) -> str:
    raw_path = str(path or '').strip()
    if not raw_path:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝写入。'
    remote_path = _ssh_remote_path(profile, raw_path)
    if remote_path is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝写入。'
    if _ssh_profile_uses_password(profile):
        ensure_err = _ensure_remote_parent_dir(profile, remote_path)
        if ensure_err:
            return ensure_err
        client, connect_err = _paramiko_connect(profile)
        if connect_err:
            return connect_err
        try:
            sftp = client.open_sftp()
            with sftp.file(remote_path, 'wb') as remote_file:
                remote_file.write(str(content or '').encode('utf-8'))
            sftp.close()
            client.close()
            return ''
        except Exception as exc:
            try:
                client.close()
            except Exception:
                pass
            return f'远程写入文件失败: {exc}'
    remote_dir = posixpath.dirname(remote_path) or '.'
    completed, err = _run_ssh_command(
        profile,
        f'mkdir -p {_quote_remote_path(remote_dir)} && cat > {_quote_remote_path(remote_path)}',
        timeout_seconds=60,
        input_bytes=str(content or '').encode('utf-8'),
    )
    if err:
        return err
    if completed is None or completed.returncode != 0:
        stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
        return f'远程写入文件失败: {stderr or "未知错误"}'
    return ''


def _create_remote_backup_file(profile: SSHProfileConfig, path: str) -> tuple[str, str]:
    raw_path = str(path or '').strip()
    if not raw_path:
        return '', '路径不合法、超出允许范围，或命中禁止访问清单，拒绝备份。'
    remote_path, _candidates = _ssh_resolve_existing_path(profile, raw_path, 'file', allow_root=False)
    if remote_path is None:
        return '', '路径不合法、超出允许范围，或命中禁止访问清单，拒绝备份。'
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f'{remote_path}.bak.{stamp}'
    completed, err = _run_ssh_command(
        profile,
        f'cp -- {_quote_remote_path(remote_path)} {_quote_remote_path(backup_path)}',
        timeout_seconds=30,
    )
    if err:
        return '', err
    if completed is None or completed.returncode != 0:
        stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
        return '', f'远程备份失败: {stderr or "未知错误"}'
    return backup_path, ''


def _list_remote_files(profile: SSHProfileConfig, subpath: str) -> str:
    remote_path, _candidates = _ssh_resolve_existing_path(profile, subpath, 'dir')
    # #region debug-point C:list-remote-files
    _debug_report_ssh_timeout(
        'C',
        'core.dev_agent:_list_remote_files',
        'list_remote_files resolved path',
        {
            'pid': os.getpid(),
            'thread_id': threading.get_ident(),
            'profile_id': str(profile.profile_id or ''),
            'subpath': str(subpath or ''),
            'resolved_path': str(remote_path or ''),
            'candidates': list(_candidates or []),
        },
    )
    # #endregion
    if remote_path is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝访问。'
    completed, err = _run_ssh_command_with_timeout_retry(
        profile,
        (
            f'if [ ! -d {_quote_remote_path(remote_path)} ]; then '
            "printf '__NOT_DIR__'; "
            'else '
            f'LC_ALL=C ls -1Ap -- {_quote_remote_path(remote_path)}; '
            'fi'
        ),
        timeout_seconds=SSH_LIST_TIMEOUT_SECONDS,
    )
    if err:
        return err
    if completed is None or completed.returncode != 0:
        stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
        return f'读取远程目录失败: {stderr or "未知错误"}'
    output = completed.stdout.decode('utf-8', errors='replace')
    if output.strip() == '__NOT_DIR__':
        return f'{subpath or "."} 不是一个目录，或不存在。'
    output = output.strip()
    return output or '(空目录)'


def _find_in_remote_project(
    profile: SSHProfileConfig,
    name_pattern: str = '',
    content_query: str = '',
    is_regex: bool = False,
    subpath: str = '',
    max_results: int = FIND_DEFAULT_MAX_RESULTS,
) -> str:
    name_pattern = str(name_pattern or '').strip()
    content_query = str(content_query or '')
    if not name_pattern and not content_query:
        return 'name_pattern 与 content_query 至少要提供一个，未执行搜索。'
    remote_path, _candidates = _ssh_resolve_existing_path(profile, subpath, 'dir')
    if remote_path is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝访问。'
    max_results = max(1, min(FIND_MAX_RESULTS_CAP, int(max_results or FIND_DEFAULT_MAX_RESULTS)))

    root = _quote_remote_path(remote_path)
    prune = ' -o '.join(f'-name {shlex.quote(name)}' for name in sorted(FIND_SKIP_DIR_NAMES))
    find_cmd = f'find {root} \\( {prune} \\) -prune -o -type f'
    if name_pattern:
        glob_pattern = name_pattern if any(ch in name_pattern for ch in '*?[') else f'*{name_pattern}*'
        find_cmd += f' -iname {shlex.quote(glob_pattern)}'
    find_cmd += ' -print'

    if content_query:
        grep_flags = '-nI' + ('E' if is_regex else 'F')
        command = (
            f'{find_cmd} -print0 2>/dev/null | '
            f'xargs -0 -r grep {grep_flags} -e {shlex.quote(content_query)} -- 2>/dev/null | '
            f'head -n {max_results}'
        )
    else:
        command = f'{find_cmd} 2>/dev/null | LC_ALL=C sort | head -n {max_results}'

    completed, err = _run_ssh_command_with_timeout_retry(
        profile,
        command,
        timeout_seconds=SSH_LIST_TIMEOUT_SECONDS,
    )
    if err:
        return err
    if completed is None:
        return '远程搜索失败: 未知错误'
    output = completed.stdout.decode('utf-8', errors='replace').strip()
    if not output:
        stderr = completed.stderr.decode('utf-8', errors='replace').strip()
        if completed.returncode not in (0, 1) and stderr:
            return f'远程搜索失败: {stderr}'
        return f'在 {subpath or "远程根目录"} 下未找到匹配项。'
    prefix = remote_path.rstrip('/') + '/'
    lines = [line[len(prefix):] if line.startswith(prefix) else line for line in output.splitlines()]
    scope = subpath or '远程根目录'
    footer = ''
    if len(lines) >= max_results:
        footer = f'\n已达 max_results={max_results} 上限，可能还有更多结果，请缩小范围或提高上限。'
    return f'在 {scope} 下找到 {len(lines)} 条结果：\n' + '\n'.join(lines) + footer


def _read_remote_file(profile: SSHProfileConfig, path: str) -> str:
    text, err, _exists = _read_remote_text_file_for_operation(profile, path, size_limit=MAX_FILE_BYTES)
    return err or str(text or '')


def _read_remote_file_chunk(
    profile: SSHProfileConfig,
    path: str,
    offset_bytes: int | None = None,
    max_bytes: int | None = None,
    start_line: int | None = None,
    line_count: int | None = None,
) -> str:
    raw_path = str(path or '').strip()
    if not raw_path:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝读取。'
    remote_path, _candidates = _ssh_resolve_existing_path(profile, raw_path, 'file', allow_root=False)
    if remote_path is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝读取。'

    if start_line is not None:
        start_line = max(1, int(start_line or 1))
        line_count = max(1, int(line_count or 120))
        end_line = start_line + line_count - 1
        q = _quote_remote_path(remote_path)
        completed, err = _run_ssh_command(
            profile,
            (
                f'if [ ! -f {q} ]; then printf "__MISSING__"; '
                f'else awk \'END{{print NR}}\' {q}; printf "__SEP__"; '
                f'sed -n "{start_line},{end_line}p" {q}; fi'
            ),
            timeout_seconds=30,
        )
        if err:
            return err
        if completed is None or completed.returncode != 0:
            stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
            return f'远程读取文件失败: {stderr or "未知错误"}'
        out = completed.stdout.decode('utf-8', errors='replace')
        if out.strip() == '__MISSING__':
            return f'{path} 不是一个文件，或不存在。'
        total_text, sep, content = out.partition('__SEP__')
        try:
            total = int(str(total_text).strip() or 0)
        except ValueError:
            total = 0
        content_lines = str(content).splitlines()
        end_idx = start_line + len(content_lines) - 1
        return (
            f'文件: {path}\n'
            f'模式: lines\n'
            f'起始行: {start_line}\n'
            f'结束行: {end_idx}\n'
            f'总行数: {total}\n'
            f'内容:\n' + '\n'.join(content_lines)
        )

    completed, err = _run_ssh_command(
        profile,
        (
            f'if [ ! -f {_quote_remote_path(remote_path)} ]; then '
            "printf '__MISSING__'; "
            'else '
            f"dd if={_quote_remote_path(remote_path)} bs=1 skip={max(0, int(offset_bytes or 0))} "
            f"count={min(MAX_FILE_CHUNK_BYTES, max(1, int(max_bytes or 120_000)))} 2>/dev/null; "
            'fi'
        ),
        timeout_seconds=30,
    )
    if err:
        return err
    if completed is None or completed.returncode != 0:
        stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
        return f'远程读取文件失败: {stderr or "未知错误"}'
    if completed.stdout == b'__MISSING__':
        return f'{path} 不是一个文件，或不存在。'
    data = completed.stdout
    # UTF-8 对齐：跳过头部续字节，避免从多字节字符中间切开
    head_skip = 0
    while head_skip < len(data) and 0x80 <= data[head_skip] <= 0xBF:
        head_skip += 1
    actual_start = max(0, int(offset_bytes or 0)) + head_skip
    data = data[head_skip:]
    text = data.decode('utf-8', errors='ignore')
    return (
        f'文件: {path}\n'
        f'模式: bytes\n'
        f'offset_bytes: {actual_start}\n'
        f'read_bytes: {len(data)}\n'
        f'内容:\n{text}'
    )


def _search_remote_file(
    profile: SSHProfileConfig,
    path: str,
    query: str,
    is_regex: bool = False,
    max_matches: int = 20,
    context_lines: int = 1,
) -> str:
    text, err, _exists = _read_remote_text_file_for_operation(profile, path, size_limit=MAX_FILE_OPERATION_BYTES)
    if err:
        return err
    result, _updated = _local_text_tool_on_temp(
        path,
        str(text or ''),
        lambda tmp_root, relative_path: _search_local_file(
            tmp_root,
            relative_path,
            query,
            is_regex=is_regex,
            max_matches=max_matches,
            context_lines=context_lines,
        ),
    )
    return result


def _run_remote_write_tool(
    profile: SSHProfileConfig,
    path: str,
    local_runner,
    *,
    allow_missing: bool = False,
    dry_run: bool = False,
    create_backup: bool = False,
) -> str:
    original_text, err, existed = _read_remote_text_file_for_operation(
        profile,
        path,
        size_limit=MAX_FILE_OPERATION_BYTES,
        allow_missing=allow_missing,
    )
    if err:
        return err
    result_text, updated_text = _local_text_tool_on_temp(
        path,
        str(original_text or ''),
        local_runner,
        create_input_file=bool(existed or not allow_missing),
    )
    if dry_run or updated_text is None or updated_text == str(original_text or ''):
        return result_text
    backup_path = ''
    if create_backup and existed:
        backup_path, backup_err = _create_remote_backup_file(profile, path)
        if backup_err:
            return backup_err
    write_err = _write_remote_text_file(profile, path, updated_text)
    if write_err:
        return write_err
    return _inject_backup_text(result_text, backup_path) if backup_path else result_text


class SSHAgentShellManager:
    def __init__(
        self,
        profile: SSHProfileConfig,
        project_root: str | None = None,
        on_transfer_report: Callable[[str], None] | None = None,
    ):
        self.profile = profile
        self.project_root = project_root or _project_root()
        self.runtime_dir = tempfile.mkdtemp(prefix='ssh_agent_shell_')
        self.jobs: dict[str, dict] = {}
        self.transfer_jobs: dict[str, dict] = {}
        self._next_job_id = 0
        self._next_transfer_id = 0
        self._lock = threading.Lock()
        self._on_transfer_report = on_transfer_report

    def _uses_password_auth(self) -> bool:
        return _ssh_profile_uses_password(self.profile)

    def _normalize_timeout(self, timeout_seconds, background: bool) -> int:
        default_timeout = SHELL_DEFAULT_BACKGROUND_TIMEOUT_SECONDS if background else SSH_SHELL_DEFAULT_TIMEOUT_SECONDS
        try:
            value = int(timeout_seconds) if timeout_seconds is not None else default_timeout
        except (TypeError, ValueError):
            value = default_timeout
        if value <= 0:
            value = default_timeout
        return min(SHELL_MAX_TIMEOUT_SECONDS, value)

    def _next_id(self) -> str:
        with self._lock:
            self._next_job_id += 1
            return f'ssh-shell-{self._next_job_id}'

    def _next_transfer(self) -> str:
        with self._lock:
            self._next_transfer_id += 1
            return f'ssh-transfer-{self._next_transfer_id}'

    def _job_duration(self, job: dict) -> float:
        started_at = float(job.get('started_at') or time.time())
        ended_at = float(job.get('ended_at') or time.time())
        if job.get('status') == 'running':
            ended_at = time.time()
        return max(0.0, ended_at - started_at)

    def _signal_job(self, process: subprocess.Popen, sig) -> None:
        try:
            if hasattr(os, 'killpg'):
                os.killpg(process.pid, sig)
            else:
                process.send_signal(sig)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.send_signal(sig)
            except Exception:
                return

    def _close_output_handle(self, job: dict) -> None:
        handle = job.get('output_handle')
        if handle is not None and not handle.closed:
            try:
                handle.close()
            except Exception:
                pass
        job['output_handle'] = None

    def _refresh_job(self, job: dict) -> dict:
        process = job.get('process')
        if process is None:
            thread = job.get('thread')
            if thread is not None and job.get('status') == 'running' and not thread.is_alive():
                job['ended_at'] = job.get('ended_at') or time.time()
                self._close_output_handle(job)
            return job
        if job.get('status') == 'running':
            timeout_seconds = int(job.get('timeout_seconds') or 0)
            if timeout_seconds > 0 and (time.time() - float(job.get('started_at') or time.time())) > timeout_seconds:
                self._stop_job(job, force=True, reason='timeout')
        if job.get('status') == 'running':
            exit_code = process.poll()
            if exit_code is not None:
                job['status'] = 'done' if exit_code == 0 else 'failed'
                job['exit_code'] = exit_code
                job['ended_at'] = time.time()
                self._close_output_handle(job)
        return job

    def _stop_job(self, job: dict, force: bool = False, wait_seconds: int = 5, reason: str = '') -> dict:
        process = job.get('process')
        if process is None:
            channel = job.get('channel')
            client = job.get('client')
            cancel_event = job.get('cancel_event')
            thread = job.get('thread')
            if cancel_event is not None:
                cancel_event.set()
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            try:
                if client is not None:
                    client.close()
            except Exception:
                pass
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(1, min(30, int(wait_seconds or 5))))
            job['ended_at'] = time.time()
            if reason == 'timeout':
                job['status'] = 'timeout'
            elif job.get('status') == 'running':
                job['status'] = 'stopped'
            self._close_output_handle(job)
            return job
        wait_seconds = max(1, min(30, int(wait_seconds or 5)))
        if process.poll() is None:
            if force:
                self._signal_job(process, signal.SIGKILL)
            else:
                self._signal_job(process, signal.SIGTERM)
                deadline = time.time() + wait_seconds
                while time.time() < deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.2)
                if process.poll() is None:
                    self._signal_job(process, signal.SIGKILL)
        exit_code = process.poll()
        job['exit_code'] = exit_code
        job['ended_at'] = time.time()
        if reason == 'timeout':
            job['status'] = 'timeout'
        elif job.get('status') == 'running':
            job['status'] = 'stopped' if (exit_code is None or exit_code < 0) else ('done' if exit_code == 0 else 'failed')
        self._close_output_handle(job)
        return job

    def _resolve_cwd(self, cwd: str, default_cwd: str = '/') -> tuple[str | None, str]:
        effective = str(cwd or '').strip() or str(default_cwd or '').strip() or '/'
        normalized = _normalize_agent_cwd_spec(effective)
        if normalized is None:
            _log_ssh_diag(self.profile, 'warn', f'SSH cwd 非法 effective={effective}')
            return None, effective
        lookup = ''
        if normalized not in {'/', '~'}:
            lookup = normalized
        remote_path, _candidates = _ssh_resolve_existing_path(self.profile, lookup, 'dir')
        _log_ssh_diag(
            self.profile,
            'info',
            f'SSH cwd 解析 effective={effective} normalized={normalized} lookup={lookup or "(root)"} candidates={_diag_text(_candidates)} chosen={remote_path or "(none)"}',
        )
        if remote_path is None:
            return None, normalized
        return remote_path, normalized

    def _resolve_local_transfer_path(self, path: str) -> tuple[str | None, str]:
        text = str(path or '').strip()
        if not text:
            return None, ''
        resolved = _resolve_safe_path(self.project_root, text)
        return resolved, text

    def _resolve_remote_transfer_path(self, path: str) -> tuple[str | None, str]:
        text = str(path or '').strip()
        normalized = _normalize_repo_relative_path(text)
        if normalized is None or not normalized:
            return None, text
        remote_path = _ssh_remote_path(self.profile, normalized)
        return remote_path, normalized

    def _build_remote_shell_command(self, remote_cwd: str, command: str) -> str:
        shell = shlex.quote(str(self.profile.shell or 'bash'))
        return f'cd {_quote_remote_path(remote_cwd)} && exec {shell} -lc {shlex.quote(str(command or ""))}'

    def _run_paramiko_background_job(self, job: dict, remote_command: str) -> None:
        client = None
        channel = None
        try:
            client, connect_err = _paramiko_connect(self.profile)
            if connect_err:
                job['status'] = 'failed'
                job['error'] = connect_err
                return
            transport = client.get_transport()
            if transport is None:
                job['status'] = 'failed'
                job['error'] = 'Paramiko transport 不可用。'
                return
            channel = transport.open_session()
            channel.set_combine_stderr(True)
            job['client'] = client
            job['channel'] = channel
            channel.exec_command(remote_command)
            output_handle = job.get('output_handle')
            timeout_seconds = int(job.get('timeout_seconds') or 0)
            deadline = time.time() + timeout_seconds if timeout_seconds > 0 else None
            while True:
                if job.get('cancel_event') is not None and job['cancel_event'].is_set():
                    job['status'] = 'stopped'
                    break
                if deadline is not None and time.time() > deadline:
                    job['status'] = 'timeout'
                    break
                if channel.recv_ready():
                    data = channel.recv(65536)
                    if data and output_handle is not None and not output_handle.closed:
                        output_handle.write(data.decode('utf-8', errors='replace'))
                        output_handle.flush()
                if channel.exit_status_ready():
                    while channel.recv_ready():
                        data = channel.recv(65536)
                        if data and output_handle is not None and not output_handle.closed:
                            output_handle.write(data.decode('utf-8', errors='replace'))
                            output_handle.flush()
                    exit_code = int(channel.recv_exit_status())
                    job['exit_code'] = exit_code
                    if job.get('status') == 'running':
                        job['status'] = 'done' if exit_code == 0 else 'failed'
                    break
                time.sleep(0.1)
            if job.get('status') in {'stopped', 'timeout'}:
                try:
                    channel.close()
                except Exception:
                    pass
                job['exit_code'] = job.get('exit_code')
        except Exception as exc:
            job['status'] = 'failed'
            job['error'] = str(exc)
        finally:
            job['ended_at'] = time.time()
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            try:
                if client is not None:
                    client.close()
            except Exception:
                pass
            self._close_output_handle(job)

    def _remote_file_size(self, remote_path: str) -> int:
        completed, err = _run_ssh_command(
            self.profile,
            (
                f'if [ -f {_quote_remote_path(remote_path)} ]; then '
                f'wc -c < {_quote_remote_path(remote_path)}; '
                'else '
                "printf '__MISSING__'; "
                'fi'
            ),
            timeout_seconds=20,
        )
        if err or completed is None or completed.returncode != 0:
            return -1
        output = completed.stdout.decode('utf-8', errors='replace').strip()
        if output == '__MISSING__':
            return -1
        try:
            return max(0, int(output or '0'))
        except ValueError:
            return -1

    def _remote_remove_file(self, remote_path: str) -> str:
        completed, err = _run_ssh_command(
            self.profile,
            f'rm -f -- {_quote_remote_path(remote_path)}',
            timeout_seconds=30,
        )
        if err:
            return err
        if completed is None or completed.returncode != 0:
            stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
            return f'远程删除文件失败: {stderr or "未知错误"}'
        return ''

    def _remote_move_file(self, src_path: str, dst_path: str) -> str:
        completed, err = _run_ssh_command(
            self.profile,
            f'mkdir -p {_quote_remote_path(posixpath.dirname(dst_path) or ".")} && mv -f -- {_quote_remote_path(src_path)} {_quote_remote_path(dst_path)}',
            timeout_seconds=60,
        )
        if err:
            return err
        if completed is None or completed.returncode != 0:
            stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
            return f'远程移动文件失败: {stderr or "未知错误"}'
        return ''

    def _remote_read_chunk(self, remote_path: str, offset: int, size: int) -> tuple[bytes | None, str]:
        completed, err = _run_ssh_command(
            self.profile,
            (
                f'if [ ! -f {_quote_remote_path(remote_path)} ]; then exit 44; fi; '
                f'dd if={_quote_remote_path(remote_path)} iflag=skip_bytes,count_bytes '
                f'skip={max(0, int(offset))} count={max(1, int(size))} status=none'
            ),
            timeout_seconds=SSH_TRANSFER_DEFAULT_TIMEOUT_SECONDS,
        )
        if err:
            return None, err
        if completed is None:
            return None, '远程读取分块失败：未获得执行结果。'
        if completed.returncode != 0:
            stderr = completed.stderr.decode('utf-8', errors='replace').strip()
            if completed.returncode == 44:
                return None, '远程源文件不存在。'
            return None, f'远程读取分块失败: {stderr or f"exit_code={completed.returncode}"}'
        return bytes(completed.stdout or b''), ''

    def _remote_write_chunk(self, remote_path: str, offset: int, chunk: bytes) -> str:
        completed, err = _run_ssh_command(
            self.profile,
            (
                f'mkdir -p {_quote_remote_path(posixpath.dirname(remote_path) or ".")} && '
                f'dd of={_quote_remote_path(remote_path)} conv=notrunc oflag=seek_bytes '
                f'seek={max(0, int(offset))} status=none'
            ),
            timeout_seconds=SSH_TRANSFER_DEFAULT_TIMEOUT_SECONDS,
            input_bytes=bytes(chunk or b''),
        )
        if err:
            return err
        if completed is None or completed.returncode != 0:
            stderr = (completed.stderr.decode('utf-8', errors='replace').strip() if completed else '').strip()
            return f'远程写入分块失败: {stderr or "未知错误"}'
        return ''

    def _make_transfer_job(
        self,
        direction: str,
        local_path: str,
        remote_path: str,
        local_display_path: str,
        remote_display_path: str,
        total_bytes: int,
        chunk_bytes: int,
        overwrite: bool,
        part_path: str,
        remote_part_path: str,
        resumed_bytes: int,
    ) -> dict:
        transfer_id = self._next_transfer()
        cancel_event = threading.Event()
        job = {
            'transfer_id': transfer_id,
            'direction': direction,
            'status': 'running',
            'started_at': time.time(),
            'ended_at': None,
            'total_bytes': max(0, int(total_bytes or 0)),
            'bytes_transferred': max(0, int(resumed_bytes or 0)),
            'chunk_bytes': max(1, int(chunk_bytes or SSH_TRANSFER_DEFAULT_CHUNK_BYTES)),
            'overwrite': bool(overwrite),
            'local_path': local_path,
            'remote_path': remote_path,
            'local_display_path': local_display_path,
            'remote_display_path': remote_display_path,
            'part_path': part_path,
            'remote_part_path': remote_part_path,
            'cancel_event': cancel_event,
            'thread': None,
            'error': '',
            'last_update_at': time.time(),
            'report_sent': False,
        }
        self.transfer_jobs[transfer_id] = job
        return job

    def _transfer_progress_text(self, job: dict) -> str:
        total = int(job.get('total_bytes') or 0)
        done = int(job.get('bytes_transferred') or 0)
        percent = 100.0 if total <= 0 else min(100.0, (done / max(1, total)) * 100.0)
        duration = self._job_duration(job)
        speed = done / duration if duration > 0 else 0.0
        return (
            f'{done}/{total} bytes '
            f'({percent:.1f}%) | { _human_bytes(done) } / { _human_bytes(total) } | '
            f'{_human_bytes(speed)}/s'
        )

    def _format_transfer_status(self, job: dict) -> str:
        return (
            f'transfer_id: {job["transfer_id"]}\n'
            f'status: {job.get("status")}\n'
            f'direction: {job.get("direction")}\n'
            f'local_path: {job.get("local_display_path")}\n'
            f'remote_path: {job.get("remote_display_path")}\n'
            f'progress: {self._transfer_progress_text(job)}\n'
            f'chunk_bytes: {job.get("chunk_bytes")}\n'
            f'overwrite: {job.get("overwrite")}\n'
            f'duration_seconds: {self._job_duration(job):.1f}\n'
            f'error: {job.get("error") or "(无)"}'
        )

    def _notify_transfer_report(self, text: str) -> None:
        if self._on_transfer_report is None or not text:
            return
        try:
            self._on_transfer_report(str(text))
        except Exception as exc:
            error(f'[DevAgent] SSH 传输回报失败: {exc}')

    def _finalize_transfer_report(self, job: dict) -> None:
        if job.get('report_sent'):
            return
        job['report_sent'] = True
        status = str(job.get('status') or '')
        header = {
            'done': '【ssh 传输完成】',
            'failed': '【ssh 传输失败】',
            'cancelled': '【ssh 传输取消】',
        }.get(status)
        if not header:
            return
        self._notify_transfer_report(f'{header}\n{self._format_transfer_status(job)}')

    def _download_worker(self, transfer_id: str) -> None:
        job = self.transfer_jobs.get(transfer_id)
        if not job:
            return
        final_path = str(job.get('local_path') or '')
        part_path = str(job.get('part_path') or '')
        remote_path = str(job.get('remote_path') or '')
        total = int(job.get('total_bytes') or 0)
        chunk_bytes = int(job.get('chunk_bytes') or SSH_TRANSFER_DEFAULT_CHUNK_BYTES)
        try:
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            if job.get('overwrite') and os.path.exists(final_path) and not os.path.exists(part_path):
                try:
                    os.remove(final_path)
                except OSError:
                    pass
            mode = 'ab' if int(job.get('bytes_transferred') or 0) > 0 else 'wb'
            with open(part_path, mode) as output:
                while int(job.get('bytes_transferred') or 0) < total:
                    if job['cancel_event'].is_set():
                        job['status'] = 'cancelled'
                        break
                    offset = int(job.get('bytes_transferred') or 0)
                    want = min(chunk_bytes, max(0, total - offset))
                    chunk, err = self._remote_read_chunk(remote_path, offset, want)
                    if err:
                        job['status'] = 'failed'
                        job['error'] = err
                        break
                    if not chunk:
                        job['status'] = 'failed'
                        job['error'] = '远程提前返回空数据，传输未完成。'
                        break
                    output.write(chunk)
                    output.flush()
                    job['bytes_transferred'] = offset + len(chunk)
                    job['last_update_at'] = time.time()
                if job.get('status') == 'running':
                    output.flush()
            if job.get('status') == 'running':
                os.replace(part_path, final_path)
                job['status'] = 'done'
        except Exception as exc:
            job['status'] = 'failed'
            job['error'] = str(exc)
        finally:
            job['ended_at'] = time.time()
            self._finalize_transfer_report(job)

    def _upload_worker(self, transfer_id: str) -> None:
        job = self.transfer_jobs.get(transfer_id)
        if not job:
            return
        local_path = str(job.get('local_path') or '')
        remote_part_path = str(job.get('remote_part_path') or '')
        remote_path = str(job.get('remote_path') or '')
        total = int(job.get('total_bytes') or 0)
        chunk_bytes = int(job.get('chunk_bytes') or SSH_TRANSFER_DEFAULT_CHUNK_BYTES)
        try:
            with open(local_path, 'rb') as source:
                source.seek(int(job.get('bytes_transferred') or 0))
                while int(job.get('bytes_transferred') or 0) < total:
                    if job['cancel_event'].is_set():
                        job['status'] = 'cancelled'
                        break
                    offset = int(job.get('bytes_transferred') or 0)
                    chunk = source.read(min(chunk_bytes, max(0, total - offset)))
                    if not chunk:
                        break
                    err = self._remote_write_chunk(remote_part_path, offset, chunk)
                    if err:
                        job['status'] = 'failed'
                        job['error'] = err
                        break
                    job['bytes_transferred'] = offset + len(chunk)
                    job['last_update_at'] = time.time()
            if job.get('status') == 'running' and int(job.get('bytes_transferred') or 0) >= total:
                err = self._remote_move_file(remote_part_path, remote_path)
                if err:
                    job['status'] = 'failed'
                    job['error'] = err
                else:
                    job['status'] = 'done'
        except Exception as exc:
            job['status'] = 'failed'
            job['error'] = str(exc)
        finally:
            job['ended_at'] = time.time()
            self._finalize_transfer_report(job)

    def start_download(
        self,
        remote_path: str,
        local_path: str,
        overwrite: bool = False,
        chunk_bytes: int | None = None,
    ) -> str:
        resolved_remote_path, remote_display = self._resolve_remote_transfer_path(remote_path)
        resolved_local_path, local_display = self._resolve_local_transfer_path(local_path)
        if resolved_remote_path is None:
            return f'远程路径不合法: {remote_path}'
        if resolved_local_path is None:
            return f'本地路径不合法: {local_path}'
        remote_size = self._remote_file_size(resolved_remote_path)
        if remote_size < 0:
            return f'远程文件不存在或无法读取大小: {remote_display}'
        part_path = resolved_local_path + '.sshpart'
        if os.path.exists(resolved_local_path) and not overwrite and not os.path.exists(part_path):
            return f'本地目标文件已存在，若确认覆盖请设置 overwrite=true: {local_display}'
        resume_from = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        if resume_from > remote_size:
            try:
                os.remove(part_path)
            except OSError:
                pass
            resume_from = 0
        chunk_size = min(SSH_TRANSFER_MAX_CHUNK_BYTES, max(64 * 1024, int(chunk_bytes or SSH_TRANSFER_DEFAULT_CHUNK_BYTES)))
        job = self._make_transfer_job(
            'download',
            resolved_local_path,
            resolved_remote_path,
            local_display,
            remote_display,
            remote_size,
            chunk_size,
            overwrite,
            part_path,
            '',
            resume_from,
        )
        thread = threading.Thread(target=self._download_worker, args=(job['transfer_id'],), daemon=True)
        job['thread'] = thread
        thread.start()
        return (
            f'已启动 SSH 下载任务。\n'
            f'transfer_id: {job["transfer_id"]}\n'
            f'remote_path: {remote_display}\n'
            f'local_path: {local_display}\n'
            f'progress: {self._transfer_progress_text(job)}\n'
            '可用 ssh_transfer_status 查看进度，ssh_transfer_cancel 取消，ssh_transfer_list 列表查看所有传输。'
        )

    def start_upload(
        self,
        local_path: str,
        remote_path: str,
        overwrite: bool = False,
        chunk_bytes: int | None = None,
    ) -> str:
        resolved_local_path, local_display = self._resolve_local_transfer_path(local_path)
        resolved_remote_path, remote_display = self._resolve_remote_transfer_path(remote_path)
        if resolved_local_path is None or not os.path.isfile(resolved_local_path):
            return f'本地源文件不存在或不合法: {local_path}'
        if resolved_remote_path is None:
            return f'远程路径不合法: {remote_path}'
        remote_part_path = resolved_remote_path + '.sshpart'
        if not overwrite and self._remote_file_size(resolved_remote_path) >= 0 and self._remote_file_size(remote_part_path) < 0:
            return f'远程目标文件已存在，若确认覆盖请设置 overwrite=true: {remote_display}'
        if overwrite:
            remove_err = self._remote_remove_file(remote_part_path)
            if remove_err:
                return remove_err
        remote_resume = self._remote_file_size(remote_part_path)
        if remote_resume < 0:
            remote_resume = 0
        total = os.path.getsize(resolved_local_path)
        if remote_resume > total:
            remove_err = self._remote_remove_file(remote_part_path)
            if remove_err:
                return remove_err
            remote_resume = 0
        chunk_size = min(SSH_TRANSFER_MAX_CHUNK_BYTES, max(64 * 1024, int(chunk_bytes or SSH_TRANSFER_DEFAULT_CHUNK_BYTES)))
        job = self._make_transfer_job(
            'upload',
            resolved_local_path,
            resolved_remote_path,
            local_display,
            remote_display,
            total,
            chunk_size,
            overwrite,
            '',
            remote_part_path,
            remote_resume,
        )
        thread = threading.Thread(target=self._upload_worker, args=(job['transfer_id'],), daemon=True)
        job['thread'] = thread
        thread.start()
        return (
            f'已启动 SSH 上传任务。\n'
            f'transfer_id: {job["transfer_id"]}\n'
            f'local_path: {local_display}\n'
            f'remote_path: {remote_display}\n'
            f'progress: {self._transfer_progress_text(job)}\n'
            '可用 ssh_transfer_status 查看进度，ssh_transfer_cancel 取消，ssh_transfer_list 列表查看所有传输。'
        )

    def transfer_status(self, transfer_id: str) -> str:
        job = self.transfer_jobs.get(str(transfer_id or '').strip())
        if not job:
            return f'未找到 SSH 传输任务: {transfer_id}'
        return self._format_transfer_status(job)

    def transfer_cancel(self, transfer_id: str) -> str:
        job = self.transfer_jobs.get(str(transfer_id or '').strip())
        if not job:
            return f'未找到 SSH 传输任务: {transfer_id}'
        if job.get('status') != 'running':
            return (
                f'传输任务已不是运行中状态，无需取消。\n'
                f'transfer_id: {job["transfer_id"]}\n'
                f'status: {job.get("status")}'
            )
        job['cancel_event'].set()
        return (
            f'已请求取消 SSH 传输任务。\n'
            f'transfer_id: {job["transfer_id"]}\n'
            '说明: 当前分块若已发出，会在该分块结束后停止。'
        )

    def transfer_list(self, active_only: bool = False) -> str:
        lines: list[str] = []
        for transfer_id in list(self.transfer_jobs.keys()):
            job = self.transfer_jobs[transfer_id]
            if active_only and job.get('status') == 'running':
                pass
            elif active_only:
                continue
            lines.append(
                f'{job["transfer_id"]} | {job.get("status")} | {job.get("direction")} | '
                f'{self._transfer_progress_text(job)} | '
                f'{job.get("local_display_path")} <-> {job.get("remote_display_path")}'
            )
        if not lines:
            return '没有 SSH 传输任务。'
        return '\n'.join(lines)

    def exec(self, command: str, cwd: str = '', timeout_seconds=None, background: bool = False, default_cwd: str = '/') -> str:
        command = str(command or '').strip()
        if not command:
            return '命令为空，未执行。'
        remote_cwd, display_cwd = self._resolve_cwd(cwd, default_cwd=default_cwd)
        if remote_cwd is None:
            _log_ssh_diag(self.profile, 'warn', f'SSH shell_exec 工作目录无效 cwd={cwd or default_cwd} display={display_cwd}')
            return f'工作目录不合法或不存在: {display_cwd or "/"}'
        timeout = self._normalize_timeout(timeout_seconds, background)
        _log_ssh_diag(
            self.profile,
            'info',
            f'SSH shell_exec 开始 cwd={display_cwd or "/"} resolved={remote_cwd} timeout={timeout} background={background} cmd={_diag_text(command)}',
        )
        self.list_jobs(active_only=False)
        remote_command = self._build_remote_shell_command(remote_cwd, command)
        uses_password = self._uses_password_auth()
        base_args = None if uses_password else _build_ssh_base_args(self.profile)
        if background:
            if not uses_password and base_args is None:
                return '本机未找到 ssh 可执行文件，无法启动 ssh_agent。'
            job_id = self._next_id()
            output_path = os.path.join(self.runtime_dir, f'{job_id}.log')
            output_handle = open(output_path, 'w', encoding='utf-8', errors='replace')
            job = {
                'job_id': job_id,
                'command': command,
                'cwd': display_cwd or '/',
                'resolved_cwd': remote_cwd,
                'status': 'running',
                'pid': None,
                'started_at': time.time(),
                'ended_at': None,
                'exit_code': None,
                'timeout_seconds': timeout,
                'output_path': output_path,
                'output_handle': output_handle,
                'process': None,
                'thread': None,
                'cancel_event': threading.Event(),
                'client': None,
                'channel': None,
                'error': '',
            }
            if uses_password:
                thread = threading.Thread(
                    target=self._run_paramiko_background_job,
                    args=(job, remote_command),
                    daemon=True,
                )
                job['thread'] = thread
                self.jobs[job_id] = job
                thread.start()
                return (
                    f'已后台启动远程 shell 任务。\n'
                    f'job_id: {job_id}\n'
                    f'pid: paramiko-thread\n'
                    f'cwd: {job["cwd"]}\n'
                    f'timeout_seconds: {timeout}\n'
                    '可稍后调用 shell_status 查看输出，或用 shell_stop 停止。'
                )
            process = subprocess.Popen(
                base_args + [remote_command],
                stdout=output_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            job['process'] = process
            job['pid'] = process.pid
            self.jobs[job_id] = job
            return (
                f'已后台启动远程 shell 任务。\n'
                f'job_id: {job_id}\n'
                f'pid: {process.pid}\n'
                f'cwd: {job["cwd"]}\n'
                f'timeout_seconds: {timeout}\n'
                '可稍后调用 shell_status 查看输出，或用 shell_stop 停止。'
            )
        completed, err = _run_ssh_command(
            self.profile,
            remote_command,
            timeout_seconds=timeout,
        )
        if err:
            _log_ssh_diag(self.profile, 'warn', f'SSH shell_exec 失败: {err}')
            return err
        if completed is None:
            _log_ssh_diag(self.profile, 'warn', 'SSH shell_exec 未获得执行结果。')
            return 'SSH 执行失败：未获得执行结果。'
        try:
            output = _format_shell_output(
                completed.stdout.decode('utf-8', errors='replace'),
                completed.stderr.decode('utf-8', errors='replace'),
            )
            return (
                f'命令执行完成。\n'
                f'exit_code: {completed.returncode}\n'
                f'cwd: {display_cwd or "/"}\n'
                f'timed_out: false\n'
                f'output:\n{output}'
            )
        except Exception as exc:
            _log_ssh_diag(self.profile, 'error', f'SSH shell_exec 异常 cwd={display_cwd or "/"} cmd={_diag_text(command)} error={exc}')
            return f'SSH 执行失败: {exc}'

    def status(self, job_id: str, tail_lines: int = SHELL_DEFAULT_TAIL_LINES) -> str:
        job = self.jobs.get(str(job_id or '').strip())
        if not job:
            return f'未找到后台 shell 任务: {job_id}'
        tail_lines = max(1, min(SHELL_MAX_TAIL_LINES, int(tail_lines or SHELL_DEFAULT_TAIL_LINES)))
        job = self._refresh_job(job)
        output = _tail_lines(_read_text_tail(job.get('output_path') or ''), tail_lines).strip() or '(暂无输出)'
        duration = self._job_duration(job)
        return (
            f'job_id: {job["job_id"]}\n'
            f'status: {job.get("status")}\n'
            f'pid: {job.get("pid")}\n'
            f'exit_code: {job.get("exit_code")}\n'
            f'cwd: {job.get("cwd")}\n'
            f'timeout_seconds: {job.get("timeout_seconds")}\n'
            f'duration_seconds: {duration:.1f}\n'
            f'command: {job.get("command")}\n'
            f'output_tail:\n{output}'
        )

    def stop(self, job_id: str, force: bool = False, wait_seconds: int = 5) -> str:
        job = self.jobs.get(str(job_id or '').strip())
        if not job:
            return f'未找到后台 shell 任务: {job_id}'
        self._refresh_job(job)
        if job.get('status') != 'running':
            return (
                f'任务已不是运行中状态，无需停止。\n'
                f'job_id: {job["job_id"]}\n'
                f'status: {job.get("status")}\n'
                f'exit_code: {job.get("exit_code")}'
            )
        job = self._stop_job(job, force=bool(force), wait_seconds=wait_seconds)
        return (
            f'已停止后台 shell 任务。\n'
            f'job_id: {job["job_id"]}\n'
            f'status: {job.get("status")}\n'
            f'exit_code: {job.get("exit_code")}'
        )

    def list_jobs(self, active_only: bool = False) -> str:
        active_only = bool(active_only)
        lines: list[str] = []
        for job_id in list(self.jobs.keys()):
            job = self._refresh_job(self.jobs[job_id])
            if active_only and job.get('status') != 'running':
                continue
            lines.append(
                f'{job["job_id"]} | {job.get("status")} | exit={job.get("exit_code")} | '
                f'cwd={job.get("cwd")} | timeout={job.get("timeout_seconds")} | cmd={job.get("command")}'
            )
        if not lines:
            return '没有后台 shell 任务。'
        return '\n'.join(lines)

    def shutdown(self) -> list[str]:
        stopped: list[str] = []
        for job in list(self.jobs.values()):
            self._refresh_job(job)
            if job.get('status') == 'running':
                self._stop_job(job, force=True, reason='shutdown')
                stopped.append(job['job_id'])
        for job in list(self.transfer_jobs.values()):
            if job.get('status') == 'running':
                job['cancel_event'].set()
                stopped.append(job['transfer_id'])
        return stopped


def _build_tools_schema(read_only: bool = False, ssh_enabled: bool = False, resident: bool = False) -> list[dict]:
    default_foreground_timeout = SSH_SHELL_DEFAULT_TIMEOUT_SECONDS if ssh_enabled else SHELL_DEFAULT_TIMEOUT_SECONDS
    if read_only:
        shell_desc = (
            '在项目本地仓库内执行 shell 命令（当前为只读模式：仅允许白名单内无副作用的只读命令，'
            '例如 ls/cat/grep/head/tail/stat/du/wc/find/diff/sort/uniq/awk/sed(无 -i) 等；'
            '可用 ; / && / || / | 组合多个白名单命令；禁止后台任务、写入重定向(>)、heredoc(<<)、任何写操作）。'
        )
    else:
        shell_desc = (
            '在项目本地仓库内执行 shell 命令。支持前台等待结果，也支持后台运行。'
            '前台模式可设置 timeout_seconds 超时秒数；后台模式会返回 job_id，之后可用 shell_status / shell_stop / shell_list 管理。'
        )
    tools = [
        {
            'name': 'shell_exec',
            'description': shell_desc,
            'input_schema': {
                'type': 'object',
                'properties': {
                    'command': {'type': 'string', 'description': '要执行的 shell 命令，将通过 bash 脚本方式执行，支持 heredoc、变量赋值、管道、多行命令等标准语法'},
                    'cwd': {'type': 'string', 'description': '工作目录。/ 为仓库根目录，~ 为项目目录，也可写 /subdir 或 ~/subdir；留空时沿用当前 agent 的默认工作目录'},
                    'timeout_seconds': {'type': 'integer', 'description': f'超时秒数。前台默认{default_foreground_timeout}秒，后台默认600秒，最大3600秒'},
                    'background': {'type': 'boolean', 'description': '是否后台运行。true 时立即返回 job_id'},
                },
                'required': ['command'],
            },
        },
        {
            'name': 'shell_status',
            'description': '查看某个后台 shell 任务的当前状态、退出码和最近输出。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'job_id': {'type': 'string', 'description': '后台任务 ID，由 shell_exec 返回'},
                    'tail_lines': {'type': 'integer', 'description': '返回末尾输出行数，默认80，最大200'},
                },
                'required': ['job_id'],
            },
        },
        {
            'name': 'shell_stop',
            'description': '停止某个后台 shell 任务。默认先温和终止，必要时可 force 强制杀掉。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'job_id': {'type': 'string', 'description': '后台任务 ID'},
                    'force': {'type': 'boolean', 'description': '是否直接强制终止'},
                    'wait_seconds': {'type': 'integer', 'description': '温和终止后等待秒数，默认5秒'},
                },
                'required': ['job_id'],
            },
        },
        {
            'name': 'shell_list',
            'description': '列出当前 tasker / agent 执行会话内创建过的 shell 后台任务。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'active_only': {'type': 'boolean', 'description': '只看仍在运行的任务'},
                },
                'required': [],
            },
        },
        {
            'name': 'find_in_project',
            'description': (
                '一次调用完成全仓库递归查找，定位文件首选这个工具，不要用多轮 list_local_files 逐层下钻。'
                'name_pattern 按文件名通配（如 *runtime*.py、config.*）；content_query 按文件内容搜索，'
                '返回命中的文件路径和行号。两个参数可单独用也可组合（组合时只在名字匹配的文件里搜内容）。'
                '自动跳过 .git/node_modules/__pycache__ 等目录。'
                '拿到路径后再用 read_local_file / read_local_file_chunk / search_local_file 精读。'
            ),
            'input_schema': {
                'type': 'object',
                'properties': {
                    'name_pattern': {'type': 'string', 'description': '文件名通配模式，支持 * 和 ?，不区分大小写；不含通配符时按子串匹配'},
                    'content_query': {'type': 'string', 'description': '要在文件内容中搜索的文本或正则'},
                    'is_regex': {'type': 'boolean', 'description': '是否把 content_query 当正则处理'},
                    'subpath': {'type': 'string', 'description': '可选，限定搜索的子目录，留空表示整个仓库'},
                    'max_results': {'type': 'integer', 'description': f'最多返回多少条结果，默认{FIND_DEFAULT_MAX_RESULTS}，上限{FIND_MAX_RESULTS_CAP}'},
                },
                'required': [],
            },
        },
        {
            'name': 'list_local_files',
            'description': '列出项目本地仓库目录下某个子路径内的文件和文件夹（相对路径，留空表示仓库根目录）。定位未知文件请用 find_in_project，不要靠这个逐层下钻。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'subpath': {'type': 'string', 'description': '相对仓库根目录的子路径，留空表示根目录'},
                },
                'required': [],
            },
        },
        {
            'name': 'read_local_file',
            'description': (
                '读取项目本地仓库目录下某个文件的完整内容（相对路径）。'
                '注意：超大文件的完整内容回填模型时有字符上限（约 36K 字符），超限会被头尾截断并给出提示；'
                '大文件请优先使用 read_local_file_chunk 按行/按字节分块读取。'
            ),
            'input_schema': {
                'type': 'object',
                'properties': {'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'}},
                'required': ['path'],
            },
        },
        {
            'name': 'read_local_file_chunk',
            'description': (
                '按字节偏移或按行范围读取本地文本文件的一部分，适合大文件分块查看与续读。'
                'lines 模式按行流式读取（任意大小文件均可），返回总行数与本次行号范围，配合 start_line 可精确定位续读；'
                'bytes 模式返回字节范围、起始行号与文件总行数，offset 会自动对齐 UTF-8 字符边界（不会从多字节字符中间切开）。'
            ),
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'},
                    'offset_bytes': {'type': 'integer', 'description': '可选，起始字节偏移，默认0；返回结果里的 offset_bytes/read_bytes 是下一次续读的真实基准'},
                    'max_bytes': {'type': 'integer', 'description': '可选，最多读取多少字节，默认120000，最大200000'},
                    'start_line': {'type': 'integer', 'description': '可选，起始行号，从1开始；大文件定位续读建议优先用该模式'},
                    'line_count': {'type': 'integer', 'description': '可选，读取行数，默认120'},
                },
                'required': ['path'],
            },
        },
        {
            'name': 'search_local_file',
            'description': '在已知路径的单个本地文本文件中查找关键词或正则，返回匹配行号和附近上下文，用于改前精确定位。返回的行号可直接作为 replace_local_file_lines / insert_local_file_lines / delete_local_file_lines 的依据，不要从其他来源(SSH/root 镜像/GitHub)拷贝行号。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'},
                    'query': {'type': 'string', 'description': '要搜索的文本或正则表达式'},
                    'is_regex': {'type': 'boolean', 'description': '是否把 query 当正则处理'},
                    'max_matches': {'type': 'integer', 'description': '最多返回多少个匹配，默认20'},
                    'context_lines': {'type': 'integer', 'description': '每个匹配前后附带多少行上下文，默认1'},
                },
                'required': ['path', 'query'],
            },
        },
        {
            'name': 'query_local_file_json',
            'description': (
                '对本地 JSON 文件做字段级结构化查询，只提取需要的字段，避免整文件读取浪费上下文。'
                'query 支持点路径：$ 表示根，$.a.b 取字段，a[0] 取数组下标（负数从末尾数），'
                '[*] 表示数组/对象展开遍历，例如 $.users[*].name 返回所有用户姓名。'
            ),
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的 JSON 文件路径'},
                    'query': {'type': 'string', 'description': '字段路径表达式，例如 $.config.timeout 或 $.users[*].email，默认 $ 返回整棵树的摘要'},
                    'max_items': {'type': 'integer', 'description': '最多返回多少个命中项，默认50'},
                },
                'required': ['path'],
            },
        },
        {
            'name': 'replace_local_file_text',
            'description': (
                '对本地文本文件做精确文本替换，可限制替换第几个匹配或要求命中次数。'
                '锚点行尾(CRLF/LF)会自动归一化，不因行尾差异误判失败。'
                '锚点内容应从真实目标文件读取，不要从 SSH/root 镜像或 GitHub 拷贝，以免缩进/内容不一致。'
                '若锚点找不到，会返回真实文件中最相近的行号与内容，直接据此改用 replace_local_file_lines 按行号一次性补丁，不要反复猜文本锚点或重复 dry-run。'
            ),
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'},
                    'old_text': {'type': 'string', 'description': '要被替换的原文本'},
                    'new_text': {'type': 'string', 'description': '替换后的新文本'},
                    'replace_all': {'type': 'boolean', 'description': '是否替换全部匹配，默认false'},
                    'occurrence': {'type': 'integer', 'description': '当 replace_all=false 时，替换第几个匹配，默认1'},
                    'expected_count': {'type': 'integer', 'description': '可选，要求原文本出现次数必须等于该值，否则拒绝修改'},
                    'dry_run': {'type': 'boolean', 'description': '是否只预览修改而不落盘'},
                    'create_backup': {'type': 'boolean', 'description': '修改前是否自动备份原文件'},
                },
                'required': ['path', 'old_text', 'new_text'],
            },
        },
        {
            'name': 'replace_local_file_lines',
            'description': '按行号区间替换本地文本文件内容，适合已定位到具体行范围后的定点修改。行号必须以 search_local_file 或 read_local_file_chunk 在真实目标文件上查到的为准，不要沿用其他来源（SSH/root 镜像/GitHub）的行号。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'},
                    'start_line': {'type': 'integer', 'description': '起始行号，从1开始'},
                    'end_line': {'type': 'integer', 'description': '结束行号，包含该行'},
                    'content': {'type': 'string', 'description': '替换后的完整文本，可多行'},
                    'dry_run': {'type': 'boolean', 'description': '是否只预览修改而不落盘'},
                    'create_backup': {'type': 'boolean', 'description': '修改前是否自动备份原文件'},
                },
                'required': ['path', 'start_line', 'end_line', 'content'],
            },
        },
        {
            'name': 'insert_local_file_lines',
            'description': '在本地文本文件某一行前或某一行后插入内容，适合增量插入代码块。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'},
                    'line': {'type': 'integer', 'description': '基准行号，从1开始'},
                    'content': {'type': 'string', 'description': '要插入的完整文本，可多行'},
                    'position': {'type': 'string', 'description': 'before 或 after，默认 after'},
                    'dry_run': {'type': 'boolean', 'description': '是否只预览修改而不落盘'},
                    'create_backup': {'type': 'boolean', 'description': '修改前是否自动备份原文件'},
                },
                'required': ['path', 'line', 'content'],
            },
        },
        {
            'name': 'delete_local_file_lines',
            'description': '删除本地文本文件指定行号区间。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'},
                    'start_line': {'type': 'integer', 'description': '起始行号，从1开始'},
                    'end_line': {'type': 'integer', 'description': '结束行号，包含该行'},
                    'dry_run': {'type': 'boolean', 'description': '是否只预览修改而不落盘'},
                    'create_backup': {'type': 'boolean', 'description': '修改前是否自动备份原文件'},
                },
                'required': ['path', 'start_line', 'end_line'],
            },
        },
        {
            'name': 'replace_local_file_regex',
            'description': '对本地文本文件做正则替换，可限制替换次数，并校验命中总数。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'},
                    'pattern': {'type': 'string', 'description': '正则表达式'},
                    'replacement': {'type': 'string', 'description': '替换文本，支持正则分组引用'},
                    'count': {'type': 'integer', 'description': '最多替换多少处，默认1，0表示全部'},
                    'expected_count': {'type': 'integer', 'description': '可选，要求正则命中总数必须等于该值，否则拒绝修改'},
                    'flags': {'type': 'string', 'description': '可选，正则标志组合，如 i,m,s'},
                    'dry_run': {'type': 'boolean', 'description': '是否只预览修改而不落盘'},
                    'create_backup': {'type': 'boolean', 'description': '修改前是否自动备份原文件'},
                },
                'required': ['path', 'pattern', 'replacement'],
            },
        },
        {
            'name': 'apply_unified_diff_to_file',
            'description': '对单个本地文本文件应用 unified diff 补丁，适合精确修改多处内容。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '可选，目标文件路径；为空时尝试从 diff 头部解析'},
                    'diff': {'type': 'string', 'description': '标准 unified diff 文本，需针对单个文件'},
                    'dry_run': {'type': 'boolean', 'description': '是否只预览修改而不落盘'},
                    'create_backup': {'type': 'boolean', 'description': '修改前是否自动备份原文件'},
                },
                'required': ['diff'],
            },
        },
        {
            'name': 'edit_local_file',
            'description': (
                '整体覆盖写入项目本地仓库目录下某个文件的内容（相对路径），会完全替换原内容（文件不存在则新建）。'
                '建议先用 read_local_file 读一遍原文件，避免整体覆盖时丢失不该丢的内容。'
            ),
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'},
                    'content': {'type': 'string', 'description': '要写入的完整文件内容'},
                    'dry_run': {'type': 'boolean', 'description': '是否只预览修改而不落盘'},
                    'create_backup': {'type': 'boolean', 'description': '修改前是否自动备份原文件'},
                },
                'required': ['path', 'content'],
            },
        },
        {
            'name': 'todo_write',
            'description': '维护当前 agent 的 todo 列表，用于锁定多步任务的进行中/待办/已完成状态，防止长轮次遗忘。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'action': {'type': 'string', 'description': 'add / update / remove / list'},
                    'todo_id': {'type': 'string', 'description': '可选，todo 项 ID；update/remove 时必填'},
                    'content': {'type': 'string', 'description': 'todo 内容；add 时必填，update 时可选'},
                    'status': {'type': 'string', 'description': 'pending / in_progress / completed / blocked'},
                },
                'required': ['action'],
            },
        },
        {
            'name': 'note_write',
            'description': '维护当前 agent 的长期备注，用来记录绝不能忘的重点，如安全警告、禁区、踩坑记录等。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'action': {'type': 'string', 'description': 'add / update / remove / list'},
                    'note_id': {'type': 'string', 'description': '可选，备注 ID；update/remove 时必填'},
                    'content': {'type': 'string', 'description': '备注内容；add/update 时必填'},
                },
                'required': ['action'],
            },
        },
        {
            'name': 'github_search_code',
            'description': '在 GitHub 上只读搜索代码，用于查阅任意公开仓库的实现做参考。需要后台已配置 GitHub API token。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': '搜索关键词'},
                    'repo': {'type': 'string', 'description': '可选，限定在某个仓库内搜索，格式 owner/repo'},
                },
                'required': ['query'],
            },
        },
        {
            'name': 'github_read_file',
            'description': '只读查看 GitHub 上任意公开仓库某个文件的内容。需要后台已配置 GitHub API token。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string', 'description': '仓库所有者'},
                    'repo': {'type': 'string', 'description': '仓库名'},
                    'path': {'type': 'string', 'description': '文件在仓库内的路径'},
                    'ref': {'type': 'string', 'description': '可选，分支/commit/tag，留空用默认分支'},
                },
                'required': ['owner', 'repo', 'path'],
            },
        },
        {
            'name': 'github_list_repos',
            'description': 'GitHub token 对应账户下可访问的仓库列表（按最近更新排序）。',
            'input_schema': {
                'type': 'object',
                'properties': {'per_page': {'type': 'integer', 'description': '返回数量，默认30'}},
                'required': [],
            },
        },
        {
            'name': 'github_search_repos',
            'description': '按关键词搜索 GitHub 上的公开仓库。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': '搜索关键词，支持 GitHub 搜索语法，如 language:python stars:>100'},
                    'per_page': {'type': 'integer', 'description': '返回数量，默认10'},
                },
                'required': ['query'],
            },
        },
        {
            'name': 'github_create_or_update_file',
            'description': '在 GitHub 仓库里创建或更新一个文件（有写权限的仓库）。会自动处理已存在文件的 sha。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'path': {'type': 'string', 'description': '文件在仓库内的路径'},
                    'content': {'type': 'string', 'description': '完整文件内容'},
                    'message': {'type': 'string', 'description': 'commit message'},
                    'branch': {'type': 'string', 'description': '可选，目标分支，留空用默认分支'},
                },
                'required': ['owner', 'repo', 'path', 'content', 'message'],
            },
        },
        {
            'name': 'github_delete_file',
            'description': '在 GitHub 仓库里删除一个文件（有写权限的仓库）。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'path': {'type': 'string'},
                    'message': {'type': 'string', 'description': 'commit message'},
                    'branch': {'type': 'string', 'description': '可选，目标分支，留空用默认分支'},
                },
                'required': ['owner', 'repo', 'path', 'message'],
            },
        },
        {
            'name': 'github_list_branches',
            'description': '列出 GitHub 仓库的所有分支。',
            'input_schema': {
                'type': 'object',
                'properties': {'owner': {'type': 'string'}, 'repo': {'type': 'string'}},
                'required': ['owner', 'repo'],
            },
        },
        {
            'name': 'github_create_branch',
            'description': '在 GitHub 仓库里基于某个已有分支创建一个新分支。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'new_branch': {'type': 'string', 'description': '新分支名'},
                    'from_branch': {'type': 'string', 'description': '可选，基础分支，留空用默认分支'},
                },
                'required': ['owner', 'repo', 'new_branch'],
            },
        },
        {
            'name': 'github_create_tag',
            'description': '在 GitHub 仓库里创建一个轻量标签，指向某个分支/commit。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'tag_name': {'type': 'string'},
                    'ref': {'type': 'string', 'description': '可选，分支名或 commit sha，留空用默认分支'},
                },
                'required': ['owner', 'repo', 'tag_name'],
            },
        },
        {
            'name': 'github_list_pull_requests',
            'description': '列出 GitHub 仓库的 Pull Request。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'state': {'type': 'string', 'description': 'open/closed/all，默认open'},
                },
                'required': ['owner', 'repo'],
            },
        },
        {
            'name': 'github_create_pull_request',
            'description': '在 GitHub 仓库里创建一个 Pull Request。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'title': {'type': 'string'},
                    'head': {'type': 'string', 'description': '源分支，如 feature-x 或 user:feature-x'},
                    'base': {'type': 'string', 'description': '目标分支，如 main'},
                    'body': {'type': 'string', 'description': '可选，PR 描述'},
                },
                'required': ['owner', 'repo', 'title', 'head', 'base'],
            },
        },
        {
            'name': 'github_merge_pull_request',
            'description': '合并 GitHub 仓库里的一个 Pull Request。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'number': {'type': 'integer', 'description': 'PR 编号'},
                    'commit_message': {'type': 'string', 'description': '可选，合并提交信息'},
                },
                'required': ['owner', 'repo', 'number'],
            },
        },
        {
            'name': 'github_close_pull_request',
            'description': '关闭（不合并）GitHub 仓库里的一个 Pull Request。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'number': {'type': 'integer'},
                },
                'required': ['owner', 'repo', 'number'],
            },
        },
        {
            'name': 'github_list_issues',
            'description': '列出 GitHub 仓库的 Issue。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'state': {'type': 'string', 'description': 'open/closed/all，默认open'},
                },
                'required': ['owner', 'repo'],
            },
        },
        {
            'name': 'github_create_issue',
            'description': '在 GitHub 仓库里创建一个 Issue。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'title': {'type': 'string'},
                    'body': {'type': 'string', 'description': '可选，Issue 正文'},
                },
                'required': ['owner', 'repo', 'title'],
            },
        },
        {
            'name': 'github_add_issue_comment',
            'description': '给 GitHub 仓库的某个 Issue 或 PR 添加评论。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'number': {'type': 'integer'},
                    'body': {'type': 'string'},
                },
                'required': ['owner', 'repo', 'number', 'body'],
            },
        },
        {
            'name': 'github_close_issue',
            'description': '关闭 GitHub 仓库的某个 Issue。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'number': {'type': 'integer'},
                },
                'required': ['owner', 'repo', 'number'],
            },
        },
        {
            'name': 'github_list_commits',
            'description': '查看 GitHub 仓库的提交历史。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'sha': {'type': 'string', 'description': '可选，分支名或 commit sha 起点'},
                    'path': {'type': 'string', 'description': '可选，只看某个文件路径的提交历史'},
                },
                'required': ['owner', 'repo'],
            },
        },
        {
            'name': 'github_get_commit',
            'description': '查看 GitHub 仓库某个 commit 的详情（含改动文件）。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'owner': {'type': 'string'},
                    'repo': {'type': 'string'},
                    'sha': {'type': 'string'},
                },
                'required': ['owner', 'repo', 'sha'],
            },
        },
    ]
    if ssh_enabled:
        tools.extend([
            {
                'name': 'ssh_download_file',
                'description': (
                    '后台从当前 SSH 服务器下载文件到本地项目目录。'
                    '支持大文件分块续传；若中途取消或中断，下次对同一路径重新发起时会尽量从 .sshpart 续传。'
                    '启动后立即返回 transfer_id，可用 ssh_transfer_status / ssh_transfer_cancel / ssh_transfer_list 管理。'
                ),
                'input_schema': {
                    'type': 'object',
                    'properties': {
                        'remote_path': {'type': 'string', 'description': '远程文件路径（相对当前 ssh profile 根目录）'},
                        'local_path': {'type': 'string', 'description': '本地目标文件路径（相对项目根目录）'},
                        'overwrite': {'type': 'boolean', 'description': '目标已存在时是否覆盖；false 时若已有完整文件会拒绝启动'},
                        'chunk_bytes': {'type': 'integer', 'description': '可选分块大小，默认 2MB，最大 16MB'},
                    },
                    'required': ['remote_path', 'local_path'],
                },
            },
            {
                'name': 'ssh_upload_file',
                'description': (
                    '后台把本地项目目录中的文件上传到当前 SSH 服务器。'
                    '支持大文件分块续传；上传时会先写入远端 .sshpart 临时文件，完成后再原子替换目标。'
                    '启动后立即返回 transfer_id，可用 ssh_transfer_status / ssh_transfer_cancel / ssh_transfer_list 管理。'
                ),
                'input_schema': {
                    'type': 'object',
                    'properties': {
                        'local_path': {'type': 'string', 'description': '本地源文件路径（相对项目根目录）'},
                        'remote_path': {'type': 'string', 'description': '远程目标文件路径（相对当前 ssh profile 根目录）'},
                        'overwrite': {'type': 'boolean', 'description': '是否覆盖远端同名目标；上传过程中的 .sshpart 会按需复用或重建'},
                        'chunk_bytes': {'type': 'integer', 'description': '可选分块大小，默认 2MB，最大 16MB'},
                    },
                    'required': ['local_path', 'remote_path'],
                },
            },
            {
                'name': 'ssh_transfer_status',
                'description': '查看某个 SSH 传输任务的当前进度、速率、方向和错误信息。',
                'input_schema': {
                    'type': 'object',
                    'properties': {
                        'transfer_id': {'type': 'string', 'description': '传输任务 ID，由 ssh_download_file / ssh_upload_file 返回'},
                    },
                    'required': ['transfer_id'],
                },
            },
            {
                'name': 'ssh_transfer_cancel',
                'description': '取消某个正在运行的 SSH 传输任务。当前分块若已发出，会在该分块结束后停止。',
                'input_schema': {
                    'type': 'object',
                    'properties': {
                        'transfer_id': {'type': 'string', 'description': '传输任务 ID'},
                    },
                    'required': ['transfer_id'],
                },
            },
            {
                'name': 'ssh_transfer_list',
                'description': '列出当前 ssh_agent 会话里的 SSH 文件传输任务和进度。',
                'input_schema': {
                    'type': 'object',
                    'properties': {
                        'active_only': {'type': 'boolean', 'description': '是否只显示仍在运行的传输任务'},
                    },
                    'required': [],
                },
            },
        ])
    if read_only:
        tools = [tool for tool in tools if tool.get('name') in READ_ONLY_AGENT_TOOLS]
    if resident:
        # 常驻 agent 专用：把"汇报进展/提问/完成"三件事从纯文本里拆出来。
        # 只读模式也需要这三个出口，所以放在只读过滤之后追加。
        tools.extend(RESIDENT_AGENT_COMM_TOOLS)
    return tools


def _list_local_files(project_root: str, subpath: str) -> str:
    resolved = _resolve_safe_path(project_root, subpath) if subpath else project_root
    if resolved is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝访问。'
    if not os.path.isdir(resolved):
        return f'{subpath or "."} 不是一个目录，或不存在。'
    try:
        entries = sorted(os.listdir(resolved))
    except OSError as exc:
        return f'读取目录失败: {exc}'
    lines = [f'{name}/' if os.path.isdir(os.path.join(resolved, name)) else name for name in entries]
    return '\n'.join(lines) if lines else '(空目录)'


def _find_name_matcher(name_pattern: str):
    """不含通配符时退化成不区分大小写的子串匹配，避免模型写 runtime 却一条都命中不到。"""
    pattern = str(name_pattern or '').strip()
    if not pattern:
        return None
    lowered = pattern.lower()
    if any(ch in lowered for ch in '*?['):
        return lambda name: fnmatch.fnmatch(name.lower(), lowered)
    return lambda name: lowered in name.lower()


def _iter_project_files(project_root: str, base_dir: str):
    """自顶向下遍历，就地裁剪噪声目录和 denylist，返回 (相对路径, 绝对路径)。"""
    for current, dir_names, file_names in os.walk(base_dir):
        dir_names[:] = sorted(
            name for name in dir_names
            if name not in FIND_SKIP_DIR_NAMES
            and _resolve_safe_path(project_root, os.path.relpath(os.path.join(current, name), project_root)) is not None
        )
        for name in sorted(file_names):
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, project_root).replace('\\', '/')
            if _resolve_safe_path(project_root, relative) is None:
                continue
            yield relative, absolute


def _find_content_hits(absolute: str, matcher, max_hits: int) -> list[tuple[int, str]]:
    try:
        if os.path.getsize(absolute) > FIND_CONTENT_MAX_FILE_BYTES:
            return []
        with open(absolute, 'r', encoding='utf-8') as handle:
            hits: list[tuple[int, str]] = []
            for line_no, line in enumerate(handle, start=1):
                if matcher(line):
                    hits.append((line_no, line.rstrip('\n')[:200]))
                    if len(hits) >= max_hits:
                        break
            return hits
    except (OSError, UnicodeDecodeError):
        return []


def _find_in_project(
    project_root: str,
    name_pattern: str = '',
    content_query: str = '',
    is_regex: bool = False,
    subpath: str = '',
    max_results: int = FIND_DEFAULT_MAX_RESULTS,
) -> str:
    name_pattern = str(name_pattern or '').strip()
    content_query = str(content_query or '')
    if not name_pattern and not content_query:
        return 'name_pattern 与 content_query 至少要提供一个，未执行搜索。'
    base_dir = _resolve_safe_path(project_root, subpath) if subpath else project_root
    if base_dir is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝访问。'
    if not os.path.isdir(base_dir):
        return f'{subpath or "."} 不是一个目录，或不存在。'
    max_results = max(1, min(FIND_MAX_RESULTS_CAP, int(max_results or FIND_DEFAULT_MAX_RESULTS)))

    name_matcher = _find_name_matcher(name_pattern)
    line_matcher = None
    if content_query:
        if is_regex:
            try:
                compiled = re.compile(content_query)
            except re.error as exc:
                return f'正则表达式无效: {exc}'
            line_matcher = compiled.search
        else:
            line_matcher = lambda line: content_query in line  # noqa: E731

    lines: list[str] = []
    result_count = 0
    scanned = 0
    truncated_scan = False
    for relative, absolute in _iter_project_files(project_root, base_dir):
        if result_count >= max_results:
            break
        scanned += 1
        if scanned > FIND_MAX_SCAN_FILES:
            truncated_scan = True
            break
        if name_matcher is not None and not name_matcher(os.path.basename(relative)):
            continue
        if line_matcher is None:
            lines.append(relative)
            result_count += 1
            continue
        hits = _find_content_hits(absolute, line_matcher, max_results - result_count)
        for line_no, text in hits:
            lines.append(f'{relative}:{line_no}: {text}')
            result_count += 1

    scope = subpath or '仓库根目录'
    if not lines:
        return f'在 {scope} 下未找到匹配项（已扫描 {scanned} 个文件）。'
    header = f'在 {scope} 下找到 {result_count} 条结果（已扫描 {scanned} 个文件）：'
    footer = ''
    if result_count >= max_results:
        footer = f'\n已达 max_results={max_results} 上限，可能还有更多结果，请缩小范围或提高上限。'
    elif truncated_scan:
        footer = f'\n已达扫描上限 {FIND_MAX_SCAN_FILES} 个文件，请用 subpath 缩小范围。'
    return header + '\n' + '\n'.join(lines) + footer


def _read_local_file(project_root: str, path: str) -> str:
    resolved = _resolve_safe_path(project_root, path)
    if resolved is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝读取。'
    if not os.path.isfile(resolved):
        return f'{path} 不是一个文件，或不存在。'
    size = os.path.getsize(resolved)
    if size > MAX_FILE_BYTES:
        return f'{path} 文件过大（{size} 字节），超过读取上限，拒绝读取。'
    try:
        with open(resolved, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        return f'{path} 不是可读的文本文件（可能是二进制文件）。'
    except OSError as exc:
        return f'读取文件失败: {exc}'


def _read_text_file_for_operation(project_root: str, path: str, size_limit: int = MAX_FILE_OPERATION_BYTES) -> tuple[str | None, str | None]:
    resolved = _resolve_safe_path(project_root, path)
    if resolved is None:
        return None, '路径不合法、超出允许范围，或命中禁止访问清单，拒绝读取。'
    if not os.path.isfile(resolved):
        return None, f'{path} 不是一个文件，或不存在。'
    size = os.path.getsize(resolved)
    if size > size_limit:
        return None, f'{path} 文件过大（{size} 字节），超过当前操作上限 {size_limit} 字节。'
    try:
        with open(resolved, 'r', encoding='utf-8') as f:
            return f.read(), None
    except UnicodeDecodeError:
        return None, f'{path} 不是可读的文本文件（可能是二进制文件）。'
    except OSError as exc:
        return None, f'读取文件失败: {exc}'


def _write_text_file(project_root: str, path: str, content: str, newline: str | None = None) -> str:
    resolved = _resolve_safe_path(project_root, path)
    if resolved is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝写入。'
    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        # newline='' 时不做平台行尾翻译，保证写入内容与内存中的 updated 完全一致（行尾风格由调用方决定）
        with open(resolved, 'w', encoding='utf-8', newline=newline) as f:
            f.write(content)
    except OSError as exc:
        return f'写入文件失败: {exc}'
    return ''


def _build_preview_diff(path: str, original: str, updated: str) -> str:
    # 两侧统一到 LF 再比，避免 CRLF 文件因行尾差异被误报成整文件改动
    def _lines(text: str) -> list[str]:
        return [line.rstrip('\r\n') + '\n' for line in text.splitlines(keepends=True)]

    diff_lines = list(
        difflib.unified_diff(
            _lines(original),
            _lines(updated),
            fromfile=f'a/{path}',
            tofile=f'b/{path}',
            lineterm='',
        )
    )
    diff_text = '\n'.join(diff_lines).strip()
    return diff_text or '(无差异)'


def _create_backup_file(project_root: str, path: str, original: str) -> tuple[str, str]:
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f'{path}.bak.{stamp}'
    write_err = _write_text_file(project_root, backup_path, original)
    if write_err:
        return '', write_err
    return backup_path, ''


def _finalize_file_update(
    project_root: str,
    path: str,
    original: str,
    updated: str,
    action_summary: str,
    dry_run: bool = False,
    create_backup: bool = False,
    newline: str | None = None,
) -> str:
    if original == updated:
        return '修改结果与原文件相同，未产生变化。'
    preview = _build_preview_diff(path, original, updated)
    if dry_run:
        return f'{action_summary}\n模式: dry_run\n预览 diff:\n{preview}'
    backup_text = ''
    if create_backup:
        backup_path, backup_err = _create_backup_file(project_root, path, original)
        if backup_err:
            return backup_err
        backup_text = f'\n备份文件: {backup_path}'
    write_err = _write_text_file(project_root, path, updated, newline=newline)
    if write_err:
        return write_err
    return f'{action_summary}{backup_text}\n应用 diff:\n{preview}'


def _stream_text_lines(path: str, start_line: int, line_count: int) -> tuple[list[str], int]:
    """流式按行读取：内存有界，一次线性扫描，返回 (目标行列表, 文件总行数)。"""
    start_line = max(1, int(start_line or 1))
    line_count = max(1, int(line_count or 120))
    collected: list[str] = []
    total = 0
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            total += 1
            if start_line <= total < start_line + line_count:
                collected.append(line.rstrip('\r\n'))
    return collected, total


def _scan_text_line_meta(path: str, byte_offset: int) -> tuple[int, int]:
    """单次线性扫描：返回 (byte_offset 所在行号, 文件总行数)。行号从 1 开始。"""
    total = 0
    line_at = 1
    remaining = max(0, byte_offset)
    with open(path, 'rb') as f:
        for raw in f:
            total += 1
            if remaining >= 0:
                remaining -= len(raw)
                if remaining < 0:
                    line_at = total
    return line_at, total


def _read_local_file_chunk(
    project_root: str,
    path: str,
    offset_bytes: int | None = None,
    max_bytes: int | None = None,
    start_line: int | None = None,
    line_count: int | None = None,
) -> str:
    resolved = _resolve_safe_path(project_root, path)
    if resolved is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝读取。'
    if not os.path.isfile(resolved):
        return f'{path} 不是一个文件，或不存在。'

    if start_line is not None:
        try:
            size = os.path.getsize(resolved)
        except OSError as exc:
            return f'读取文件失败: {exc}'
        if size > MAX_STREAM_LINE_FILE_BYTES:
            return f'{path} 过大（{size} 字节），lines 模式暂不支撑，请改用 bytes 模式分块读取。'
        start_line = max(1, int(start_line or 1))
        line_count = max(1, int(line_count or 120))
        chunk_lines, total = _stream_text_lines(resolved, start_line, line_count)
        end_idx = start_line + len(chunk_lines) - 1
        return (
            f'文件: {path}\n'
            f'模式: lines\n'
            f'起始行: {start_line}\n'
            f'结束行: {end_idx}\n'
            f'总行数: {total}\n'
            f'内容:\n' + ('\n'.join(chunk_lines))
        )

    offset_bytes = max(0, int(offset_bytes or 0))
    max_bytes = min(MAX_FILE_CHUNK_BYTES, max(1, int(max_bytes or 120_000)))
    try:
        size = os.path.getsize(resolved)
        if offset_bytes >= size:
            return f'偏移 {offset_bytes} 已超出文件末尾（文件大小 {size} 字节）。'
        with open(resolved, 'rb') as f:
            f.seek(offset_bytes)
            data = f.read(max_bytes)
    except OSError as exc:
        return f'读取文件失败: {exc}'
    # UTF-8 对齐：跳过头部续字节，保证不会从多字节字符中间切开
    head_skip = 0
    while head_skip < len(data) and 0x80 <= data[head_skip] <= 0xBF:
        head_skip += 1
    actual_start = offset_bytes + head_skip
    data = data[head_skip:]
    text = data.decode('utf-8', errors='ignore')
    start_line_no, total_lines = _scan_text_line_meta(resolved, actual_start)
    if text:
        spanned = text.count('\n') + 1 - (1 if text.endswith('\n') else 0)
        end_line_no = start_line_no + spanned - 1
    else:
        end_line_no = start_line_no - 1
    return (
        f'文件: {path}\n'
        f'模式: bytes\n'
        f'offset_bytes: {actual_start}\n'
        f'read_bytes: {len(data)}\n'
        f'start_line: {start_line_no}\n'
        f'end_line: {end_line_no}\n'
        f'total_lines: {total_lines}\n'
        f'内容:\n{text}'
    )


def _search_local_file(
    project_root: str,
    path: str,
    query: str,
    is_regex: bool = False,
    max_matches: int = 20,
    context_lines: int = 1,
) -> str:
    text, err = _read_text_file_for_operation(project_root, path)
    if err:
        return err
    query = str(query or '')
    if not query:
        return '搜索关键词为空，未执行搜索。'
    max_matches = max(1, min(100, int(max_matches or 20)))
    context_lines = max(0, min(5, int(context_lines or 1)))
    lines = text.splitlines()
    pattern = None
    if is_regex:
        try:
            pattern = re.compile(query)
        except re.error as exc:
            return f'正则表达式无效: {exc}'
    results: list[str] = []
    match_count = 0
    for idx, line in enumerate(lines, start=1):
        matched = bool(pattern.search(line)) if pattern else (query in line)
        if not matched:
            continue
        match_count += 1
        if len(results) >= max_matches:
            continue
        start = max(1, idx - context_lines)
        end = min(len(lines), idx + context_lines)
        block = [f'命中 #{match_count} | 行 {idx}']
        for line_no in range(start, end + 1):
            prefix = '>' if line_no == idx else ' '
            block.append(f'{prefix} {line_no}: {lines[line_no - 1]}')
        results.append('\n'.join(block))
    if match_count == 0:
        return '未找到匹配内容。'
    suffix = '' if match_count <= max_matches else f'\n仅展示前 {max_matches} 个命中。'
    return f'共找到 {match_count} 处匹配。\n' + '\n\n'.join(results) + suffix


def _parse_json_path_tokens(query: str) -> list[tuple[str, object]]:
    """解析 $.a.b[0][*].c 或 a.b[0] 形式的 JSON 路径，返回 (类型, 值) 元组列表。"""
    tokens: list[tuple[str, object]] = []
    rest = str(query or '').strip()
    if not rest:
        return tokens
    if rest.startswith('$'):
        rest = rest[1:]
    i = 0
    n = len(rest)
    while i < n:
        ch = rest[i]
        if ch == '.':
            i += 1
            if i >= n:
                break
        if ch == '[':
            j = rest.find(']', i)
            if j < 0:
                raise ValueError(f'缺少 ]: {query}')
            inner = rest[i + 1:j].strip()
            if inner == '*':
                tokens.append(('wild', None))
            else:
                try:
                    tokens.append(('index', int(inner)))
                except ValueError as exc:
                    raise ValueError(f'不支持的下标/键: {inner!r}') from exc
            i = j + 1
            continue
        j = i
        while j < n and rest[j] not in '.[':
            j += 1
        name = rest[i:j]
        if not name:
            raise ValueError(f'无法解析字段名: {query}')
        tokens.append(('field', name))
        i = j
    return tokens


def _query_local_file_json(
    project_root: str,
    path: str,
    query: str = '$',
    max_items: int = 50,
) -> str:
    text, err = _read_text_file_for_operation(project_root, path, size_limit=MAX_FILE_OPERATION_BYTES)
    if err:
        return err
    query = str(query or '').strip() or '$'
    max_items = max(1, min(200, int(max_items or 50)))
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return f'文件不是合法 JSON: {exc}'
    try:
        tokens = _parse_json_path_tokens(query)
    except ValueError as exc:
        return f'查询表达式无效: {exc}'

    matches: list[tuple[str, object]] = []
    total_count = 0

    def walk(current: object, rest: list[tuple[str, object]], path_str: str) -> None:
        nonlocal total_count
        if not rest:
            total_count += 1
            if len(matches) < max_items:
                matches.append((path_str, current))
            return
        token, *remain = rest
        if token[0] == 'wild':
            if isinstance(current, list):
                for i, item in enumerate(current):
                    walk(item, remain, f'{path_str}[{i}]')
            elif isinstance(current, dict):
                for k, v in current.items():
                    walk(v, remain, f'{path_str}.{k}')
        elif token[0] == 'index':
            if isinstance(current, list):
                idx = int(token[1])
                if idx < 0:
                    idx += len(current)
                if 0 <= idx < len(current):
                    walk(current[idx], remain, f'{path_str}[{idx}]')
        else:  # field
            name = str(token[1])
            if isinstance(current, dict) and name in current:
                walk(current[name], remain, f'{path_str}.{name}')

    walk(data, tokens, '$')
    if total_count == 0:
        return f'查询 {query} 无命中（文件已成功解析，但该路径不存在）。'

    def render(value: object) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return json.dumps(value, ensure_ascii=False)

    rendered = [f'{p} = {render(v)}' for p, v in matches]
    suffix = ''
    if total_count > max_items:
        suffix = f'\n（共命中 {total_count} 项，仅展示前 {max_items} 项；可提高 max_items 或缩小查询范围）'
    return f'文件: {path}\n查询: {query}\n命中: {total_count} 项\n' + '\n'.join(rendered) + suffix


def _query_remote_file_json(
    profile: SSHProfileConfig,
    path: str,
    query: str = '$',
    max_items: int = 50,
) -> str:
    text, err, _exists = _read_remote_text_file_for_operation(profile, path, size_limit=MAX_FILE_OPERATION_BYTES)
    if err:
        return err
    result, _updated = _local_text_tool_on_temp(
        path,
        str(text or ''),
        lambda tmp_root, relative_path: _query_local_file_json(tmp_root, relative_path, query, max_items=max_items),
    )
    return result


def _normalize_anchor_line_endings(old_text: str, new_text: str) -> tuple[str, str]:
    """把锚点文本的行尾统一成 LF，避免从 SSH/raw 读取/上传文件带来的 CRLF 导致锚点匹配失败。"""
    return (
        str(old_text or '').replace('\r\n', '\n').replace('\r', '\n'),
        str(new_text or '').replace('\r\n', '\n').replace('\r', '\n'),
    )


def _detect_crlf(project_root: str, path: str) -> bool:
    """按原始字节探测文件行尾是否为 CRLF，不受平台文本模式翻译影响。"""
    resolved = _resolve_safe_path(project_root, path)
    if resolved is None or not os.path.isfile(resolved):
        return False
    try:
        with open(resolved, 'rb') as f:
            head = f.read(8192)
    except OSError:
        return False
    return b'\r\n' in head


def _find_occurrence_line_numbers(text: str, needle: str, limit: int = 10) -> str:
    """返回 needle 在 text 中出现的行号列表（用于命中数不符时定位真实位置）。"""
    if not needle:
        return ''
    numbers: list[str] = []
    start = 0
    while start <= len(text):
        idx = text.find(needle, start)
        if idx < 0:
            break
        numbers.append(str(text.count('\n', 0, idx) + 1))
        if len(numbers) >= limit:
            numbers.append('…更多')
            break
        start = idx + max(1, len(needle))
    return '、'.join(numbers)


def _build_nearest_line_hints(text: str, target: str, limit: int = 3) -> str:
    """锚点 0 命中时，返回与目标最相似的真实行号与内容，供改用按行定位，避免反复猜锚点。"""
    lines = text.splitlines()
    target_lines = [line for line in target.splitlines() if line.strip()]
    if not lines or not target_lines:
        return ''
    scored = []
    for idx, line in enumerate(lines, start=1):
        best = max(difflib.SequenceMatcher(None, line, tline).ratio() for tline in target_lines)
        scored.append((best, idx, line))
    scored.sort(key=lambda item: -item[0])
    parts = []
    for score, idx, line in scored[:limit]:
        if score <= 0:
            break
        snippet = line if len(line) <= 120 else line[:120] + '…'
        parts.append(f'行 {idx}（相似度 {score:.0%}）：{snippet}')
    return '；'.join(parts)


def _replace_local_file_text(
    project_root: str,
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    occurrence: int = 1,
    expected_count: int | None = None,
    dry_run: bool = False,
    create_backup: bool = False,
) -> str:
    text, err = _read_text_file_for_operation(project_root, path)
    if err:
        return err
    old_text, new_text = _normalize_anchor_line_endings(old_text, new_text)
    if not old_text:
        return 'old_text 为空，拒绝替换。'
    # 匹配统一以 LF 为基准，避免平台/来源行尾差异造成误判；落盘按文件真实行尾风格还原，与运行平台无关。
    is_crlf = _detect_crlf(project_root, path)
    searchable = text.replace('\r\n', '\n').replace('\r', '\n')
    actual_count = searchable.count(old_text)
    if actual_count == 0:
        hints = _build_nearest_line_hints(searchable, old_text)
        msg = '未找到要替换的原文本，未修改文件。可能与真实文件存在行尾(CRLF/LF)、缩进或内容差异，或参考来源(SSH/root 镜像/GitHub)与目标文件不一致。'
        if hints:
            msg += f'\n最相近的真实内容（可直接据此改用按行定位）：\n{hints}'
        msg += '\n建议：先用 search_local_file 在真实文件上重新定位，再按行号用 replace_local_file_lines 一次性补丁；不要继续猜文本锚点或反复 dry-run。'
        return msg
    if expected_count is not None and actual_count != int(expected_count):
        located = _find_occurrence_line_numbers(searchable, old_text)
        return f'命中次数与预期不符：实际 {actual_count} 次，预期 {int(expected_count)} 次，已拒绝修改。\n实际命中行号：{located}'
    if replace_all:
        updated = searchable.replace(old_text, new_text)
        replaced_count = actual_count
    else:
        occurrence = max(1, int(occurrence or 1))
        start = 0
        target_index = -1
        for _ in range(occurrence):
            target_index = searchable.find(old_text, start)
            if target_index < 0:
                return f'只找到 {actual_count} 次匹配，第 {occurrence} 次不存在，已拒绝修改。'
            start = target_index + len(old_text)
        updated = searchable[:target_index] + new_text + searchable[target_index + len(old_text):]
        replaced_count = 1
    if is_crlf:
        updated = updated.replace('\n', '\r\n')
    return _finalize_file_update(
        project_root,
        path,
        text,
        updated,
        f'已定点替换 {path}，命中 {actual_count} 次，本次计划修改 {replaced_count} 处。',
        dry_run=dry_run,
        create_backup=create_backup,
        newline='',
    )


def _replace_local_file_lines(
    project_root: str,
    path: str,
    start_line: int,
    end_line: int,
    content: str,
    dry_run: bool = False,
    create_backup: bool = False,
) -> str:
    text, err = _read_text_file_for_operation(project_root, path)
    if err:
        return err
    lines = text.splitlines(keepends=True)
    start_line = int(start_line or 0)
    end_line = int(end_line or 0)
    if start_line <= 0 or end_line <= 0 or end_line < start_line:
        return '行号范围不合法，未修改。'
    if start_line > len(lines) or end_line > len(lines):
        return f'行号超出范围：文件共 {len(lines)} 行，请先搜索或分块读取确认位置。'
    replacement = str(content or '')
    replacement_lines = replacement.splitlines(keepends=True)
    if replacement and not replacement.endswith('\n'):
        replacement_lines[-1] = replacement_lines[-1] + '\n'
    updated_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
    updated = ''.join(updated_lines)
    return _finalize_file_update(
        project_root,
        path,
        text,
        updated,
        f'已按行替换 {path} 的第 {start_line}-{end_line} 行。',
        dry_run=dry_run,
        create_backup=create_backup,
    )


def _insert_local_file_lines(
    project_root: str,
    path: str,
    line: int,
    content: str,
    position: str = 'after',
    dry_run: bool = False,
    create_backup: bool = False,
) -> str:
    text, err = _read_text_file_for_operation(project_root, path)
    if err:
        return err
    lines = text.splitlines(keepends=True)
    line = int(line or 0)
    if line <= 0:
        return 'line 必须从 1 开始。'
    position = str(position or 'after').strip().lower()
    if position not in {'before', 'after'}:
        return 'position 只能是 before 或 after。'
    if not lines:
        insert_index = 0
    else:
        if line > len(lines):
            return f'行号超出范围：文件共 {len(lines)} 行。'
        insert_index = line - 1 if position == 'before' else line
    insert_lines = str(content or '').splitlines(keepends=True)
    if content and '\n' in content and not content.endswith('\n'):
        insert_lines[-1] = insert_lines[-1] + '\n'
    updated_lines = lines[:insert_index] + insert_lines + lines[insert_index:]
    updated = ''.join(updated_lines)
    return _finalize_file_update(
        project_root,
        path,
        text,
        updated,
        f'已在 {path} 的第 {line} 行{("前" if position == "before" else "后")}插入内容。',
        dry_run=dry_run,
        create_backup=create_backup,
    )


def _delete_local_file_lines(
    project_root: str,
    path: str,
    start_line: int,
    end_line: int,
    dry_run: bool = False,
    create_backup: bool = False,
) -> str:
    text, err = _read_text_file_for_operation(project_root, path)
    if err:
        return err
    lines = text.splitlines(keepends=True)
    start_line = int(start_line or 0)
    end_line = int(end_line or 0)
    if start_line <= 0 or end_line <= 0 or end_line < start_line:
        return '行号范围不合法，未修改。'
    if start_line > len(lines) or end_line > len(lines):
        return f'行号超出范围：文件共 {len(lines)} 行。'
    updated_lines = lines[: start_line - 1] + lines[end_line:]
    updated = ''.join(updated_lines)
    return _finalize_file_update(
        project_root,
        path,
        text,
        updated,
        f'已删除 {path} 的第 {start_line}-{end_line} 行。',
        dry_run=dry_run,
        create_backup=create_backup,
    )


def _regex_flags_from_text(flags_text: str) -> tuple[int | None, str | None]:
    flags = 0
    mapping = {'i': re.IGNORECASE, 'm': re.MULTILINE, 's': re.DOTALL}
    for ch in str(flags_text or '').strip().lower():
        if ch not in mapping:
            return None, f'不支持的正则标志: {ch}'
        flags |= mapping[ch]
    return flags, None


def _replace_local_file_regex(
    project_root: str,
    path: str,
    pattern: str,
    replacement: str,
    count: int = 1,
    expected_count: int | None = None,
    flags: str = '',
    dry_run: bool = False,
    create_backup: bool = False,
) -> str:
    text, err = _read_text_file_for_operation(project_root, path)
    if err:
        return err
    pattern = str(pattern or '')
    if not pattern:
        return 'pattern 为空，拒绝替换。'
    regex_flags, flag_err = _regex_flags_from_text(flags)
    if flag_err:
        return flag_err
    try:
        compiled = re.compile(pattern, regex_flags or 0)
    except re.error as exc:
        return f'正则表达式无效: {exc}'
    matches = list(compiled.finditer(text))
    actual_count = len(matches)
    if actual_count == 0:
        return '未找到正则匹配内容，未修改文件。'
    if expected_count is not None and actual_count != int(expected_count):
        return f'命中次数与预期不符：实际 {actual_count} 次，预期 {int(expected_count)} 次，已拒绝修改。'
    count = int(count or 0)
    replace_count = 0 if count < 0 else count
    updated, replaced_count = compiled.subn(str(replacement or ''), text, count=replace_count)
    return _finalize_file_update(
        project_root,
        path,
        text,
        updated,
        f'已按正则替换 {path}，命中 {actual_count} 次，本次计划修改 {replaced_count} 处。',
        dry_run=dry_run,
        create_backup=create_backup,
    )


def _normalize_unified_diff_path(path_text: str) -> str:
    path_text = str(path_text or '').strip()
    if path_text.startswith('a/') or path_text.startswith('b/'):
        return path_text[2:]
    return path_text


def _apply_unified_diff_to_file(
    project_root: str,
    path: str,
    diff_text: str,
    dry_run: bool = False,
    create_backup: bool = False,
) -> str:
    diff_lines = str(diff_text or '').splitlines()
    if not diff_lines:
        return 'diff 为空，未执行补丁。'
    target_path = str(path or '').strip()
    old_header = ''
    new_header = ''
    for line in diff_lines:
        if line.startswith('--- '):
            old_header = _normalize_unified_diff_path(line[4:].split('\t', 1)[0].strip())
        elif line.startswith('+++ '):
            new_header = _normalize_unified_diff_path(line[4:].split('\t', 1)[0].strip())
            break
    if not target_path:
        target_path = new_header or old_header
    target_path = _normalize_unified_diff_path(target_path)
    if not target_path:
        return '无法从 diff 中解析目标文件路径，请显式传入 path。'
    text, err = _read_text_file_for_operation(project_root, target_path)
    if err:
        return err
    source_lines = text.splitlines(keepends=True)
    result_lines: list[str] = []
    src_index = 0
    i = 0
    applied_hunks = 0
    while i < len(diff_lines):
        line = diff_lines[i]
        if not line.startswith('@@ '):
            i += 1
            continue
        matched = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
        if not matched:
            return f'无法解析 hunk 头: {line}'
        old_start = int(matched.group(1))
        old_count = int(matched.group(2) or 1)
        old_start_index = max(0, old_start - 1)
        if old_start_index < src_index:
            return f'diff hunk 顺序异常，无法应用: {line}'
        result_lines.extend(source_lines[src_index:old_start_index])
        src_index = old_start_index
        i += 1
        consumed_old = 0
        while i < len(diff_lines):
            hunk_line = diff_lines[i]
            if hunk_line.startswith('@@ ') or hunk_line.startswith('--- ') or hunk_line.startswith('+++ '):
                break
            if not hunk_line:
                prefix = ' '
                body = ''
            else:
                prefix = hunk_line[0]
                body = hunk_line[1:]
            if prefix == ' ':
                if src_index >= len(source_lines) or source_lines[src_index].rstrip('\n') != body:
                    return f'上下文不匹配，无法应用补丁: {body}'
                result_lines.append(source_lines[src_index])
                src_index += 1
                consumed_old += 1
            elif prefix == '-':
                if src_index >= len(source_lines) or source_lines[src_index].rstrip('\n') != body:
                    return f'删除行不匹配，无法应用补丁: {body}'
                src_index += 1
                consumed_old += 1
            elif prefix == '+':
                result_lines.append(body + '\n')
            elif prefix == '\\':
                pass
            else:
                return f'不支持的 diff 行: {hunk_line}'
            i += 1
        if old_count != consumed_old:
            return f'hunk 旧文件行数不匹配：预期 {old_count}，实际 {consumed_old}。'
        applied_hunks += 1
    if applied_hunks == 0:
        return '未找到任何可应用的 hunk。'
    result_lines.extend(source_lines[src_index:])
    updated = ''.join(result_lines)
    return _finalize_file_update(
        project_root,
        target_path,
        text,
        updated,
        f'已对 {target_path} 应用 unified diff，共应用 {applied_hunks} 个 hunk。',
        dry_run=dry_run,
        create_backup=create_backup,
    )


def _edit_local_file(
    project_root: str,
    path: str,
    content: str,
    dry_run: bool = False,
    create_backup: bool = False,
) -> str:
    text, read_err = _read_text_file_for_operation(project_root, path)
    if read_err and '不是一个文件，或不存在' not in read_err:
        return read_err
    original = text if text is not None else ''
    return _finalize_file_update(
        project_root,
        path,
        original,
        content,
        f'已整体写入 {path}（{len(content)} 字符）。',
        dry_run=dry_run,
        create_backup=create_backup,
    )


def _bash_exec_argv() -> list[str]:
    """返回执行本地 shell 命令的 argv 前缀。

    Windows 上 `System32/bash.exe` 是 WSL launcher shim，它经 `wsl.exe` 默认模式
    二次解释命令，会破坏命令中的引号与 `$变量`（实测 export 后立即读取为空）。
    检测到该 shim 时改用 `wsl --exec bash` 直接 exec，保证变量语义正确。
    """
    if sys.platform == 'win32':
        system_root = os.environ.get('SystemRoot') or r'C:\Windows'
        if os.path.exists(os.path.join(system_root, 'System32', 'bash.exe')):
            return ['wsl', '--exec', 'bash']
    return ['bash']


def _shell_script_path_for_exec(script_path: str) -> str:
    """把本地临时脚本路径转成当前 shell 后端能直接打开的形式。

    Windows 上 System32/bash.exe 是 WSL shim，本地 `C:\\...` 路径必须映射成
    WSL 的 `/mnt/c/...` 形式 bash 才能打开；其余场景统一转成正斜杠（兼容 msys）。
    与 `_bash_exec_argv()` 保持同一后端判断，避免一半转一半不转。
    """
    if not sys.platform.startswith('win'):
        return script_path
    norm = os.path.normpath(script_path)
    system_root = os.environ.get('SystemRoot') or r'C:\Windows'
    if os.path.exists(os.path.join(system_root, 'System32', 'bash.exe')):
        drive, rest = os.path.splitdrive(norm)
        if drive:
            return f'/mnt/{drive.rstrip(":").lower()}{rest.replace(os.sep, "/")}'
    return norm.replace(os.sep, '/')


def _resolve_shell_cwd(project_root: str, cwd: str, default_cwd: str = '/') -> tuple[str | None, str]:
    effective = str(cwd or '').strip() or str(default_cwd or '').strip() or '/'
    normalized = _normalize_agent_cwd_spec(effective)
    if normalized is None:
        return None, effective
    if normalized in {'/', '~'}:
        return project_root, normalized
    relative = normalized[2:] if normalized.startswith('~/') else normalized[1:] if normalized.startswith('/') else normalized
    resolved = _resolve_safe_path(project_root, relative)
    if resolved is None:
        return None, normalized
    if not os.path.isdir(resolved):
        return None, normalized
    return resolved, normalized


def _read_text_tail(path: str, max_chars: int = SHELL_MAX_OUTPUT_CHARS) -> str:
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, max_chars * 4)
            if read_size > 0:
                f.seek(-read_size, os.SEEK_END)
            data = f.read()
    except OSError as exc:
        return f'(读取输出失败: {exc})'
    text = _sanitize_shell_text(data.decode('utf-8', errors='replace'))
    if len(text) > max_chars:
        text = '...(输出过长，已截断前部)\n' + text[-max_chars:]
    return text


def _tail_lines(text: str, line_count: int) -> str:
    lines = str(text or '').splitlines()
    if len(lines) <= line_count:
        return '\n'.join(lines)
    return '...(仅显示最后几行)\n' + '\n'.join(lines[-line_count:])


def _decode_shell_output(data) -> str:
    """把 shell 进程的字节输出安全解码为 UTF-8 文本。

    Windows 上子进程（尤其 WSL launcher）会混入非 UTF-8 字节（例如 UTF-16LE
    编码的中文警告），text=True 按系统 locale 解码会直接抛 UnicodeDecodeError，
    导致输出被静默吞掉、成功命令误判失败。这里固定按 UTF-8 + replace 解码。
    """
    if data is None:
        return ''
    if isinstance(data, str):
        return data
    return data.decode('utf-8', errors='replace')


def _sanitize_shell_text(text: str) -> str:
    """去掉 NUL / C0 控制字符 / ANSI 转义等噪声，保留 \\n \\r \\t。

    避免 UTF-16LE 残留字节、\\x1b 颜色码等干扰模型对工具输出的判断。
    """
    return ''.join(ch for ch in text if ch in '\n\r\t' or '\x20' <= ch <= '\x7e' or ord(ch) >= 0x80)


def _format_shell_output(stdout_text: str, stderr_text: str = '') -> str:
    stdout_text = _sanitize_shell_text(str(stdout_text or '')).strip()
    stderr_text = _sanitize_shell_text(str(stderr_text or '')).strip()
    parts: list[str] = []
    if stdout_text:
        parts.append(f'[stdout]\n{stdout_text}')
    if stderr_text:
        parts.append(f'[stderr]\n{stderr_text}')
    return '\n\n'.join(parts) if parts else '(无输出)'


class DevAgentShellManager:
    def __init__(self, project_root: str):
        self.project_root = project_root
        self.runtime_dir = tempfile.mkdtemp(prefix='dev_agent_shell_')
        self.jobs: dict[str, dict] = {}
        self._next_job_id = 0
        self._lock = threading.Lock()

    def _normalize_timeout(self, timeout_seconds, background: bool) -> int:
        default_timeout = SHELL_DEFAULT_BACKGROUND_TIMEOUT_SECONDS if background else SHELL_DEFAULT_TIMEOUT_SECONDS
        try:
            value = int(timeout_seconds) if timeout_seconds is not None else default_timeout
        except (TypeError, ValueError):
            value = default_timeout
        if value <= 0:
            value = default_timeout
        return min(SHELL_MAX_TIMEOUT_SECONDS, value)

    def _next_id(self) -> str:
        with self._lock:
            self._next_job_id += 1
            return f'shell-{self._next_job_id}'

    def _job_duration(self, job: dict) -> float:
        started_at = float(job.get('started_at') or time.time())
        ended_at = float(job.get('ended_at') or time.time())
        if job.get('status') == 'running':
            ended_at = time.time()
        return max(0.0, ended_at - started_at)

    def _signal_job(self, process: subprocess.Popen, sig) -> None:
        try:
            if hasattr(os, 'killpg'):
                os.killpg(process.pid, sig)
            else:
                process.send_signal(sig)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.send_signal(sig)
            except Exception:
                return

    def _close_output_handle(self, job: dict) -> None:
        handle = job.get('output_handle')
        if handle is not None and not handle.closed:
            try:
                handle.close()
            except Exception:
                pass
        job['output_handle'] = None

    def _delete_job_script(self, job: dict) -> None:
        """删除后台任务对应的临时脚本文件（随任务结束即清理，避免残留）。"""
        script_path = job.get('script_path')
        if script_path:
            try:
                os.remove(script_path)
            except OSError:
                pass
        job['script_path'] = None

    def _write_exec_script(self, job_id: str, command: str) -> str | None:
        """把命令原样写入临时 .sh 脚本，返回脚本路径；失败返回 None。

        脚本文件方式替代 `bash -lc '<command>'` 的 argv 直传：heredoc、引号、
        `$变量`、换行等经 wsl.exe / Windows 命令行往返会失真，落到文件里
        逐字节保留，彻底规避"变量赋值/退出码/heredoc 时好时坏"的问题。
        """
        script_path = os.path.join(self.runtime_dir, f'{job_id}.sh')
        try:
            with open(script_path, 'w', encoding='utf-8', newline='\n') as f:
                # 统一换行：Windows 上若混入 CRLF，heredoc 结束符会带 \r 匹配不上
                f.write(str(command or '').replace('\r\n', '\n').replace('\r', '\n'))
        except OSError:
            return None
        return script_path

    def _refresh_job(self, job: dict) -> dict:
        process = job.get('process')
        if process is None:
            return job
        if job.get('status') == 'running':
            timeout_seconds = int(job.get('timeout_seconds') or 0)
            if timeout_seconds > 0 and (time.time() - float(job.get('started_at') or time.time())) > timeout_seconds:
                self._stop_job(job, force=True, reason='timeout')
        if job.get('status') == 'running':
            exit_code = process.poll()
            if exit_code is not None:
                job['status'] = 'done' if exit_code == 0 else 'failed'
                job['exit_code'] = exit_code
                job['ended_at'] = time.time()
                self._close_output_handle(job)
                self._delete_job_script(job)
        return job

    def _stop_job(self, job: dict, force: bool = False, wait_seconds: int = 5, reason: str = '') -> dict:
        process = job.get('process')
        if process is None:
            return job
        wait_seconds = max(1, min(30, int(wait_seconds or 5)))
        if process.poll() is None:
            if force:
                self._signal_job(process, signal.SIGKILL)
            else:
                self._signal_job(process, signal.SIGTERM)
                deadline = time.time() + wait_seconds
                while time.time() < deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.2)
                if process.poll() is None:
                    self._signal_job(process, signal.SIGKILL)
        exit_code = process.poll()
        job['exit_code'] = exit_code
        job['ended_at'] = time.time()
        if reason == 'timeout':
            job['status'] = 'timeout'
        elif job.get('status') == 'running':
            job['status'] = 'stopped' if (exit_code is None or exit_code < 0) else ('done' if exit_code == 0 else 'failed')
        self._close_output_handle(job)
        self._delete_job_script(job)
        return job

    def exec(self, command: str, cwd: str = '', timeout_seconds=None, background: bool = False, default_cwd: str = '/') -> str:
        command = str(command or '').strip()
        if not command:
            return '命令为空，未执行。'
        resolved_cwd, display_cwd = _resolve_shell_cwd(self.project_root, cwd, default_cwd=default_cwd)
        if resolved_cwd is None:
            return f'工作目录不合法或不存在: {display_cwd or "/"}'
        timeout = self._normalize_timeout(timeout_seconds, background)
        self.list_jobs(active_only=False)
        if background:
            job_id = self._next_id()
            output_path = os.path.join(self.runtime_dir, f'{job_id}.log')
            script_path = self._write_exec_script(job_id, command)
            if script_path is None:
                return 'shell 脚本写入失败，未启动后台任务。'
            # 二进制句柄 + 原始字节写入：文本句柄会按系统 locale 解码子进程输出，
            # WSL 混入的 UTF-16LE/非 UTF-8 字节会让 reader 线程解码异常、输出静默丢失
            output_handle = open(output_path, 'wb')
            process = subprocess.Popen(
                _bash_exec_argv() + ['-l', _shell_script_path_for_exec(script_path)],
                cwd=resolved_cwd,
                stdout=output_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            job = {
                'job_id': job_id,
                'command': command,
                'cwd': display_cwd or '/',
                'resolved_cwd': resolved_cwd,
                'status': 'running',
                'pid': process.pid,
                'started_at': time.time(),
                'ended_at': None,
                'exit_code': None,
                'timeout_seconds': timeout,
                'output_path': output_path,
                'output_handle': output_handle,
                'script_path': script_path,
                'process': process,
            }
            self.jobs[job_id] = job
            return (
                f'已后台启动 shell 任务。\n'
                f'job_id: {job_id}\n'
                f'pid: {process.pid}\n'
                f'cwd: {job["cwd"]}\n'
                f'timeout_seconds: {timeout}\n'
                '可稍后调用 shell_status 查看输出，或用 shell_stop 停止。'
            )

        script_path = self._write_exec_script(self._next_id(), command)
        if script_path is None:
            return 'shell 脚本写入失败，未执行命令。'
        try:
            completed = subprocess.run(
                _bash_exec_argv() + ['-l', _shell_script_path_for_exec(script_path)],
                cwd=resolved_cwd,
                capture_output=True,
                timeout=timeout,
            )
            # 固定按 UTF-8 解码（text=True 会按系统 locale 解码，中文/非 UTF-8 字节
            # 会触发 UnicodeDecodeError 导致输出丢失、成功命令被误判为失败）
            output = _format_shell_output(
                _decode_shell_output(completed.stdout),
                _decode_shell_output(completed.stderr),
            )
            return (
                f'命令执行完成。\n'
                f'exit_code: {completed.returncode}\n'
                f'cwd: {display_cwd or "/"}\n'
                f'timed_out: false\n'
                f'output:\n{output}'
            )
        except subprocess.TimeoutExpired as exc:
            output = _format_shell_output(
                _decode_shell_output(exc.stdout),
                _decode_shell_output(exc.stderr),
            )
            return (
                f'命令执行超时，已终止。\n'
                f'exit_code: timeout\n'
                f'cwd: {display_cwd or "/"}\n'
                f'timed_out: true\n'
                f'timeout_seconds: {timeout}\n'
                f'output:\n{output}'
            )
        except Exception as exc:
            return f'shell 执行失败: {exc}'
        finally:
            try:
                os.remove(script_path)
            except OSError:
                pass

    def status(self, job_id: str, tail_lines: int = SHELL_DEFAULT_TAIL_LINES) -> str:
        job = self.jobs.get(str(job_id or '').strip())
        if not job:
            return f'未找到后台 shell 任务: {job_id}'
        tail_lines = max(1, min(SHELL_MAX_TAIL_LINES, int(tail_lines or SHELL_DEFAULT_TAIL_LINES)))
        job = self._refresh_job(job)
        output = _tail_lines(_read_text_tail(job.get('output_path') or ''), tail_lines).strip() or '(暂无输出)'
        duration = self._job_duration(job)
        return (
            f'job_id: {job["job_id"]}\n'
            f'status: {job.get("status")}\n'
            f'pid: {job.get("pid")}\n'
            f'exit_code: {job.get("exit_code")}\n'
            f'cwd: {job.get("cwd")}\n'
            f'timeout_seconds: {job.get("timeout_seconds")}\n'
            f'duration_seconds: {duration:.1f}\n'
            f'command: {job.get("command")}\n'
            f'output_tail:\n{output}'
        )

    def stop(self, job_id: str, force: bool = False, wait_seconds: int = 5) -> str:
        job = self.jobs.get(str(job_id or '').strip())
        if not job:
            return f'未找到后台 shell 任务: {job_id}'
        self._refresh_job(job)
        if job.get('status') != 'running':
            return (
                f'任务已不是运行中状态，无需停止。\n'
                f'job_id: {job["job_id"]}\n'
                f'status: {job.get("status")}\n'
                f'exit_code: {job.get("exit_code")}'
            )
        job = self._stop_job(job, force=bool(force), wait_seconds=wait_seconds)
        return (
            f'已停止后台 shell 任务。\n'
            f'job_id: {job["job_id"]}\n'
            f'status: {job.get("status")}\n'
            f'exit_code: {job.get("exit_code")}'
        )

    def list_jobs(self, active_only: bool = False) -> str:
        active_only = bool(active_only)
        lines: list[str] = []
        for job_id in list(self.jobs.keys()):
            job = self._refresh_job(self.jobs[job_id])
            if active_only and job.get('status') != 'running':
                continue
            lines.append(
                f'{job["job_id"]} | {job.get("status")} | exit={job.get("exit_code")} | '
                f'cwd={job.get("cwd")} | timeout={job.get("timeout_seconds")} | cmd={job.get("command")}'
            )
        if not lines:
            return '没有后台 shell 任务。'
        return '\n'.join(lines)

    def shutdown(self) -> list[str]:
        stopped: list[str] = []
        for job in list(self.jobs.values()):
            self._refresh_job(job)
            if job.get('status') == 'running':
                self._stop_job(job, force=True, reason='shutdown')
                stopped.append(job['job_id'])
        return stopped


def _github_search_code(token: str, query: str, repo: str) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    query = (query or '').strip()
    if not query:
        return '搜索关键词为空，未执行搜索。'
    service = GitHubService(token=token)
    try:
        data = _call_with_retry(
            'GitHub 代码搜索',
            lambda: service.search_code(query, repo),
        )
    except Exception as exc:
        return f'GitHub 代码搜索失败: {exc}'
    items = data.get('items') or []
    if not items:
        return '未搜索到相关代码。'
    lines = []
    for item in items[:10]:
        repo_name = (item.get('repository') or {}).get('full_name', '')
        lines.append(f"{repo_name}: {item.get('path')} ({item.get('html_url')})")
    return '\n'.join(lines)


def _github_read_file(token: str, owner: str, repo: str, path: str, ref: str) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    if not owner or not repo or not path:
        return 'owner/repo/path 不能为空。'
    service = GitHubService(token=token)
    try:
        data = _call_with_retry(
            'GitHub 文件读取',
            lambda: service.get_file_contents(owner, repo, path, ref),
        )
    except Exception as exc:
        return f'GitHub 文件读取失败: {exc}'
    text = data.get('decoded_text')
    if text is None:
        return f"读取到的内容不是文本文件或格式无法解析: {data.get('type')}"
    if len(text) > MAX_FILE_BYTES:
        text = text[:MAX_FILE_BYTES] + '\n...(内容过长，已截断)'
    return text


def _github_call(token: str, fn_name: str, *args) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    service = GitHubService(token=token)
    try:
        result = _call_with_retry(
            f'GitHub 操作 {fn_name}',
            lambda: getattr(service, fn_name)(*args),
        )
    except Exception as exc:
        return f'GitHub 操作失败: {exc}'
    return str(result)


def _github_list_repos(token: str, per_page: int) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    service = GitHubService(token=token)
    try:
        repos = _call_with_retry(
            'GitHub 仓库列表获取',
            lambda: service.list_repos(per_page or 30),
        )
    except Exception as exc:
        return f'GitHub 仓库列表获取失败: {exc}'
    if not repos:
        return '没有可访问的仓库。'
    return '\n'.join(f"{r.get('full_name')} ({'private' if r.get('private') else 'public'}) - {r.get('html_url')}" for r in repos)


def _github_search_repos(token: str, query: str, per_page: int) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    query = (query or '').strip()
    if not query:
        return '搜索关键词为空，未执行搜索。'
    service = GitHubService(token=token)
    try:
        data = _call_with_retry(
            'GitHub 仓库搜索',
            lambda: service.search_repos(query, per_page or 10),
        )
    except Exception as exc:
        return f'GitHub 仓库搜索失败: {exc}'
    items = data.get('items') or []
    if not items:
        return '未搜索到相关仓库。'
    return '\n'.join(f"{r.get('full_name')} ⭐{r.get('stargazers_count')} - {r.get('html_url')}" for r in items)


def _github_list_branches(token: str, owner: str, repo: str) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    service = GitHubService(token=token)
    try:
        branches = _call_with_retry(
            'GitHub 分支列表获取',
            lambda: service.list_branches(owner, repo),
        )
    except Exception as exc:
        return f'GitHub 分支列表获取失败: {exc}'
    if not branches:
        return '没有分支。'
    return '\n'.join(b.get('name', '') for b in branches)


def _github_list_pull_requests(token: str, owner: str, repo: str, state: str) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    service = GitHubService(token=token)
    try:
        prs = _call_with_retry(
            'GitHub PR 列表获取',
            lambda: service.list_pull_requests(owner, repo, state or 'open'),
        )
    except Exception as exc:
        return f'GitHub PR 列表获取失败: {exc}'
    if not prs:
        return '没有符合条件的 PR。'
    return '\n'.join(f"#{p.get('number')} {p.get('title')} ({p.get('head', {}).get('ref')} -> {p.get('base', {}).get('ref')}) {p.get('html_url')}" for p in prs)


def _github_list_issues(token: str, owner: str, repo: str, state: str) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    service = GitHubService(token=token)
    try:
        issues = _call_with_retry(
            'GitHub Issue 列表获取',
            lambda: service.list_issues(owner, repo, state or 'open'),
        )
    except Exception as exc:
        return f'GitHub Issue 列表获取失败: {exc}'
    issues = [i for i in issues if 'pull_request' not in i]
    if not issues:
        return '没有符合条件的 Issue。'
    return '\n'.join(f"#{i.get('number')} {i.get('title')} {i.get('html_url')}" for i in issues)


def _github_list_commits(token: str, owner: str, repo: str, sha: str, path: str) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    service = GitHubService(token=token)
    try:
        commits = _call_with_retry(
            'GitHub 提交历史获取',
            lambda: service.list_commits(owner, repo, sha, path),
        )
    except Exception as exc:
        return f'GitHub 提交历史获取失败: {exc}'
    if not commits:
        return '没有提交记录。'
    lines = []
    for c in commits[:20]:
        commit_info = c.get('commit', {})
        message = (commit_info.get('message') or '').splitlines()[0]
        lines.append(f"{c.get('sha', '')[:7]} {message} ({commit_info.get('author', {}).get('date', '')})")
    return '\n'.join(lines)


def _github_get_commit(token: str, owner: str, repo: str, sha: str) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    service = GitHubService(token=token)
    try:
        data = _call_with_retry(
            'GitHub commit 详情获取',
            lambda: service.get_commit(owner, repo, sha),
        )
    except Exception as exc:
        return f'GitHub commit 详情获取失败: {exc}'
    commit_info = data.get('commit', {})
    files = data.get('files') or []
    lines = [f"{data.get('sha', '')}: {commit_info.get('message', '')}", f"作者: {commit_info.get('author', {}).get('name', '')}"]
    for f in files[:30]:
        lines.append(f"  {f.get('status')} {f.get('filename')} (+{f.get('additions')}/-{f.get('deletions')})")
    return '\n'.join(lines)


def _execute_tool_call(
    name: str,
    tool_input: dict,
    project_root: str,
    github_token: str,
    shell_manager: DevAgentShellManager | SSHAgentShellManager | None = None,
    default_cwd: str = '/',
    read_only: bool = False,
    ssh_profile: SSHProfileConfig | None = None,
) -> str:
    tool_input = tool_input or {}
    try:
        if read_only:
            if name == 'shell_exec':
                command = str(tool_input.get('command') or '')
                allowed, reason = _is_read_only_shell_command(command)
                if not allowed:
                    return (
                        '当前 agent 处于只读模式，shell_exec 仅允许白名单内的只读命令'
                        '（ls/cat/grep/head/tail/stat/du/wc/find/diff/sort/uniq 等），'
                        '可用 ; / && / || / | 组合，禁止后台任务、写入重定向与 heredoc。'
                        f'该命令已被拒绝：{reason}'
                    )
                if bool(tool_input.get('background')):
                    return '当前 agent 处于只读模式，禁止后台 shell 任务。'
                # 白名单内只读命令放行，由下方 shell_manager.exec 执行
            if name in LOCAL_WRITE_TOOLS:
                return f'当前 agent 处于只读模式，禁止修改本地文件: {name}'
            if name in GITHUB_WRITE_TOOLS:
                return f'当前 agent 处于只读模式，禁止写 GitHub: {name}'
            if name in SSH_TRANSFER_START_TOOLS:
                return f'当前 agent 处于只读模式，禁止启动 SSH 文件传输: {name}'
        if shell_manager is not None:
            if name == 'shell_exec':
                return shell_manager.exec(
                    str(tool_input.get('command') or ''),
                    cwd=str(tool_input.get('cwd') or ''),
                    timeout_seconds=tool_input.get('timeout_seconds'),
                    background=bool(tool_input.get('background')),
                    default_cwd=default_cwd,
                )
            if name == 'shell_status':
                return shell_manager.status(
                    str(tool_input.get('job_id') or ''),
                    tail_lines=int(tool_input.get('tail_lines') or SHELL_DEFAULT_TAIL_LINES),
                )
            if name == 'shell_stop':
                return shell_manager.stop(
                    str(tool_input.get('job_id') or ''),
                    force=bool(tool_input.get('force')),
                    wait_seconds=int(tool_input.get('wait_seconds') or 5),
                )
            if name == 'shell_list':
                return shell_manager.list_jobs(active_only=bool(tool_input.get('active_only')))
            if isinstance(shell_manager, SSHAgentShellManager):
                if name == 'ssh_download_file':
                    return shell_manager.start_download(
                        str(tool_input.get('remote_path') or ''),
                        str(tool_input.get('local_path') or ''),
                        overwrite=bool(tool_input.get('overwrite')),
                        chunk_bytes=tool_input.get('chunk_bytes'),
                    )
                if name == 'ssh_upload_file':
                    return shell_manager.start_upload(
                        str(tool_input.get('local_path') or ''),
                        str(tool_input.get('remote_path') or ''),
                        overwrite=bool(tool_input.get('overwrite')),
                        chunk_bytes=tool_input.get('chunk_bytes'),
                    )
                if name == 'ssh_transfer_status':
                    return shell_manager.transfer_status(str(tool_input.get('transfer_id') or ''))
                if name == 'ssh_transfer_cancel':
                    return shell_manager.transfer_cancel(str(tool_input.get('transfer_id') or ''))
                if name == 'ssh_transfer_list':
                    return shell_manager.transfer_list(active_only=bool(tool_input.get('active_only')))
        if ssh_profile is not None:
            if name == 'find_in_project':
                return _find_in_remote_project(
                    ssh_profile,
                    name_pattern=str(tool_input.get('name_pattern') or ''),
                    content_query=str(tool_input.get('content_query') or ''),
                    is_regex=bool(tool_input.get('is_regex')),
                    subpath=str(tool_input.get('subpath') or ''),
                    max_results=int(tool_input.get('max_results') or FIND_DEFAULT_MAX_RESULTS),
                )
            if name == 'list_local_files':
                return _list_remote_files(ssh_profile, str(tool_input.get('subpath') or ''))
            if name == 'read_local_file':
                return _read_remote_file(ssh_profile, str(tool_input.get('path') or ''))
            if name == 'read_local_file_chunk':
                return _read_remote_file_chunk(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    offset_bytes=tool_input.get('offset_bytes'),
                    max_bytes=tool_input.get('max_bytes'),
                    start_line=tool_input.get('start_line'),
                    line_count=tool_input.get('line_count'),
                )
            if name == 'search_local_file':
                return _search_remote_file(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    str(tool_input.get('query') or ''),
                    is_regex=bool(tool_input.get('is_regex')),
                    max_matches=int(tool_input.get('max_matches') or 20),
                    context_lines=int(tool_input.get('context_lines') or 1),
                )
            if name == 'query_local_file_json':
                return _query_remote_file_json(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    query=str(tool_input.get('query') or '$'),
                    max_items=int(tool_input.get('max_items') or 50),
                )
            if name == 'replace_local_file_text':
                return _run_remote_write_tool(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    lambda tmp_root, relative_path: _replace_local_file_text(
                        tmp_root,
                        relative_path,
                        str(tool_input.get('old_text') or ''),
                        str(tool_input.get('new_text') or ''),
                        replace_all=bool(tool_input.get('replace_all')),
                        occurrence=int(tool_input.get('occurrence') or 1),
                        expected_count=tool_input.get('expected_count'),
                        dry_run=bool(tool_input.get('dry_run')),
                        create_backup=False,
                    ),
                    dry_run=bool(tool_input.get('dry_run')),
                    create_backup=bool(tool_input.get('create_backup')),
                )
            if name == 'replace_local_file_lines':
                return _run_remote_write_tool(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    lambda tmp_root, relative_path: _replace_local_file_lines(
                        tmp_root,
                        relative_path,
                        int(tool_input.get('start_line') or 0),
                        int(tool_input.get('end_line') or 0),
                        str(tool_input.get('content') or ''),
                        dry_run=bool(tool_input.get('dry_run')),
                        create_backup=False,
                    ),
                    dry_run=bool(tool_input.get('dry_run')),
                    create_backup=bool(tool_input.get('create_backup')),
                )
            if name == 'insert_local_file_lines':
                return _run_remote_write_tool(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    lambda tmp_root, relative_path: _insert_local_file_lines(
                        tmp_root,
                        relative_path,
                        int(tool_input.get('line') or 0),
                        str(tool_input.get('content') or ''),
                        position=str(tool_input.get('position') or 'after'),
                        dry_run=bool(tool_input.get('dry_run')),
                        create_backup=False,
                    ),
                    dry_run=bool(tool_input.get('dry_run')),
                    create_backup=bool(tool_input.get('create_backup')),
                )
            if name == 'delete_local_file_lines':
                return _run_remote_write_tool(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    lambda tmp_root, relative_path: _delete_local_file_lines(
                        tmp_root,
                        relative_path,
                        int(tool_input.get('start_line') or 0),
                        int(tool_input.get('end_line') or 0),
                        dry_run=bool(tool_input.get('dry_run')),
                        create_backup=False,
                    ),
                    dry_run=bool(tool_input.get('dry_run')),
                    create_backup=bool(tool_input.get('create_backup')),
                )
            if name == 'replace_local_file_regex':
                return _run_remote_write_tool(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    lambda tmp_root, relative_path: _replace_local_file_regex(
                        tmp_root,
                        relative_path,
                        str(tool_input.get('pattern') or ''),
                        str(tool_input.get('replacement') or ''),
                        count=int(tool_input.get('count') or 1),
                        expected_count=tool_input.get('expected_count'),
                        flags=str(tool_input.get('flags') or ''),
                        dry_run=bool(tool_input.get('dry_run')),
                        create_backup=False,
                    ),
                    dry_run=bool(tool_input.get('dry_run')),
                    create_backup=bool(tool_input.get('create_backup')),
                )
            if name == 'apply_unified_diff_to_file':
                return _run_remote_write_tool(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    lambda tmp_root, relative_path: _apply_unified_diff_to_file(
                        tmp_root,
                        relative_path,
                        str(tool_input.get('diff_text') or ''),
                        dry_run=bool(tool_input.get('dry_run')),
                        create_backup=False,
                    ),
                    dry_run=bool(tool_input.get('dry_run')),
                    create_backup=bool(tool_input.get('create_backup')),
                )
            if name == 'edit_local_file':
                return _run_remote_write_tool(
                    ssh_profile,
                    str(tool_input.get('path') or ''),
                    lambda tmp_root, relative_path: _edit_local_file(
                        tmp_root,
                        relative_path,
                        str(tool_input.get('content') or ''),
                        dry_run=bool(tool_input.get('dry_run')),
                        create_backup=False,
                    ),
                    allow_missing=True,
                    dry_run=bool(tool_input.get('dry_run')),
                    create_backup=bool(tool_input.get('create_backup')),
                )
        if name == 'find_in_project':
            return _find_in_project(
                project_root,
                name_pattern=str(tool_input.get('name_pattern') or ''),
                content_query=str(tool_input.get('content_query') or ''),
                is_regex=bool(tool_input.get('is_regex')),
                subpath=str(tool_input.get('subpath') or ''),
                max_results=int(tool_input.get('max_results') or FIND_DEFAULT_MAX_RESULTS),
            )
        if name == 'list_local_files':
            return _list_local_files(project_root, str(tool_input.get('subpath') or ''))
        if name == 'read_local_file':
            return _read_local_file(project_root, str(tool_input.get('path') or ''))
        if name == 'read_local_file_chunk':
            return _read_local_file_chunk(
                project_root,
                str(tool_input.get('path') or ''),
                offset_bytes=tool_input.get('offset_bytes'),
                max_bytes=tool_input.get('max_bytes'),
                start_line=tool_input.get('start_line'),
                line_count=tool_input.get('line_count'),
            )
        if name == 'search_local_file':
            return _search_local_file(
                project_root,
                str(tool_input.get('path') or ''),
                str(tool_input.get('query') or ''),
                is_regex=bool(tool_input.get('is_regex')),
                max_matches=int(tool_input.get('max_matches') or 20),
                context_lines=int(tool_input.get('context_lines') or 1),
            )
        if name == 'query_local_file_json':
            return _query_local_file_json(
                project_root,
                str(tool_input.get('path') or ''),
                query=str(tool_input.get('query') or '$'),
                max_items=int(tool_input.get('max_items') or 50),
            )
        if name == 'replace_local_file_text':
            return _replace_local_file_text(
                project_root,
                str(tool_input.get('path') or ''),
                str(tool_input.get('old_text') or ''),
                str(tool_input.get('new_text') or ''),
                replace_all=bool(tool_input.get('replace_all')),
                occurrence=int(tool_input.get('occurrence') or 1),
                expected_count=tool_input.get('expected_count'),
                dry_run=bool(tool_input.get('dry_run')),
                create_backup=bool(tool_input.get('create_backup')),
            )
        if name == 'replace_local_file_lines':
            return _replace_local_file_lines(
                project_root,
                str(tool_input.get('path') or ''),
                int(tool_input.get('start_line') or 0),
                int(tool_input.get('end_line') or 0),
                str(tool_input.get('content') or ''),
                dry_run=bool(tool_input.get('dry_run')),
                create_backup=bool(tool_input.get('create_backup')),
            )
        if name == 'insert_local_file_lines':
            return _insert_local_file_lines(
                project_root,
                str(tool_input.get('path') or ''),
                int(tool_input.get('line') or 0),
                str(tool_input.get('content') or ''),
                str(tool_input.get('position') or 'after'),
                dry_run=bool(tool_input.get('dry_run')),
                create_backup=bool(tool_input.get('create_backup')),
            )
        if name == 'delete_local_file_lines':
            return _delete_local_file_lines(
                project_root,
                str(tool_input.get('path') or ''),
                int(tool_input.get('start_line') or 0),
                int(tool_input.get('end_line') or 0),
                dry_run=bool(tool_input.get('dry_run')),
                create_backup=bool(tool_input.get('create_backup')),
            )
        if name == 'replace_local_file_regex':
            return _replace_local_file_regex(
                project_root,
                str(tool_input.get('path') or ''),
                str(tool_input.get('pattern') or ''),
                str(tool_input.get('replacement') or ''),
                count=int(tool_input.get('count') or 1),
                expected_count=tool_input.get('expected_count'),
                flags=str(tool_input.get('flags') or ''),
                dry_run=bool(tool_input.get('dry_run')),
                create_backup=bool(tool_input.get('create_backup')),
            )
        if name == 'apply_unified_diff_to_file':
            return _apply_unified_diff_to_file(
                project_root,
                str(tool_input.get('path') or ''),
                str(tool_input.get('diff') or ''),
                dry_run=bool(tool_input.get('dry_run')),
                create_backup=bool(tool_input.get('create_backup')),
            )
        if name == 'edit_local_file':
            return _edit_local_file(
                project_root,
                str(tool_input.get('path') or ''),
                str(tool_input.get('content') or ''),
                dry_run=bool(tool_input.get('dry_run')),
                create_backup=bool(tool_input.get('create_backup')),
            )
        if name == 'github_search_code':
            return _github_search_code(github_token, str(tool_input.get('query') or ''), str(tool_input.get('repo') or ''))
        if name == 'github_read_file':
            return _github_read_file(
                github_token,
                str(tool_input.get('owner') or ''),
                str(tool_input.get('repo') or ''),
                str(tool_input.get('path') or ''),
                str(tool_input.get('ref') or ''),
            )
        if name == 'github_list_repos':
            return _github_list_repos(github_token, int(tool_input.get('per_page') or 30))
        if name == 'github_search_repos':
            return _github_search_repos(github_token, str(tool_input.get('query') or ''), int(tool_input.get('per_page') or 10))
        if name == 'github_create_or_update_file':
            return _github_call(
                github_token, 'create_or_update_file',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('path') or ''), str(tool_input.get('content') or ''),
                str(tool_input.get('message') or ''), str(tool_input.get('branch') or ''),
            )
        if name == 'github_delete_file':
            return _github_call(
                github_token, 'delete_file',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('path') or ''), str(tool_input.get('message') or ''),
                str(tool_input.get('branch') or ''),
            )
        if name == 'github_list_branches':
            return _github_list_branches(github_token, str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''))
        if name == 'github_create_branch':
            return _github_call(
                github_token, 'create_branch',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('new_branch') or ''), str(tool_input.get('from_branch') or ''),
            )
        if name == 'github_create_tag':
            return _github_call(
                github_token, 'create_tag',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('tag_name') or ''), str(tool_input.get('ref') or ''),
            )
        if name == 'github_list_pull_requests':
            return _github_list_pull_requests(
                github_token, str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('state') or 'open'),
            )
        if name == 'github_create_pull_request':
            return _github_call(
                github_token, 'create_pull_request',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('title') or ''), str(tool_input.get('head') or ''),
                str(tool_input.get('base') or ''), str(tool_input.get('body') or ''),
            )
        if name == 'github_merge_pull_request':
            return _github_call(
                github_token, 'merge_pull_request',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                int(tool_input.get('number') or 0), str(tool_input.get('commit_message') or ''),
            )
        if name == 'github_close_pull_request':
            return _github_call(
                github_token, 'close_pull_request',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                int(tool_input.get('number') or 0),
            )
        if name == 'github_list_issues':
            return _github_list_issues(
                github_token, str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('state') or 'open'),
            )
        if name == 'github_create_issue':
            return _github_call(
                github_token, 'create_issue',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('title') or ''), str(tool_input.get('body') or ''),
            )
        if name == 'github_add_issue_comment':
            return _github_call(
                github_token, 'add_issue_comment',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                int(tool_input.get('number') or 0), str(tool_input.get('body') or ''),
            )
        if name == 'github_close_issue':
            return _github_call(
                github_token, 'close_issue',
                str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                int(tool_input.get('number') or 0),
            )
        if name == 'github_list_commits':
            return _github_list_commits(
                github_token, str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('sha') or ''), str(tool_input.get('path') or ''),
            )
        if name == 'github_get_commit':
            return _github_get_commit(
                github_token, str(tool_input.get('owner') or ''), str(tool_input.get('repo') or ''),
                str(tool_input.get('sha') or ''),
            )
        return f'未知工具: {name}'
    except Exception as exc:
        return f'工具执行出错: {exc}'


def _tool_result_limit(name: str) -> int:
    if name in TOOL_RESULT_LIMITS:
        return TOOL_RESULT_LIMITS[name]
    if name in PARALLEL_READ_TOOLS:
        return DEFAULT_READ_RESULT_LIMIT
    return DEFAULT_SERIAL_RESULT_LIMIT


def _tool_result_followup_hint(name: str, tool_input: dict) -> str:
    tool_input = tool_input or {}
    if name == 'read_local_file':
        path = str(tool_input.get('path') or '')
        return (
            f'结果较长已截断。请改用 read_local_file_chunk(path={path!r}, start_line=1, line_count=200) 按行读取，'
            '输出含总行数与行号范围，可据此精确定位续读。'
        )
    if name == 'read_local_file_chunk':
        path = str(tool_input.get('path') or '')
        if tool_input.get('start_line') is not None:
            start = max(1, int(tool_input.get('start_line') or 1))
            count = max(1, int(tool_input.get('line_count') or 120))
            return f'可继续调用 read_local_file_chunk(path={path!r}, start_line={start + count}, line_count={count})。'
        # bytes 模式：以上一次结果里返回的 offset_bytes/read_bytes 为基准（已做 UTF-8 对齐）
        return (
            f'bytes 模式结果已截断。请以上次结果返回的 offset_bytes + read_bytes 作为下一次 offset_bytes 续读'
            f'（path={path!r}）；或改用 start_line 模式（输出含总行数）按行精确定位。'
        )
    if name == 'find_in_project':
        return '可用 subpath 限定目录、收窄 name_pattern/content_query，或降低 max_results 后重试。'
    if name == 'search_local_file':
        return '可缩小 query、减少 context_lines，或根据已返回行号调用 read_local_file_chunk 追读。'
    if name == 'query_local_file_json':
        return '可收窄查询路径（例如 $.a[0].b）、提高 max_items，或改用 read_local_file_chunk 查看原文。'
    if name == 'github_read_file':
        return 'GitHub 文件结果已保留头尾；如需完整内容，请改为读取更具体的文件或在本地取得文件后按范围查看。'
    if name.startswith('github_'):
        return '可缩小仓库、状态、路径或查询条件后继续读取。'
    if name.startswith('shell_'):
        return 'shell 诊断结果已保留头尾；可用更精确命令或 shell_status 获取目标日志片段。'
    if name in {
        'replace_local_file_text', 'replace_local_file_lines', 'insert_local_file_lines',
        'delete_local_file_lines', 'replace_local_file_regex', 'apply_unified_diff_to_file',
        'edit_local_file',
    }:
        return '编辑结果已保留 diff 头尾；可读取目标文件相关行或运行 git diff -- <path> 继续核验。'
    return '可使用更精确的参数重新查询剩余信息。'

def _truncate_tool_result(name: str, tool_input: dict, content: str, limit: int) -> str:
    text = str(content or '')
    limit = int(limit or 0)
    if limit <= 0:
        return ''
    if len(text) <= limit:
        return text
    hint = _tool_result_followup_hint(name, tool_input)
    marker = (
        f'\n\n……【结果过长已截断：原始 {len(text)} 字符，本轮最多保留 {limit} 字符。'
        f'{hint}】……\n\n'
    )
    # 极小预算下先压缩说明本身，保证任何情况下都不突破调用方给定上限。
    if len(marker) >= limit:
        compact = f'【已截断，原始{len(text)}字符；请缩小范围追读】'
        return compact[:limit]
    available = limit - len(marker)
    # 所有类型均保留头尾；对 diff、shell 和诊断信息尤其重要，错误结论通常位于末尾。
    head_size = max(1, available * 2 // 3)
    tail_size = max(0, available - head_size)
    tail = text[-tail_size:] if tail_size else ''
    return text[:head_size] + marker + tail


def _budget_tool_results(calls, results: list[str], total_limit: int = MAX_TOOL_BATCH_RESULT_CHARS) -> list[str]:
    """按工具类型和整批硬预算分配结果；小结果优先完整保留，不改变数量和顺序。"""
    calls = list(calls or [])
    raw_results = [str(result or '') for result in results]
    if not calls:
        return []
    if len(raw_results) != len(calls):
        raise ValueError('tool result count does not match tool call count')

    total_limit = max(1, int(total_limit or 0))
    per_tool_caps = [min(len(result), _tool_result_limit(call.name)) for call, result in zip(calls, raw_results)]
    if sum(per_tool_caps) <= total_limit:
        return [
            _truncate_tool_result(call.name, call.input, result, cap)
            for call, result, cap in zip(calls, raw_results, per_tool_caps)
        ]

    # 水位分配：短结果完整保留，只压缩真正占用预算的大结果。
    # 当调用数异常多时允许每项额度低于常规最小值，以保证整批硬上限始终成立。
    allocations = [0] * len(calls)
    remaining_indices = set(range(len(calls)))
    remaining_budget = total_limit
    while remaining_indices:
        fair_share = max(0, remaining_budget // len(remaining_indices))
        completed = [i for i in remaining_indices if per_tool_caps[i] <= fair_share]
        if not completed:
            ordered_indices = sorted(remaining_indices)
            for offset, i in enumerate(ordered_indices):
                allocations[i] = fair_share + (1 if offset < remaining_budget % len(ordered_indices) else 0)
            break
        for i in completed:
            allocations[i] = per_tool_caps[i]
            remaining_budget -= allocations[i]
            remaining_indices.remove(i)

    return [
        _truncate_tool_result(call.name, call.input, result, allocations[index])
        for index, (call, result) in enumerate(zip(calls, raw_results))
    ]


async def _execute_tool_calls_ordered(
    calls,
    project_root: str,
    github_token: str,
    shell_manager: DevAgentShellManager | SSHAgentShellManager | None,
    default_cwd: str = '/',
    read_only: bool = False,
    ssh_profile: SSHProfileConfig | None = None,
    execute_fn=None,
    local_concurrency: int = LOCAL_READ_CONCURRENCY,
    github_concurrency: int = GITHUB_READ_CONCURRENCY,
    max_parallel_sub_batch: int = MAX_PARALLEL_READ_SUB_BATCH,
    total_result_limit: int = MAX_TOOL_BATCH_RESULT_CHARS,
) -> list[str]:
    """受控执行一轮工具调用。

    连续的安全只读工具可并发；任意 shell、文件写、GitHub 写或未知工具都是严格串行屏障。
    返回顺序始终与 calls 顺序一致，单调用异常会转成对应错误结果，不取消同批其它调用。
    """
    calls = list(calls or [])
    if not calls:
        return []
    execute_fn = execute_fn or _execute_tool_call
    ordered: list[str | None] = [None] * len(calls)
    local_sem = asyncio.Semaphore(max(1, int(local_concurrency or 1)))
    github_sem = asyncio.Semaphore(max(1, int(github_concurrency or 1)))
    sub_batch_limit = max(1, int(max_parallel_sub_batch or 1))

    async def run_one(index: int, call, parallel: bool) -> tuple[int, str]:
        async def invoke() -> str:
            try:
                result = await _run_blocking(
                    execute_fn,
                    call.name, call.input, project_root, github_token, shell_manager, default_cwd, read_only, ssh_profile,
                )
                return str(result or '')
            except Exception as exc:
                return f'工具执行出错: {type(exc).__name__}: {exc}'

        if parallel:
            semaphore = github_sem if call.name in PARALLEL_GITHUB_READ_TOOLS else local_sem
            async with semaphore:
                return index, await invoke()
        return index, await invoke()

    pending: list[tuple[int, object]] = []

    async def flush_pending() -> None:
        nonlocal pending
        if not pending:
            return
        for start in range(0, len(pending), sub_batch_limit):
            chunk = pending[start:start + sub_batch_limit]
            completed = await asyncio.gather(*(run_one(index, call, True) for index, call in chunk))
            for index, result in completed:
                ordered[index] = result
        pending = []

    for index, call in enumerate(calls):
        if call.name in PARALLEL_READ_TOOLS:
            pending.append((index, call))
            continue
        # 串行工具是屏障：先完成前面的只读批次，再执行它，之后的读取才可开始。
        await flush_pending()
        result_index, result = await run_one(index, call, False)
        ordered[result_index] = result
    await flush_pending()

    raw_results = [str(result or '') for result in ordered]
    return _budget_tool_results(calls, raw_results, total_limit=total_result_limit)


def _trim_old_tool_results(messages: list[dict], keep_recent_rounds: int = TOOL_RESULT_TRIM_KEEP_RECENT_ROUNDS) -> None:
    tool_result_msg_indices = [
        i for i, m in enumerate(messages)
        if m.get('role') == 'user' and isinstance(m.get('content'), list)
        and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in m['content'])
    ]
    trimmable = tool_result_msg_indices[:-keep_recent_rounds] if keep_recent_rounds else tool_result_msg_indices
    for i in trimmable:
        for block in messages[i]['content']:
            if isinstance(block, dict) and block.get('type') == 'tool_result':
                content = block.get('content')
                if isinstance(content, str) and len(content) > 200:
                    block['content'] = content[:200] + '\n……【历史工具结果已截断，为节省上下文仅保留前200字符】'


async def _maybe_compact_agent_messages(
    model,
    messages: list[dict],
    history_summaries: list[dict],
) -> tuple[list[dict], list[dict], bool]:
    removed_messages, kept_messages = _plan_history_compaction(messages)
    if not removed_messages:
        return messages, history_summaries, False
    summary_text = await _summarize_history_chunk(model, removed_messages)
    updated_summaries = _append_history_summary(history_summaries, summary_text)
    return kept_messages, updated_summaries, True


async def run_dev_agent(
    model: AnthropicChatModel,
    github_token: str,
    task_desc: str,
    github_repo: str = '',
    prompt_path: str = 'data/prompt/dev_agent.txt',
    project_root: str | None = None,
    on_finished: Callable[[dict], Awaitable[None] | None] | None = None,
    token_usage_store=None,
) -> str:
    project_root = project_root or _project_root()
    shell_manager = DevAgentShellManager(project_root)
    try:
        with open(prompt_path, 'r', encoding='utf-8') as f:
            system_prompt = f.read()
    except OSError:
        system_prompt = '你是这个项目专属的后台代码/资料助手，操作范围限定在本地仓库目录内，可以只读查阅GitHub任意仓库做参考。'

    # 将原始任务描述固定注入 system_prompt，防止长轮次下被稀释
    system_prompt += f'\n\n本次任务原始描述：\n{task_desc}'

    tools = _build_tools_schema()
    task_text = task_desc
    if github_repo:
        task_text += f'\n\n（可优先参考 GitHub 仓库: {github_repo}）'
    messages: list[dict] = [{'role': 'user', 'content': task_text}]
    runtime_state = {
        'history_summaries': [],
        'todo_items': [],
        'notes': [],
    }
    final_result = ''
    final_status = 'failed'

    try:
        def _execute_runtime_tool(
            name: str,
            tool_input: dict,
            project_root_arg: str,
            github_token_arg: str,
            shell_manager_arg,
            default_cwd: str = '/',
            read_only: bool = False,
            ssh_profile=None,
        ) -> str:
            if name == 'todo_write':
                return _apply_todo_write(runtime_state['todo_items'], tool_input)
            if name == 'note_write':
                return _apply_note_write(runtime_state['notes'], tool_input)
            return _execute_tool_call(
                name,
                tool_input,
                project_root_arg,
                github_token_arg,
                shell_manager_arg,
                default_cwd,
                read_only,
                ssh_profile,
            )

        for _ in range(MAX_ITERATIONS):
            _trim_old_tool_results(messages, keep_recent_rounds=TOOL_RESULT_TRIM_KEEP_RECENT_ROUNDS)
            messages, runtime_state['history_summaries'], _ = await _maybe_compact_agent_messages(
                model,
                messages,
                runtime_state['history_summaries'],
            )
            total_chars = sum(len(json.dumps(m.get('content'), ensure_ascii=False)) for m in messages)
            if total_chars > MAX_CONTEXT_CHARS:
                _trim_old_tool_results(messages, keep_recent_rounds=TOOL_RESULT_TRIM_KEEP_RECENT_ROUNDS)
            effective_system_prompt = system_prompt + _render_agent_state_prompt(
                runtime_state['history_summaries'],
                runtime_state['todo_items'],
                runtime_state['notes'],
            )
            reply = await _run_blocking(
                _call_with_retry,
                'DevAgent 模型调用',
                lambda: _complete_with_valid_response(
                    model,
                    effective_system_prompt,
                    messages,
                    tools,
                    0.4,
                    4096,
                ),
            )
            if token_usage_store is not None:
                token_usage_store.record(
                    reply.input_tokens,
                    reply.output_tokens,
                    estimated=bool(reply.usage_estimated),
                    model=getattr(model, 'model_name', ''),
                    scope_key=None,
                )
            if not reply.tool_calls:
                final_status = 'done'
                final_result = reply.text or '(任务结束，模型没有给出文字汇报)'
                return final_result

            messages.append({'role': 'assistant', 'content': reply.raw_content})
            ordered_results = await _execute_tool_calls_ordered(
                reply.tool_calls,
                project_root,
                github_token,
                shell_manager,
                execute_fn=_execute_runtime_tool,
            )
            result_blocks = [
                {
                    'type': 'tool_result',
                    'tool_use_id': call.call_id,
                    'content': result_text,
                }
                for call, result_text in zip(reply.tool_calls, ordered_results)
            ]
            messages.append({'role': 'user', 'content': result_blocks})
        final_result = '已达到最大工具调用轮数上限，任务可能未完全完成，建议拆分成更小的任务重新委托。'
        return final_result
    except Exception as exc:
        error(f'[DevAgent] 执行异常 iter消息数={len(messages)} 错误={exc}')
        final_result = f'Tasker 执行异常: {exc}'
        return final_result
    finally:
        stopped_jobs = shell_manager.shutdown()
        if stopped_jobs:
            stopped_text = f'后台 shell 任务已在 tasker 结束时自动停止: {", ".join(stopped_jobs)}'
            final_result = f'{final_result}\n\n{stopped_text}'.strip() if final_result else stopped_text
        if not final_result:
            final_result = 'Tasker 已结束，但没有返回可用结果。'
        await _notify_run_finished(
            on_finished,
            {
                'status': final_status,
                'result': final_result,
                'task_desc': task_desc,
                'github_repo': github_repo,
                'message_count': len(messages),
            },
        )
