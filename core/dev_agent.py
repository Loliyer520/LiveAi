import asyncio
import difflib
import inspect
import json
import os
import random
import re
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from typing import Awaitable, Callable

from pack.anthropic_chat_model import AnthropicChatModel
from pack.github_service import GitHubService
from pack.console_logger import error, warn
from core.logger import get_bot_logger, CAT_API

MAX_ITERATIONS = 100
MAX_FILE_BYTES = 300_000
MAX_FILE_OPERATION_BYTES = 5_000_000
MAX_FILE_CHUNK_BYTES = 200_000
MAX_CONTEXT_CHARS = 120_000
DENYLIST_PREFIXES = ('.env', 'data/msgs', 'data/state')
API_MAX_RETRIES = 3
API_RETRY_BASE_DELAY = 1.2
API_RETRY_MAX_DELAY = 8.0
SHELL_DEFAULT_TIMEOUT_SECONDS = 20
SHELL_DEFAULT_BACKGROUND_TIMEOUT_SECONDS = 600
SHELL_MAX_TIMEOUT_SECONDS = 3600
SHELL_MAX_OUTPUT_CHARS = 12000
SHELL_DEFAULT_TAIL_LINES = 80
SHELL_MAX_TAIL_LINES = 200


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
    relative_path = (relative_path or '').strip().lstrip('/\\')
    if not relative_path:
        return None
    normalized = os.path.normpath(relative_path)
    if normalized.startswith('..') or os.path.isabs(normalized):
        return None
    normalized_slashes = normalized.replace('\\', '/')
    for prefix in DENYLIST_PREFIXES:
        if normalized_slashes == prefix or normalized_slashes.startswith(prefix + '/'):
            return None
    resolved = os.path.normpath(os.path.join(project_root, normalized))
    try:
        common = os.path.commonpath([resolved, project_root])
    except ValueError:
        return None
    if common != project_root:
        return None
    return resolved


