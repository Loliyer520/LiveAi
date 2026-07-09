import json
import os
import subprocess
import sys
import threading
from pack.console_logger import error
import time
import urllib.request
from typing import Optional


class UpdateService:
    def __init__(self, github_token: str, repo_owner: str = 'Loliyer520', repo_name: str = 'LiveAi'):
        self.github_token = str(github_token or '').strip()
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.api_base = 'https://api.github.com'
        self.repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._current_commit = self._get_local_commit()

    def _get_local_commit(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                capture_output=True,
                text=True,
                check=True,
                cwd=self.repo_dir,
                timeout=15,
            )
            return result.stdout.strip()
        except Exception:
            return None

    def _run_git(self, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['git', *args],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.repo_dir,
            timeout=timeout,
        )

    def _github_get_json(self, path: str) -> Optional[dict]:
        url = f'{self.api_base}{path}'
        headers = {
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'LiveAi-Updater',
        }
        if self.github_token and not self.github_token.startswith(('ghp_your_', 'your_')):
            headers['Authorization'] = f'token {self.github_token}'
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            error(f'[UpdateService] GitHub 请求失败: {e}')
            return None

    async def get_latest_commit(self) -> Optional[dict]:
        data = await self._to_thread(
            self._github_get_json,
            f'/repos/{self.repo_owner}/{self.repo_name}/commits/main',
        )
        if not data:
            return None
        try:
            return {
                'sha': data['sha'],
                'message': data['commit']['message'],
                'author': data['commit']['author']['name'],
                'date': data['commit']['author']['date'],
                'url': data['html_url'],
            }
        except Exception:
            return None

    async def check_update(self) -> Optional[dict]:
        self._current_commit = self._get_local_commit()
        if not self._current_commit:
            return None
        latest = await self.get_latest_commit()
        if not latest or latest['sha'] == self._current_commit:
            return None
        return {
            'has_update': True,
            'current_version': self._current_commit[:7],
            'latest_version': latest['sha'][:7],
            'latest_commit_message': latest['message'],
            'latest_commit_author': latest['author'],
            'latest_commit_date': latest['date'],
            'latest_commit_url': latest['url'],
        }

    def get_current_version(self) -> str:
        self._current_commit = self._get_local_commit()
        return self._current_commit[:7] if self._current_commit else 'unknown'

    async def get_version_info(self) -> dict:
        self._current_commit = self._get_local_commit()
        latest = await self.get_latest_commit()
        current = self._current_commit
        result = {
            'current_version': current[:7] if current else 'unknown',
            'current_commit': current,
        }
        if latest:
            result.update({
                'latest_version': latest['sha'][:7],
                'latest_commit': latest['sha'],
                'latest_message': latest['message'],
                'latest_author': latest['author'],
                'latest_date': latest['date'],
                'latest_url': latest['url'],
                'has_update': (current != latest['sha']) if current else False,
            })
        return result

    def check_now_sync(self) -> dict:
        """同步版本检查，供启动时使用。返回 {current, latest, has_update, ...} 或空。"""
        self._current_commit = self._get_local_commit()
        current = self._current_commit
        latest_data = self._github_get_json(f'/repos/{self.repo_owner}/{self.repo_name}/commits/main')
        result = {
            'current_version': current[:7] if current else 'unknown',
        }
        if latest_data:
            latest_sha = latest_data.get('sha', '')
            result.update({
                'latest_version': latest_sha[:7] if latest_sha else '?',
                'latest_message': (latest_data.get('commit') or {}).get('message', ''),
                'has_update': bool(current and latest_sha and current != latest_sha),
            })
        else:
            result['latest_version'] = '?'
            result['has_update'] = False
        return result

    async def execute_update(self) -> dict:
        return await self._to_thread(self._execute_update_sync)

    def _execute_update_sync(self) -> dict:
        try:
            status = self._run_git(['status', '--porcelain'], timeout=15)
            if status.stdout.strip():
                return {
                    'success': False,
                    'error': '本地有未提交的更改，无法自动更新。请先处理本地修改。',
                    'uncommitted_changes': status.stdout.strip(),
                }
            previous = self._get_local_commit()
            pull = self._run_git(['pull', 'origin', 'main'], timeout=180)
            new_commit = self._get_local_commit()
            self._current_commit = new_commit
            return {
                'success': True,
                'message': '更新成功',
                'previous_version': previous[:7] if previous else 'unknown',
                'new_version': new_commit[:7] if new_commit else 'unknown',
                'git_output': pull.stdout.strip(),
                'need_restart': bool(previous and new_commit and previous != new_commit),
            }
        except subprocess.CalledProcessError as e:
            return {'success': False, 'error': f'Git 操作失败: {e.stderr.strip()}', 'git_output': e.stdout.strip()}
        except Exception as e:
            return {'success': False, 'error': f'更新失败: {e}'}

    def restart_program(self, delay_seconds: float = 1.5) -> dict:
        try:
            python = sys.executable
            main_script = os.path.join(self.repo_dir, 'main.py')

            def _restart():
                time.sleep(max(0.1, float(delay_seconds)))
                if sys.platform == 'win32':
                    subprocess.Popen([python, main_script], cwd=self.repo_dir, creationflags=subprocess.CREATE_NEW_CONSOLE)
                    os._exit(0)
                else:
                    # 原地替换进程，保持在 screen/systemd 会话内
                    os.execv(python, [python, main_script])

            threading.Thread(target=_restart, daemon=False).start()
            return {'success': True, 'message': f'将在 {delay_seconds:.1f} 秒后重启程序'}
        except Exception as e:
            return {'success': False, 'error': f'重启失败: {e}'}

    async def _to_thread(self, func, *args):
        import asyncio
        return await asyncio.to_thread(func, *args)
