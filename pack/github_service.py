import base64

import requests

API_ROOT = 'https://api.github.com'


class GitHubService:
    def __init__(self, token: str = ''):
        self.token = token

    def _headers(self) -> dict:
        headers = {
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    def _require_token(self):
        if not self.token:
            raise RuntimeError('GitHub API token 未配置')

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        self._require_token()
        response = requests.request(method, url, headers=self._headers(), timeout=20, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f'GitHub API 请求失败: {method} {url} -> {response.status_code} {response.text[:300]}')
        return response

    # ---- 仓库检索 ----

    def list_repos(self, per_page: int = 30) -> list:
        response = self._request(
            'GET', f'{API_ROOT}/user/repos', params={'per_page': per_page, 'sort': 'updated'},
        )
        return response.json()

    def search_repos(self, query: str, per_page: int = 10) -> dict:
        query = (query or '').strip()
        if not query:
            raise ValueError('搜索关键词为空')
        response = self._request(
            'GET', f'{API_ROOT}/search/repositories', params={'q': query, 'per_page': per_page},
        )
        return response.json()

    def get_repo(self, owner: str, repo: str) -> dict:
        response = self._request('GET', f'{API_ROOT}/repos/{owner}/{repo}')
        return response.json()

    # ---- 代码检索 ----

    def search_code(self, query: str, repo: str = '') -> dict:
        query = (query or '').strip()
        if not query:
            raise ValueError('搜索关键词为空')
        q = f'{query} repo:{repo}' if repo else query
        response = self._request('GET', f'{API_ROOT}/search/code', params={'q': q})
        return response.json()

    def get_file_contents(self, owner: str, repo: str, path: str, ref: str = '') -> dict:
        path = (path or '').strip().lstrip('/')
        if not owner or not repo or not path:
            raise ValueError('owner/repo/path 不能为空')
        params = {'ref': ref} if ref else {}
        response = self._request('GET', f'{API_ROOT}/repos/{owner}/{repo}/contents/{path}', params=params)
        data = response.json()
        if isinstance(data, dict) and data.get('encoding') == 'base64' and data.get('content'):
            data['decoded_text'] = base64.b64decode(data['content']).decode('utf-8', errors='replace')
        return data

    # ---- 文件写入 ----

    def create_or_update_file(
        self, owner: str, repo: str, path: str, content: str, message: str, branch: str = '',
    ) -> dict:
        path = (path or '').strip().lstrip('/')
        if not owner or not repo or not path or not message:
            raise ValueError('owner/repo/path/message 不能为空')
        sha = ''
        try:
            existing = self.get_file_contents(owner, repo, path, branch)
            if isinstance(existing, dict):
                sha = existing.get('sha') or ''
        except RuntimeError:
            sha = ''
        payload = {
            'message': message,
            'content': base64.b64encode(content.encode('utf-8')).decode('ascii'),
        }
        if branch:
            payload['branch'] = branch
        if sha:
            payload['sha'] = sha
        response = self._request('PUT', f'{API_ROOT}/repos/{owner}/{repo}/contents/{path}', json=payload)
        return response.json()

    def delete_file(self, owner: str, repo: str, path: str, message: str, branch: str = '') -> dict:
        path = (path or '').strip().lstrip('/')
        if not owner or not repo or not path or not message:
            raise ValueError('owner/repo/path/message 不能为空')
        existing = self.get_file_contents(owner, repo, path, branch)
        sha = existing.get('sha') if isinstance(existing, dict) else ''
        if not sha:
            raise RuntimeError('未能获取文件 sha，无法删除')
        payload = {'message': message, 'sha': sha}
        if branch:
            payload['branch'] = branch
        response = self._request('DELETE', f'{API_ROOT}/repos/{owner}/{repo}/contents/{path}', json=payload)
        return response.json()

    # ---- 分支 / 标签 ----

    def list_branches(self, owner: str, repo: str) -> list:
        response = self._request('GET', f'{API_ROOT}/repos/{owner}/{repo}/branches', params={'per_page': 100})
        return response.json()

    def _get_ref_sha(self, owner: str, repo: str, ref: str) -> str:
        response = self._request('GET', f'{API_ROOT}/repos/{owner}/{repo}/git/ref/heads/{ref}')
        return response.json().get('object', {}).get('sha', '')

    def create_branch(self, owner: str, repo: str, new_branch: str, from_branch: str = '') -> dict:
        if not owner or not repo or not new_branch:
            raise ValueError('owner/repo/new_branch 不能为空')
        base_branch = from_branch or self.get_repo(owner, repo).get('default_branch', 'main')
        sha = self._get_ref_sha(owner, repo, base_branch)
        if not sha:
            raise RuntimeError(f'未能解析基础分支 {base_branch} 的提交 sha')
        payload = {'ref': f'refs/heads/{new_branch}', 'sha': sha}
        response = self._request('POST', f'{API_ROOT}/repos/{owner}/{repo}/git/refs', json=payload)
        return response.json()

    def create_tag(self, owner: str, repo: str, tag_name: str, ref: str = '') -> dict:
        if not owner or not repo or not tag_name:
            raise ValueError('owner/repo/tag_name 不能为空')
        base_ref = ref or self.get_repo(owner, repo).get('default_branch', 'main')
        sha = self._get_ref_sha(owner, repo, base_ref)
        if not sha:
            # ref 本身可能已经是一个 commit sha
            sha = base_ref
        payload = {'ref': f'refs/tags/{tag_name}', 'sha': sha}
        response = self._request('POST', f'{API_ROOT}/repos/{owner}/{repo}/git/refs', json=payload)
        return response.json()

    # ---- Pull Request ----

    def list_pull_requests(self, owner: str, repo: str, state: str = 'open') -> list:
        response = self._request(
            'GET', f'{API_ROOT}/repos/{owner}/{repo}/pulls', params={'state': state, 'per_page': 30},
        )
        return response.json()

    def create_pull_request(self, owner: str, repo: str, title: str, head: str, base: str, body: str = '') -> dict:
        if not owner or not repo or not title or not head or not base:
            raise ValueError('owner/repo/title/head/base 不能为空')
        payload = {'title': title, 'head': head, 'base': base, 'body': body}
        response = self._request('POST', f'{API_ROOT}/repos/{owner}/{repo}/pulls', json=payload)
        return response.json()

    def merge_pull_request(self, owner: str, repo: str, number: int, commit_message: str = '') -> dict:
        payload = {}
        if commit_message:
            payload['commit_message'] = commit_message
        response = self._request('PUT', f'{API_ROOT}/repos/{owner}/{repo}/pulls/{number}/merge', json=payload)
        return response.json()

    def close_pull_request(self, owner: str, repo: str, number: int) -> dict:
        response = self._request(
            'PATCH', f'{API_ROOT}/repos/{owner}/{repo}/pulls/{number}', json={'state': 'closed'},
        )
        return response.json()

    # ---- Issue ----

    def list_issues(self, owner: str, repo: str, state: str = 'open') -> list:
        response = self._request(
            'GET', f'{API_ROOT}/repos/{owner}/{repo}/issues', params={'state': state, 'per_page': 30},
        )
        return response.json()

    def create_issue(self, owner: str, repo: str, title: str, body: str = '') -> dict:
        if not owner or not repo or not title:
            raise ValueError('owner/repo/title 不能为空')
        response = self._request('POST', f'{API_ROOT}/repos/{owner}/{repo}/issues', json={'title': title, 'body': body})
        return response.json()

    def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        if not body:
            raise ValueError('评论内容不能为空')
        response = self._request(
            'POST', f'{API_ROOT}/repos/{owner}/{repo}/issues/{number}/comments', json={'body': body},
        )
        return response.json()

    def close_issue(self, owner: str, repo: str, number: int) -> dict:
        response = self._request(
            'PATCH', f'{API_ROOT}/repos/{owner}/{repo}/issues/{number}', json={'state': 'closed'},
        )
        return response.json()

    # ---- 提交历史 ----

    def list_commits(self, owner: str, repo: str, sha: str = '', path: str = '', per_page: int = 20) -> list:
        params = {'per_page': per_page}
        if sha:
            params['sha'] = sha
        if path:
            params['path'] = path
        response = self._request('GET', f'{API_ROOT}/repos/{owner}/{repo}/commits', params=params)
        return response.json()

    def get_commit(self, owner: str, repo: str, sha: str) -> dict:
        if not sha:
            raise ValueError('sha 不能为空')
        response = self._request('GET', f'{API_ROOT}/repos/{owner}/{repo}/commits/{sha}')
        return response.json()