def _build_tools_schema() -> list[dict]:
    return [
        {
            'name': 'shell_exec',
            'description': (
                '在项目本地仓库内执行 shell 命令。支持前台等待结果，也支持后台运行。'
                '前台模式可设置 timeout_seconds 超时秒数；后台模式会返回 job_id，之后可用 shell_status / shell_stop / shell_list 管理。'
            ),
            'input_schema': {
                'type': 'object',
                'properties': {
                    'command': {'type': 'string', 'description': '要执行的 shell 命令，将通过 bash -lc 执行'},
                    'cwd': {'type': 'string', 'description': '相对仓库根目录的工作目录，留空表示仓库根目录'},
                    'timeout_seconds': {'type': 'integer', 'description': '超时秒数。前台默认20秒，后台默认600秒，最大3600秒'},
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
            'description': '列出当前 dev agent 会话内创建过的 shell 后台任务。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'active_only': {'type': 'boolean', 'description': '只看仍在运行的任务'},
                },
                'required': [],
            },
        },
        {
            'name': 'list_local_files',
            'description': '列出项目本地仓库目录下某个子路径内的文件和文件夹（相对路径，留空表示仓库根目录）。',
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
            'description': '读取项目本地仓库目录下某个文件的完整内容（相对路径）。',
            'input_schema': {
                'type': 'object',
                'properties': {'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'}},
                'required': ['path'],
            },
        },
        {
            'name': 'read_local_file_chunk',
            'description': '按字节偏移或按行范围读取本地文本文件的一部分，适合大文件分块查看。',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '相对仓库根目录的文件路径'},
                    'offset_bytes': {'type': 'integer', 'description': '可选，起始字节偏移，默认0'},
                    'max_bytes': {'type': 'integer', 'description': '可选，最多读取多少字节，默认120000，最大200000'},
                    'start_line': {'type': 'integer', 'description': '可选，起始行号，从1开始'},
                    'line_count': {'type': 'integer', 'description': '可选，读取行数，默认120'},
                },
                'required': ['path'],
            },
        },
        {
            'name': 'search_local_file',
            'description': '在本地文本文件中查找关键词或正则，返回匹配行号和附近上下文，用于先定位再修改。',
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
            'name': 'replace_local_file_text',
            'description': '对本地文本文件做精确文本替换。可限制替换第几个匹配，也可要求命中次数，适合定点修改。',
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
            'description': '按行号区间替换本地文本文件内容，适合已定位到具体行范围后的定点修改。',
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


def _write_text_file(project_root: str, path: str, content: str) -> str:
    resolved = _resolve_safe_path(project_root, path)
    if resolved is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝写入。'
    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(content)
    except OSError as exc:
        return f'写入文件失败: {exc}'
    return ''


def _build_preview_diff(path: str, original: str, updated: str) -> str:
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
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
    write_err = _write_text_file(project_root, path, updated)
    if write_err:
        return write_err
    return f'{action_summary}{backup_text}\n应用 diff:\n{preview}'


def _read_local_file_chunk(
    project_root: str,
    path: str,
    offset_bytes: int | None = None,
    max_bytes: int | None = None,
    start_line: int | None = None,
    line_count: int | None = None,
) -> str:
    if start_line is not None:
        text, err = _read_text_file_for_operation(project_root, path)
        if err:
            return err
        start_line = max(1, int(start_line or 1))
        line_count = max(1, int(line_count or 120))
        lines = text.splitlines()
        begin_idx = start_line - 1
        end_idx = min(len(lines), begin_idx + line_count)
        chunk_lines = lines[begin_idx:end_idx]
        chunk_text = '\n'.join(chunk_lines)
        return (
            f'文件: {path}\n'
            f'模式: lines\n'
            f'起始行: {start_line}\n'
            f'结束行: {end_idx}\n'
            f'总行数: {len(lines)}\n'
            f'内容:\n{chunk_text}'
        )

    resolved = _resolve_safe_path(project_root, path)
    if resolved is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝读取。'
    if not os.path.isfile(resolved):
        return f'{path} 不是一个文件，或不存在。'
    offset_bytes = max(0, int(offset_bytes or 0))
    max_bytes = min(MAX_FILE_CHUNK_BYTES, max(1, int(max_bytes or 120_000)))
    try:
        with open(resolved, 'rb') as f:
            f.seek(offset_bytes)
            data = f.read(max_bytes)
    except OSError as exc:
        return f'读取文件失败: {exc}'
    text = data.decode('utf-8', errors='replace')
    return (
        f'文件: {path}\n'
        f'模式: bytes\n'
        f'offset_bytes: {offset_bytes}\n'
        f'read_bytes: {len(data)}\n'
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
    old_text = str(old_text or '')
    new_text = str(new_text or '')
    if not old_text:
        return 'old_text 为空，拒绝替换。'
    actual_count = text.count(old_text)
    if actual_count == 0:
        return '未找到要替换的原文本，未修改文件。'
    if expected_count is not None and actual_count != int(expected_count):
        return f'命中次数与预期不符：实际 {actual_count} 次，预期 {int(expected_count)} 次，已拒绝修改。'
    if replace_all:
        updated = text.replace(old_text, new_text)
        replaced_count = actual_count
    else:
        occurrence = max(1, int(occurrence or 1))
        start = 0
        target_index = -1
        for _ in range(occurrence):
            target_index = text.find(old_text, start)
            if target_index < 0:
                return f'只找到 {actual_count} 次匹配，第 {occurrence} 次不存在，已拒绝修改。'
            start = target_index + len(old_text)
        updated = text[:target_index] + new_text + text[target_index + len(old_text):]
        replaced_count = 1
    return _finalize_file_update(
        project_root,
        path,
        text,
        updated,
        f'已定点替换 {path}，命中 {actual_count} 次，本次计划修改 {replaced_count} 处。',
        dry_run=dry_run,
        create_backup=create_backup,
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


def _resolve_shell_cwd(project_root: str, cwd: str) -> tuple[str | None, str]:
    cwd = str(cwd or '').strip()
    if not cwd:
        return project_root, '.'
    resolved = _resolve_safe_path(project_root, cwd)
    if resolved is None:
        return None, cwd
    if not os.path.isdir(resolved):
        return None, cwd
    return resolved, cwd


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
    text = data.decode('utf-8', errors='replace')
    if len(text) > max_chars:
        text = '...(输出过长，已截断前部)\n' + text[-max_chars:]
    return text


def _tail_lines(text: str, line_count: int) -> str:
    lines = str(text or '').splitlines()
    if len(lines) <= line_count:
        return '\n'.join(lines)
    return '...(仅显示最后几行)\n' + '\n'.join(lines[-line_count:])


def _format_shell_output(stdout_text: str, stderr_text: str = '') -> str:
    stdout_text = str(stdout_text or '').strip()
    stderr_text = str(stderr_text or '').strip()
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
        return job

    def exec(self, command: str, cwd: str = '', timeout_seconds=None, background: bool = False) -> str:
        command = str(command or '').strip()
        if not command:
            return '命令为空，未执行。'
        resolved_cwd, display_cwd = _resolve_shell_cwd(self.project_root, cwd)
        if resolved_cwd is None:
            return f'工作目录不合法或不存在: {display_cwd or "."}'
        timeout = self._normalize_timeout(timeout_seconds, background)
        self.list_jobs(active_only=False)
        if background:
            job_id = self._next_id()
            output_path = os.path.join(self.runtime_dir, f'{job_id}.log')
            output_handle = open(output_path, 'w', encoding='utf-8', errors='replace')
            process = subprocess.Popen(
                ['bash', '-lc', command],
                cwd=resolved_cwd,
                stdout=output_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            job = {
                'job_id': job_id,
                'command': command,
                'cwd': display_cwd or '.',
                'resolved_cwd': resolved_cwd,
                'status': 'running',
                'pid': process.pid,
                'started_at': time.time(),
                'ended_at': None,
                'exit_code': None,
                'timeout_seconds': timeout,
                'output_path': output_path,
                'output_handle': output_handle,
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

        try:
            completed = subprocess.run(
                ['bash', '-lc', command],
                cwd=resolved_cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = _format_shell_output(completed.stdout, completed.stderr)
            return (
                f'命令执行完成。\n'
                f'exit_code: {completed.returncode}\n'
                f'cwd: {display_cwd or "."}\n'
                f'timed_out: false\n'
                f'output:\n{output}'
            )
        except subprocess.TimeoutExpired as exc:
            output = _format_shell_output(exc.stdout or '', exc.stderr or '')
            return (
                f'命令执行超时，已终止。\n'
                f'exit_code: timeout\n'
                f'cwd: {display_cwd or "."}\n'
                f'timed_out: true\n'
                f'timeout_seconds: {timeout}\n'
                f'output:\n{output}'
            )
        except Exception as exc:
            return f'shell 执行失败: {exc}'

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
    shell_manager: DevAgentShellManager | None = None,
) -> str:
    tool_input = tool_input or {}
    try:
        if shell_manager is not None:
            if name == 'shell_exec':
                return shell_manager.exec(
                    str(tool_input.get('command') or ''),
                    cwd=str(tool_input.get('cwd') or ''),
                    timeout_seconds=tool_input.get('timeout_seconds'),
                    background=bool(tool_input.get('background')),
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


def _trim_old_tool_results(messages: list[dict], keep_recent_rounds: int = 3) -> None:
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
                    block['content'] = content[:200] + '\n...(历史工具结果，为节省上下文已省略)'


async def run_dev_agent(
    model: AnthropicChatModel,
    github_token: str,
    task_desc: str,
    github_repo: str = '',
    prompt_path: str = 'data/prompt/dev_agent.txt',
    project_root: str | None = None,
    on_finished: Callable[[dict], Awaitable[None] | None] | None = None,
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
    final_result = ''
    final_status = 'failed'

    try:
        for _ in range(MAX_ITERATIONS):
            total_chars = sum(len(json.dumps(m.get('content'), ensure_ascii=False)) for m in messages)
            if total_chars > MAX_CONTEXT_CHARS:
                _trim_old_tool_results(messages)
            reply = await asyncio.to_thread(
                _call_with_retry,
                'DevAgent 模型调用',
                lambda: (
                    response if (response := model.complete(system_prompt, messages, tools, None, 0.4, 4096)) is not None
                    else (_ for _ in ()).throw(RetryableAPIError('模型没有返回有效响应'))
                ),
            )
            if not reply.tool_calls:
                final_status = 'done'
                final_result = reply.text or '(任务结束，模型没有给出文字汇报)'
                return final_result

            messages.append({'role': 'assistant', 'content': reply.raw_content})
            result_blocks = []
            for call in reply.tool_calls:
                result_text = await asyncio.to_thread(
                    _execute_tool_call, call.name, call.input, project_root, github_token, shell_manager,
                )
                result_blocks.append({
                    'type': 'tool_result',
                    'tool_use_id': call.call_id,
                    'content': result_text,
                })
            messages.append({'role': 'user', 'content': result_blocks})
        final_result = '已达到最大工具调用轮数上限，任务可能未完全完成，建议拆分成更小的任务重新委托。'
        return final_result
    except Exception as exc:
        error(f'[DevAgent] 执行异常 iter消息数={len(messages)} 错误={exc}')
        final_result = f'Dev agent 执行异常: {exc}'
        return final_result
    finally:
        stopped_jobs = shell_manager.shutdown()
        if stopped_jobs:
            stopped_text = f'后台 shell 任务已在 dev agent 结束时自动停止: {", ".join(stopped_jobs)}'
            final_result = f'{final_result}\n\n{stopped_text}'.strip() if final_result else stopped_text
        if not final_result:
            final_result = 'Dev agent 已结束，但没有返回可用结果。'
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
