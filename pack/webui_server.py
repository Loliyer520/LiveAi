from pathlib import Path
import json
import threading
import time
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core.ai_repository import AIRepository
from core.ai_runtime import AIOrchestrator
from pack.console_logger import ok, warn


class WebUIService:
    def __init__(self, host: str, port: int, repo: AIRepository, orchestrator: AIOrchestrator):
        self.host = host
        self.port = port
        self.repo = repo
        self.orchestrator = orchestrator
        self.httpd = None
        self.thread = None
        self._html_cache = None

    def start(self):
        if self.thread:
            return
        handler = self._build_handler()
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        ok(f'WebUI 管理面板 → http://{self.host}:{self.port}')

    def _build_handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == '/':
                    return self._write_html(service._render_index())
                if parsed.path == '/api/overview':
                    return self._write_json(service.get_overview())
                if parsed.path == '/api/commands':
                    return self._write_json({'items': service.get_commands()})
                if parsed.path == '/api/agents':
                    params = parse_qs(parsed.query)
                    keyword = (params.get('q') or [''])[0]
                    scope_type = (params.get('scope_type') or [''])[0]
                    return self._write_json({'items': service.list_agents(keyword, scope_type)})
                if parsed.path == '/api/agent':
                    params = parse_qs(parsed.query)
                    scope_type = (params.get('scope_type') or [''])[0]
                    scope_id = (params.get('scope_id') or [''])[0]
                    if not scope_type or not scope_id:
                        return self._write_json({'error': 'missing scope_type or scope_id'}, status=400)
                    detail = service.get_agent_detail(scope_type, scope_id)
                    if not detail:
                        return self._write_json({'error': 'agent not found'}, status=404)
                    return self._write_json(detail)
                if parsed.path == '/api/agent/turn':
                    params = parse_qs(parsed.query)
                    scope_type = (params.get('scope_type') or [''])[0]
                    scope_id = (params.get('scope_id') or [''])[0]
                    turn_id = (params.get('turn_id') or [''])[0]
                    if not scope_type or not scope_id or not turn_id:
                        return self._write_json({'error': 'missing scope_type, scope_id or turn_id'}, status=400)
                    detail = service.get_turn_detail(scope_type, scope_id, turn_id)
                    if not detail:
                        return self._write_json({'error': 'turn not found'}, status=404)
                    return self._write_json(detail)
                if parsed.path == '/api/tasks':
                    params = parse_qs(parsed.query)
                    status = (params.get('status') or [''])[0]
                    return self._write_json({'items': service.list_tasks(status)})
                if parsed.path == '/api/settings':
                    return self._write_json(service.get_settings())
                if parsed.path == '/api/knowledge':
                    return self._write_json({'items': service.list_knowledge()})
                if parsed.path == '/api/relations':
                    return self._write_json(service.get_relations())
                if parsed.path == '/api/models_config':
                    return self._write_json(service.get_models_config())
                return self._write_json({'error': 'not found'}, status=404)

            def do_POST(self):
                parsed = urlparse(self.path)
                payload = self._read_json()
                if parsed.path == '/api/model':
                    ok, message = service.switch_model(payload.get('profile', ''))
                    return self._write_json({'ok': ok, 'message': message}, status=200 if ok else 400)
                if parsed.path == '/api/agent/action':
                    ok, message = service.handle_agent_action(
                        payload.get('scope_type', ''),
                        payload.get('scope_id', ''),
                        payload.get('action', ''),
                        payload,
                    )
                    return self._write_json({'ok': ok, 'message': message}, status=200 if ok else 400)
                if parsed.path == '/api/agent/send_admin_message':
                    ok, message = service.send_admin_message(
                        payload.get('scope_type', ''),
                        payload.get('scope_id', ''),
                        payload.get('text', ''),
                    )
                    return self._write_json({'ok': ok, 'message': message}, status=200 if ok else 400)
                if parsed.path == '/api/settings':
                    ok, message = service.update_settings(payload)
                    return self._write_json({'ok': ok, 'message': message}, status=200 if ok else 400)
                if parsed.path == '/api/knowledge':
                    ok, message = service.handle_knowledge_action(
                        payload.get('action', ''),
                        payload,
                    )
                    return self._write_json({'ok': ok, 'message': message}, status=200 if ok else 400)
                if parsed.path == '/api/relations':
                    ok, message = service.handle_relation_action(
                        payload.get('action', ''),
                        payload,
                    )
                    return self._write_json({'ok': ok, 'message': message}, status=200 if ok else 400)
                if parsed.path == '/api/models_config':
                    ok, message = service.update_models_config(payload)
                    return self._write_json({'ok': ok, 'message': message}, status=200 if ok else 400)
                if parsed.path == '/api/channel_models':
                    ok, result = service.fetch_channel_models(payload)
                    body = {'ok': ok}
                    if ok:
                        body['models'] = result
                    else:
                        body['message'] = result
                    return self._write_json(body, status=200 if ok else 400)
                return self._write_json({'error': 'not found'}, status=404)

            def _read_json(self):
                length = int(self.headers.get('Content-Length') or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length).decode('utf-8')
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {}

            def _write_json(self, payload: dict, status: int = 200):
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _write_html(self, html_text: str, status: int = 200):
                body = html_text.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args):
                return

        return Handler

    def get_overview(self) -> dict:
        state = self.repo.load_state()
        agents = list((state.get('agents') or {}).values())
        tasks = list((state.get('tasks') or {}).values())
        memories = state.get('memories') or {}
        runtime = self.orchestrator.get_runtime_status()
        top_agents = []
        for item in self.list_agents()[:8]:
            top_agents.append(
                {
                    'agent_id': item.get('agent_id'),
                    'scope_type': item.get('scope_type'),
                    'scope_id': item.get('scope_id'),
                    'messages_count': item.get('messages_count'),
                    'notes_count': item.get('notes_count'),
                    'updated_at': item.get('updated_at'),
                }
            )
        return {
            'bot_id': getattr(self.orchestrator.bot, 'self_id', ''),
            'runtime': runtime,
            'counts': {
                'agents': len(agents),
                'groups': sum(1 for item in agents if item.get('scope_type') == 'group'),
                'privates': sum(1 for item in agents if item.get('scope_type') == 'private'),
                'masters': sum(1 for item in agents if item.get('scope_type') == 'master'),
                'tasks': len(tasks),
                'queued_tasks': sum(1 for item in tasks if item.get('status') in {'queued', 'running', 'scheduled'}),
                'memories': len(memories),
            },
            'task_status_counts': self._count_values(tasks, 'status'),
            'task_kind_counts': self._count_values(tasks, 'kind'),
            'top_agents': top_agents,
            'recent_tasks': [
                self._serialize_task(item)
                for item in sorted(tasks, key=lambda item: item.get('updated_at', 0), reverse=True)[:10]
            ],
        }

    def get_commands(self) -> list[dict]:
        return self.orchestrator.get_command_catalog()

    def list_agents(self, keyword: str = '', scope_type: str = '') -> list[dict]:
        keyword = (keyword or '').strip().lower()
        scope_type = (scope_type or '').strip().lower()
        result = []
        state = self.repo.load_state()
        agents = state.get('agents') or {}
        memories = state.get('memories') or {}

        # Sort agents by updated_at descending
        sorted_agents = sorted(agents.values(), key=lambda item: item.get('updated_at', 0), reverse=True)

        for agent in sorted_agents:
            if scope_type and str(agent.get('scope_type') or '').lower() != scope_type:
                continue
            
            s_type = agent.get('scope_type', '')
            s_id = str(agent.get('scope_id', ''))
            memory = memories.get(f'{s_type}:{s_id}') or {}
            
            item = {
                'agent_id': agent.get('agent_id'),
                'scope_type': agent.get('scope_type'),
                'scope_id': s_id,
                'role': agent.get('role'),
                'persona': agent.get('persona', ''),
                'display_name': agent.get('display_name') or '',
                'trigger_words': agent.get('trigger_words') or [],
                'message_count': agent.get('message_count', 0),
                'trigger_rate': agent.get('trigger_rate'),
                'impression': agent.get('impression', ''),
                'updated_at': self._fmt_ts(agent.get('updated_at')),
                'impression_updated_at': self._fmt_ts(agent.get('impression_updated_at')),
                'notes_count': len(memory.get('notes') or []),
                'messages_count': len(memory.get('messages') or []),
            }
            haystack = ' '.join(
                [
                    str(item.get('agent_id') or ''),
                    str(item.get('scope_type') or ''),
                    str(item.get('scope_id') or ''),
                    str(item.get('impression') or ''),
                ]
            ).lower()
            if keyword and keyword not in haystack:
                continue
            result.append(item)
        return result

    def get_agent_detail(self, scope_type: str, scope_id: str) -> dict | None:
        agent = self.repo.get_agent(scope_type, scope_id)
        if not agent:
            return None
        memory = self.repo.get_memory(scope_type, scope_id)
        messages = memory.get('messages') or []
        notes = memory.get('notes') or []
        tool_logs = memory.get('tool_logs') or []
        turn_logs = memory.get('turn_logs') or []
        return {
            'agent': {
                **agent,
                'created_at_text': self._fmt_ts(agent.get('created_at')),
                'updated_at_text': self._fmt_ts(agent.get('updated_at')),
                'impression_updated_at_text': self._fmt_ts(agent.get('impression_updated_at')),
            },
            'messages': [self._serialize_message(item) for item in messages[-40:]],
            'notes': [self._serialize_note(item) for item in notes[-40:]],
            'tool_logs': [self._serialize_tool_log(item) for item in tool_logs[-40:]],
            'recent_turns': [self._serialize_turn_summary(item) for item in turn_logs[-40:]],
            'recent_tasks': [
                self._serialize_task(item)
                for item in reversed(self.repo.list_tasks())
                if item.get('source_agent') == agent.get('agent_id')
            ][:20],
        }

    def get_turn_detail(self, scope_type: str, scope_id: str, turn_id: str) -> dict | None:
        item = self.repo.get_turn_log(scope_type, scope_id, turn_id)
        if not item:
            return None
        return {'turn': self._serialize_turn_detail(item)}

    def list_tasks(self, status: str = '') -> list[dict]:
        items = self.repo.list_tasks(statuses=[status] if status else None)
        items.reverse()
        return [self._serialize_task(item) for item in items[:200]]

    def switch_model(self, profile: str) -> tuple[bool, str]:
        return self.orchestrator.switch_model_profile(profile)

    def get_settings(self) -> dict:
        stored_key = str(self.repo.get_setting('search_api_key', '') or '')
        env_default = str(getattr(self.orchestrator.config, 'search_api_key', '') or '')
        active_key = stored_key or env_default
        stored_github_token = str(self.repo.get_setting('github_api_token', '') or '')
        github_env_default = str(getattr(self.orchestrator.config, 'github_api_token', '') or '')
        active_github_token = stored_github_token or github_env_default

        return {
            'search_api_key_set': bool(active_key),
            'search_api_key_masked': self._mask_secret(active_key),
            'search_api_key_source': 'settings' if stored_key else ('env' if env_default else 'none'),
            'search_base_url': getattr(self.orchestrator.config, 'search_base_url', ''),
            'github_api_token_set': bool(active_github_token),
            'github_api_token_masked': self._mask_secret(active_github_token),
            'github_api_token_source': 'settings' if stored_github_token else ('env' if github_env_default else 'none'),
            'master_qq': getattr(self.orchestrator.config, 'master_qq', 0),
            'auto_update_enabled': getattr(self.orchestrator.config, 'auto_update_enabled', True),
            'auto_update_check_hour': getattr(self.orchestrator.config, 'auto_update_check_hour', 4),
            'update_repo_owner': getattr(self.orchestrator.config, 'update_repo_owner', 'Loliyer520'),
            'update_repo_name': getattr(self.orchestrator.config, 'update_repo_name', 'LiveAi'),
            'model_profiles': self.orchestrator.get_model_profiles_info(),
        }

    def update_settings(self, payload: dict) -> tuple[bool, str]:
        payload = payload or {}
        if 'search_api_key' in payload:
            value = str(payload.get('search_api_key') or '').strip()
            self.repo.set_setting('search_api_key', value)
            # Persist to config.yaml
            from core.config import save_config_to_yaml
            if value:
                save_config_to_yaml({'ai': {'search_api_key': value}})
            return True, '已更新搜索 API Key。' if value else '已清空搜索 API Key（将回退到环境变量默认值）。'
        if 'github_api_token' in payload:
            value = str(payload.get('github_api_token') or '').strip()
            self.repo.set_setting('github_api_token', value)
            from core.config import save_config_to_yaml
            if value:
                save_config_to_yaml({'ai': {'github_api_token': value}})
            return True, '已更新 GitHub API Token。' if value else '已清空 GitHub API Token（将回退到环境变量默认值）。'
        if 'master_qq' in payload:
            value = int(payload.get('master_qq') or 0)
            self.orchestrator.config.master_qq = value
            from core.config import save_config_to_yaml
            save_config_to_yaml({'ai': {'master_qq': value}})
            return True, '已更新主人 QQ。'
        if 'auto_update_enabled' in payload:
            value = bool(payload.get('auto_update_enabled'))
            self.orchestrator.config.auto_update_enabled = value
            from core.config import save_config_to_yaml
            save_config_to_yaml({'ai': {'auto_update_enabled': value}})
            return True, '已更新自动更新开关。'
        if 'auto_update_check_hour' in payload:
            value = max(0, min(23, int(payload.get('auto_update_check_hour') or 4)))
            self.orchestrator.config.auto_update_check_hour = value
            from core.config import save_config_to_yaml
            save_config_to_yaml({'ai': {'auto_update_check_hour': value}})
            return True, '已更新自动检查时间。'
        if 'update_repo_owner' in payload or 'update_repo_name' in payload:
            owner = str(payload.get('update_repo_owner') or getattr(self.orchestrator.config, 'update_repo_owner', 'Loliyer520')).strip()
            name = str(payload.get('update_repo_name') or getattr(self.orchestrator.config, 'update_repo_name', 'LiveAi')).strip()
            self.orchestrator.config.update_repo_owner = owner
            self.orchestrator.config.update_repo_name = name
            self.orchestrator.update_service.repo_owner = owner
            self.orchestrator.update_service.repo_name = name
            from core.config import save_config_to_yaml
            save_config_to_yaml({'ai': {'update_repo_owner': owner, 'update_repo_name': name}})
            return True, '已更新自动更新仓库。'
        if 'model_profile' in payload:
            name = str(payload.get('model_profile') or '').strip()
            if not name:
                return False, '缺少 model_profile。'
            return self.orchestrator.update_model_profile(
                name,
                base_url=payload.get('base_url'),
                api_key=payload.get('api_key'),
                model_name=payload.get('model_name'),
                messages_path=payload.get('messages_path'),
            )
        return False, '没有可更新的设置项。'

    def list_knowledge(self) -> list[dict]:
        items = self.repo.get_knowledge_base()
        items = sorted(items, key=lambda item: item.get('updated_at', 0), reverse=True)
        return [
            {
                'entry_id': item.get('entry_id'),
                'content': item.get('content'),
                'created_at': self._fmt_ts(item.get('created_at')),
                'updated_at': self._fmt_ts(item.get('updated_at')),
            }
            for item in items
        ]

    def handle_knowledge_action(self, action: str, payload: dict | None = None) -> tuple[bool, str]:
        action = str(action or '').strip()
        payload = payload or {}
        if action == 'add':
            content = str(payload.get('content') or '').strip()
            if not content:
                return False, '内容不能为空。'
            entry = self.repo.add_knowledge_entry(content)
            if not entry:
                return False, '添加失败。'
            return True, '已添加知识库条目。'
        if action == 'update':
            entry_id = str(payload.get('entry_id') or '').strip()
            content = str(payload.get('content') or '').strip()
            if not entry_id or not content:
                return False, '缺少 entry_id 或 content。'
            entry = self.repo.update_knowledge_entry(entry_id, content)
            if not entry:
                return False, '未找到该条目。'
            return True, '已更新知识库条目。'
        if action == 'delete':
            entry_id = str(payload.get('entry_id') or '').strip()
            if not entry_id:
                return False, '缺少 entry_id。'
            ok = self.repo.delete_knowledge_entry(entry_id)
            if not ok:
                return False, '未找到该条目。'
            return True, '已删除知识库条目。'
        return False, '不支持的操作。'

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = str(value or '')
        if not value:
            return ''
        if len(value) <= 8:
            return '*' * len(value)
        return f'{value[:4]}{"*" * (len(value) - 8)}{value[-4:]}'

    def handle_agent_action(
        self,
        scope_type: str,
        scope_id: str,
        action: str,
        payload: dict | None = None,
    ) -> tuple[bool, str]:
        scope_type = str(scope_type or '').strip()
        scope_id = str(scope_id or '').strip()
        action = str(action or '').strip()
        payload = payload or {}
        if not scope_type or not scope_id:
            return False, '缺少 scope_type 或 scope_id。'
        if action == 'clear_chat':
            self.repo.clear_messages(scope_type, scope_id)
            return True, '已清空聊天记录。'
        if action == 'clear_notes':
            self.repo.clear_notes(scope_type, scope_id)
            return True, '已清空备注。'
        if action == 'clear_memory':
            self.repo.clear_memory(scope_type, scope_id)
            return True, '已清空聊天记录和备注。'
        if action == 'refresh_impression':
            ok, result = self.orchestrator.schedule_refresh_impression(scope_type, scope_id)
            if ok:
                return True, f'已提交印象刷新任务: {result}'
            return False, result
        if action == 'update_impression':
            impression = payload.get('impression', '').strip()
            self.repo.update_agent_impression(scope_type, scope_id, impression)
            return True, '已更新印象。'
        return False, '不支持的操作。'

    def send_admin_message(self, scope_type: str, scope_id: str, text: str) -> tuple[bool, str]:
        """从后台向指定 AI 发送管理员消息"""
        return self.orchestrator.send_admin_message(scope_type, scope_id, text)

    def get_relations(self) -> dict:
        """获取所有关系网数据"""
        scopes = self.repo.list_scope_relations()
        users = self.repo.list_user_relations()

        return {
            'scopes': [
                {
                    'scope_key': item['scope_key'],
                    'scope_type': item['scope_type'],
                    'scope_id': item['scope_id'],
                    'display_name': (self.repo.get_agent(item['scope_type'], item['scope_id']) or {}).get('display_name') or '',
                    'affinity': item['affinity'],
                    'relevance': item['relevance'],
                    'admin_note': item['admin_note'],
                    'message_count': item['message_count'],
                    'impression': item['impression'][:100] if item['impression'] else '',
                    'updated_at': self._fmt_ts(item['updated_at']),
                }
                for item in scopes
            ],
            'users': [
                {
                    'user_id': item['user_id'],
                    'aliases': item['aliases'][:3],
                    'affinity': item['affinity'],
                    'admin_note': item['admin_note'],
                    'fact_count': len(item['facts']),
                    'scope_count': len(item['scopes']),
                    'updated_at': self._fmt_ts(item['updated_at']),
                }
                for item in users
            ],
        }

    def handle_relation_action(self, action: str, payload: dict | None = None) -> tuple[bool, str]:
        """处理关系网编辑操作"""
        action = str(action or '').strip()
        payload = payload or {}

        if action == 'update_scope':
            scope_type = str(payload.get('scope_type', '')).strip()
            scope_id = str(payload.get('scope_id', '')).strip()
            if not scope_type or not scope_id:
                return False, '缺少 scope_type 或 scope_id。'

            affinity = payload.get('affinity')
            relevance = payload.get('relevance')
            admin_note = payload.get('admin_note')

            self.repo.update_scope_relation(
                scope_type, scope_id,
                affinity=float(affinity) if affinity is not None else None,
                relevance=float(relevance) if relevance is not None else None,
                admin_note=str(admin_note) if admin_note is not None else None,
            )
            return True, '已更新群聊/私聊关系数据。'

        if action == 'update_user':
            user_id = str(payload.get('user_id', '')).strip()
            if not user_id:
                return False, '缺少 user_id。'

            affinity = payload.get('affinity')
            admin_note = payload.get('admin_note')

            self.repo.update_user_relation(
                user_id,
                affinity=float(affinity) if affinity is not None else None,
                admin_note=str(admin_note) if admin_note is not None else None,
            )
            return True, '已更新用户关系数据。'

        return False, '不支持的操作。'

    def get_models_config(self) -> dict:
        """获取模型配置"""
        from core.config import AppConfig
        config = AppConfig()

        # Load existing models config from a JSON file if it exists
        config_file = Path(__file__).resolve().parent.parent / 'data' / 'models_config.json'
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass

        # Return default structure
        return {
            'channels': [],
            'vision': {'strategy': 'random', 'models': []},
            'main': {'strategy': 'random', 'models': []},
            'tiered': {'strategy': 'random', 'models': []},
        }

    def update_models_config(self, payload: dict) -> tuple[bool, str]:
        """更新模型配置"""
        try:
            config_file = Path(__file__).resolve().parent.parent / 'data' / 'models_config.json'
            config_file.parent.mkdir(parents=True, exist_ok=True)

            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            result = self.orchestrator.reload_models_config()
            if result.get('loaded'):
                current = result.get('current') or '当前模型'
                return True, f'模型配置已保存，已热加载：{current}'
            return True, '模型配置已保存（无有效渠道或主模型，保持原有配置）'
        except Exception as e:
            return False, f'保存失败: {e}'

    def _extract_model_ids(self, data) -> list[str]:
        models: list[str] = []

        def visit(value):
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return

            model_id = value.get('id') or value.get('model') or value.get('model_id') or value.get('name')
            if isinstance(model_id, str) and model_id.strip():
                models.append(model_id.strip())

            for key in ('data', 'models', 'result', 'results', 'items'):
                nested = value.get(key)
                if isinstance(nested, (list, dict)):
                    visit(nested)

        visit(data)
        return sorted(set(models))

    @staticmethod
    def _preview_response_text(text: str, limit: int = 800) -> str:
        text = str(text or '').replace('\n', ' ').replace('\r', ' ').strip()
        if len(text) <= limit:
            return text
        return text[:limit] + '...'

    def fetch_channel_models(self, payload: dict) -> tuple[bool, list[str] | str]:
        """从渠道接口拉取模型列表"""
        base_url = str((payload or {}).get('base_url') or '').strip().rstrip('/')
        api_key = str((payload or {}).get('api_key') or '').strip()
        if not base_url:
            return False, '缺少 Base URL。'

        headers = {'Accept': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
            headers['x-api-key'] = api_key

        errors = []
        for path in ('/models', '/v1/models'):
            try:
                response = requests.get(f'{base_url}{path}', headers=headers, timeout=20)
                if response.status_code >= 400:
                    preview = self._preview_response_text(response.text)
                    warn(f'[WebUI][models] {path} HTTP {response.status_code}: {preview}')
                    errors.append(f'{path}: HTTP {response.status_code}，预览: {preview[:200]}')
                    continue
                try:
                    data = response.json()
                except ValueError:
                    preview = self._preview_response_text(response.text)
                    warn(f'[WebUI][models] {path} 非 JSON 响应: {preview}')
                    errors.append(f'{path}: 返回内容不是 JSON，预览: {preview[:200]}')
                    continue
                models = self._extract_model_ids(data)
                if models:
                    return True, models
                preview = self._preview_response_text(response.text)
                warn(f'[WebUI][models] {path} 未解析到模型: {preview}')
                errors.append(f'{path}: 未解析到模型，预览: {preview[:200]}')
            except Exception as exc:
                errors.append(f'{path}: {exc}')
        return False, '；'.join(errors) or '拉取失败。'


    def _serialize_task(self, item: dict) -> dict:
        return {
            'task_id': item.get('task_id'),
            'source_agent': item.get('source_agent'),
            'kind': item.get('kind'),
            'status': item.get('status'),
            'result': item.get('result'),
            'created_at': self._fmt_ts(item.get('created_at')),
            'updated_at': self._fmt_ts(item.get('updated_at')),
        }

    def _serialize_message(self, item: dict) -> dict:
        return {
            'nickname': item.get('nickname') or item.get('user_id'),
            'user_id': item.get('user_id'),
            'text': item.get('text'),
            'source_label': item.get('source_label'),
            'timestamp': self._fmt_ts(item.get('timestamp')),
            'generation_ms': item.get('generation_ms'),
            'think_note': item.get('think_note') or '',
        }

    def _serialize_note(self, item: dict) -> dict:
        return {
            'note_id': item.get('note_id'),
            'content': item.get('content'),
            'created_at': self._fmt_ts(item.get('created_at')),
            'updated_at': self._fmt_ts(item.get('updated_at')),
        }

    def _serialize_tool_log(self, item: dict) -> dict:
        return {
            'log_id': item.get('log_id'),
            'agent_id': item.get('agent_id'),
            'tool_name': item.get('tool_name'),
            'tool_input': item.get('tool_input'),
            'tool_result': item.get('tool_result'),
            'created_at': self._fmt_ts(item.get('created_at')),
        }

    def _serialize_turn_summary(self, item: dict) -> dict:
        meta = item.get('turn_meta') or {}
        model_messages = item.get('model_messages') or []
        preview = ''
        if len(model_messages) > 1:
            preview = str((model_messages[1] or {}).get('content') or '')
        return {
            'turn_id': item.get('turn_id'),
            'agent_id': item.get('agent_id'),
            'created_at': self._fmt_ts(item.get('created_at')),
            'temperature': item.get('temperature'),
            'turn_kind': meta.get('turn_kind') or 'unknown',
            'trigger_count': meta.get('trigger_count'),
            'tool_iteration_count': len(item.get('tool_iterations') or []),
            'final_reply': item.get('final_reply') or '',
            'preview': preview[:240],
        }

    def _serialize_turn_detail(self, item: dict) -> dict:
        return {
            'turn_id': item.get('turn_id'),
            'agent_id': item.get('agent_id'),
            'created_at': self._fmt_ts(item.get('created_at')),
            'temperature': item.get('temperature'),
            'turn_meta': item.get('turn_meta') or {},
            'raw_reply': item.get('raw_reply') or '',
            'final_reply': item.get('final_reply') or '',
            'model_messages': list(item.get('model_messages') or []),
            'tool_iterations': list(item.get('tool_iterations') or []),
        }

    @staticmethod
    def _count_values(items: list[dict], key: str) -> list[dict]:
        counts: dict[str, int] = {}
        for item in items:
            name = str(item.get(key) or 'unknown')
            counts[name] = counts.get(name, 0) + 1
        return [
            {'name': name, 'count': count}
            for name, count in sorted(counts.items(), key=lambda pair: pair[1], reverse=True)
        ]

    @staticmethod
    def _fmt_ts(value) -> str:
        try:
            if not value:
                return ''
            return datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return ''

    def _render_index(self) -> str:
        if self._html_cache:
            return self._html_cache
        html_path = Path(__file__).resolve().parent.parent / 'data' / 'res' / 'admin.html'
        try:
            self._html_cache = html_path.read_text(encoding='utf-8')
            return self._html_cache
        except FileNotFoundError:
            return '<!doctype html><html lang="zh-CN"><body><pre>admin.html not found</pre></body></html>'
