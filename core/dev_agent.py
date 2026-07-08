import asyncio
import json
import os

from pack.anthropic_chat_model import AnthropicChatModel
from pack.github_service import GitHubService

MAX_ITERATIONS = 20
MAX_FILE_BYTES = 40_000
MAX_CONTEXT_CHARS = 120_000
DENYLIST_PREFIXES = ('.env', 'data/msgs', 'data/state')


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


def _edit_local_file(project_root: str, path: str, content: str) -> str:
    resolved = _resolve_safe_path(project_root, path)
    if resolved is None:
        return '路径不合法、超出允许范围，或命中禁止访问清单，拒绝写入。'
    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(content)
    except OSError as exc:
        return f'写入文件失败: {exc}'
    return f'已写入 {path}（{len(content)} 字符）。'


def _github_search_code(token: str, query: str, repo: str) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    query = (query or '').strip()
    if not query:
        return '搜索关键词为空，未执行搜索。'
    service = GitHubService(token=token)
    try:
        data = service.search_code(query, repo)
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
        data = service.get_file_contents(owner, repo, path, ref)
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
        result = getattr(service, fn_name)(*args)
    except Exception as exc:
        return f'GitHub 操作失败: {exc}'
    return str(result)


def _github_list_repos(token: str, per_page: int) -> str:
    if not token:
        return '未配置 GitHub API token，请联系管理员在后台设置。'
    service = GitHubService(token=token)
    try:
        repos = service.list_repos(per_page or 30)
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
        data = service.search_repos(query, per_page or 10)
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
        branches = service.list_branches(owner, repo)
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
        prs = service.list_pull_requests(owner, repo, state or 'open')
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
        issues = service.list_issues(owner, repo, state or 'open')
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
        commits = service.list_commits(owner, repo, sha, path)
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
        data = service.get_commit(owner, repo, sha)
    except Exception as exc:
        return f'GitHub commit 详情获取失败: {exc}'
    commit_info = data.get('commit', {})
    files = data.get('files') or []
    lines = [f"{data.get('sha', '')}: {commit_info.get('message', '')}", f"作者: {commit_info.get('author', {}).get('name', '')}"]
    for f in files[:30]:
        lines.append(f"  {f.get('status')} {f.get('filename')} (+{f.get('additions')}/-{f.get('deletions')})")
    return '\n'.join(lines)


def _execute_tool_call(name: str, tool_input: dict, project_root: str, github_token: str) -> str:
    tool_input = tool_input or {}
    try:
        if name == 'list_local_files':
            return _list_local_files(project_root, str(tool_input.get('subpath') or ''))
        if name == 'read_local_file':
            return _read_local_file(project_root, str(tool_input.get('path') or ''))
        if name == 'edit_local_file':
            return _edit_local_file(
                project_root,
                str(tool_input.get('path') or ''),
                str(tool_input.get('content') or ''),
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
) -> str:
    project_root = project_root or _project_root()
    try:
        with open(prompt_path, 'r', encoding='utf-8') as f:
            system_prompt = f.read()
    except OSError:
        system_prompt = '你是这个项目专属的后台代码/资料助手，操作范围限定在本地仓库目录内，可以只读查阅GitHub任意仓库做参考。'

    tools = _build_tools_schema()
    task_text = task_desc
    if github_repo:
        task_text += f'\n\n（可优先参考 GitHub 仓库: {github_repo}）'
    messages: list[dict] = [{'role': 'user', 'content': task_text}]

    try:
        for _ in range(MAX_ITERATIONS):
            total_chars = sum(len(json.dumps(m.get('content'), ensure_ascii=False)) for m in messages)
            if total_chars > MAX_CONTEXT_CHARS:
                _trim_old_tool_results(messages)
            reply = await asyncio.to_thread(model.complete, system_prompt, messages, tools, None, 0.4, 4096)
            if reply is None:
                return '模型没有返回有效响应。'
            if not reply.tool_calls:
                return reply.text or '(任务结束，模型没有给出文字汇报)'

            messages.append({'role': 'assistant', 'content': reply.raw_content})
            result_blocks = []
            for call in reply.tool_calls:
                result_text = await asyncio.to_thread(
                    _execute_tool_call, call.name, call.input, project_root, github_token,
                )
                result_blocks.append({
                    'type': 'tool_result',
                    'tool_use_id': call.call_id,
                    'content': result_text,
                })
            messages.append({'role': 'user', 'content': result_blocks})
        return '已达到最大工具调用轮数上限，任务可能未完全完成，建议拆分成更小的任务重新委托。'
    except Exception as exc:
        print(f'[DevAgent] 执行异常 iter消息数={len(messages)} 错误={exc}')
        return f'Dev agent 执行异常: {exc}'
