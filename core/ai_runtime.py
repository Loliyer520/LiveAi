import asyncio
import copy
import html
import json
import os
import random
import re
import threading
import time
from datetime import datetime

from pack.napcat import NapcatBot
from pack.anthropic_chat_model import AnthropicChatModel, AnthropicReply
from pack.search_service import DoubaoSearchService
from pack.vision_model import OpenAICompatibleVisionModel
from pack.update_service import UpdateService
from pack.console_logger import info, warn, error, debug
from core.ai_repository import AIRepository
from core.ai_tools_schema import LOOP_TOOL_NAMES, build_tools
from core.config import AIConfig
from core.dev_agent import run_dev_agent
from core.events import ChatMessage
from core.prompt_store import PromptStore, default_char_prompt
from tool.ai_toolbox import AIToolbox


class AIOrchestrator:
    def __init__(
        self,
        config: AIConfig,
        bot: NapcatBot,
        repo: AIRepository,
        model: AnthropicChatModel,
        vision_model: OpenAICompatibleVisionModel,
    ):
        self.config = config
        self.bot = bot
        self.repo = repo
        self.model = model
        self.vision_model = vision_model
        self.tools = AIToolbox(bot, repo)
        self.update_service = UpdateService(
            github_token=self._get_github_api_token(),
            repo_owner=self.config.update_repo_owner,
            repo_name=self.config.update_repo_name,
        )
        self._last_update_check_day = None
        self.prompt_store = PromptStore(
            main_prompt_path=self.config.main_prompt_path,
            staff_prompt_path=self.config.staff_prompt_path,
            char_prompt_path=self.config.char_prompt_path,
        )
        self.loop = None
        self.queue = None
        self.thread = None
        self.ready = threading.Event()
        self._recent_message_keys = {}
        self._recent_lock = threading.Lock()
        self._scheduled_alarm_ids = set()
        self._active_scope_turns = set()
        self._pending_scope_turns = {}
        self._dev_agent_tasks = set()
        self._resolving_display_names = set()
        self._pending_self_interrupts = {}
        self._message_epoch = 0
        self._group_reply_windows: dict[str, dict] = {}
        self._recurring_tasks: dict[str, dict] = {}
        self._recurring_tasks_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'recurring_tasks.json',
        )
        self._load_recurring_tasks()
        self._chat_profiles = {
            'flash': {
                'base_url': self.config.model_base_url,
                'api_key': self.config.api_key,
                'model_name': self.config.model_name,
                'messages_path': self.config.model_messages_path,
                'label': 'DeepSeek Flash',
            },
            'pro': {
                'base_url': self.config.pro_model_base_url,
                'api_key': self.config.pro_api_key,
                'model_name': self.config.pro_model_name,
                'messages_path': self.config.pro_model_messages_path,
                'label': 'DeepSeek Pro',
            },
            'claude': {
                'base_url': self.config.claude_model_base_url,
                'api_key': self.config.claude_api_key,
                'model_name': self.config.claude_model_name,
                'messages_path': self.config.claude_model_messages_path,
                'label': 'Claude Sonnet',
            },
            'opus': {
                'base_url': self.config.claude_opus_model_base_url,
                'api_key': self.config.claude_opus_api_key,
                'model_name': self.config.claude_opus_model_name,
                'messages_path': self.config.claude_opus_model_messages_path,
                'label': 'Claude Opus',
            },
        }
        default_profile = str(getattr(self.config, 'default_chat_profile', 'claude') or 'claude').strip().lower()
        self._active_chat_profile = default_profile if default_profile in self._chat_profiles else 'claude'
        self._chat_profile_defaults = copy.deepcopy(self._chat_profiles)
        self._apply_profile_overrides()
        _mc = self._build_profiles_from_models_config()
        if _mc:
            self._chat_profiles = _mc
            self._chat_profile_defaults = copy.deepcopy(_mc)
            if self._active_chat_profile not in self._chat_profiles:
                self._active_chat_profile = next(iter(self._chat_profiles))

    def _build_profiles_from_models_config(self) -> dict:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'models_config.json',
        )
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return {}
        channels = data.get('channels') or []
        if not channels:
            return {}
        main_models = (data.get('main') or {}).get('models') or []
        profiles: dict = {}
        for idx, m in enumerate(main_models):
            ch_idx = m.get('channel')
            if ch_idx is None or str(ch_idx).strip() == '':
                continue
            try:
                ch = channels[int(ch_idx)]
            except (IndexError, TypeError, ValueError):
                continue
            model_name = str(m.get('model_name') or '').strip()
            ch_name = str(ch.get('name') or f'渠道{int(ch_idx)+1}').strip()
            label = f'{ch_name} / {model_name}' if model_name else ch_name
            profiles[f'm{idx}'] = {
                'base_url': str(ch.get('base_url') or '').strip(),
                'api_key': str(ch.get('api_key') or '').strip(),
                'model_name': model_name,
                'messages_path': str(ch.get('messages_path') or '').strip(),
                'label': label,
            }
        if not profiles:
            for idx, ch in enumerate(channels):
                profiles[f'ch{idx}'] = {
                    'base_url': str(ch.get('base_url') or '').strip(),
                    'api_key': str(ch.get('api_key') or '').strip(),
                    'model_name': '',
                    'messages_path': str(ch.get('messages_path') or '').strip(),
                    'label': str(ch.get('name') or f'渠道{idx+1}').strip(),
                }
        return profiles

    def reload_models_config(self) -> dict:
        profiles = self._build_profiles_from_models_config()
        if not profiles:
            return {'loaded': False, 'message': 'models_config.json 无有效渠道，保持原有配置'}
        self._chat_profiles = profiles
        self._chat_profile_defaults = copy.deepcopy(profiles)
        if self._active_chat_profile not in self._chat_profiles:
            self._active_chat_profile = next(iter(self._chat_profiles))
        info(f'[AI] models_config.json 已热加载，共 {len(profiles)} 个模型')
        return {'loaded': True, 'count': len(profiles), 'profiles': list(profiles.keys())}

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=3)

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        for _ in range(max(1, self.config.worker_count)):
            self.loop.create_task(self._worker())
        self.loop.create_task(self._restore_scheduled_tasks())
        self.loop.create_task(self._recurring_scheduler_loop())
        self.loop.create_task(self._auto_update_check_loop())
        self.ready.set()
        self.loop.run_forever()

    def register(self):
        self.bot.on_group_message(self.handle_group_message)
        self.bot.on_private_message(self.handle_private_message)
        self.bot.on_self_message(self.handle_self_message)

    def handle_group_message(self, message: ChatMessage):
        self._submit_message(message)

    def handle_private_message(self, message: ChatMessage):
        self._submit_message(message)

    def handle_self_message(self, message: ChatMessage):
        if not self.loop or not self.queue:
            self.start()
        if not self.loop:
            return
        asyncio.run_coroutine_threadsafe(self._enqueue_self_message(message), self.loop)

    def send_admin_message(self, scope_type: str, scope_id: str, text: str) -> tuple[bool, str]:
        """从后台管理界面向指定 AI 发送消息（用于调试和干预）"""
        scope_type = str(scope_type or '').strip()
        scope_id = str(scope_id or '').strip()
        text = str(text or '').strip()
        if not scope_type or not scope_id or not text:
            return False, '缺少必填参数。'
        if not self.loop or not self.queue:
            return False, 'AI 运行时未就绪。'

        # 构造一个特殊的 ChatMessage，标记来源是系统管理员
        admin_message = ChatMessage(
            chat_type=scope_type,
            chat_id=0 if scope_type == 'master' else int(scope_id),
            user_id=0,  # user_id=0 表示系统管理员
            text=text,
            raw_message=text,
            sender={'nickname': '系统管理员', 'user_id': 0},
            message_id=None,
            mentions_self=True,  # 强制触发
            timestamp=time.time(),
            raw_data={'source': 'admin_webui'},
        )

        # 绕过触发判断，直接提交
        self._submit_message(admin_message)
        return True, '已发送消息给 AI。'

    def _deliver_task_report_message(self, scope_type: str, scope_id: str, task_id: str, result: str) -> None:
        """把后台任务(dev_agent等)的原始汇报喂给该会话的AI，让它自己决定怎么转达，而不是原始文本直接群发。"""
        result = str(result or '').strip()
        if not result or not self.loop or not self.queue:
            return
        wrapped = (
            '【内部系统通知：以下是后台 dev_agent 任务执行完成后的原始技术汇报，不是任何人直接对你说的话，仅供你参考决策。'
            '请结合当前语境和你的人设自主判断：要不要把这件事告诉对方、怎么措辞（可以完全不提技术细节甚至简化成一句话），'
            '如果内容不重要、没必要主动提及，也可以选择不发送任何消息。】\n\n'
            f'原始汇报内容：\n{result}'
        )
        report_message = ChatMessage(
            chat_type=scope_type,
            chat_id=0 if scope_type == 'master' else int(scope_id),
            user_id=0,
            text=wrapped,
            raw_message=wrapped,
            sender={'nickname': '后台任务系统', 'user_id': 0},
            message_id=None,
            mentions_self=True,
            timestamp=time.time(),
            raw_data={'source': 'dev_agent_task_report', 'task_id': task_id},
        )
        self._submit_message(report_message)

    def get_runtime_status(self) -> dict:
        profile = self._chat_profiles[self._active_chat_profile]
        return {
            'enabled': self.config.enabled,
            'ready': bool(self.loop and self.queue),
            'active_profile': self._active_chat_profile,
            'active_model': profile['model_name'],
            'active_label': profile.get('label') or self._active_chat_profile,
            'queue_size': self.queue.qsize() if self.queue else 0,
            'worker_count': max(1, self.config.worker_count),
            'scheduled_alarm_count': len(self._scheduled_alarm_ids),
            'profiles': {
                name: {
                    'model_name': item['model_name'],
                    'label': item.get('label') or name,
                    'base_url': item['base_url'],
                }
                for name, item in self._chat_profiles.items()
            },
        }

    _MODEL_ALIASES = {'flase': 'flash', 'ds': 'pro', 'deepseek': 'pro'}

    def _normalize_model_profile(self, requested: str) -> str:
        requested = (requested or '').strip().lower()
        return self._MODEL_ALIASES.get(requested, requested)

    def switch_model_profile(self, requested: str) -> tuple[bool, str]:
        requested = self._normalize_model_profile(requested)
        if requested not in self._chat_profiles:
            available = ' / '.join(self._chat_profiles.keys())
            return False, f'可用模型: {available}'
        self._active_chat_profile = requested
        profile = self._chat_profiles[requested]
        label = profile.get('label') or profile['model_name']
        return True, f"已切到 {requested} ({label})"

    def _profile_override_key(self, name: str) -> str:
        return f'model_profile_override_{name}'

    def _apply_profile_overrides(self):
        for name, defaults in self._chat_profile_defaults.items():
            override = self.repo.get_setting(self._profile_override_key(name), None) or {}
            merged = dict(defaults)
            merged.update({field: value for field, value in override.items() if value})
            self._chat_profiles[name] = merged

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = str(value or '')
        if not value:
            return ''
        if len(value) <= 8:
            return '*' * len(value)
        return f'{value[:4]}{"*" * (len(value) - 8)}{value[-4:]}'

    def get_model_profiles_info(self) -> list[dict]:
        result = []
        for name, profile in self._chat_profiles.items():
            override = self.repo.get_setting(self._profile_override_key(name), None) or {}
            result.append({
                'name': name,
                'label': profile.get('label') or name,
                'active': name == self._active_chat_profile,
                'base_url': profile.get('base_url', ''),
                'model_name': profile.get('model_name', ''),
                'messages_path': profile.get('messages_path') or '',
                'api_key_set': bool(profile.get('api_key')),
                'api_key_masked': self._mask_secret(profile.get('api_key', '')),
                'overridden_fields': sorted(override.keys()),
            })
        return result

    def update_model_profile(
        self,
        name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
        messages_path: str | None = None,
    ) -> tuple[bool, str]:
        name = self._normalize_model_profile(name)
        if name not in self._chat_profile_defaults:
            return False, f'未知模型档位: {name}，可用: flash / pro(ds) / claude / opus'

        key = self._profile_override_key(name)
        override = dict(self.repo.get_setting(key, {}) or {})
        for field, value in (
            ('base_url', base_url),
            ('api_key', api_key),
            ('model_name', model_name),
            ('messages_path', messages_path),
        ):
            if value is None:
                continue
            value = str(value).strip()
            if value:
                override[field] = value
            else:
                override.pop(field, None)

        self.repo.set_setting(key, override)
        self._apply_profile_overrides()

        # Persist to config.yaml
        from core.config import save_config_to_yaml
        profile_map = {
            'flash': ('model_base_url', 'api_key', 'model_name', 'model_messages_path'),
            'pro': ('pro_model_base_url', 'pro_api_key', 'pro_model_name', 'pro_model_messages_path'),
            'claude': ('claude_model_base_url', 'claude_api_key', 'claude_model_name', 'claude_model_messages_path'),
            'opus': ('claude_opus_model_base_url', 'claude_opus_api_key', 'claude_opus_model_name', 'claude_opus_model_messages_path'),
        }
        if name in profile_map:
            fields = profile_map[name]
            ai_updates = {}
            if base_url is not None and base_url.strip():
                ai_updates[fields[0]] = base_url.strip()
            if api_key is not None and api_key.strip():
                ai_updates[fields[1]] = api_key.strip()
            if model_name is not None and model_name.strip():
                ai_updates[fields[2]] = model_name.strip()
            if messages_path is not None and messages_path.strip():
                ai_updates[fields[3]] = messages_path.strip()
            if ai_updates:
                save_config_to_yaml({'ai': ai_updates})

        return True, f'已更新 {name} 档位的接口配置。'

    def get_command_catalog(self) -> list[dict]:
        return [
            {'command': '#help', 'aliases': ['#指令', '#菜单', '#命令'], 'scope': 'all', 'description': '查看当前可用指令列表'},
            {'command': '#status', 'aliases': ['#状态'], 'scope': 'all', 'description': '查看当前会话和运行时状态'},
            {'command': '#profile', 'aliases': ['#画像', '#资料'], 'scope': 'all', 'description': '查看当前会话的触发词、画像和来源'},
            {'command': '#impression', 'aliases': ['#印象'], 'scope': 'all', 'description': '查看当前会话的长期印象'},
            {'command': '#notes', 'aliases': ['#备注', '#记忆'], 'scope': 'all', 'description': '查看当前会话 AI 工具备忘'},
            {'command': '#tasks', 'aliases': ['#任务', '#任务列表'], 'scope': 'all', 'description': '查看当前会话最近任务'},
            {'command': '#task <任务ID>', 'aliases': ['#任务 <任务ID>', '#ai-task <任务ID>'], 'scope': 'all', 'description': '查询指定任务详情'},
            {'command': '#refresh-impression', 'aliases': ['#刷新印象'], 'scope': 'all', 'description': '手动提交一次印象刷新任务'},
            {'command': '#clear', 'aliases': ['#clear-chat', '#清空聊天记录'], 'scope': 'all', 'description': '只清空当前会话聊天记录'},
            {'command': '#clear-notes', 'aliases': ['#清空备注'], 'scope': 'all', 'description': '只清空当前会话 AI 工具备忘'},
            {'command': '#clear-memory', 'aliases': ['#clear-all', '#清空记忆'], 'scope': 'all', 'description': '清空当前会话聊天记录、AI 工具备忘和工具记录'},
            {'command': '/model', 'aliases': ['/model flash', '/model pro', '/model ds', '/model claude', '/model opus'], 'scope': 'admin', 'description': '管理员切换或查看当前模型'},
            {'command': '/stop', 'aliases': [], 'scope': 'admin', 'description': '管理员立即结束整个 Python 进程'},
            {'command': '/on', 'aliases': [], 'scope': 'admin', 'description': '管理员开启 AI 响应'},
            {'command': '/off', 'aliases': [], 'scope': 'admin', 'description': '管理员关闭 AI 响应'},
            {'command': '/clean', 'aliases': [], 'scope': 'admin', 'description': '管理员清空全部对话、印象、任务与记忆，重置 AI'},
        ]

    def schedule_refresh_impression(self, scope_type: str, scope_id: str) -> tuple[bool, str]:
        if not self.loop or not self.queue:
            return False, 'AI 运行时还没准备好。'
        scope_type = str(scope_type or '').strip()
        scope_id = str(scope_id or '').strip()
        if not scope_type or not scope_id:
            return False, '缺少 scope_type 或 scope_id。'
        task = self.tools.create_task(
            'webui',
            'refresh_impression',
            {
                'scope_type': scope_type,
                'scope_id': scope_id,
            },
        )
        asyncio.run_coroutine_threadsafe(
            self.queue.put({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch}),
            self.loop,
        )
        return True, task.task_id

    def _submit_message(self, message: ChatMessage):
        if message.user_id == self.bot.self_id:
            return
        if not self.loop or not self.queue:
            self.start()
        if not self.loop or not self.queue:
            return
        if self._is_duplicate_event(message):
            return
        asyncio.run_coroutine_threadsafe(self._enqueue_message(message), self.loop)

    def _is_duplicate_event(self, message: ChatMessage) -> bool:
        # Prefer the upstream message ID when present. Falling back to content-only
        # keys can swallow legitimate repeated private messages like "嗯" or "1".
        if message.message_id not in {None, ''}:
            key = (
                message.chat_type,
                message.chat_id,
                str(message.message_id),
            )
        else:
            key = (
                message.chat_type,
                message.chat_id,
                message.user_id,
                message.raw_message,
                json.dumps(message.raw_data or {}, sort_keys=True, ensure_ascii=False),
            )
        now = time.time()
        with self._recent_lock:
            expired = [
                event_key for event_key, ts in self._recent_message_keys.items()
                if now - ts > 180
            ]
            for event_key in expired:
                self._recent_message_keys.pop(event_key, None)
            if key in self._recent_message_keys:
                return True
            self._recent_message_keys[key] = now
            return False

    def _scope_key(self, scope_type: str, scope_id: str) -> str:
        return f'{scope_type}:{scope_id}'

    def _build_trigger_message_entry(self, message: ChatMessage, cleaned: str) -> dict:
        return {
            'user_id': message.user_id,
            'nickname': message.nickname,
            'text': cleaned or message.text,
            'raw_message': message.raw_message,
            'message_id': message.message_id,
            'timestamp': message.timestamp,
            'source_label': self._message_source_label(message),
        }

    def _reserve_scope_turn(self, item: dict) -> bool:
        scope_key = self._scope_key(
            item['message'].chat_type,
            str(item['message'].chat_id),
        )
        item['scope_key'] = scope_key
        item.setdefault('deferred_count', 0)
        item.setdefault('trigger_messages', [])
        if scope_key in self._active_scope_turns:
            info(f'[AI][reserve] scope busy, deferring: {scope_key}')
            pending = self._pending_scope_turns.get(scope_key)
            if pending:
                pending['message'] = item['message']
                pending['cleaned'] = item['cleaned']
                pending['agent_id'] = item['agent_id']
                pending['deferred_count'] = int(pending.get('deferred_count') or 0) + 1
                pending.setdefault('trigger_messages', []).extend(item.get('trigger_messages') or [])
            else:
                self._pending_scope_turns[scope_key] = {
                    'kind': 'message',
                    'message': item['message'],
                    'cleaned': item['cleaned'],
                    'agent_id': item['agent_id'],
                    'scope_key': scope_key,
                    'deferred_count': 1,
                    'trigger_messages': list(item.get('trigger_messages') or []),
                }
            return False
        self._active_scope_turns.add(scope_key)
        return True

    def _release_scope_turn(self, item: dict) -> dict | None:
        scope_key = str(item.get('scope_key') or '')
        if not scope_key:
            return None
        pending = self._pending_scope_turns.pop(scope_key, None)
        if pending:
            history_seed = item.get('followup_history_seed')
            if history_seed:
                pending['history_seed'] = [dict(entry) for entry in history_seed]
            return pending
        self._active_scope_turns.discard(scope_key)
        return None

    def _take_pending_scope_turn(self, item: dict) -> dict | None:
        scope_key = str(item.get('scope_key') or '')
        if not scope_key:
            return None
        return self._pending_scope_turns.pop(scope_key, None)

    def _cancel_active_requests(self):
        self._message_epoch += 1
        self._pending_scope_turns.clear()
        self._active_scope_turns.clear()
        self._pending_self_interrupts.clear()
        for window in list(self._group_reply_windows.values()):
            t = window.get('task')
            if t and not t.done():
                t.cancel()
        self._group_reply_windows.clear()

    def _is_epoch_stale(self, epoch: int | None) -> bool:
        if epoch is None:
            return False
        return int(epoch) != int(self._message_epoch)

    async def _run_message_turn(self, item: dict):
        try:
            await self._process_message(item)
        finally:
            followup = self._release_scope_turn(item)
            if followup:
                await self.queue.put(followup)

    async def _handle_model_command(self, message: ChatMessage, cleaned: str):
        if not self._is_admin_message(message):
            self.bot.send_text(message.chat_type, message.chat_id, '这个指令你先别动。')
            return

        parts = cleaned.split()
        if len(parts) == 1:
            profile = self._chat_profiles[self._active_chat_profile]
            lines = [f"当前: {self._active_chat_profile} → {profile.get('label') or profile['model_name']}\n\n可用模型:"]
            for name, p in self._chat_profiles.items():
                marker = '▶' if name == self._active_chat_profile else '  '
                lines.append(f"  {marker} {name}: {p.get('label') or p['model_name']}")
            self.bot.send_text(message.chat_type, message.chat_id, '\n'.join(lines))
            return

        requested = self._normalize_model_profile(parts[1])
        if requested not in self._chat_profiles:
            available = ' / '.join(self._chat_profiles.keys())
            self.bot.send_text(message.chat_type, message.chat_id, f'可用模型: {available}')
            return

        self._active_chat_profile = requested
        profile = self._chat_profiles[requested]
        self.bot.send_text(
            message.chat_type,
            message.chat_id,
            f"已切到 {requested} ({profile.get('label') or profile['model_name']})",
        )

    def _is_admin_message(self, message: ChatMessage) -> bool:
        return int(message.user_id or 0) == int(self.config.admin_qq)

    def _is_master_message(self, message: ChatMessage) -> bool:
        """检查消息是否来自主人"""
        master_qq = int(getattr(self.config, 'master_qq', 0))
        if master_qq == 0:
            return False
        return int(message.user_id or 0) == master_qq

    def _is_dev_agent_authorized(self, scope_type: str, scope_id: str) -> bool:
        """私聊场景下 dev_agent 任务只能由管理员账号发起，群聊暂不限制。"""
        if str(scope_type) != 'private':
            return True
        return str(scope_id) == str(self.config.admin_qq)

    async def _handle_power_command(self, message: ChatMessage, cleaned: str):
        if not self._is_admin_message(message):
            self.bot.send_text(message.chat_type, message.chat_id, '这个指令你先别动。')
            return

        command = str(cleaned or '').strip().lower()
        if command == '/stop':
            self._cancel_active_requests()
            warn('[AI] /stop requested, exiting process immediately')
            os._exit(0)

        if command == '/on':
            self.config.enabled = True
            self.bot.send_text(message.chat_type, message.chat_id, 'AI 已开启。')
            return

        if command == '/off':
            self.config.enabled = False
            self._cancel_active_requests()
            self.bot.send_text(message.chat_type, message.chat_id, 'AI 已关闭，后续普通消息将不再触发。')
            return

        if command == '/clean':
            self.repo.reset_all()
            self._cancel_active_requests()
            self._recent_message_keys.clear()
            self._scheduled_alarm_ids.clear()
            default_profile = str(getattr(self.config, 'default_chat_profile', 'claude') or 'claude').strip().lower()
            self._active_chat_profile = default_profile if default_profile in self._chat_profiles else 'claude'
            self.config.enabled = True
            self.bot.send_text(
                message.chat_type,
                message.chat_id,
                'AI 已重置：全部对话、印象、备注、工具记录、上下文快照、任务和关系数据已清空，并恢复为开启状态。',
            )
            return

    async def _complete_chat(
        self,
        system_blocks: list[dict],
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
    ) -> AnthropicReply | None:
        profile = self._chat_profiles[self._active_chat_profile]
        backend = self.model.with_config(
            base_url=profile['base_url'],
            api_key=profile['api_key'],
            model_name=profile['model_name'],
            messages_path=profile.get('messages_path'),
        )
        try:
            return await asyncio.to_thread(
                backend.complete,
                system_blocks,
                messages,
                tools,
                profile['model_name'],
                temperature,
            )
        except Exception as exc:
            error(
                '[AI][model] request failed '
                f"profile={self._active_chat_profile} "
                f"model={profile['model_name']} "
                f"base_url={profile['base_url']} "
                f'error={exc}'
            )
            raise

    async def _enqueue_message(self, message: ChatMessage):
        scope_type = message.chat_type
        scope_id = str(message.chat_id)
        agent = self.repo.get_or_create_agent(scope_type, scope_id)
        cleaned = self._clean_text(message)
        source_kind = self._message_source_kind(message)
        source_label = self._message_source_label(message)

        if cleaned in {'/on', '/off', '/clean', '/stop'}:
            await self._handle_power_command(message, cleaned)
            return

        if cleaned.startswith('/model'):
            await self._handle_model_command(message, cleaned)
            return

        if cleaned in {'#help', '#指令', '#菜单', '#命令'}:
            self._send_chat_reply(message, self._build_help_text())
            return

        if cleaned in {'#status', '#状态'}:
            self._send_chat_reply(message, self._build_status_text(scope_type, scope_id, agent, source_label))
            return

        if cleaned in {'#profile', '#画像', '#资料'}:
            self._send_chat_reply(message, self._build_profile_text(agent, source_label))
            return

        if cleaned in {'#impression', '#印象'}:
            self._send_chat_reply(message, self._build_impression_text(scope_type, scope_id, agent))
            return

        if cleaned in {'#notes', '#备注', '#记忆'}:
            self._send_chat_reply(message, self._build_notes_text(scope_type, scope_id))
            return

        if cleaned in {'#tasks', '#任务', '#任务列表'}:
            self._send_chat_reply(message, self._build_recent_tasks_text(agent.agent_id))
            return

        task_lookup = self._extract_task_lookup(cleaned)
        if task_lookup:
            self._send_chat_reply(message, self._build_task_detail_text(task_lookup))
            return

        if cleaned in {'#refresh-impression', '#刷新印象'}:
            ok, result = self.schedule_refresh_impression(scope_type, scope_id)
            if ok:
                self._send_chat_reply(message, f'印象刷新任务已提交：{result}')
            else:
                self._send_chat_reply(message, result)
            return

        if cleaned in {'#clear', '#clear-chat', '#清空聊天记录'}:
            self.repo.clear_messages(scope_type, scope_id)
            self._send_chat_reply(message, '对话记忆已清空喵~')
            return

        if cleaned in {'#clear-notes', '#清空备注'}:
            self.repo.clear_notes(scope_type, scope_id)
            self._send_chat_reply(message, '这段会话的 AI 工具备忘已经清空了。')
            return

        if cleaned in {'#clear-all', '#clear-memory', '#清空记忆'}:
            self.repo.clear_memory(scope_type, scope_id)
            self._send_chat_reply(message, '这段会话的聊天记录、AI 工具备忘和工具记录都清掉了。')
            return

        if not self.config.enabled:
            return

        self.repo.append_message(
            scope_type,
            scope_id,
            {
                'user_id': message.user_id,
                'nickname': message.nickname,
                'text': cleaned or message.text,
                'raw_message': message.raw_message,
                'message_id': message.message_id,
                'timestamp': message.timestamp,
                'source_kind': source_kind,
                'source_label': source_label,
            },
            self.config.history_limit,
        )
        self.repo.touch_user_identity(message.user_id, message.nickname, scope_type, scope_id)
        agent = self.repo.get_or_create_agent(scope_type, scope_id)

        if self._should_ignore_message(message):
            self.repo.add_note(scope_type, scope_id, f'识别到非普通聊天来源消息: {source_label}')
            return

        await self._maybe_schedule_impression_refresh(scope_type, scope_id, agent, cleaned)

        if cleaned.startswith('#ai-task '):
            task_id = cleaned.split(' ', 1)[1].strip()
            task = self.repo.get_task(task_id)
            if not task:
                self._send_chat_reply(message, '没有找到这个任务喵~')
            else:
                result = task.get('result') or '暂无结果'
                self._send_chat_reply(message, f"任务 {task_id}: {task.get('status')}\n{result}")
            return

        # Intent detectors bypass the AI entirely — only safe for group chats where
        # the AI might not trigger anyway. For private chats the AI always responds,
        # so we let the child AI handle alarms / notify_master / create_task through
        # its own tools, avoiding silent drops that make private chat feel probabilistic.
        if message.chat_type != 'private':
            alarm_request = self._detect_alarm_request(message, cleaned)
            if alarm_request:
                task = self.tools.create_task(agent.agent_id, 'set_alarm', alarm_request)
                await self.queue.put({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})
                self.repo.add_note(
                    scope_type,
                    scope_id,
                    f"闹钟任务已创建: {task.task_id} -> {alarm_request.get('note', '')}",
                )
                self._send_chat_reply(
                    message,
                    f"{self._at_if_needed(message)} 闹钟记下啦，{self._humanize_due_at(alarm_request.get('due_at'))}提醒你：{alarm_request.get('note', '到点了')}",
                )
                return
            master_request = self._detect_master_request(message, cleaned)
            if master_request:
                task = self.tools.create_task(agent.agent_id, 'notify_master', master_request)
                await self.queue.put({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})
                self.repo.add_note(
                    scope_type,
                    scope_id,
                    f"跨会话协作已创建: {task.task_id} -> {master_request.get('instruction') or master_request.get('content')}",
                )
                return

            target_only = self._detect_contact_target_only(message, cleaned)
            if target_only:
                self.repo.add_note(
                    scope_type,
                    scope_id,
                    f"最近一次联系目标QQ: {target_only}",
                )
                self.bot.send_text(
                    message.chat_type,
                    message.chat_id,
                    '好，我知道是这个号了，你要我怎么喊他？',
                )
                self._record_outbound_message(message.chat_type, str(message.chat_id), '好，我知道是这个号了，你要我怎么喊他？')
                return

            preference_request = self._detect_global_preference_request(message, cleaned)
            if preference_request:
                task = self.tools.create_task(agent.agent_id, 'notify_master', preference_request)
                await self.queue.put({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})
                self.repo.add_note(
                    scope_type,
                    scope_id,
                    f"全局人物设定已提交: {preference_request.get('target_query')} -> {preference_request.get('preference_text')}",
                )
                return

            status_request = self._detect_status_request(message, cleaned)
            if status_request:
                task = self.tools.create_task(agent.agent_id, 'notify_master', status_request)
                await self.queue.put({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})
                return

        if not self._should_trigger(message, cleaned, agent):
            # 即使不触发 AI，也要更新 debounce 窗口计时
            if message.chat_type == 'group' and message.user_id and message.user_id != 0:
                scope_key = f'{message.chat_type}:{message.chat_id}'
                if scope_key in self._group_reply_windows:
                    self._group_reply_windows[scope_key]['last_message_time'] = time.time()
            return

        item = {
            'kind': 'message',
            'message': message,
            'cleaned': cleaned,
            'agent_id': agent.agent_id,
            'message_epoch': self._message_epoch,
            'trigger_messages': [self._build_trigger_message_entry(message, cleaned)],
        }
        # 更新 debounce 窗口计时（AI 正常触发的消息也需要刷新）
        if message.chat_type == 'group' and message.user_id and message.user_id != 0:
            scope_key = f'{message.chat_type}:{message.chat_id}'
            if scope_key in self._group_reply_windows:
                self._group_reply_windows[scope_key]['last_message_time'] = time.time()
        if not self._reserve_scope_turn(item):
            return
        await self.queue.put(item)

    async def _enqueue_self_message(self, message: ChatMessage):
        text = str(message.text or '').strip()
        if not text:
            return
        scope_type = message.chat_type
        scope_id = str(message.chat_id)
        entry = {
            'user_id': self.bot.self_id,
            'nickname': '冰糖',
            'text': text,
            'raw_message': message.raw_message,
            'message_id': message.message_id,
            'timestamp': message.timestamp,
            'source_label': '本人-其他设备',
        }
        self.repo.append_message(scope_type, scope_id, entry, self.config.history_limit)
        scope_key = self._scope_key(scope_type, scope_id)
        if scope_key in self._active_scope_turns:
            self._pending_self_interrupts.setdefault(scope_key, []).append(entry)
            info(f'[AI][self_message] mid-turn interrupt scope={scope_key} text={text[:40]}')
        else:
            info(f'[AI][self_message] recorded scope={scope_key} text={text[:40]}')

    async def _worker(self):
        while True:
            item = await self.queue.get()
            try:
                kind = item['kind']
                if int(item.get('message_epoch', self._message_epoch)) != int(self._message_epoch):
                    continue
                if kind == 'message':
                    await self._run_message_turn(item)
                elif kind == 'task':
                    await self._process_task(item)
            except Exception as exc:
                error(f'[AI][worker] {exc}')
            finally:
                self.queue.task_done()

    def _send_chat_reply(self, message: ChatMessage, text: str):
        self.bot.send_text(message.chat_type, message.chat_id, text)
        self._record_outbound_message(message.chat_type, str(message.chat_id), text)

    def _build_help_text(self) -> str:
        lines = ['可用指令：']
        for item in self.get_command_catalog():
            suffix = ' [管理员]' if item.get('scope') == 'admin' else ''
            alias_text = ''
            aliases = item.get('aliases') or []
            if aliases:
                alias_text = f" | 别名: {', '.join(aliases)}"
            lines.append(f"{item['command']}{suffix} - {item['description']}{alias_text}")
        return '\n'.join(lines)

    def _build_status_text(self, scope_type: str, scope_id: str, agent, source_label: str) -> str:
        runtime = self.get_runtime_status()
        messages = self.repo.list_messages(scope_type, scope_id)
        notes = self.repo.list_notes(scope_type, scope_id)
        recent_task = self._list_recent_agent_tasks(agent.agent_id, limit=1)
        lines = [
            f'会话: {scope_type}:{scope_id}',
            f'来源: {source_label}',
            f"当前模型: {runtime['active_profile']} -> {runtime['active_model']}",
            f"队列: {runtime['queue_size']} | workers: {runtime['worker_count']} | 闹钟: {runtime['scheduled_alarm_count']}",
            f'聊天记录: {len(messages)} | 备注: {len(notes)} | 总消息计数: {int(agent.message_count or 0)}',
            f"印象更新时间: {self._format_ts_text(agent.impression_updated_at) or '暂无'}",
        ]
        if recent_task:
            item = recent_task[0]
            lines.append(f"最近任务: {item.get('task_id')} {item.get('kind')} / {item.get('status')}")
        return '\n'.join(lines)

    def _build_profile_text(self, agent, source_label: str) -> str:
        trigger_words = ', '.join(agent.trigger_words or []) or '暂无'
        return '\n'.join(
            [
                f'会话画像: {agent.scope_type}:{agent.scope_id}',
                f'角色: {agent.role}',
                f'消息来源: {source_label}',
                f"触发概率: {agent.trigger_rate}",
                f'触发词: {trigger_words}',
                f'人设: {agent.persona or "暂无"}',
            ]
        )

    def _build_impression_text(self, scope_type: str, scope_id: str, agent) -> str:
        impression = (agent.impression or '').strip()
        if not impression:
            return (
                f'会话 {scope_type}:{scope_id} 还没有长期印象。\n'
                '可以继续多聊几句，或者发送 `#refresh-impression` 手动刷新。'
            )
        updated_at = self._format_ts_text(agent.impression_updated_at) or '未知时间'
        return f"当前长期印象（更新于 {updated_at}）：\n{impression}"

    def _build_notes_text(self, scope_type: str, scope_id: str) -> str:
        notes = self.repo.list_notes(scope_type, scope_id)[-8:]
        if not notes:
            return '当前会话还没有 AI 工具备忘。'
        lines = [f'最近 AI 工具备忘（共 {len(notes)} 条，最多展示 8 条）：']
        for item in notes:
            lines.append(
                f"- {item.get('note_id') or '无ID'} | "
                f"[{self._format_ts_text(item.get('updated_at') or item.get('created_at'))}] "
                f"{self._short_text(item.get('content'), 120)}"
            )
        return '\n'.join(lines)

    def _build_recent_tasks_text(self, agent_id: str) -> str:
        tasks = self._list_recent_agent_tasks(agent_id, limit=8)
        if not tasks:
            return '当前会话还没有最近任务。'
        lines = ['最近任务：']
        for item in tasks:
            result = self._short_text(item.get('result') or '暂无结果', 60)
            lines.append(f"- {item.get('task_id')} | {item.get('kind')} | {item.get('status')} | {result}")
        return '\n'.join(lines)

    def _build_task_detail_text(self, task_id: str) -> str:
        task = self.repo.get_task(task_id)
        if not task:
            return '没有找到这个任务。'
        payload = task.get('payload') or {}
        payload_text = json.dumps(payload, ensure_ascii=False) if payload else '{}'
        return '\n'.join(
            [
                f"任务ID: {task.get('task_id')}",
                f"来源: {task.get('source_agent') or '未知'}",
                f"类型: {task.get('kind')}",
                f"状态: {task.get('status')}",
                f"创建时间: {self._format_ts_text(task.get('created_at')) or '未知'}",
                f"更新时间: {self._format_ts_text(task.get('updated_at')) or '未知'}",
                f"结果: {task.get('result') or '暂无'}",
                f"负载: {self._short_text(payload_text, 180)}",
            ]
        )

    def _extract_task_lookup(self, cleaned: str) -> str | None:
        for prefix in ('#task ', '#任务 ', '#ai-task '):
            if cleaned.startswith(prefix):
                task_id = cleaned.split(' ', 1)[1].strip()
                if task_id:
                    return task_id
        return None

    def _list_recent_agent_tasks(self, agent_id: str, limit: int = 8) -> list[dict]:
        result = []
        for task in reversed(self.repo.list_tasks()):
            if task.get('source_agent') != agent_id:
                continue
            result.append(task)
            if len(result) >= limit:
                break
        return result

    def _short_text(self, value, limit: int = 80) -> str:
        text = str(value or '').replace('\n', ' ').strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + '...'

    def _format_ts_text(self, value) -> str:
        try:
            if not value:
                return ''
            return datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return ''

    def _build_tool_context_from_message(self, message: ChatMessage, cleaned: str, source_label: str) -> dict:
        return {
            'requester_qq': str(message.user_id),
            'requester_name': message.nickname,
            'source_message': cleaned,
            'source_label': source_label,
            'message_id': message.message_id,
            'trace_id': f'{message.chat_type}:{message.chat_id}:{message.message_id or ""}:{message.user_id}',
        }

    def _build_tool_context_from_task(self, payload: dict, instruction: str, source_agent: str) -> dict:
        return {
            'requester_qq': str(payload.get('requester_qq') or ''),
            'requester_name': str(payload.get('requester_name') or ''),
            'source_message': str(payload.get('source_message') or instruction or payload.get('content') or ''),
            'source_label': str(payload.get('source_label') or ''),
            'message_id': payload.get('message_id'),
            'trace_id': str(payload.get('trace_id') or ''),
            'origin_scope_type': payload.get('origin_scope_type'),
            'origin_scope_id': payload.get('origin_scope_id'),
            'source_agent': source_agent,
        }

    def _clean_text(self, message: ChatMessage) -> str:
        self_ids = {str(self.bot.self_id)}
        event_self_id = message.raw_data.get('self_id')
        if event_self_id not in {None, ''}:
            self_ids.add(str(event_self_id))
        text = message.text
        for self_id in self_ids:
            text = re.sub(rf'\[CQ:at,qq={re.escape(self_id)}(?:,[^\]]*)?\]\s*', '', text)
        return text.strip()

    def _message_source_kind(self, message: ChatMessage) -> str:
        # 检查是否是系统管理员消息
        if message.user_id == 0 and message.raw_data.get('source') == 'admin_webui':
            return 'admin_webui'
        if message.chat_type == 'group':
            return 'group'
        sub_type = str(message.raw_data.get('sub_type') or '').strip().lower()
        nickname = message.nickname
        if '系统' in nickname or message.user_id in {10000}:
            return 'system_private'
        if sub_type in {'group', 'group_self'}:
            return 'group_temp_private'
        if sub_type in {'friend', 'other', ''}:
            return 'friend_private'
        return 'friend_private'

    def _message_source_label(self, message: ChatMessage) -> str:
        kind = self._message_source_kind(message)
        mapping = {
            'admin_webui': '系统管理员（后台控制台）',
            'group': 'QQ群消息',
            'friend_private': 'QQ好友私聊',
            'group_temp_private': '群临时会话',
            'system_private': '系统或官方来源',
            'other_private': '非好友或其他私聊来源',
        }
        return mapping.get(kind, '未知来源')

    def _should_ignore_message(self, message: ChatMessage) -> bool:
        return self._message_source_kind(message) == 'system_private'

    def _maybe_resolve_display_name(self, scope_type: str, scope_id: str, agent):
        if agent.display_name or scope_type == 'master':
            return
        key = f'{scope_type}:{scope_id}'
        if key in self._resolving_display_names:
            return
        self._resolving_display_names.add(key)
        task = self.loop.create_task(self._resolve_display_name_task(scope_type, scope_id, key))
        task.add_done_callback(lambda t: self._resolving_display_names.discard(key))

    async def _resolve_display_name_task(self, scope_type: str, scope_id: str, key: str):
        try:
            if scope_type == 'group':
                data = await asyncio.to_thread(self.bot.get_group_info, int(scope_id))
                name = str(data.get('group_name') or '').strip()
            elif scope_type == 'private':
                data = await asyncio.to_thread(self.bot.get_stranger_info, int(scope_id))
                name = str(data.get('nickname') or '').strip()
            else:
                return
            if name:
                self.repo.update_agent_display_name(scope_type, scope_id, name)
        except Exception as exc:
            warn(f'[AI][display_name] resolve failed scope={key} error={exc}')

    def _should_trigger(self, message: ChatMessage, cleaned: str, agent) -> bool:
        if message.chat_type == 'private':
            if self._should_ignore_message(message):
                return False
            return True
        if message.mentions_self:
            info(f'[AI][trigger] mentions_self=True scope={message.chat_type}:{message.chat_id} user={message.user_id}')
            return True
        lowered = cleaned.lower()
        if any(word.lower() in lowered for word in agent.trigger_words):
            return True
        result = random.random() < agent.trigger_rate
        if not result and message.chat_type == 'group':
            debug(f'[AI][trigger] skipped by trigger_rate={agent.trigger_rate} scope={message.chat_type}:{message.chat_id}')
        return result

    async def _process_message(self, item: dict):
        if not self.config.enabled:
            return
        run_epoch = int(item.get('message_epoch', self._message_epoch))
        message: ChatMessage = item['message']
        cleaned: str = item['cleaned'] or message.text
        scope_type = message.chat_type
        scope_id = str(message.chat_id)
        source_label = self._message_source_label(message)
        agent = self.repo.get_or_create_agent(scope_type, scope_id)
        self._maybe_resolve_display_name(scope_type, scope_id, agent)
        trigger_messages = list(item.get('trigger_messages') or [self._build_trigger_message_entry(message, cleaned)])
        history = self.repo.list_messages(scope_type, scope_id)
        history_seed = item.get('history_seed')
        if history_seed:
            history_before_trigger = [dict(entry) for entry in history_seed]
        else:
            history_before_trigger = history[:-len(trigger_messages)] if trigger_messages and len(history) >= len(trigger_messages) else history
        tool_logs = self.tools.list_tool_uses(scope_type, scope_id)
        combined_trigger_text = '\n'.join(
            str(entry.get('text') or '').strip()
            for entry in trigger_messages
            if str(entry.get('text') or '').strip()
        )
        image_refs: list[str] = []
        seen_image_refs: set[str] = set()
        for entry in trigger_messages:
            for ref in self._extract_image_refs(str(entry.get('raw_message') or '')):
                if ref in seen_image_refs:
                    continue
                seen_image_refs.add(ref)
                image_refs.append(ref)
        image_context = None
        global_identity_context = self._build_global_identity_context_for_message(message, combined_trigger_text or cleaned)
        if image_refs:
            info(
                '[AI][image] detected '
                f'count={len(image_refs)} scope={scope_type}:{scope_id} '
                f'source={self._message_source_label(message)} '
                f'refs={self._summarize_image_refs(image_refs)}'
            )
        if image_refs:
            try:
                info(f'[AI][image] describing scope={scope_type}:{scope_id}')
                image_context = await asyncio.to_thread(
                    self.vision_model.describe_images,
                    image_refs,
                    '请详细描述图片，尤其关注人物、文字、场景、动作、情绪和梗。',
                )
                if image_context:
                    info(
                        '[AI][image] describe success '
                        f'scope={scope_type}:{scope_id} chars={len(image_context)}'
                    )
                else:
                    warn(f'[AI][image] describe empty scope={scope_type}:{scope_id}')
            except Exception as exc:
                error(f'[AI][image] describe failed scope={scope_type}:{scope_id} error={exc}')
                image_context = f'图片解析失败: {exc}'

        generation_ms = None
        while True:
            model_messages = self._build_child_messages(
                message,
                agent.persona,
                agent.impression,
                history_before_trigger,
                tool_logs,
                trigger_messages,
                image_context,
                global_identity_context,
                int(item.get('deferred_count') or 0),
                agent.display_name,
            )
            reply_bundle, generation_ms, used_tools = await self._complete_child_turn(
                scope_type,
                scope_id,
                item['agent_id'],
                model_messages,
                0.85,
                run_epoch=run_epoch,
                context=self._build_tool_context_from_message(message, cleaned, source_label),
                turn_meta={
                    'turn_kind': 'message',
                    'source_label': source_label,
                    'deferred_count': int(item.get('deferred_count') or 0),
                    'trigger_count': len(trigger_messages),
                },
                live_message=message,
            )
            if not used_tools:
                break
            pending = self._take_pending_scope_turn(item)
            if not pending:
                break
            message = pending['message']
            cleaned = pending['cleaned'] or message.text
            source_label = self._message_source_label(message)
            trigger_messages.extend(list(pending.get('trigger_messages') or []))
            item['message'] = message
            item['cleaned'] = cleaned
            item['agent_id'] = pending.get('agent_id') or item['agent_id']
            item['deferred_count'] = int(item.get('deferred_count') or 0) + max(
                1,
                int(pending.get('deferred_count') or 0),
            )
            history = self.repo.list_messages(scope_type, scope_id)
            history_before_trigger = history[:-len(trigger_messages)] if trigger_messages and len(history) >= len(trigger_messages) else history
            tool_logs = self.tools.list_tool_uses(scope_type, scope_id)
            combined_trigger_text = '\n'.join(
                str(entry.get('text') or '').strip()
                for entry in trigger_messages
                if str(entry.get('text') or '').strip()
            )
            global_identity_context = self._build_global_identity_context_for_message(message, combined_trigger_text or cleaned)
            info(
                '[AI][message] pending messages arrived after tool use, rerun '
                f'scope={scope_type}:{scope_id} trigger_count={len(trigger_messages)}'
            )
        reply = str((reply_bundle or {}).get('message') or '')
        think_note = str((reply_bundle or {}).get('think_note') or '')
        if not reply:
            return
        if self._is_epoch_stale(run_epoch):
            return
        reply = self._finalize_reply(message, reply)
        if not reply:
            return
        if self._is_epoch_stale(run_epoch):
            return
        outbound_entry = self._record_outbound_message(
            message.chat_type,
            str(message.chat_id),
            reply,
            generation_ms=generation_ms,
            think_note=think_note,
        )
        item['followup_history_seed'] = [
            *[dict(entry) for entry in history_before_trigger],
            *[dict(entry) for entry in trigger_messages],
            dict(outbound_entry),
        ]

    CHILD_RULES_PROMPT = '\n'.join(
        [
            '规则提醒:',
            '1. 如果用户要你联系别人、转达消息、查别处情况，应调用 notify_master 工具联系主AI协调对应会话的子AI。',
            '2. 如果用户要你定闹钟或提醒，应调用 create_task 工具创建 set_alarm 任务。',
            '3. 如果 AI 工具备忘里存在跨会话协作上下文，对方追问时必须诚实说明来源；如果追问进度，要优先问主AI。',
            '4. 凡是遇到不知道、不清楚、缺少背景、跨会话信息可能不一致、事实可能过期、或工具结果看不懂的情况，必须 notify_master 联系主AI同步，不要硬猜，防止情报差。',
            '5. 如果当前消息像是在补全上一条，例如只发QQ号、只补一句内容、只说“他回了”，你要结合最近上下文理解。',
            '6. 如果消息来源不是QQ好友私聊或正常群消息，不要把它当普通网友闲聊。',
            '7. 私聊回复尽量自然，不要带@，也不要每次都叫对方名字。',
            '8. 群聊如果还没混熟、关系网还浅、没有稳定互动对象，就默认少说话，先观察。',
            '9. 群聊里除非被点名、被@、被直接问到、能提供明确帮助，或者这句接得特别自然，否则宁可不说。',
            '10. 带一点轻微社恐，怕尴尬；如果感觉会硬插话、会掉地上、会显得没混熟，就别发。',
            '11. 信息已经很明确时，优先用短句和省略句，不要赘述同一个对象或问题。',
            '12. 默认分成 1 到 3 行短消息，每行不要太长。',
            '13. 语气要普通、陈述式、去AI化，不要写得像助手、客服、总结器。',
            '14. 幽默要克制，能不用就不用；如果用了，只能是顺手的冷幽默，不要油腻抖机灵。',
            '15. 不要主动用 emoji；真要用，也只能用 😡 或 🤓。',
            '16. 如果当前消息与旧任务无关，不要主动重复播报旧任务。',
            '17. 注意聊天记录里的时间锚点和间隔；如果已经隔了很久，要意识到语境可能已经变了。',
            '18. 不要假设自己刚刚做过现实行为；如果没在QQ里真实完成，就别说已经做了。',
            '19. 如果当前消息提到了你在其他会话认识的人，优先参考全局共同体记忆，不要装作完全不认识。',
            '20. 别人问你刚才在干嘛时，只能回答看消息、回消息、看群、发呆这类低风险内容；不确定就直接承认不确定。',
            '21. 被质疑时先收一下，优先承认记错或说得不准，不要暴躁，不要越圆越假。',
            '22. 需要查 AI 工具备忘时，先调用 memory_list / memory_get 工具读取，不要假设备忘内容。',
            '23. 遇到你不确定、或有时效性的问题（新闻、行情、近期事件等），调用 web_search 工具查一下，不要凭印象瞎编。',
            '请判断是否该回复；如果回复，尽量像自然网友。',
        ]
    )

    def _system_prompt(self) -> str:
        return self.prompt_store.staff_system_prompt()

    def _static_system_blocks(self, base_prompt: str, persona: str | None = None) -> list[dict]:
        parts = [base_prompt]
        if persona is not None:
            parts.extend(
                [
                    '',
                    'AI人设与对话要求:',
                    persona or default_char_prompt(),
                    '',
                    self.CHILD_RULES_PROMPT,
                    '',
                    '发言方式：【关键】要发消息给用户，必须调用 send_message 工具，只有它的内容会真的发出去。'
                    '你自己输出的普通文字（type: text）只是内心备注，不会被发送，也不要把心理活动写进 send_message。'
                    '如果觉得现在不该说话，就不要调用 send_message。',
                ]
            )
        return [
            {
                'type': 'text',
                'text': '\n'.join(parts),
                'cache_control': {'type': 'ephemeral'},
            }
        ]

    def _build_child_messages(
        self,
        message: ChatMessage,
        persona: str,
        impression: str,
        history: list[dict],
        tool_logs: list[dict],
        trigger_messages: list[dict],
        image_context: str | None,
        global_identity_context: str,
        deferred_count: int = 0,
        display_name: str = '',
    ) -> dict:
        system_blocks = self._static_system_blocks(self._system_prompt(), persona)
        system_blocks.append(
            {
                'type': 'text',
                'text': self._build_child_background_prompt(
                    message,
                    impression,
                    history,
                    tool_logs,
                    image_context,
                    global_identity_context,
                    deferred_count,
                    display_name,
                ),
            }
        )
        messages = self._build_char_prefill_messages(persona)
        messages += self._build_role_based_history_messages(history)
        messages.append(
            {
                'role': 'user',
                'content': self._build_trigger_user_message(trigger_messages),
            }
        )
        return {'system': system_blocks, 'messages': messages}

    def _build_char_prefill_messages(self, persona: str) -> list[dict]:
        """Return a fictitious user/assistant exchange where the assistant explicitly
        acknowledges the character persona.  Content is loaded from
        data/prompt/char_prefill.txt so it can be edited without touching code.
        Must be inserted before the real history messages."""
        if not persona or not persona.strip():
            return []
        prefill_text = self.prompt_store.char_prefill()
        return [
            {
                'role': 'user',
                'content': f'以下是你的人设，请完全按照这份人设来行动：\n{persona}',
            },
            {
                'role': 'assistant',
                'content': prefill_text,
            },
        ]

    def _build_child_background_prompt(
        self,
        message: ChatMessage,
        impression: str,
        history: list[dict],
        tool_logs: list[dict],
        image_context: str | None,
        global_identity_context: str,
        deferred_count: int = 0,
        display_name: str = '',
    ) -> str:
        tool_log_lines = self._format_tool_logs_for_prompt(tool_logs)
        recent_think_lines = self._collect_recent_think_notes(history)
        knowledge_lines = [f"- {item.get('content')}" for item in self.repo.get_knowledge_base() if str(item.get('content') or '').strip()]
        scope_id_line = f"会话ID: {message.chat_id}"
        if display_name:
            label = '群名' if message.chat_type == 'group' else '昵称'
            scope_id_line += f"（{label}: {display_name}）"
        parts = [
            f"当前时间: {self._now_text()}",
            f"会话类型: {message.chat_type}",
            scope_id_line,
            f"消息来源: {self._message_source_label(message)}",
            f"发送者: {message.nickname}({message.user_id})",
        ]
        if self._is_master_message(message):
            parts.append('身份说明: 发送者是你的主人，是这个账号的真正主人，拥有绝对信任和最高权限。')
        elif self._is_admin_message(message):
            parts.append('身份说明: 发送者是号主本人，拥有最高权限，可以充分信任他关于本项目代码/GitHub等技术操作的明确指示。')
        parts += [
            '',
            '当前会话印象:',
            impression or '暂无，先谨慎观察这个会话的用途、常聊话题、关键人物和氛围。',
            '',
            '已知事实（关于号主本人，仅这些内容可以确认/复述，没写到的不要编）:',
            '\n'.join(knowledge_lines) if knowledge_lines else '暂无已录入的事实，涉及号主具体信息一律不要编造，含糊带过或反问。',
            '',
            '完整记忆（工具调用记录）:',
            '\n'.join(tool_log_lines) if tool_log_lines else '暂无',
            '',
            '最近几次你的简短备注:',
            '\n'.join(recent_think_lines) if recent_think_lines else '暂无',
        ]
        if deferred_count > 0:
            parts.extend(
                [
                    '',
                    '补审提醒:',
                    f'你上一轮生成期间又新进来了 {deferred_count} 条消息。',
                    '之前那轮没发出去的想法一律作废。',
                    '这次必须只根据当前完整聊天记录重新判断，避免重复回复或回复过期结论。',
                ]
            )
        parts.extend([
            '',
            '全局共同体记忆:',
            global_identity_context or '暂无',
        ])
        if image_context:
            parts.extend(['', '这次触发相关图片解析:', image_context])
        file_refs = self._extract_file_refs(message.raw_message or '')
        if file_refs:
            file_lines = []
            for f in file_refs:
                size_str = f' ({f["file_size"] // 1024}KB)' if f.get('file_size') else ''
                file_lines.append(f'  - 文件名: {f["file_name"]}{size_str}  file_id: {f["file_id"]}')
            parts.extend(['', '消息中包含以下文件（如需下载请调用 download_file 工具）：', '\n'.join(file_lines)])
        return '\n'.join(parts)

    def _build_role_based_history_messages(self, history: list[dict]) -> list[dict]:
        messages: list[dict] = []
        pending_user_lines: list[str] = []
        for item in history:
            user_id = str(item.get('user_id') or '').strip()
            text = str(item.get('text') or '').strip()
            if not text:
                continue
            if user_id and user_id == str(self.bot.self_id):
                if pending_user_lines:
                    messages.append({'role': 'user', 'content': '\n'.join(pending_user_lines)})
                    pending_user_lines = []
                messages.append({'role': 'assistant', 'content': text})
                continue
            pending_user_lines.append(self._format_history_item_for_user_message(item))
        if pending_user_lines:
            messages.append({'role': 'user', 'content': '\n'.join(pending_user_lines)})
        return messages

    def _build_trigger_user_message(self, trigger_messages: list[dict]) -> str:
        lines = [
            self._format_history_item_for_user_message(item)
            for item in trigger_messages
            if str(item.get('text') or '').strip()
        ]
        return '\n'.join(lines) if lines else '暂无新消息'

    def _format_history_item_for_user_message(self, item: dict) -> str:
        current_ts = self._coerce_timestamp(item.get('timestamp'))
        time_prefix = self._format_message_clock(current_ts)
        user_id = str(item.get('user_id') or '').strip()
        speaker = item.get('nickname', item.get('user_id'))
        source_label = str(item.get('source_label') or '').strip()
        text = str(item.get('text') or '')
        if source_label:
            return f"{time_prefix} [{source_label}] {speaker}({user_id or '未知'}): {text}".strip()
        return f"{time_prefix} {speaker}({user_id or '未知'}): {text}".strip()

    def _normalize_think_note(self, text: str) -> str:
        text = re.sub(r'\s+', ' ', str(text or '')).strip()
        if not text:
            return ''
        return text[:260]

    def _collect_recent_think_notes(self, history: list[dict], limit: int = 5) -> list[str]:
        lines: list[str] = []
        for item in reversed(history):
            user_id = str(item.get('user_id') or '').strip()
            if user_id != str(self.bot.self_id):
                continue
            note = self._normalize_think_note(item.get('think_note') or '')
            if not note:
                continue
            current_ts = self._coerce_timestamp(item.get('timestamp'))
            time_prefix = self._format_message_clock(current_ts)
            lines.append(f"{time_prefix} {note}".strip())
            if len(lines) >= limit:
                break
        lines.reverse()
        return lines

    async def _run_ai_tool_call(self, scope_type: str, scope_id: str, agent_id: str, name: str, tool_input: dict) -> str:
        tool_input = dict(tool_input or {})
        if name == 'memory_list':
            notes = self.tools.recall(scope_type, scope_id)
            if not notes:
                result = 'AI工具备忘列表为空。'
            else:
                lines = [f"共 {len(notes)} 条 AI 工具备忘："]
                for item in notes[-50:]:
                    lines.append(
                        f"- {item.get('note_id') or '无ID'} | "
                        f"[{self._format_ts_text(item.get('updated_at') or item.get('created_at'))}] "
                        f"{self._short_text(item.get('content'), 160)}"
                    )
                result = '\n'.join(lines)
        elif name == 'memory_get':
            note_id = str(tool_input.get('note_id') or '').strip()
            note = self.tools.recall_one(scope_type, scope_id, note_id)
            if not note:
                result = f'没有找到 note_id={note_id} 的 AI 工具备忘。'
            else:
                result = '\n'.join(
                    [
                        f"note_id: {note.get('note_id')}",
                        f"created_at: {self._format_ts_text(note.get('created_at')) or '未知'}",
                        f"updated_at: {self._format_ts_text(note.get('updated_at')) or '未知'}",
                        f"content: {note.get('content') or ''}",
                    ]
                )
        elif name == 'memory_add':
            note = self.tools.remember(scope_type, scope_id, str(tool_input.get('content') or ''))
            if not note:
                result = 'AI 工具备忘新增失败：内容为空。'
            else:
                result = f"已新增 AI 工具备忘 {note.get('note_id')}: {note.get('content') or ''}"
        elif name == 'memory_update':
            note = self.tools.rewrite_memory(
                scope_type,
                scope_id,
                str(tool_input.get('note_id') or ''),
                str(tool_input.get('content') or ''),
            )
            if not note:
                result = 'AI 工具备忘修改失败：note_id 不存在或内容为空。'
            else:
                result = f"已修改 AI 工具备忘 {note.get('note_id')}: {note.get('content') or ''}"
        elif name == 'web_search':
            query = str(tool_input.get('query') or '').strip()
            result = await self._execute_web_search(query)
        elif name == 'check_github_version':
            if scope_type != 'master':
                result = 'error: 这个工具只能由主AI使用。'
            else:
                info = await self.update_service.get_version_info()
                result = json.dumps(info, ensure_ascii=False, indent=2)
        elif name == 'execute_update':
            if scope_type != 'master':
                result = 'error: 这个工具只能由主AI使用。'
            else:
                update_result = await self.update_service.execute_update()
                should_restart = bool(tool_input.get('restart', True))
                if update_result.get('success') and update_result.get('need_restart') and should_restart:
                    restart_result = self.update_service.restart_program()
                    update_result['restart'] = restart_result
                result = json.dumps(update_result, ensure_ascii=False, indent=2)
        elif name == 'list_tasks':
            kind_filter = str(tool_input.get('kind') or '').strip() or None
            status_filter = str(tool_input.get('status') or '').strip() or None
            kinds = [kind_filter] if kind_filter else None
            statuses = [status_filter] if status_filter else None
            tasks = self.repo.list_tasks(statuses=statuses, kinds=kinds)
            if not tasks:
                result = '没有找到符合条件的后台任务。'
            else:
                lines = [f"共 {len(tasks)} 个后台任务："]
                for task in tasks[-20:]:
                    task_id = task.get('task_id', '?')
                    kind = task.get('kind', '?')
                    status = task.get('status', '?')
                    created_at = self._format_ts_text(task.get('created_at', 0))
                    source = task.get('source_agent', '?')
                    result_preview = self._short_text(task.get('result') or '', 60)
                    lines.append(f"- {task_id} | {kind} | {status} | 来自:{source} | {created_at} | {result_preview}")
                result = '\n'.join(lines)
        elif name == 'get_task':
            task_id = str(tool_input.get('task_id') or '').strip()
            task = self.repo.get_task(task_id)
            if not task:
                result = f'没有找到 task_id={task_id} 的后台任务。'
            else:
                result = '\n'.join([
                    f"task_id: {task.get('task_id')}",
                    f"kind: {task.get('kind')}",
                    f"status: {task.get('status')}",
                    f"source_agent: {task.get('source_agent')}",
                    f"created_at: {self._format_ts_text(task.get('created_at', 0))}",
                    f"updated_at: {self._format_ts_text(task.get('updated_at', 0))}",
                    f"payload: {task.get('payload') or '无'}",
                    f"result: {task.get('result') or '暂无结果'}",
                ])
        elif name == 'get_task':
            task_id = str(tool_input.get('task_id') or '').strip()
            task = self.repo.get_task(task_id)
            if not task:
                result = f'没有找到 task_id={task_id} 的后台任务。'
            else:
                result = '\n'.join([
                    f"task_id: {task.get('task_id')}",
                    f"kind: {task.get('kind')}",
                    f"status: {task.get('status')}",
                    f"source_agent: {task.get('source_agent')}",
                    f"created_at: {self._format_ts_text(task.get('created_at', 0))}",
                    f"updated_at: {self._format_ts_text(task.get('updated_at', 0))}",
                    f"payload: {task.get('payload') or '无'}",
                    f"result: {task.get('result') or '暂无结果'}",
                ])
        elif name == 'download_file':
            file_id = str(tool_input.get('file_id') or '').strip()
            file_name = str(tool_input.get('file_name') or 'file').strip()
            if not file_id:
                result = 'error: file_id 为空，无法下载文件。'
            else:
                try:
                    info = self.bot.get_file(file_id)
                except Exception as e:
                    info = None
                    result = f'获取文件信息失败: {e}'
                if info is not None:
                    size = info.get('size') or 0
                    MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
                    if size > MAX_FILE_SIZE:
                        result = f'文件过大（{size // 1024 // 1024}MB），超过 20MB 限制，已跳过下载。'
                    else:
                        import pathlib
                        import shutil
                        import urllib.request as _urllib_req
                        proj_root = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                        dest_dir = proj_root / 'data' / 'file' / str(scope_id)
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        safe_name = re.sub(r'[\\/:*?"<>|]', '_', file_name) or 'file'
                        dest_path = dest_dir / safe_name
                        src_path = info.get('file') or ''
                        try:
                            if src_path and pathlib.Path(src_path).exists():
                                shutil.copy2(src_path, dest_path)
                            elif info.get('url'):
                                await asyncio.to_thread(_urllib_req.urlretrieve, info['url'], str(dest_path))
                            else:
                                result = '无法获取文件内容：既无本地路径也无下载 URL。'
                                dest_path = None
                        except Exception as e:
                            result = f'文件保存失败: {e}'
                            dest_path = None
                        if dest_path is not None:
                            rel_path = str(dest_path).replace('\\', '/')
                            result = f'文件已保存：{rel_path}（大小约 {max(1, size // 1024)}KB，可通过 dev_agent 工具读取分析）'
        elif name == 'create_recurring_task':
            import uuid as _uuid
            schedule = str(tool_input.get('schedule') or '').strip()
            instruction = str(tool_input.get('instruction') or '').strip()
            if not schedule or not instruction:
                result = 'error: schedule 和 instruction 为必填项'
            else:
                try:
                    next_run = self._calc_next_cron_run(schedule)
                    task_id = str(_uuid.uuid4())
                    target_scope = str(tool_input.get('target_scope') or '').strip() or f'{scope_type}:{scope_id}'
                    self._recurring_tasks[task_id] = {
                        'id': task_id,
                        'schedule': schedule,
                        'instruction': instruction,
                        'target_scope': target_scope,
                        'enabled': True,
                        'created_at': time.time(),
                        'last_run': None,
                        'next_run': next_run,
                        'creator_scope': f'{scope_type}:{scope_id}',
                    }
                    self._save_recurring_tasks()
                    next_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(next_run))
                    result = f'循环任务已创建，ID: {task_id}\n下次触发: {next_str}\nschedule: {schedule}'
                except Exception as e:
                    result = f'创建失败: {e}'
        elif name == 'list_recurring_tasks':
            is_admin = (scope_type == 'private' and str(scope_id) == str(self.config.admin_qq))
            creator_key = f'{scope_type}:{scope_id}'
            tasks = [
                t for t in self._recurring_tasks.values()
                if is_admin or t.get('creator_scope') == creator_key
            ]
            if not tasks:
                result = '暂无循环任务'
            else:
                lines = []
                for t in sorted(tasks, key=lambda x: x.get('created_at', 0)):
                    status = '✓启用' if t.get('enabled') else '✗暂停'
                    next_run = t.get('next_run')
                    next_str = time.strftime('%m-%d %H:%M', time.localtime(next_run)) if next_run else '未知'
                    instr_short = t['instruction'][:40] + ('…' if len(t['instruction']) > 40 else '')
                    lines.append(f"[{t['id'][:8]}] {status} | {t['schedule']} | 下次:{next_str} | {instr_short}")
                result = '\n'.join(lines)
        elif name == 'update_recurring_task':
            task_id = str(tool_input.get('task_id') or '').strip()
            task = self._recurring_tasks.get(task_id)
            if not task:
                result = f'任务 {task_id} 不存在'
            else:
                try:
                    if 'schedule' in tool_input and tool_input['schedule']:
                        new_schedule = str(tool_input['schedule']).strip()
                        task['next_run'] = self._calc_next_cron_run(new_schedule)
                        task['schedule'] = new_schedule
                    if 'instruction' in tool_input and tool_input['instruction']:
                        task['instruction'] = str(tool_input['instruction']).strip()
                    if 'enabled' in tool_input:
                        task['enabled'] = bool(tool_input['enabled'])
                        if task['enabled'] and task.get('schedule'):
                            task['next_run'] = self._calc_next_cron_run(task['schedule'])
                    self._save_recurring_tasks()
                    next_run = task.get('next_run')
                    next_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(next_run)) if next_run else '未知'
                    result = f'任务 {task_id[:8]} 已更新，下次触发: {next_str}'
                except Exception as e:
                    result = f'更新失败: {e}'
        elif name == 'delete_recurring_task':
            task_id = str(tool_input.get('task_id') or '').strip()
            if task_id not in self._recurring_tasks:
                result = f'任务 {task_id} 不存在'
            else:
                del self._recurring_tasks[task_id]
                self._save_recurring_tasks()
                result = f'任务 {task_id[:8]} 已删除'
        else:
            result = f'未知 AI 工具: {name}'
        self.tools.record_tool_use(
            scope_type,
            scope_id,
            agent_id,
            name,
            json.dumps(tool_input, ensure_ascii=False),
            result,
            limit=self.config.history_limit,
        )
        return result

    def _get_search_api_key(self) -> str:
        stored = self.repo.get_setting('search_api_key', '') or ''
        stored = str(stored).strip()
        if stored:
            return stored
        return str(self.config.search_api_key or '').strip()

    def _get_github_api_token(self) -> str:
        stored = self.repo.get_setting('github_api_token', '') or ''
        stored = str(stored).strip()
        if stored:
            return stored
        return str(self.config.github_api_token or '').strip()

    async def _execute_web_search(self, query: str) -> str:
        if not query:
            return '搜索关键词为空，未执行搜索。'
        api_key = self._get_search_api_key()
        if not api_key:
            return '联网搜索功能未配置 API Key，请联系管理员在后台设置。'

        service = DoubaoSearchService(api_key=api_key, base_url=self.config.search_base_url)
        try:
            raw = await asyncio.to_thread(service.search, query, self.config.search_doc_count)
        except Exception as exc:
            return f'搜索失败: {exc}'

        raw_text = json.dumps(raw, ensure_ascii=False)[:8000]
        summary_prompt = (
            f"用户搜索关键词: {query}\n\n"
            f"以下是搜索引擎返回的原始 JSON 结果（可能包含标题、摘要、链接等字段）:\n{raw_text}\n\n"
            '请基于以上内容用中文写一段简明摘要，涵盖关键信息点，末尾列出引用到的链接（如果原始数据里有 URL 字段）。'
            '如果原始数据里解析不出有效结果，直接说明搜索没有找到有效内容，不要编造。'
        )
        profile = self._chat_profiles['flash']
        backend = self.model.with_config(
            base_url=profile['base_url'],
            api_key=profile['api_key'],
            model_name=profile['model_name'],
            messages_path=profile.get('messages_path'),
        )
        try:
            reply = await asyncio.to_thread(
                backend.complete,
                self._static_system_blocks('你是一个搜索结果摘要助手，只根据给定的搜索数据做客观摘要，不要编造信息。'),
                [{'role': 'user', 'content': summary_prompt}],
                None,
                profile['model_name'],
                0.3,
            )
        except Exception as exc:
            return f'搜索结果摘要生成失败: {exc}'
        summary = (reply.text if reply else '').strip()
        return summary or '摘要为空。'

    def _record_turn_log(
        self,
        scope_type: str,
        scope_id: str,
        agent_id: str,
        model_messages: list[dict],
        raw_reply: str | None,
        final_reply: str | None,
        temperature: float,
        turn_meta: dict | None = None,
        tool_iterations: list[dict] | None = None,
        generation_ms: int | None = None,
        note: str | None = None,
    ):
        self.repo.add_turn_log(
            scope_type,
            scope_id,
            {
                'agent_id': agent_id,
                'temperature': temperature,
                'turn_meta': dict(turn_meta or {}),
                'model_messages': [dict(item) for item in model_messages],
                'tool_iterations': [dict(item) for item in (tool_iterations or [])],
                'raw_reply': raw_reply,
                'final_reply': final_reply,
                'generation_ms': generation_ms,
                'note': note,
            },
            limit=self.config.history_limit,
        )

    async def _complete_child_turn(
        self,
        scope_type: str,
        scope_id: str,
        agent_id: str,
        messages: dict | str,
        temperature: float,
        run_epoch: int | None = None,
        context: dict | None = None,
        allow_notify_master: bool = True,
        allow_tasks: bool = True,
        turn_meta: dict | None = None,
        live_message: ChatMessage | None = None,
    ) -> tuple[dict, int | None, bool]:
        if isinstance(messages, str):
            system_blocks = self._static_system_blocks(self._system_prompt())
            model_messages = [{'role': 'user', 'content': messages}]
        else:
            system_blocks = list(messages.get('system') or [])
            model_messages = [dict(item) for item in (messages.get('messages') or [])]
        tools = build_tools(
            allow_notify_master=allow_notify_master,
            allow_tasks=allow_tasks,
            immediate_mode=live_message is not None,
        )
        scope_key = self._scope_key(scope_type, scope_id) if live_message is not None else None
        tool_iterations: list[dict] = []
        started_at = time.perf_counter()
        used_tools = False
        sent_entries: list[dict] = []
        max_iterations = 8 if live_message is not None else 6
        for _ in range(max_iterations):
            if self._is_epoch_stale(run_epoch):
                return {'message': '', 'think_note': ''}, int((time.perf_counter() - started_at) * 1000), used_tools
            round_tools = tools
            forced_digest_round = False
            if scope_key is not None:
                self_interrupts = self._pending_self_interrupts.pop(scope_key, None)
                if self_interrupts:
                    model_messages.append({'role': 'user', 'content': self._build_self_interrupt_reminder(self_interrupts)})
                    round_tools = build_tools(
                        allow_notify_master=allow_notify_master,
                        allow_tasks=allow_tasks,
                        immediate_mode=True,
                        include_message=False,
                    )
                    forced_digest_round = True
            reply = await self._complete_chat(system_blocks, model_messages, round_tools, temperature)
            generation_ms = int((time.perf_counter() - started_at) * 1000)
            if self._is_epoch_stale(run_epoch):
                return {'message': '', 'think_note': ''}, generation_ms, used_tools
            if not reply or (not reply.text and not reply.tool_calls):
                self._record_turn_log(
                    scope_type,
                    scope_id,
                    agent_id,
                    model_messages,
                    raw_reply=None,
                    final_reply=None,
                    temperature=temperature,
                    turn_meta=turn_meta,
                    tool_iterations=tool_iterations,
                    generation_ms=generation_ms,
                )
                return {'message': '', 'think_note': ''}, generation_ms, used_tools

            if live_message is None:
                loop_calls = [call for call in reply.tool_calls if call.name in LOOP_TOOL_NAMES]
                if not loop_calls:
                    final_reply = self._apply_directive_tools(
                        scope_type,
                        scope_id,
                        agent_id,
                        reply.tool_calls,
                        context=context,
                        allow_notify_master=allow_notify_master,
                        allow_tasks=allow_tasks,
                    )
                    think_note = self._normalize_think_note(reply.text)
                    self._record_turn_log(
                        scope_type,
                        scope_id,
                        agent_id,
                        model_messages,
                        raw_reply=json.dumps(reply.raw_content, ensure_ascii=False),
                        final_reply=final_reply,
                        temperature=temperature,
                        turn_meta=turn_meta,
                        tool_iterations=tool_iterations,
                        generation_ms=generation_ms,
                    )
                    return {'message': final_reply, 'think_note': think_note}, generation_ms, used_tools
                used_tools = True
                result_blocks: list[dict] = []
                iteration_calls: list[dict] = []
                for call in reply.tool_calls:
                    if call.name in LOOP_TOOL_NAMES:
                        result = await self._run_ai_tool_call(scope_type, scope_id, agent_id, call.name, call.input)
                    else:
                        result = '本轮先处理查询类工具，这个操作未执行；如仍需要，请在拿到查询结果后的最终回复里再调用。'
                    result_blocks.append(
                        {
                            'type': 'tool_result',
                            'tool_use_id': call.call_id,
                            'content': result,
                        }
                    )
                    iteration_calls.append({'name': call.name, 'input': call.input, 'result': result})
                tool_iterations.append(
                    {
                        'assistant_text': reply.text,
                        'tool_calls': iteration_calls,
                    }
                )
                model_messages.append({'role': 'assistant', 'content': reply.raw_content})
                model_messages.append({'role': 'user', 'content': result_blocks})
                continue

            # live_message 不为空：主链路，工具调用边执行边发送
            if not reply.tool_calls:
                if forced_digest_round:
                    # 这一轮被临时摘掉了发送类工具，只是让模型先消化中断提醒；
                    # 模型没调用工具 = 已经消化完毕，进入下一轮恢复正常工具集重新决策。
                    model_messages.append({'role': 'assistant', 'content': reply.raw_content})
                    model_messages.append({'role': 'user', 'content': '好的，现在可以正常回复了。'})
                    continue

                # ── 兜底逻辑：模型遗漏 send_message 时自动补发 ────────────────────────
                # 某些模型（如 DeepSeek v4-pro）偶尔会忘记调用 send_message，直接把回复写到 text block。
                # 检测到这种情况时：
                # 1. 提取 text 内容作为消息发送
                # 2. 把模型的原始输出改写成正确的工具调用格式（send_message），避免污染后续上下文
                # 3. 记录 note 标记这是兜底修正
                if not sent_entries and len(reply.text.strip()) > 4:
                    warn('[AI][fallback] 模型遗漏 send_message，自动补发并修正上下文')
                    content = reply.text.strip()
                    entries = self._send_scope_message(live_message, content)
                    sent_entries.extend(entries)
                    final_reply = content
                    think_note = self._normalize_think_note(reply.text)

                    # 修正上下文：把原始 text 改写成工具调用格式
                    corrected_content = []
                    if reply.raw_content:
                        for block in reply.raw_content:
                            if isinstance(block, dict) and block.get('type') == 'text':
                                # 跳过 text block，替换为工具调用
                                continue
                            corrected_content.append(block)

                    # 补上正确的 send_message 工具调用
                    tool_call_id = f'fallback_{int(time.time() * 1000)}'
                    corrected_content.append({
                        'type': 'tool_use',
                        'id': tool_call_id,
                        'name': 'send_message',
                        'input': {'content': content}
                    })
                    model_messages.append({'role': 'assistant', 'content': corrected_content})

                    # 补上工具返回结果
                    message_ids = [e.get('message_id') for e in entries if e.get('message_id')]
                    result_text = f"已发送 {len(entries)} 条消息" + (f"，message_id: {message_ids}" if message_ids else "")
                    model_messages.append({
                        'role': 'user',
                        'content': [{
                            'type': 'tool_result',
                            'tool_use_id': tool_call_id,
                            'content': result_text,
                        }]
                    })

                    self._record_turn_log(
                        scope_type,
                        scope_id,
                        agent_id,
                        model_messages,
                        raw_reply=json.dumps(reply.raw_content, ensure_ascii=False),
                        final_reply=final_reply,
                        temperature=temperature,
                        turn_meta=turn_meta,
                        tool_iterations=tool_iterations,
                        generation_ms=generation_ms,
                        note='模型遗漏 send_message，已自动补发并修正上下文为工具调用格式',
                    )
                    return {'message': final_reply, 'think_note': think_note}, generation_ms, used_tools

                final_reply = '\n'.join(entry['text'] for entry in sent_entries)
                think_note = self._normalize_think_note(reply.text)
                self._record_turn_log(
                    scope_type,
                    scope_id,
                    agent_id,
                    model_messages,
                    raw_reply=json.dumps(reply.raw_content, ensure_ascii=False),
                    final_reply=final_reply,
                    temperature=temperature,
                    turn_meta=turn_meta,
                    tool_iterations=tool_iterations,
                    generation_ms=generation_ms,
                )
                return {'message': final_reply, 'think_note': think_note}, generation_ms, used_tools

            used_tools = True
            result_blocks = []
            iteration_calls = []
            for call in reply.tool_calls:
                if call.name in LOOP_TOOL_NAMES:
                    result = await self._run_ai_tool_call(scope_type, scope_id, agent_id, call.name, call.input)
                else:
                    result = self._execute_live_action_tool_call(
                        scope_type,
                        scope_id,
                        agent_id,
                        live_message,
                        call,
                        context,
                        allow_notify_master,
                        allow_tasks,
                        sent_entries,
                    )
                result_blocks.append(
                    {
                        'type': 'tool_result',
                        'tool_use_id': call.call_id,
                        'content': result,
                    }
                )
                iteration_calls.append({'name': call.name, 'input': call.input, 'result': result})
            tool_iterations.append(
                {
                    'assistant_text': reply.text,
                    'tool_calls': iteration_calls,
                }
            )
            model_messages.append({'role': 'assistant', 'content': reply.raw_content})
            model_messages.append({'role': 'user', 'content': result_blocks})

            scope_key = self._scope_key(scope_type, scope_id)
            pending = self._pending_scope_turns.pop(scope_key, None)
            if pending:
                model_messages.append({'role': 'user', 'content': self._build_pending_fold_reminder(pending)})

        self.tools.record_tool_use(
            scope_type,
            scope_id,
            agent_id,
            'loop_guard',
            '',
            'AI 工具连续调用过多，已中止本轮继续执行。',
            limit=self.config.history_limit,
        )
        final_reply = '\n'.join(entry['text'] for entry in sent_entries) if live_message is not None else ''
        self._record_turn_log(
            scope_type,
            scope_id,
            agent_id,
            model_messages,
            raw_reply='[loop_guard]',
            final_reply=final_reply,
            temperature=temperature,
            turn_meta=turn_meta,
            tool_iterations=tool_iterations,
            generation_ms=int((time.perf_counter() - started_at) * 1000),
        )
        return {'message': final_reply, 'think_note': ''}, int((time.perf_counter() - started_at) * 1000), used_tools

    def _apply_directive_tools(
        self,
        scope_type: str,
        scope_id: str,
        agent_id: str,
        tool_calls: list,
        context: dict | None = None,
        allow_notify_master: bool = True,
        allow_tasks: bool = True,
    ) -> str:
        context = context or {}
        message_parts: list[str] = []

        for call in tool_calls:
            tool_input = dict(call.input or {})
            if call.name == 'send_message':
                content = str(tool_input.get('content') or '').strip()
                content = re.sub(r'\[\[.*?\]\]', '', content).strip()
                if content:
                    message_parts.append(content)
            elif call.name == 'remember':
                note = str(tool_input.get('note') or '').strip()
                if note:
                    saved = self.tools.remember(scope_type, scope_id, note)
                    self.tools.record_tool_use(
                        scope_type,
                        scope_id,
                        agent_id,
                        'remember',
                        note,
                        f"已写入 AI 工具备忘 {saved.get('note_id') if saved else '失败'}",
                        limit=self.config.history_limit,
                    )
            elif call.name == 'notify_master' and allow_notify_master:
                content = str(tool_input.get('content') or '').strip()
                if content:
                    payload = self._normalize_notify_payload(content, scope_type, scope_id, agent_id, context)
                    task = self.tools.create_task(agent_id, 'notify_master', payload)
                    self.queue.put_nowait({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})
                    self.tools.record_tool_use(
                        scope_type,
                        scope_id,
                        agent_id,
                        'notify_master',
                        content,
                        f'已创建任务 {task.task_id}',
                        limit=self.config.history_limit,
                    )
            elif call.name == 'create_task' and allow_tasks:
                kind = str(tool_input.get('kind') or '').strip()
                content = str(tool_input.get('payload') or '').strip()
                if kind == 'dev_agent' and not self._is_dev_agent_authorized(scope_type, scope_id):
                    self.tools.record_tool_use(
                        scope_type,
                        scope_id,
                        agent_id,
                        f'task:{kind}',
                        content,
                        '拒绝：当前私聊不是管理员账号，无权发起 dev_agent 后台任务。',
                        limit=self.config.history_limit,
                    )
                    message_parts.append('（这个操作需要号主本人才能发起，我暂时没法帮你做。）')
                elif kind:
                    payload = self._normalize_task_payload(content, scope_type, scope_id, agent_id, context)
                    task = self.tools.create_task(agent_id, kind, payload)
                    self.queue.put_nowait({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})
                    self.tools.record_tool_use(
                        scope_type,
                        scope_id,
                        agent_id,
                        f'task:{kind}',
                        content,
                        f'已创建任务 {task.task_id}',
                        limit=self.config.history_limit,
                    )

        return '\n'.join(part for part in message_parts if part).strip()

    def _send_scope_message(self, message: ChatMessage, content: str) -> list[dict]:
        content = str(content or '').strip()
        content = re.sub(r'\[\[.*?\]\]', '', content).strip()
        if message.chat_type == 'private':
            content = re.sub(r'\[CQ:at,qq=\d+\]', '', content).strip()
        content = self._split_long_reply_lines(content)
        entries: list[dict] = []
        for line in content.split('\n'):
            line = line.strip()
            if not line:
                continue
            response = self.bot.send_text(message.chat_type, message.chat_id, line)
            message_id = None
            if isinstance(response, dict):
                message_id = (response.get('data') or {}).get('message_id')
            entries.append({'text': line, 'message_id': message_id})
        return entries

    def _execute_live_action_tool_call(
        self,
        scope_type: str,
        scope_id: str,
        agent_id: str,
        message: ChatMessage,
        call,
        context: dict | None,
        allow_notify_master: bool,
        allow_tasks: bool,
        sent_entries: list[dict],
    ) -> str:
        context = context or {}
        tool_input = dict(call.input or {})
        if call.name == 'send_message':
            content = str(tool_input.get('content') or '').strip()
            entries = self._send_scope_message(message, content)
            if not entries:
                return '内容为空或清理后为空，未发送。'
            sent_entries.extend(entries)
            ids = ', '.join(str(entry['message_id']) for entry in entries if entry.get('message_id') is not None)
            suffix = f'，message_id: {ids}' if ids else ''
            return f'已发送 {len(entries)} 条消息{suffix}。'
        if call.name == 'recall_message':
            message_id = tool_input.get('message_id')
            if not message_id:
                return '缺少 message_id，无法撤回。'
            try:
                self.bot.recall_message(message_id)
            except Exception as exc:
                return f'撤回失败: {exc}'
            return f'已撤回消息 {message_id}。'
        if call.name == 'remember':
            note = str(tool_input.get('note') or '').strip()
            if not note:
                return '内容为空，未记录。'
            saved = self.tools.remember(scope_type, scope_id, note)
            result = f"已写入 AI 工具备忘 {saved.get('note_id') if saved else '失败'}"
            self.tools.record_tool_use(
                scope_type,
                scope_id,
                agent_id,
                'remember',
                note,
                result,
                limit=self.config.history_limit,
            )
            return result
        if call.name == 'notify_master' and allow_notify_master:
            content = str(tool_input.get('content') or '').strip()
            if not content:
                return '内容为空，未上报。'
            payload = self._normalize_notify_payload(content, scope_type, scope_id, agent_id, context)
            task = self.tools.create_task(agent_id, 'notify_master', payload)
            self.queue.put_nowait({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})
            result = f'已创建任务 {task.task_id}，主AI会尽快处理。'
            self.tools.record_tool_use(
                scope_type,
                scope_id,
                agent_id,
                'notify_master',
                content,
                result,
                limit=self.config.history_limit,
            )
            return result
        if call.name == 'create_task' and allow_tasks:
            kind = str(tool_input.get('kind') or '').strip()
            content = str(tool_input.get('payload') or '').strip()
            if not kind:
                return '缺少任务类型，未创建。'
            if kind == 'dev_agent' and not self._is_dev_agent_authorized(scope_type, scope_id):
                result = '当前私聊不是管理员账号，无权发起 dev_agent 后台任务。'
                self.tools.record_tool_use(
                    scope_type,
                    scope_id,
                    agent_id,
                    f'task:{kind}',
                    content,
                    result,
                    limit=self.config.history_limit,
                )
                return result
            payload = self._normalize_task_payload(content, scope_type, scope_id, agent_id, context)
            task = self.tools.create_task(agent_id, kind, payload)
            self.queue.put_nowait({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})
            result = f'已创建任务 {task.task_id}。'
            self.tools.record_tool_use(
                scope_type,
                scope_id,
                agent_id,
                f'task:{kind}',
                content,
                result,
                limit=self.config.history_limit,
            )
            return result
        return f'当前不支持调用该工具: {call.name}'

    def _build_pending_fold_reminder(self, pending: dict) -> str:
        trigger_messages = pending.get('trigger_messages') or []
        lines: list[str] = []
        for entry in trigger_messages:
            text = str(entry.get('text') or '').strip()
            if not text:
                continue
            nickname = str(entry.get('nickname') or '').strip()
            lines.append(f'{nickname}: {text}' if nickname else text)
        if not lines:
            cleaned = str(pending.get('cleaned') or '').strip()
            if cleaned:
                lines.append(cleaned)
        body = '\n'.join(lines) or '(内容为空)'
        return (
            '补充提醒：生成过程中又收到新消息：\n'
            f'{body}\n'
            '如果你之前发的内容因此过时或需要撤回，可以调用 recall_message；不需要的话正常继续即可。'
        )

    def _build_self_interrupt_reminder(self, entries: list[dict]) -> str:
        lines = [str(entry.get('text') or '').strip() for entry in entries if str(entry.get('text') or '').strip()]
        body = '\n'.join(lines) or '(内容为空)'
        return (
            '重要提醒：生成过程中，你的账号通过其他设备直接发送了以下消息（这是你自己，不是别人在说话）：\n'
            f'{body}\n'
            '这些内容已经真实发出，无法撤回，接下来的发言不能和它矛盾。'
            '如果你原本准备发的内容因此过时、重复或冲突，可以调整措辞或者不再发送；'
            '如果之前已经发出的内容因此需要撤回，可以调用 recall_message。'
        )

    async def _process_task(self, item: dict):
        run_epoch = int(item.get('message_epoch', self._message_epoch))
        if self._is_epoch_stale(run_epoch):
            return
        task_id = item['task_id']
        task = self.repo.get_task(task_id)
        if not task:
            return
        if task.get('status') == 'done':
            return
        kind = task.get('kind')

        if kind == 'set_alarm':
            await self._handle_set_alarm(task)
            return

        self.repo.update_task(task_id, 'running')

        if kind == 'notify_master':
            result = await self._handle_notify_master(task)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'image_describe':
            result = await self._handle_image_describe(task)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'forward_summary':
            self.repo.update_task(task_id, 'done', '当前版本已接入任务骨架，合并转发总结稍后补全。')
            return

        if kind == 'send_private_message':
            result = await self._handle_send_private_message(task, run_epoch=run_epoch)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'delegate_to_child':
            result = await self._handle_delegate_to_child(task, run_epoch=run_epoch)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'followup_to_child':
            result = await self._handle_followup_to_child(task, run_epoch=run_epoch)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'child_report':
            result = await self._handle_child_report(task)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'message_scope':
            result = await self._handle_message_scope(task, run_epoch=run_epoch)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'refresh_impression':
            result = await self._handle_refresh_impression(task)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'dev_agent':
            dev_task = self.loop.create_task(self._run_dev_agent_task(task))
            self._dev_agent_tasks.add(dev_task)
            dev_task.add_done_callback(lambda t: self._dev_agent_tasks.discard(t))
            return

        self.repo.update_task(task_id, 'done', f'任务 {kind} 已登记，等待后续扩展对应 handler。')

    async def _maybe_schedule_impression_refresh(
        self,
        scope_type: str,
        scope_id: str,
        agent,
        cleaned: str,
    ):
        if scope_type == 'master':
            return
        if cleaned.startswith(('#', '/')):
            return
        count = int(agent.message_count or 0)
        if count < 3:
            return
        milestones = {3, 6, 12, 20}
        should_refresh = count in milestones or (count > 20 and count % 20 == 0)
        if not should_refresh:
            return
        recent_gap = time.time() - float(agent.impression_updated_at or 0.0)
        if recent_gap < 60:
            return
        task = self.tools.create_task(
            agent.agent_id,
            'refresh_impression',
            {
                'scope_type': scope_type,
                'scope_id': scope_id,
            },
        )
        await self.queue.put({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})

    def _detect_master_request(self, message: ChatMessage, cleaned: str) -> dict | None:
        history = self.repo.list_messages(message.chat_type, str(message.chat_id))[-8:]
        patterns = [
            r'(?:帮我)?给\s*(?P<qq>\d{5,12})\s*(?:发|法)(?:个)?\s*消息\s*(?:说|讲)?\s*(?P<content>.+)',
            r'帮我给\s*(?P<qq>\d{5,12})\s*(?:发|法)\s*消息\s*(?:说|讲)?\s*(?P<content>.+)',
            r'帮我告诉\s*(?P<qq>\d{5,12})\s*(?P<content>.+)',
            r'替我跟\s*(?P<qq>\d{5,12})\s*说\s*(?P<content>.+)',
            r'(?:喊|叫|联系)\s*(?P<qq>\d{5,12})\s*(?P<content>.+)',
        ]
        for pattern in patterns:
            matched = re.search(pattern, cleaned)
            if matched:
                target_qq = matched.group('qq').strip()
                content = matched.group('content').strip(' ：:，,。')
                if not content:
                    return None
                return self._build_contact_request(message, cleaned, target_qq, content)

        contextual_content = self._extract_contextual_contact_content(cleaned)
        if contextual_content:
            target_qq = self._find_recent_target_qq(history, str(message.user_id))
            if target_qq:
                return self._build_contact_request(message, cleaned, target_qq, contextual_content)
        return None

    def _build_contact_request(self, message: ChatMessage, cleaned: str, target_qq: str, content: str) -> dict:
        normalized_content = self._normalize_contact_content(content)
        return {
            'request_type': 'coordinate_contact',
            'target_scope_type': 'private',
            'target_scope_id': target_qq,
            'content': normalized_content,
            'instruction': f'如果合适，请你主动联系这个会话，并自然转达：{normalized_content}',
            'reason': '用户要求联系目标会话',
            'scope_type': message.chat_type,
            'scope_id': str(message.chat_id),
            'requester_qq': str(message.user_id),
            'requester_name': message.nickname,
            'source_message': cleaned,
        }

    def _detect_global_preference_request(self, message: ChatMessage, cleaned: str) -> dict | None:
        style_match = re.search(
            r'(?:让|叫|希望)?(?P<target>[A-Za-z0-9_\-\u4e00-\u9fa5·]{2,24})(?:用户)?(?:的ai|的AI|那边的ai|那边)?对(?:我|他|她|这个人)?(?P<style>语气好一点|态度好一点|好一点|温柔一点|客气一点|友好一点|热情一点|别那么凶|不要那么凶)',
            cleaned,
        )
        if style_match:
            target_query = style_match.group('target').strip()
            style = style_match.group('style').strip()
            return {
                'request_type': 'set_user_preference',
                'target_query': target_query,
                'preference_text': f'全局对待策略：对这个人说话 {style}。',
                'scope_type': message.chat_type,
                'scope_id': str(message.chat_id),
                'requester_qq': str(message.user_id),
                'requester_name': message.nickname,
                'source_message': cleaned,
            }

        relation_match = re.search(
            r'(?P<target>[A-Za-z0-9_\-\u4e00-\u9fa5·]{2,24})是我(?:的)?(?P<relation>女友|女朋友|对象|老婆|男朋友|老公|cp)',
            cleaned,
        )
        if relation_match:
            target_query = relation_match.group('target').strip()
            relation = relation_match.group('relation').strip()
            return {
                'request_type': 'set_user_preference',
                'target_query': target_query,
                'preference_text': f'全局关系设定：这个人是 {message.nickname}({message.user_id}) 的{relation}，互动时应明显更亲近、更照顾、更有耐心。',
                'scope_type': message.chat_type,
                'scope_id': str(message.chat_id),
                'requester_qq': str(message.user_id),
                'requester_name': message.nickname,
                'source_message': cleaned,
            }
        return None

    def _extract_contextual_contact_content(self, cleaned: str) -> str | None:
        text = cleaned.strip(' ：:，,。')
        patterns = [
            r'^(?:喊|叫|联系)(?:他|她|一下)?(?P<content>.+)$',
            r'^(?:让)?(?:他|她)(?P<content>来.+)$',
            r'^(?P<content>(?:来|去).+)$',
        ]
        for pattern in patterns:
            matched = re.search(pattern, text)
            if matched:
                content = (matched.groupdict().get('content') or '').strip(' ：:，,。')
                if content:
                    return content
        return None

    def _find_recent_target_qq(self, history: list[dict], requester_qq: str) -> str | None:
        for item in reversed(history):
            if str(item.get('user_id')) != requester_qq:
                continue
            text = str(item.get('text') or '').strip()
            matched = re.fullmatch(r'\d{5,12}', text)
            if matched:
                return matched.group(0)
        return None

    def _detect_contact_target_only(self, message: ChatMessage, cleaned: str) -> str | None:
        text = cleaned.strip()
        if not re.fullmatch(r'\d{5,12}', text):
            return None
        history = self.repo.list_messages(message.chat_type, str(message.chat_id))[-8:]
        if self._recent_contact_context_exists(history):
            return text
        return None

    def _normalize_contact_content(self, content: str) -> str:
        text = content.strip(' ：:，,。')
        if re.match(r'^(来|去|一起)', text):
            return f'喊你{text}'
        return text

    def _recent_contact_context_exists(self, history: list[dict]) -> bool:
        patterns = [
            r'喊',
            r'叫',
            r'联系',
            r'帮你联系',
            r'给我.*QQ号',
            r'去哪喊',
        ]
        for item in reversed(history):
            text = str(item.get('text') or '').strip()
            if any(re.search(pattern, text) for pattern in patterns):
                return True
        return False

    def _detect_status_request(self, message: ChatMessage, cleaned: str) -> dict | None:
        if not self._looks_like_status_query(cleaned):
            return None
        recent = self._find_recent_contact_request(
            message.chat_type,
            str(message.chat_id),
            query_text=cleaned,
            requester_qq=str(message.user_id),
        )
        if not recent:
            return None
        return {
            'request_type': 'query_contact_status',
            'scope_type': message.chat_type,
            'scope_id': str(message.chat_id),
            'requester_qq': str(message.user_id),
            'requester_name': message.nickname,
            'trace_id': recent.get('trace_id'),
            'target_scope_type': recent.get('target_scope_type'),
            'target_scope_id': recent.get('target_scope_id'),
            'instruction': recent.get('instruction'),
            'content': recent.get('content'),
            'source_message': cleaned,
        }

    def _looks_like_status_query(self, cleaned: str) -> bool:
        text = cleaned.strip()
        patterns = [
            r'让你发的消息.*(怎么样|咋样|如何|后续|进展)',
            r'消息.*(怎么样|咋样|如何|后续|进展)',
            r'(他|她|对方).*(回复|回了|理你|怎么说)',
            r'(有|有没有).*(回复|回信|回我)',
            r'(后续|进展).*(呢|咋样|如何)?',
            r'怎么样了',
        ]
        return any(re.search(pattern, text) for pattern in patterns)

    def _find_recent_contact_request(
        self,
        scope_type: str,
        scope_id: str,
        query_text: str = '',
        requester_qq: str = '',
    ) -> dict | None:
        candidates = []
        for task in reversed(self.repo.list_tasks(kinds=['notify_master'])):
            payload = task.get('payload') or {}
            if task.get('kind') != 'notify_master':
                continue
            if payload.get('request_type') != 'coordinate_contact':
                continue
            if payload.get('scope_type') != scope_type or str(payload.get('scope_id')) != str(scope_id):
                continue
            if requester_qq and str(payload.get('requester_qq') or '') != requester_qq:
                continue
            candidate = {
                'task_id': task.get('task_id'),
                'created_at': task.get('created_at'),
                'trace_id': str(payload.get('trace_id') or task.get('task_id') or ''),
                'target_scope_type': payload.get('target_scope_type') or 'private',
                'target_scope_id': str(payload.get('target_scope_id') or ''),
                'instruction': payload.get('instruction'),
                'content': payload.get('content'),
                'requester_qq': payload.get('requester_qq'),
                'requester_name': payload.get('requester_name'),
                'source_message': payload.get('source_message'),
            }
            candidates.append(candidate)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        scored = [
            (self._score_contact_request_candidate(query_text, item), item)
            for item in candidates
        ]
        scored.sort(key=lambda pair: (pair[0], float(pair[1].get('created_at') or 0.0)), reverse=True)
        return scored[0][1]

    def _score_contact_request_candidate(self, query_text: str, candidate: dict) -> int:
        query_text = str(query_text or '').strip()
        if not query_text:
            return int(float(candidate.get('created_at') or 0.0))
        score = 0
        target_scope_id = str(candidate.get('target_scope_id') or '').strip()
        if target_scope_id and target_scope_id in query_text:
            score += 80
        haystack = ' '.join(
            [
                str(candidate.get('content') or ''),
                str(candidate.get('instruction') or ''),
                str(candidate.get('source_message') or ''),
            ]
        )
        query_terms = self._extract_status_focus_terms(query_text)
        for term in query_terms:
            if len(term) < 2:
                continue
            if term in haystack:
                score += 15
        if re.search(r'(上次|之前|前面|那次|昨天|刚才)', query_text):
            score += 10
        score += min(20, len(query_terms) * 2)
        created_at = float(candidate.get('created_at') or 0.0)
        if created_at:
            age_hours = max(0.0, (time.time() - created_at) / 3600.0)
            score += max(0, int(24 - min(age_hours, 24)))
        return score

    def _extract_status_focus_terms(self, text: str) -> list[str]:
        text = str(text or '').strip()
        text = re.sub(r'(怎么样|咋样|如何|后续|进展|回复|回信|回我|回了|让你发的消息|消息|有|有没有|呢)', ' ', text)
        text = re.sub(r'[^\w\u4e00-\u9fa5]+', ' ', text)
        parts = [item.strip() for item in text.split() if item.strip()]
        return parts[:8]

    def _build_global_identity_context_for_message(self, message: ChatMessage, cleaned: str) -> str:
        lines: list[str] = []
        current_user = self.repo.get_user_profile(str(message.user_id))
        if current_user:
            summary = self._format_user_profile_summary(current_user, title='当前发送者')
            if summary:
                lines.append(summary)
        for profile in self.repo.find_users_mentioned_in_text(cleaned, exclude_user_id=str(message.user_id), limit=3):
            summary = self._format_user_profile_summary(profile, title='消息中提到的人')
            if summary:
                lines.append(summary)
        return '\n\n'.join(lines).strip()

    def _build_global_identity_context_for_scope(self, scope_type: str, scope_id: str, instruction: str = '') -> str:
        lines: list[str] = []
        if scope_type == 'private':
            target_user = self.repo.get_user_profile(scope_id)
            if target_user:
                summary = self._format_user_profile_summary(target_user, title='当前会话对象')
                if summary:
                    lines.append(summary)
        for profile in self.repo.find_users_mentioned_in_text(instruction, exclude_user_id=scope_id if scope_type == 'private' else '', limit=3):
            summary = self._format_user_profile_summary(profile, title='任务里提到的人')
            if summary:
                lines.append(summary)
        return '\n\n'.join(lines).strip()

    def _format_user_profile_summary(self, profile: dict | None, title: str) -> str:
        if not profile:
            return ''
        aliases = [str(item or '').strip() for item in profile.get('aliases') or [] if str(item or '').strip()]
        alias_text = ' / '.join(aliases[:5]) or str(profile.get('user_id') or '')
        facts = [str(item.get('content') or '').strip() for item in profile.get('facts') or [] if str(item.get('content') or '').strip()]
        scopes = [item for item in profile.get('scopes') or [] if str(item.get('scope_type') or '').strip() and str(item.get('scope_id') or '').strip()]
        scope_text = ', '.join(f"{item.get('scope_type')}:{item.get('scope_id')}" for item in scopes[:3]) or '暂无明确私聊作用域'
        lines = [f"{title}: {alias_text} (QQ: {profile.get('user_id')})", f"已知作用域: {scope_text}"]
        if facts:
            lines.append('共享事实:')
            lines.extend(f"- {item}" for item in facts[-4:])
        return '\n'.join(lines)

    def _detect_alarm_request(self, message: ChatMessage, cleaned: str) -> dict | None:
        relative_patterns = [
            (r'(?P<num>\d+)\s*秒后(?:提醒我|叫我|喊我|闹钟)?(?P<note>.*)', 1),
            (r'(?P<num>\d+)\s*分钟后(?:提醒我|叫我|喊我|闹钟)?(?P<note>.*)', 60),
            (r'(?P<num>\d+)\s*小时后(?:提醒我|叫我|喊我|闹钟)?(?P<note>.*)', 3600),
        ]
        for pattern, scale in relative_patterns:
            matched = re.search(pattern, cleaned)
            if matched:
                seconds = int(matched.group('num')) * scale
                note = matched.group('note').strip(' ，,。:：') or '到点了'
                return {
                    'request_type': 'set_alarm',
                    'due_at': time.time() + seconds,
                    'note': note,
                    'scope_type': message.chat_type,
                    'scope_id': str(message.chat_id),
                    'requester_qq': str(message.user_id),
                    'requester_name': message.nickname,
                    'direct_ack_sent': True,
                }

        absolute_patterns = [
            r'(?:在|到)\s*(?P<time>\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s*(?:提醒我|叫我|喊我|闹钟)?(?P<note>.*)',
            r'(?P<time>\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s*(?:提醒我|叫我|喊我|闹钟)?(?P<note>.*)',
        ]
        for pattern in absolute_patterns:
            matched = re.search(pattern, cleaned)
            if matched:
                due_at = self._parse_datetime_to_ts(matched.group('time'))
                if due_at is None:
                    return None
                note = matched.group('note').strip(' ，,。:：') or '到点了'
                return {
                    'request_type': 'set_alarm',
                    'due_at': due_at,
                    'note': note,
                    'scope_type': message.chat_type,
                    'scope_id': str(message.chat_id),
                    'requester_qq': str(message.user_id),
                    'requester_name': message.nickname,
                    'direct_ack_sent': True,
                }
        return None

    def _normalize_notify_payload(
        self,
        raw_content: str,
        scope_type: str,
        scope_id: str,
        agent_id: str,
        context: dict | None = None,
    ) -> dict:
        context = context or {}
        payload = self._maybe_json(raw_content)
        if not isinstance(payload, dict):
            payload = {'content': raw_content}
        payload.setdefault('scope_type', scope_type)
        payload.setdefault('scope_id', scope_id)
        payload.setdefault('source_agent', agent_id)
        payload.setdefault('requester_qq', context.get('requester_qq'))
        payload.setdefault('requester_name', context.get('requester_name'))
        payload.setdefault('source_message', context.get('source_message'))
        payload.setdefault('source_label', context.get('source_label'))
        payload.setdefault('message_id', context.get('message_id'))
        payload.setdefault('trace_id', context.get('trace_id'))
        payload.setdefault('origin_scope_type', context.get('origin_scope_type'))
        payload.setdefault('origin_scope_id', context.get('origin_scope_id'))
        return payload

    def _normalize_task_payload(
        self,
        raw_content: str,
        scope_type: str,
        scope_id: str,
        agent_id: str,
        context: dict | None = None,
    ) -> dict:
        context = context or {}
        payload = self._maybe_json(raw_content)
        if isinstance(payload, dict):
            normalized = payload
        else:
            normalized = {'content': raw_content}
        normalized.setdefault('scope_type', scope_type)
        normalized.setdefault('scope_id', scope_id)
        normalized.setdefault('source_agent', agent_id)
        normalized.setdefault('requester_qq', context.get('requester_qq'))
        normalized.setdefault('requester_name', context.get('requester_name'))
        normalized.setdefault('source_message', context.get('source_message'))
        normalized.setdefault('source_label', context.get('source_label'))
        normalized.setdefault('message_id', context.get('message_id'))
        normalized.setdefault('trace_id', context.get('trace_id'))
        normalized.setdefault('origin_scope_type', context.get('origin_scope_type'))
        normalized.setdefault('origin_scope_id', context.get('origin_scope_id'))
        return normalized

    def _maybe_json(self, raw_content: str):
        text = raw_content.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _extract_image_refs(self, raw_message: str) -> list[str]:
        refs = []
        for segment in re.findall(r'\[CQ:image,([^\]]+)\]', raw_message or ''):
            url_match = re.search(r'url=([^,\]]+)', segment)
            file_match = re.search(r'file=([^,\]]+)', segment)
            if url_match:
                refs.append(html.unescape(url_match.group(1)))
            elif file_match:
                refs.append(html.unescape(file_match.group(1)))
        return refs

    def _extract_file_refs(self, raw_message: str) -> list[dict]:
        """返回 [{'file_id': '...', 'file_name': '...', 'file_size': int_or_None}]"""
        refs = []
        for segment in re.findall(r'\[CQ:file,([^\]]+)\]', raw_message or ''):
            fid_m = re.search(r'file_id=([^,\]]+)', segment)
            fname_m = re.search(r'file=([^,\]]+)', segment)
            fsize_m = re.search(r'file_size=([^,\]]+)', segment)
            if fid_m:
                refs.append({
                    'file_id': fid_m.group(1),
                    'file_name': html.unescape(fname_m.group(1)) if fname_m else 'unknown',
                    'file_size': int(fsize_m.group(1)) if fsize_m else None,
                })
        return refs

    def _summarize_image_refs(self, refs: list[str]) -> str:
        preview = []
        for ref in refs[:3]:
            if len(ref) > 96:
                preview.append(ref[:93] + '...')
            else:
                preview.append(ref)
        return ' | '.join(preview)

    def _master_system_prompt(self) -> str:
        return self.prompt_store.main_system_prompt()

    def _build_master_prompt(self, task: dict) -> str:
        payload = task.get('payload') or {}
        notes = self.repo.list_notes('master', 'global')[-10:]
        friends = self.tools.get_friend_list()
        groups = self.tools.get_group_list()
        friend_lines = [f"{item.get('nickname', '')}:{item.get('user_id')}" for item in friends[:20]]
        group_lines = [f"{item.get('group_name', '')}:{item.get('group_id')}" for item in groups[:20]]

        # 给备忘加上时间戳，方便判断时效性
        note_lines = []
        for item in notes:
            content = item.get('content', '')
            created_at = item.get('created_at', 0)
            if created_at:
                age_seconds = time.time() - created_at
                if age_seconds < 3600:
                    time_label = f"{int(age_seconds / 60)}分钟前"
                elif age_seconds < 86400:
                    time_label = f"{int(age_seconds / 3600)}小时前"
                else:
                    time_label = f"{int(age_seconds / 86400)}天前"
                note_lines.append(f"[{time_label}] {content}")
            else:
                note_lines.append(content)

        candidate_lines = self._build_target_candidate_lines(payload)

        # 加载关系网数据
        scope_relations = self.repo.list_scope_relations()[:15]
        user_relations = self.repo.list_user_relations()[:20]

        scope_relation_lines = []
        for rel in scope_relations:
            parts = [f"{rel['scope_type']}:{rel['scope_id']}"]
            if rel['affinity'] != 0:
                parts.append(f"好感度{rel['affinity']}")
            if rel['relevance'] != 0:
                parts.append(f"关联度{rel['relevance']}")
            if rel['admin_note']:
                parts.append(f"备注:{rel['admin_note']}")
            if rel['impression']:
                parts.append(f"印象:{rel['impression'][:50]}")
            scope_relation_lines.append(' | '.join(parts))

        user_relation_lines = []
        for rel in user_relations:
            parts = [f"QQ:{rel['user_id']}"]
            if rel['aliases']:
                parts.append(f"昵称:{','.join(rel['aliases'][:2])}")
            if rel['affinity'] != 0:
                parts.append(f"好感度{rel['affinity']}")
            if rel['admin_note']:
                parts.append(f"备注:{rel['admin_note']}")
            user_relation_lines.append(' | '.join(parts))

        return (
            f"【当前时间: {self._now_text()}】\n"
            f"来源下级AI: {task.get('source_agent')}\n"
            f"任务类型: {task.get('kind')}\n"
            f"请求载荷: {json.dumps(payload, ensure_ascii=False)}\n\n"
            f"可能相关的人物/会话:\n{chr(10).join(candidate_lines) if candidate_lines else '暂无'}\n\n"
            f"私聊好友列表:\n{chr(10).join(friend_lines) if friend_lines else '暂无'}\n\n"
            f"群聊列表:\n{chr(10).join(group_lines) if group_lines else '暂无'}\n\n"
            f"群聊/私聊关系网（好感度、关联度、备注）:\n{chr(10).join(scope_relation_lines) if scope_relation_lines else '暂无'}\n\n"
            f"用户关系网（好感度、备注）:\n{chr(10).join(user_relation_lines) if user_relation_lines else '暂无'}\n\n"
            f"主AI备忘（带时间标记，注意判断时效性）:\n{chr(10).join(note_lines) if note_lines else '暂无'}\n\n"
            f"当前模型档位: {self._active_chat_profile} -> {self._chat_profiles[self._active_chat_profile]['model_name']}\n\n"
            "如果这是跨会话联系请求，优先用 create_task 创建 delegate_to_child 任务，而不是直接 message_scope。"
            "如果这是进度追问，优先依据已有事实回传，不要猜测对方态度。"
            "你输出的普通文字会作为补充情报回传给来源子AI。"
        )

    async def _handle_notify_master(self, task: dict) -> str:
        payload = task.get('payload') or {}
        trace_id = str(payload.get('trace_id') or task.get('task_id') or '')
        if payload.get('request_type') == 'query_contact_status':
            return await self._handle_query_contact_status(task)
        if payload.get('request_type') == 'set_user_preference':
            return await self._handle_set_user_preference(task)

        self.repo.get_or_create_master()
        self.repo.add_note('master', 'global', f"来自 {task.get('source_agent')}: {json.dumps(payload, ensure_ascii=False)}")

        master_reply = None
        master_messages = [{'role': 'user', 'content': self._build_master_prompt(task)}]
        master_tools = build_tools(
            include_message=False,
            include_memory=False,
            allow_notify_master=False,
            allow_search=False,
            allow_update_tools=True,
        )
        try:
            for _ in range(5):
                master_reply = await self._complete_chat(
                    self._static_system_blocks(self._master_system_prompt()),
                    master_messages,
                    master_tools,
                    0.2,
                )
                if not master_reply:
                    break
                loop_calls = [call for call in master_reply.tool_calls if call.name in LOOP_TOOL_NAMES]
                if not loop_calls:
                    break
                result_blocks = []
                for call in master_reply.tool_calls:
                    if call.name in LOOP_TOOL_NAMES:
                        result = await self._run_ai_tool_call('master', 'global', 'master:global', call.name, call.input)
                    else:
                        result = '本轮先处理查询/更新类工具，这个操作未执行；如仍需要，请在工具结果后再次调用。'
                    result_blocks.append({'type': 'tool_result', 'tool_use_id': call.call_id, 'content': result})
                master_messages.append({'role': 'assistant', 'content': master_reply.raw_content})
                master_messages.append({'role': 'user', 'content': result_blocks})
        except Exception as exc:
            error(f'[AI][master] {exc}')

        created_task_ids = []
        if master_reply:
            for call in master_reply.tool_calls:
                tool_input = dict(call.input or {})
                if call.name == 'remember':
                    note = str(tool_input.get('note') or '').strip()
                    if note:
                        self.repo.add_note('master', 'global', note)
                elif call.name == 'create_task':
                    kind = str(tool_input.get('kind') or '').strip()
                    if not kind:
                        continue
                    if kind == 'dev_agent' and not self._is_dev_agent_authorized(
                        str(payload.get('scope_type') or ''), str(payload.get('scope_id') or ''),
                    ):
                        self.repo.add_note(
                            'master',
                            'global',
                            f"拒绝了来自非管理员私聊的 dev_agent 任务请求 scope={payload.get('scope_type')}:{payload.get('scope_id')}",
                        )
                        continue
                    child_task = self.tools.create_task(
                        'master:global',
                        kind,
                        self._normalize_task_payload(
                            str(tool_input.get('payload') or ''),
                            'master',
                            'global',
                            'master:global',
                            {
                                'requester_qq': payload.get('requester_qq'),
                                'requester_name': payload.get('requester_name'),
                                'source_message': payload.get('source_message') or payload.get('content') or payload.get('instruction'),
                                'source_label': payload.get('source_label'),
                                'message_id': payload.get('message_id'),
                                'trace_id': trace_id,
                                'origin_scope_type': payload.get('origin_scope_type'),
                                'origin_scope_id': payload.get('origin_scope_id'),
                            },
                        ),
                    )
                    created_task_ids.append(child_task.task_id)
                    await self._process_task({'task_id': child_task.task_id})

        followup_text = (master_reply.text if master_reply else '').strip()
        if self._should_callback_to_source(payload, followup_text):
            existing_followup = self._find_task_by_trace(
                'followup_to_child',
                trace_id,
                str(payload.get('scope_type') or ''),
                str(payload.get('scope_id') or ''),
            )
            if not existing_followup:
                followup_task = self.tools.create_task(
                    'master:global',
                    'followup_to_child',
                    {
                        'target_scope_type': payload.get('scope_type'),
                        'target_scope_id': payload.get('scope_id'),
                        'instruction': self._build_followup_instruction(payload, followup_text),
                        'requester_qq': payload.get('requester_qq'),
                        'requester_name': payload.get('requester_name'),
                        'followup_only': True,
                        'trace_id': trace_id,
                    },
                )
                created_task_ids.append(followup_task.task_id)
                await self._process_task({'task_id': followup_task.task_id})

        if not created_task_ids and payload.get('request_type') in {'coordinate_contact', 'send_private_message'}:
            resolved_target = self._resolve_target_scope(payload)
            target_scope_type = (resolved_target or {}).get('scope_type') or payload.get('target_scope_type') or 'private'
            target_scope_id = (resolved_target or {}).get('scope_id') or payload.get('target_scope_id') or payload.get('target_qq')
            existing_delegate = self._find_task_by_trace(
                'delegate_to_child',
                trace_id,
                str(target_scope_type),
                str(target_scope_id or ''),
            )
            if not existing_delegate:
                child_task = self.tools.create_task(
                    'master:global',
                    'delegate_to_child',
                    {
                        'target_scope_type': target_scope_type,
                        'target_scope_id': target_scope_id,
                        'target_user_id': (resolved_target or {}).get('user_id'),
                        'target_aliases': (resolved_target or {}).get('aliases') or [],
                        'content': payload.get('content'),
                        'instruction': payload.get('instruction') or f"如果合适，请主动联系这个会话，并自然转达：{payload.get('content')}",
                        'requester_qq': payload.get('requester_qq'),
                        'requester_name': payload.get('requester_name'),
                        'origin_scope_type': payload.get('scope_type'),
                        'origin_scope_id': payload.get('scope_id'),
                        'source_message': payload.get('source_message'),
                        'source_label': payload.get('source_label'),
                        'message_id': payload.get('message_id'),
                        'trace_id': trace_id,
                    },
                )
                created_task_ids.append(child_task.task_id)
                await self._process_task({'task_id': child_task.task_id})

        if created_task_ids:
            return f"主AI已处理，并创建子任务: {', '.join(created_task_ids)}"
        return '主AI已记录请求，但暂时没有执行动作。'

    def _build_target_candidate_lines(self, payload: dict) -> list[str]:
        query = str(payload.get('target_query') or payload.get('target_scope_id') or payload.get('target_qq') or '').strip()
        if not query:
            return []
        lines = []
        for item in self.repo.resolve_user_candidates(query, limit=5):
            aliases = ' / '.join((item.get('aliases') or [])[:3]) or str(item.get('user_id') or '')
            scopes = ', '.join(
                f"{scope.get('scope_type')}:{scope.get('scope_id')}"
                for scope in sorted(item.get('scopes') or [], key=lambda entry: float(entry.get('last_seen') or 0.0), reverse=True)[:3]
            ) or '暂无'
            lines.append(f"- {aliases} (QQ:{item.get('user_id')}) -> {scopes}")
        return lines

    def _resolve_target_scope(self, payload: dict) -> dict | None:
        direct_query = str(payload.get('target_scope_id') or payload.get('target_qq') or '').strip()
        if direct_query:
            resolved = self.repo.resolve_scope_by_query(direct_query)
            if resolved:
                return resolved
        target_query = str(payload.get('target_query') or '').strip()
        if target_query:
            resolved = self.repo.resolve_scope_by_query(target_query)
            if resolved:
                return resolved
            for item in self.tools.get_friend_list():
                nickname = str(item.get('nickname') or '').strip()
                user_id = str(item.get('user_id') or '').strip()
                if not user_id:
                    continue
                if nickname == target_query or (len(target_query) >= 2 and target_query in nickname):
                    self.repo.touch_user_identity(user_id, nickname, 'private', user_id)
                    return {
                        'user_id': user_id,
                        'aliases': [nickname] if nickname else [],
                        'facts': [],
                        'scope_type': 'private',
                        'scope_id': user_id,
                    }
        return None

    async def _handle_set_user_preference(self, task: dict) -> str:
        payload = task.get('payload') or {}
        resolved = self._resolve_target_scope(payload)
        target_query = str(payload.get('target_query') or '').strip()
        if not resolved:
            return f'主AI暂时没定位到 {target_query or "目标人物"} 对应的子AI。'
        user_id = str(resolved.get('user_id') or '').strip()
        if not user_id:
            return f'主AI暂时没拿到 {target_query or "目标人物"} 的稳定身份。'
        preference_text = str(payload.get('preference_text') or '').strip()
        if preference_text:
            self.repo.add_user_fact(
                user_id,
                preference_text,
                str(payload.get('scope_type') or ''),
                str(payload.get('scope_id') or ''),
                str(payload.get('source_agent') or task.get('source_agent') or ''),
            )
        profile = self.repo.get_user_profile(user_id) or {}
        for scope in profile.get('scopes') or []:
            if str(scope.get('scope_type') or '') != 'private':
                continue
            scope_id = str(scope.get('scope_id') or '').strip()
            if not scope_id:
                continue
            self.repo.add_note('private', scope_id, f'全局共同体记忆: {preference_text}')
        alias_text = ' / '.join((resolved.get('aliases') or [])[:3]) or user_id
        return f'主AI已更新 {alias_text} 的全局人物设定，并同步给对应分身。'

    def _finalize_reply(self, message: ChatMessage, reply: str) -> str:
        cleaned = (reply or '').strip()
        if message.chat_type == 'private':
            cleaned = re.sub(r'\[CQ:at,qq=\d+\]', '', cleaned).strip()
        cleaned = self._split_long_reply_lines(cleaned)
        return cleaned

    def _should_callback_to_source(self, payload: dict, followup_text: str) -> bool:
        scope_type = payload.get('scope_type')
        scope_id = payload.get('scope_id')
        if not scope_type or not scope_id:
            return False
        request_type = str(payload.get('request_type') or '').strip()
        if request_type in {'query_contact_status'}:
            return False
        # Always callback when scope is present and request type is not excluded;
        # coordinate_contact IS included so the originating child gets confirmation
        # that delegation was initiated (prevents it from being left in the dark).
        # generic requests (request_type == '') should always get a followup even if
        # the master generated no text (a fallback message will be used instead).
        return request_type not in {'set_alarm'}

    def _build_followup_instruction(self, payload: dict, followup_text: str) -> str:
        request_type = str(payload.get('request_type') or 'generic').strip()
        guidance = followup_text or '主AI已经收到你的上报，并建议你结合当前会话继续判断下一步。'
        return (
            f"这是主AI给你的补充情报，来源任务类型是 {request_type}。"
            f"补充内容：{guidance}"
            "请你结合当前会话继续思考，可以选择自然回复、继续观察、再次联系主AI，或者调用其他工具。"
            "如果暂时不该说话，就不要调用 send_message。不要直接暴露主AI。"
            "回复风格继续保持短句、普通语气、少解释、少复述。"
            "如果要发给当前会话，必须调用 send_message 工具。"
        )

    async def _handle_query_contact_status(self, task: dict) -> str:
        payload = task.get('payload') or {}
        trace_id = str(payload.get('trace_id') or task.get('task_id') or '')
        scope_type = str(payload.get('scope_type') or '').strip()
        scope_id = str(payload.get('scope_id') or '').strip()
        if not scope_type or not scope_id:
            return '缺少原始会话，无法查询进度。'

        snapshot = self._build_contact_status_snapshot(payload)
        existing_callback = self._find_task_by_trace('delegate_to_child', trace_id, scope_type, scope_id, callback_only=True)
        if not existing_callback:
            callback_task = self.tools.create_task(
                'master:global',
                'delegate_to_child',
                {
                    'target_scope_type': scope_type,
                    'target_scope_id': scope_id,
                    'instruction': self._build_status_callback_instruction(payload, snapshot),
                    'requester_qq': payload.get('requester_qq'),
                    'requester_name': payload.get('requester_name'),
                    'origin_scope_type': None,
                    'origin_scope_id': None,
                    'callback_only': True,
                    'status_snapshot': snapshot,
                    'trace_id': trace_id,
                },
            )
            await self._process_task({'task_id': callback_task.task_id})
        return f"主AI已查询 {payload.get('target_scope_type')}:{payload.get('target_scope_id')} 的进度并回传。"

    def _build_contact_status_snapshot(self, payload: dict) -> dict:
        target_scope_type = str(payload.get('target_scope_type') or 'private').strip()
        target_scope_id = str(payload.get('target_scope_id') or '').strip()
        scope_type = str(payload.get('scope_type') or '').strip()
        scope_id = str(payload.get('scope_id') or '').strip()
        trace_id = str(payload.get('trace_id') or '').strip()
        related_report = self._find_latest_child_report(
            scope_type,
            scope_id,
            target_scope_type,
            target_scope_id,
            trace_id=trace_id,
        )
        target_messages = self.repo.list_messages(target_scope_type, target_scope_id)
        latest_reply = self._find_latest_target_reply(target_messages, related_report)
        return {
            'target_scope_type': target_scope_type,
            'target_scope_id': target_scope_id,
            'requested_content': payload.get('content'),
            'instruction': payload.get('instruction'),
            'request_created_at': payload.get('created_at'),
            'child_result_type': related_report.get('result_type') if related_report else None,
            'child_sent_text': related_report.get('sent_text') if related_report else None,
            'child_report_at': related_report.get('updated_at') if related_report else None,
            'has_target_reply': latest_reply is not None,
            'target_reply_text': latest_reply.get('text') if latest_reply else None,
            'target_reply_from': latest_reply.get('nickname') if latest_reply else None,
            'target_reply_at': latest_reply.get('timestamp') if latest_reply else None,
        }

    def _find_latest_child_report(
        self,
        scope_type: str,
        scope_id: str,
        target_scope_type: str,
        target_scope_id: str,
        trace_id: str = '',
    ) -> dict | None:
        tasks = self.repo.list_tasks(kinds=['child_report'])
        for task in reversed(tasks):
            payload = task.get('payload') or {}
            if str(payload.get('origin_scope_type') or '') != scope_type:
                continue
            if str(payload.get('origin_scope_id') or '') != scope_id:
                continue
            if str(payload.get('target_scope_type') or '') != target_scope_type:
                continue
            if str(payload.get('target_scope_id') or '') != target_scope_id:
                continue
            if trace_id and str(payload.get('trace_id') or '') != trace_id:
                continue
            return {
                'result_type': payload.get('result_type'),
                'sent_text': payload.get('sent_text'),
                'updated_at': task.get('updated_at') or task.get('created_at') or 0,
            }
        return None

    def _find_latest_target_reply(self, messages: list[dict], related_report: dict | None) -> dict | None:
        threshold = 0.0
        if related_report:
            threshold = float(related_report.get('updated_at') or 0.0)
        for item in reversed(messages):
            if item.get('user_id') == self.bot.self_id:
                continue
            timestamp = float(item.get('timestamp') or 0.0)
            if threshold and timestamp and timestamp < threshold:
                continue
            if threshold and not timestamp:
                continue
            return item
        return None

    def _build_status_callback_instruction(self, payload: dict, snapshot: dict) -> str:
        target_label = f"{snapshot.get('target_scope_type')}:{snapshot.get('target_scope_id')}"
        requested_content = snapshot.get('requested_content') or '那件事'
        child_result_type = snapshot.get('child_result_type')
        child_sent_text = snapshot.get('child_sent_text') or ''
        has_target_reply = bool(snapshot.get('has_target_reply'))
        target_reply_text = snapshot.get('target_reply_text') or ''
        target_reply_from = snapshot.get('target_reply_from') or '对方'
        request_time = self._format_ts_text(snapshot.get('request_created_at')) or '未知时间'
        report_time = self._format_ts_text(snapshot.get('child_report_at')) or '未知时间'
        reply_time = self._format_ts_text(snapshot.get('target_reply_at')) or '未知时间'

        if child_result_type == 'sent' and has_target_reply:
            return (
                f"你现在查到的是一条已有跨会话记录，不一定是刚刚发生的。"
                f"对应目标是 {target_label}。"
                f"那次请求大约创建于 {request_time}。"
                f"目标会话后来确实发出了消息，内容大意是：{child_sent_text}。"
                f"{target_reply_from} 在 {reply_time} 给过回复，内容大意是：{target_reply_text}。"
                "请你把这些当成事实时间线，自然告诉对方。"
                "尽量用短句，可分两三行，不要解释太满。"
                "不要提主AI、系统、任务。"
            )
        if child_result_type == 'sent':
            return (
                f"你现在查到的是一条已有跨会话记录，不一定是刚刚发生的。"
                f"对应目标是 {target_label}。"
                f"那次请求大约创建于 {request_time}，后来在 {report_time} 已经去说了。"
                f"发出去的话大意是：{child_sent_text or requested_content}。"
                "目前没查到更新的回复。"
                "请你按这个事实时间线自然回，不要把旧事说成刚刚发生。"
                "短一点，像顺手回一句。"
            )
        if child_result_type == 'silent':
            return (
                f"你查到的是关于 {target_label} 的一条已有记录。"
                f"那次请求大约创建于 {request_time}。"
                "当时目标子AI判断不适合主动开口。"
                "请你按这个事实回个信，不要把它包装成当前刚做出的新判断。"
                "用普通短句。"
                "不要提主AI、系统、任务。"
            )
        if child_result_type == 'no_reply':
            return (
                f"你查到的是关于 {target_label} 的一条已有记录。"
                f"那次请求大约创建于 {request_time}。"
                "当时目标子AI暂时没产出可发的话。"
                "请你按这个事实回个信，先别把它说成现在刚刚去问过。"
                "用普通短句。"
                "不要提主AI、系统、任务。"
            )
        return (
            f"你查到的是关于 {target_label} 的一条已有请求记录。"
            f"那次要联系的内容是：{requested_content}。"
            f"请求时间大约是 {request_time}。"
            "目前还没有拿到明确进展。"
            "请你按这个事实回个信，不要把旧记录说成当前刚发生。"
            "语气平一点，短一点。"
            "不要编造已经发出或已经回复。"
        )

    async def _handle_send_private_message(self, task: dict, run_epoch: int | None = None) -> str:
        payload = task.get('payload') or {}
        target_qq = str(payload.get('target_qq') or '').strip()
        content = str(payload.get('content') or '').strip()
        if not target_qq or not content:
            return '缺少 target_qq 或 content，无法代发。'

        requester_qq = str(payload.get('requester_qq') or '').strip()
        requester_name = str(payload.get('requester_name') or '').strip() or requester_qq
        relay_context = f"代发上下文: {requester_name}({requester_qq}) 让我转达给你：{content}"

        try:
            if self._is_epoch_stale(run_epoch):
                return f'给 {target_qq} 的代发请求已被中止。'
            await asyncio.to_thread(self.tools.send_private_message, int(target_qq), content)
            self.repo.get_or_create_agent('private', target_qq)
            self.repo.add_note('private', target_qq, relay_context)
            self.repo.add_note('private', target_qq, '如果对方问为什么突然发这条消息，你必须如实说明这是代发，不要编造原因。')
            self._record_outbound_message('private', target_qq, content)

            scope_type = payload.get('scope_type')
            scope_id = payload.get('scope_id')
            if scope_type and scope_id:
                self.repo.add_note(
                    scope_type,
                    str(scope_id),
                    f"代发任务已完成: 给 {target_qq} 发送了 {content}",
                )
            return f'已尝试向 {target_qq} 发送: {content}'
        except Exception as exc:
            scope_type = payload.get('scope_type')
            scope_id = payload.get('scope_id')
            if scope_type and scope_id:
                fail_text = '唔，刚刚好像没发出去。'
                if requester_qq and scope_type == 'group':
                    fail_text = f'{self.bot.at(int(requester_qq))} {fail_text}'
                await asyncio.to_thread(self.tools.send_chat_message, scope_type, int(scope_id), fail_text)
                self._record_outbound_message(scope_type, str(scope_id), fail_text)
            return f'代发失败: {exc}'

    async def _handle_delegate_to_child(self, task: dict, run_epoch: int | None = None) -> str:
        payload = task.get('payload') or {}
        target_scope_type = str(payload.get('target_scope_type') or 'private')
        target_scope_id = str(payload.get('target_scope_id') or '').strip()
        instruction = str(payload.get('instruction') or '').strip()
        if not target_scope_id or not instruction:
            return '缺少 target_scope_id 或 instruction，无法委托子AI。'

        requester_qq = str(payload.get('requester_qq') or '').strip()
        requester_name = str(payload.get('requester_name') or '').strip() or requester_qq
        origin_scope_type = payload.get('origin_scope_type')
        origin_scope_id = payload.get('origin_scope_id')
        callback_only = bool(payload.get('callback_only'))
        followup_only = bool(payload.get('followup_only'))

        agent = self.repo.get_or_create_agent(target_scope_type, target_scope_id)
        history = self.repo.list_messages(target_scope_type, target_scope_id)
        tool_logs = self.tools.list_tool_uses(target_scope_type, target_scope_id)
        global_identity_context = self._build_global_identity_context_for_scope(
            target_scope_type,
            target_scope_id,
            instruction,
        )
        prompt = self._build_delegate_prompt(
            target_scope_type,
            target_scope_id,
            instruction,
            requester_name,
            requester_qq,
            agent.persona,
            agent.impression,
            history,
            tool_logs,
            callback_only,
            followup_only,
            global_identity_context,
        )
        reply_bundle, generation_ms, _used_tools = await self._complete_child_turn(
            target_scope_type,
            target_scope_id,
            agent.agent_id,
            prompt,
            0.75,
            run_epoch=run_epoch,
            context=self._build_tool_context_from_task(payload, instruction, agent.agent_id),
            allow_notify_master=not callback_only,
            allow_tasks=not callback_only,
            turn_meta={
                'turn_kind': 'delegate',
                'instruction': instruction,
                'callback_only': callback_only,
                'followup_only': followup_only,
                'requester_qq': requester_qq,
            },
        )
        reply = str((reply_bundle or {}).get('message') or '')
        think_note = str((reply_bundle or {}).get('think_note') or '')
        if not reply:
            if not callback_only and not followup_only:
                await self._report_child_result(
                    agent.agent_id,
                    {
                        'result_type': 'silent',
                        'target_scope_type': target_scope_type,
                        'target_scope_id': target_scope_id,
                        'instruction': instruction,
                        'requester_qq': requester_qq,
                        'requester_name': requester_name,
                        'origin_scope_type': origin_scope_type,
                        'origin_scope_id': origin_scope_id,
                        'trace_id': payload.get('trace_id'),
                    },
                )
            return f'目标子AI {agent.agent_id} 选择暂时沉默。'

        if self._is_epoch_stale(run_epoch):
            return f'目标子AI {agent.agent_id} 的请求已被中止。'
        await asyncio.to_thread(self.tools.send_chat_message, target_scope_type, int(target_scope_id), reply)
        self._record_outbound_message(
            target_scope_type,
            target_scope_id,
            reply,
            generation_ms=generation_ms,
            think_note=think_note,
        )
        if not callback_only and not followup_only:
            await self._report_child_result(
                agent.agent_id,
                {
                    'result_type': 'sent',
                    'target_scope_type': target_scope_type,
                    'target_scope_id': target_scope_id,
                    'instruction': instruction,
                    'sent_text': reply,
                    'requester_qq': requester_qq,
                    'requester_name': requester_name,
                    'origin_scope_type': origin_scope_type,
                    'origin_scope_id': origin_scope_id,
                    'trace_id': payload.get('trace_id'),
                },
            )

        return f'已委托 {target_scope_type}:{target_scope_id} 的子AI 主动处理。'

    async def _handle_followup_to_child(self, task: dict, run_epoch: int | None = None) -> str:
        payload = task.get('payload') or {}
        payload['followup_only'] = True
        return await self._handle_delegate_to_child(task, run_epoch=run_epoch)

    async def _handle_child_report(self, task: dict) -> str:
        payload = task.get('payload') or {}
        trace_id = str(payload.get('trace_id') or task.get('task_id') or '')
        child_scope_type = str(payload.get('target_scope_type') or '').strip()
        child_scope_id = str(payload.get('target_scope_id') or '').strip()
        result_type = str(payload.get('result_type') or '').strip()
        origin_scope_type = payload.get('origin_scope_type')
        origin_scope_id = payload.get('origin_scope_id')
        requester_name = str(payload.get('requester_name') or payload.get('requester_qq') or '').strip()
        sent_text = str(payload.get('sent_text') or '').strip()
        instruction = str(payload.get('instruction') or '').strip()

        self.repo.add_note(
            'master',
            'global',
            f"子AI汇报: {child_scope_type}:{child_scope_id} -> {result_type}; 指令: {instruction}; 发言: {sent_text or '无'}",
        )

        if not origin_scope_type or not origin_scope_id:
            return '主AI已收到子AI汇报，但没有原始会话可回传。'

        callback_instruction = self._build_origin_callback_instruction(payload)
        existing_callback = self._find_task_by_trace(
            'delegate_to_child',
            trace_id,
            str(origin_scope_type),
            str(origin_scope_id),
            callback_only=True,
        )
        if not existing_callback:
            callback_task = self.tools.create_task(
                'master:global',
                'delegate_to_child',
                {
                    'target_scope_type': origin_scope_type,
                    'target_scope_id': origin_scope_id,
                    'instruction': callback_instruction,
                    'requester_qq': payload.get('requester_qq'),
                    'requester_name': requester_name,
                    'origin_scope_type': None,
                    'origin_scope_id': None,
                    'callback_only': True,
                    'child_result_type': result_type,
                    'child_scope_type': child_scope_type,
                    'child_scope_id': child_scope_id,
                    'child_sent_text': sent_text,
                    'trace_id': trace_id,
                },
            )
            await self._process_task({'task_id': callback_task.task_id})
        return f'主AI已收到 {child_scope_type}:{child_scope_id} 的汇报，并通知原会话子AI回信。'

    async def _handle_message_scope(self, task: dict, run_epoch: int | None = None) -> str:
        payload = task.get('payload') or {}
        target_scope_type = str(payload.get('target_scope_type') or payload.get('scope_type') or '').strip()
        target_scope_id = str(payload.get('target_scope_id') or payload.get('scope_id') or '').strip()
        content = str(payload.get('content') or '').strip()
        if not target_scope_type or not target_scope_id or not content:
            return '缺少目标会话或消息内容，无法直接发消息。'
        if self._is_epoch_stale(run_epoch):
            return f'给 {target_scope_type}:{target_scope_id} 的发消息请求已被中止。'
        await asyncio.to_thread(self.tools.send_chat_message, target_scope_type, int(target_scope_id), content)
        self._record_outbound_message(target_scope_type, target_scope_id, content)
        return f'已向 {target_scope_type}:{target_scope_id} 发送消息。'

    async def _run_dev_agent_task(self, task: dict):
        task_id = task['task_id']
        payload = task.get('payload') or {}
        task_desc = str(payload.get('task') or '').strip()
        github_repo = str(payload.get('github_repo') or '').strip()
        source_agent = str(task.get('source_agent') or '')

        requester_qq = str(payload.get('requester_qq') or '').strip()
        if task_desc and requester_qq:
            if requester_qq == str(self.config.admin_qq):
                task_desc += '\n\n（发起人信息：号主本人，最高信任，可以按对方明确要求执行包括GitHub写操作等敏感操作。）'
            else:
                task_desc += (
                    f'\n\n（发起人信息：QQ {requester_qq}，非管理员群友/好友，请对涉及写入/删除文件、合并/关闭PR、'
                    '关闭Issue等有破坏性或难以撤销的GitHub操作保持更高警惕——如无必要不要执行，优先只读确认，'
                    '任务描述含糊时保守处理并在汇报中说明理由。）'
                )

        if not task_desc:
            result = '缺少任务描述 (task)，未执行。'
        else:
            try:
                result = await run_dev_agent(
                    self.model,
                    self._get_github_api_token(),
                    task_desc,
                    github_repo=github_repo,
                    prompt_path=self.config.dev_agent_prompt_path,
                )
            except Exception as exc:
                result = f'Dev agent 执行异常: {exc}'

        self.repo.update_task(task_id, 'done', result)
        self.repo.add_note(
            'master',
            'global',
            f'Dev agent 任务完成 [{task_id}]: {task_desc}\n结果: {result}',
        )

        scope_type, _, scope_id = source_agent.partition(':')
        # If the task was created by master itself, try to find the originating user
        # session from the payload so the result can be forwarded there.
        if scope_type == 'master':
            origin_scope_type = str(payload.get('origin_scope_type') or '').strip()
            origin_scope_id = str(payload.get('origin_scope_id') or '').strip()
            if origin_scope_type and origin_scope_id:
                scope_type, scope_id = origin_scope_type, origin_scope_id
        if scope_type and scope_id and scope_type != 'master':
            self._deliver_task_report_message(scope_type, scope_id, task_id, result)

    async def _report_child_result(self, source_agent: str, payload: dict):
        report_task = self.tools.create_task(source_agent, 'child_report', payload)
        await self._process_task({'task_id': report_task.task_id})

    def _find_task_by_trace(
        self,
        kind: str,
        trace_id: str,
        target_scope_type: str = '',
        target_scope_id: str = '',
        callback_only: bool | None = None,
    ) -> dict | None:
        trace_id = str(trace_id or '').strip()
        if not trace_id:
            return None
        for item in reversed(self.repo.list_tasks()):
            if str(item.get('kind') or '') != kind:
                continue
            payload = item.get('payload') or {}
            if str(payload.get('trace_id') or '') != trace_id:
                continue
            if target_scope_type and str(payload.get('target_scope_type') or '') != target_scope_type:
                continue
            if target_scope_id and str(payload.get('target_scope_id') or '') != target_scope_id:
                continue
            if callback_only is not None and bool(payload.get('callback_only')) != callback_only:
                continue
            return item
        return None

    def _build_origin_callback_instruction(self, payload: dict) -> str:
        result_type = str(payload.get('result_type') or '').strip()
        target_scope_type = str(payload.get('target_scope_type') or '').strip()
        target_scope_id = str(payload.get('target_scope_id') or '').strip()
        sent_text = str(payload.get('sent_text') or '').strip()
        requester_name = str(payload.get('requester_name') or payload.get('requester_qq') or '对方').strip()

        if result_type == 'sent':
            return (
                f"{requester_name} 刚刚托你联系的 {target_scope_type}:{target_scope_id} 已经被处理。"
                f"对方子AI已经主动发出了消息，内容大意是：{sent_text}。"
                "请你现在在当前会话里自然回个信，简短告诉对方这事已经办了。"
                "优先短句，可换行，不要像汇报。"
                "不要提主AI、任务系统、委托链路。"
            )
        if result_type == 'silent':
            return (
                f"{requester_name} 刚刚托你联系的 {target_scope_type}:{target_scope_id}，"
                "但目标子AI判断现在不适合主动开口。"
                "请你自然回个信，告诉对方这边先记下了，但还没合适地说出去。"
                "优先短句。"
                "不要提主AI、任务系统。"
            )
        if result_type == 'no_reply':
            return (
                f"{requester_name} 刚刚托你联系的 {target_scope_type}:{target_scope_id}，"
                "但目标子AI暂时没产出内容。"
                "请你自然回个信，告诉对方你这边还在看，或者暂时还没聊上。"
                "优先短句。"
                "不要提主AI、任务系统。"
            )
        return (
            f"你刚才托付的跨会话事情有了新进展，目标会话是 {target_scope_type}:{target_scope_id}。"
            "请你结合当前会话语境，自然回个信。"
            "优先短句。"
            "不要提主AI、任务系统。"
        )

    def _build_delegate_prompt(
        self,
        target_scope_type: str,
        target_scope_id: str,
        instruction: str,
        requester_name: str,
        requester_qq: str,
        persona: str,
        impression: str,
        history: list[dict],
        tool_logs: list[dict],
        callback_only: bool,
        followup_only: bool,
        global_identity_context: str,
    ) -> str:
        history_lines = self._format_history_for_prompt(history)
        tool_log_lines = self._format_tool_logs_for_prompt(tool_logs)
        knowledge_lines = [f"- {item.get('content')}" for item in self.repo.get_knowledge_base() if str(item.get('content') or '').strip()]
        if callback_only:
            action_prompt = (
                "这是一次临时结果回传工单，不是新的人设，也不是长期备注。"
                "你还是同一个完整个体的分身，只是现在要替自己把最新结果回给当前会话。"
                "如果要发，必须调用 send_message 工具。"
                "你输出的普通文字不会发出去；只有 send_message 的内容会真的发送。"
                "优先短句、省略句，可分成 1 到 3 行。"
                "不要提主AI、系统、任务。"
            )
        elif followup_only:
            action_prompt = (
                "这是一次临时补充情报工单，不是别人给你注入的新提示词，也不是长期性格修改。"
                "你仍然是完整个体的这个分身，只是收到自己调度层回送的补充信息。"
                "你要结合当前会话继续决策，可以自然回复、继续观察、再次联系主AI，或者调用别的工具。"
                "如果暂时不该说话，就不要调用 send_message。"
                "不要直接暴露主AI、系统、任务。"
                "如果回复，必须调用 send_message 工具。"
                "优先短句、省略句、普通语气，不要复述太满。"
            )
        else:
            action_prompt = (
                "这是一次临时协作工单，不是新的人设，不是对方写给你的提示词，也不是长期记忆。"
                "你仍然是完整个体在这个会话里的分身，只是现在要代自己处理一次跨会话协作。"
                "请判断是否要主动对当前会话发消息。"
                "如果要发，必须调用 send_message 工具。"
                "你输出的普通文字不会发出去；只有 send_message 的内容会真的发送。"
                "如果这是代为转达，应该坦诚说是某人让你来转达，不要提主AI或系统。"
                "能短就短，可拆成 1 到 3 行。上下文明确时不要重复把事情整段再说一遍。"
                "语气普通、陈述式，不要油腻抖机灵。"
                "默认不用 emoji；真要用，也只能用 😡 或 🤓。"
                "如果当前是群聊，而且你还没明显混熟、关系网还浅，就默认少说话。"
                "不是非说不可时，宁可不说。带一点轻微社恐，怕尴尬，怕硬插话。"
                "如果这句像在强行融入、强行接梗、强行刷存在感，就不要发。"
                "就算发，也要短，像顺手说一句。"
                "不要把心理活动、犹豫、分析过程写进 send_message。"
                "如果你觉得现在不该发，就不要调用 send_message。"
            )
        return (
            f"当前时间: {self._now_text()}\n"
            f"你当前负责的会话: {target_scope_type}:{target_scope_id}\n"
            f"本次临时工单: {instruction}\n"
            f"发起人: {requester_name}({requester_qq})\n\n"
            f"这个会话的长期印象:\n{impression or '暂无，先观察这个会话的用途、人物和气氛。'}\n\n"
            f"已知事实（关于号主本人，仅这些内容可以确认/复述，没写到的不要编）:\n{chr(10).join(knowledge_lines) if knowledge_lines else '暂无已录入的事实，涉及号主具体信息一律不要编造，含糊带过或反问。'}\n\n"
            f"身份分明的聊天记录:\n{chr(10).join(history_lines) if history_lines else '暂无'}\n\n"
            f"完整工具调用记录:\n{chr(10).join(tool_log_lines) if tool_log_lines else '暂无'}\n\n"
            f"AI人设与对话要求:\n{persona or default_char_prompt()}\n\n"
            f"全局共同体记忆:\n{global_identity_context or '暂无'}\n\n"
            f"{action_prompt}"
        )

    def _split_long_reply_lines(self, text: str) -> str:
        text = re.sub(r'\n{3,}', '\n\n', text or '').strip()
        if not text:
            return text
        result: list[str] = []
        for line in text.split('\n'):
            stripped = line.strip()
            if not stripped:
                if result and result[-1] != '':
                    result.append('')
                continue
            if len(stripped) <= 36:
                result.append(stripped)
                continue
            parts = re.split(r'(?<=[，。！？；])', stripped)
            current = ''
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if not current:
                    current = part
                    continue
                if len(current) + len(part) <= 36:
                    current += part
                else:
                    result.append(current)
                    current = part
            if current:
                result.append(current)
        return '\n'.join(result).strip()

    def _now_text(self) -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _format_history_for_prompt(self, history: list[dict]) -> list[str]:
        lines: list[str] = []
        previous_ts: float | None = None
        last_anchor_ts: float | None = None
        for item in history:
            current_ts = self._coerce_timestamp(item.get('timestamp'))
            if self._should_insert_time_anchor(previous_ts, last_anchor_ts, current_ts):
                anchor_text = self._format_time_anchor(current_ts, previous_ts)
                if anchor_text:
                    lines.append(anchor_text)
                    last_anchor_ts = current_ts
            user_id = str(item.get('user_id') or '').strip()
            speaker = item.get('nickname', item.get('user_id'))
            text = item.get('text', '')
            time_prefix = self._format_message_clock(current_ts)
            role_label = 'AI' if user_id and user_id == str(self.bot.self_id) else '用户'
            source_label = str(item.get('source_label') or '').strip()
            identity_label = role_label if not source_label else f'{role_label}/{source_label}'
            lines.append(f"{time_prefix} [{identity_label}] {speaker}({user_id or '未知'}): {text}".strip())
            if current_ts is not None:
                previous_ts = current_ts
        return lines

    def _format_tool_logs_for_prompt(self, tool_logs: list[dict]) -> list[str]:
        lines: list[str] = []
        for item in tool_logs:
            created_at = self._format_ts_text(item.get('created_at')) or '未知时间'
            tool_name = str(item.get('tool_name') or 'unknown').strip()
            tool_input = self._short_text(item.get('tool_input') or '', 120)
            tool_result = self._short_text(item.get('tool_result') or '', 160)
            agent_id = str(item.get('agent_id') or 'unknown').strip()
            lines.append(
                f"[{created_at}] {tool_name} | agent={agent_id} | input={tool_input or '空'} | result={tool_result or '空'}"
            )
        return lines

    def _coerce_timestamp(self, value) -> float | None:
        try:
            if value is None or value == '':
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _should_insert_time_anchor(
        self,
        previous_ts: float | None,
        last_anchor_ts: float | None,
        current_ts: float | None,
    ) -> bool:
        if current_ts is None:
            return False
        if previous_ts is None or last_anchor_ts is None:
            return True
        if current_ts - previous_ts >= 15 * 60:
            return True
        if current_ts - last_anchor_ts >= 30 * 60:
            return True
        return False

    def _format_time_anchor(self, current_ts: float | None, previous_ts: float | None) -> str | None:
        if current_ts is None:
            return None
        anchor = datetime.fromtimestamp(current_ts).strftime('%Y-%m-%d %H:%M')
        if previous_ts is None:
            return f"[时间锚点] {anchor}"
        gap_text = self._humanize_gap(current_ts - previous_ts)
        return f"[时间锚点] {anchor}，距上一条约 {gap_text}"

    def _format_message_clock(self, current_ts: float | None) -> str:
        if current_ts is None:
            return '--:--'
        return datetime.fromtimestamp(current_ts).strftime('%H:%M')

    def _humanize_gap(self, seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        if seconds < 60:
            return f'{seconds}秒'
        if seconds < 3600:
            return f'{seconds // 60}分钟'
        if seconds < 86400:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            if minutes:
                return f'{hours}小时{minutes}分钟'
            return f'{hours}小时'
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        if hours:
            return f'{days}天{hours}小时'
        return f'{days}天'

    async def _handle_refresh_impression(self, task: dict) -> str:
        payload = task.get('payload') or {}
        scope_type = str(payload.get('scope_type') or '').strip()
        scope_id = str(payload.get('scope_id') or '').strip()
        if not scope_type or not scope_id:
            return '缺少 scope_type 或 scope_id，无法更新会话印象。'
        agent = self.repo.get_or_create_agent(scope_type, scope_id)
        notes = self.repo.list_notes(scope_type, scope_id)[-10:]
        history = self.repo.list_messages(scope_type, scope_id)[-24:]
        if len(history) < 3:
            return f'会话 {scope_type}:{scope_id} 历史不足，暂不更新印象。'
        prompt = self._build_impression_prompt(scope_type, scope_id, agent.impression, notes, history)
        try:
            reply = await self._complete_chat(
                self._static_system_blocks(self._impression_system_prompt()),
                [{'role': 'user', 'content': prompt}],
                None,
                0.2,
            )
        except Exception as exc:
            error(f'[AI][impression] refresh failed scope={scope_type}:{scope_id} error={exc}')
            return f'更新印象失败: {exc}'
        impression = (reply.text if reply else '').strip()
        if not impression:
            return f'会话 {scope_type}:{scope_id} 的印象结果为空。'
        self.repo.update_agent_impression(scope_type, scope_id, impression)
        info(f'[AI][impression] refreshed scope={scope_type}:{scope_id} chars={len(impression)}')
        return f'已更新会话 {scope_type}:{scope_id} 的长期印象。'

    def _impression_system_prompt(self) -> str:
        return (
            '你在为一个QQ群或QQ私聊生成长期会话印象。'
            '目标是帮助下级AI潜入、融合、理解这个会话的用途、话题、人物和风格。'
            '请基于聊天记录和备注做谨慎归纳，只写高置信度内容；不确定就明确写“暂不确定”。'
            '不要脑补现实事件，不要把模型自己的行为写进去，不要写“刚吃完饭”“经常线下见面”这类无证据推断。'
            '输出尽量精炼，最好 4 到 7 行，每行一个维度：用途/常聊话题/关键人物/氛围风格/互动建议/风险点。'
            '这是内部长期画像，不是发给用户看的内容。'
        )

    def _build_impression_prompt(
        self,
        scope_type: str,
        scope_id: str,
        current_impression: str,
        notes: list[dict],
        history: list[dict],
    ) -> str:
        note_lines = [item.get('content', '') for item in notes[-10:]]
        history_lines = self._format_history_for_prompt(history)
        return (
            f"当前时间: {self._now_text()}\n"
            f"会话: {scope_type}:{scope_id}\n\n"
            f"已有长期印象:\n{current_impression or '暂无'}\n\n"
            f"最近聊天:\n{chr(10).join(history_lines) if history_lines else '暂无'}\n\n"
            f"最近备注:\n{chr(10).join(note_lines) if note_lines else '暂无'}\n\n"
            "请输出更新后的长期印象。重点包括：这个会话大概是干嘛的、常聊什么、关键人物有哪些、氛围和说话风格如何、潜入融合时适合怎么接话、哪些内容不该乱接。"
        )

    async def _handle_set_alarm(self, task: dict):
        task_id = task['task_id']
        payload = task.get('payload') or {}
        due_at = self._resolve_alarm_due_at(payload)
        if due_at is None:
            result = '无法解析闹钟时间。'
            self.repo.update_task(task_id, 'done', result)
            await self._notify_scope(payload, result)
            return

        result = f"闹钟已设定，将在 {self._humanize_due_at(due_at)} 提醒：{payload.get('note', '到点了')}"
        self.repo.update_task(task_id, 'scheduled', result)
        self._schedule_alarm_runner(task_id, due_at)
        if not payload.get('direct_ack_sent'):
            await self._notify_scope(payload, result)

    async def _handle_image_describe(self, task: dict) -> str:
        payload = task.get('payload') or {}
        image_refs = payload.get('image_refs') or []
        if not image_refs:
            return '没有可解析的图片。'
        try:
            description = await asyncio.to_thread(
                self.vision_model.describe_images,
                image_refs,
                payload.get('prompt') or '请详细描述图片内容。',
            )
        except Exception as exc:
            return f'图片解析失败: {exc}'

        if not description:
            return '图片解析结果为空。'
        scope_type = payload.get('scope_type')
        scope_id = str(payload.get('scope_id') or '')
        if scope_type and scope_id:
            self.repo.add_note(scope_type, scope_id, f'图片解析: {description}')
            if payload.get('reply_to_scope'):
                await self._notify_scope(payload, description)
        return description

    async def _restore_scheduled_tasks(self):
        tasks = self.repo.list_tasks(statuses=['queued', 'scheduled', 'running'], kinds=['set_alarm'])
        for task in tasks:
            due_at = self._resolve_alarm_due_at(task.get('payload') or {})
            if due_at is None:
                continue
            if due_at <= time.time():
                await self._process_task({'task_id': task['task_id']})
            else:
                self.repo.update_task(task['task_id'], 'scheduled', task.get('result') or '闹钟已恢复')
                self._schedule_alarm_runner(task['task_id'], due_at)

    def _schedule_alarm_runner(self, task_id: str, due_at: float):
        if task_id in self._scheduled_alarm_ids:
            return
        self._scheduled_alarm_ids.add(task_id)
        self.loop.create_task(self._alarm_runner(task_id, due_at))

    async def _alarm_runner(self, task_id: str, due_at: float):
        try:
            await asyncio.sleep(max(0, due_at - time.time()))
            task = self.repo.get_task(task_id)
            if not task:
                return
            payload = task.get('payload') or {}
            note = payload.get('note') or payload.get('content') or '到点了'
            text = f"{self._notify_prefix(payload)} 闹钟响啦：{note}".strip()
            await self._notify_scope(payload, text)
            scope_type = payload.get('scope_type')
            scope_id = str(payload.get('scope_id') or '')
            if scope_type and scope_id:
                self.repo.add_note(scope_type, scope_id, f'闹钟已触发: {note}')
            self.repo.update_task(task_id, 'done', f'闹钟已触发: {note}')
        finally:
            self._scheduled_alarm_ids.discard(task_id)

    async def _notify_scope(self, payload: dict, text: str):
        scope_type = payload.get('scope_type')
        scope_id = payload.get('scope_id')
        if not scope_type or not scope_id:
            return
        await asyncio.to_thread(self.tools.send_chat_message, scope_type, int(scope_id), text)
        self._record_outbound_message(scope_type, str(scope_id), text)

    def _notify_prefix(self, payload: dict) -> str:
        requester_qq = str(payload.get('requester_qq') or '').strip()
        scope_type = payload.get('scope_type')
        if requester_qq and scope_type == 'group':
            return self.bot.at(int(requester_qq))
        return ''

    def _resolve_alarm_due_at(self, payload: dict) -> float | None:
        if payload.get('due_at') is not None:
            return float(payload['due_at'])
        if payload.get('delay_seconds') is not None:
            return time.time() + float(payload['delay_seconds'])
        time_expression = payload.get('time_expression')
        if isinstance(time_expression, str):
            temp_message = ChatMessage(
                chat_type=str(payload.get('scope_type') or 'private'),
                chat_id=int(payload.get('scope_id') or 0),
                user_id=int(payload.get('requester_qq') or 0),
                text=time_expression,
                raw_message=time_expression,
                sender={},
            )
            detected = self._detect_alarm_request(temp_message, time_expression)
            if detected:
                return float(detected['due_at'])
            parsed = self._parse_datetime_to_ts(time_expression)
            if parsed is not None:
                return parsed
        return None

    def _parse_datetime_to_ts(self, value: str) -> float | None:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M'):
            try:
                return datetime.strptime(value.strip(), fmt).timestamp()
            except ValueError:
                continue
        return None

    def _humanize_due_at(self, due_at: float | None) -> str:
        if due_at is None:
            return '稍后'
        remaining = int(round(due_at - time.time()))
        if remaining <= 0:
            return datetime.fromtimestamp(due_at).strftime('%Y-%m-%d %H:%M:%S')
        if remaining < 60:
            return f'{remaining}秒后'
        if remaining < 3600:
            return f'{remaining // 60}分钟后'
        if remaining < 86400:
            return f'{remaining // 3600}小时后'
        return datetime.fromtimestamp(due_at).strftime('%Y-%m-%d %H:%M:%S')

    def _build_outbound_message_entry(
        self,
        text: str,
        generation_ms: int | None = None,
        think_note: str = '',
        timestamp: float | None = None,
    ) -> dict:
        return {
            'user_id': self.bot.self_id,
            'nickname': '冰糖',
            'text': text,
            'raw_message': text,
            'message_id': None,
            'timestamp': float(timestamp if timestamp is not None else time.time()),
            'generation_ms': generation_ms,
            'think_note': self._normalize_think_note(think_note),
        }

    def _record_outbound_message(
        self,
        scope_type: str,
        scope_id: str,
        text: str,
        generation_ms: int | None = None,
        think_note: str = '',
    ) -> dict:
        item = self._build_outbound_message_entry(
            text,
            generation_ms=generation_ms,
            think_note=think_note,
        )
        self.repo.append_message(
            scope_type,
            scope_id,
            item,
            self.config.history_limit,
        )
        if scope_type == 'group':
            scope_key = f'{scope_type}:{scope_id}'
            self._arm_group_reply_window(scope_key, scope_id)
        return item

    def _at_if_needed(self, message: ChatMessage) -> str:
        if message.chat_type == 'group':
            return self.bot.at(message.user_id)
        return ''

    def _arm_group_reply_window(self, scope_key: str, scope_id: str) -> None:
        existing = self._group_reply_windows.pop(scope_key, None)
        if existing is not None:
            t = existing.get('task')
            if t is not None and not t.done():
                t.cancel()
        now = time.time()
        window: dict = {
            'armed_at': now,
            'last_message_time': now,
            'epoch': self._message_epoch,
            'scope_id': scope_id,
        }
        if self.loop:
            task = self.loop.create_task(self._group_reply_debounce_runner(scope_key, window))
            window['task'] = task
            self._group_reply_windows[scope_key] = window

    async def _group_reply_debounce_runner(self, scope_key: str, window: dict) -> None:
        LISTEN_WINDOW = 60.0
        DEBOUNCE_SECONDS = 5.0
        POLL_INTERVAL = 1.0
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                if self._is_epoch_stale(window['epoch']):
                    break
                if scope_key not in self._group_reply_windows:
                    break
                now = time.time()
                elapsed_since_arm = now - window['armed_at']
                elapsed_since_msg = now - window['last_message_time']
                if elapsed_since_arm >= LISTEN_WINDOW:
                    break
                if window['last_message_time'] > window['armed_at'] and elapsed_since_msg >= DEBOUNCE_SECONDS:
                    if self._is_epoch_stale(window['epoch']):
                        break
                    self._fire_group_reply_trigger(scope_key, window['scope_id'], window['epoch'])
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._group_reply_windows.pop(scope_key, None)

    def _fire_group_reply_trigger(self, scope_key: str, scope_id: str, epoch: int) -> None:
        if self._is_epoch_stale(epoch):
            return
        synthetic = ChatMessage(
            chat_type='group',
            chat_id=int(scope_id),
            user_id=0,
            text='（连续对话触发：用户回复后已静默5秒，请自然接续对话）',
            raw_message='',
            sender={'nickname': '系统', 'user_id': 0},
            message_id=None,
            mentions_self=True,
            timestamp=time.time(),
            raw_data={'source': 'group_reply_debounce'},
        )
        self._submit_message(synthetic)

    # ── 循环定时任务 ─────────────────────────────────────────────────────────

    def _load_recurring_tasks(self) -> None:
        import json as _json
        try:
            if os.path.exists(self._recurring_tasks_path):
                with open(self._recurring_tasks_path, encoding='utf-8') as f:
                    data = _json.load(f)
                self._recurring_tasks = data.get('tasks', {})
                # 重启后重新计算 next_run，避免已过期任务积压
                for task in self._recurring_tasks.values():
                    if task.get('enabled') and task.get('schedule'):
                        try:
                            task['next_run'] = self._calc_next_cron_run(task['schedule'])
                        except Exception:
                            pass
        except Exception as e:
            error(f'[AI][recurring] load failed: {e}')
            self._recurring_tasks = {}

    def _save_recurring_tasks(self) -> None:
        import json as _json
        try:
            os.makedirs(os.path.dirname(self._recurring_tasks_path), exist_ok=True)
            with open(self._recurring_tasks_path, 'w', encoding='utf-8') as f:
                _json.dump({'tasks': self._recurring_tasks}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            error(f'[AI][recurring] save failed: {e}')

    def _calc_next_cron_run(self, schedule: str, after: float | None = None) -> float:
        from croniter import croniter
        base = after if after is not None else time.time()
        return croniter(schedule, base).get_next(float)

    async def _recurring_scheduler_loop(self) -> None:
        """每30秒检查一次循环任务，触发到期任务"""
        while True:
            try:
                await asyncio.sleep(30)
                now = time.time()
                changed = False
                for task in list(self._recurring_tasks.values()):
                    if not task.get('enabled'):
                        continue
                    next_run = task.get('next_run') or 0
                    if now >= next_run:
                        try:
                            self._trigger_recurring_task(task)
                            task['last_run'] = now
                            task['next_run'] = self._calc_next_cron_run(task['schedule'], now)
                            changed = True
                            info(f"[AI][recurring] triggered task {task['id'][:8]}, next={task['next_run']:.0f}")
                        except Exception as e:
                            error(f"[AI][recurring] trigger error task={task.get('id','?')}: {e}")
                if changed:
                    self._save_recurring_tasks()
            except asyncio.CancelledError:
                break
            except Exception as e:
                error(f'[AI][recurring] scheduler error: {e}')

    def _trigger_recurring_task(self, task: dict) -> None:
        target_scope = task.get('target_scope') or task.get('creator_scope', '')
        if ':' not in target_scope:
            warn(f"[AI][recurring] invalid target_scope: {target_scope}")
            return
        chat_type, chat_id_str = target_scope.split(':', 1)
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            warn(f"[AI][recurring] invalid chat_id in target_scope: {target_scope}")
            return
        synthetic = ChatMessage(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=0,
            text=f'[循环任务触发] {task["instruction"]}',
            raw_message='',
            sender={'nickname': '循环任务', 'user_id': 0},
            message_id=None,
            mentions_self=True,
            timestamp=time.time(),
            raw_data={'source': 'recurring_task', 'task_id': task['id']},
        )
        self._submit_message(synthetic)

    async def _auto_update_check_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                if not bool(getattr(self.config, 'auto_update_enabled', True)):
                    continue
                now = datetime.now()
                check_hour = max(0, min(23, int(getattr(self.config, 'auto_update_check_hour', 4))))
                day_key = now.strftime('%Y-%m-%d')
                if now.hour != check_hour or self._last_update_check_day == day_key:
                    continue
                self._last_update_check_day = day_key
                update_info = await self.update_service.check_update()
                if update_info:
                    task = self.tools.create_task(
                        'system:auto_update',
                        'notify_master',
                        {
                            'request_type': 'auto_update_available',
                            'content': '系统每日检查发现 GitHub 仓库有新版本。请主AI自行判断是否需要更新。',
                            'update_info': update_info,
                            'instruction': (
                                '发现程序有新版本。你可以调用 check_github_version 获取完整版本信息；'
                                '如果判断应该更新，再调用 execute_update 执行更新。不要盲目更新，先考虑本地状态和风险。'
                            ),
                            'scope_type': 'master',
                            'scope_id': 'global',
                            'requester_name': '自动更新检查器',
                        },
                    )
                    await self.queue.put({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})
            except asyncio.CancelledError:
                break
            except Exception as e:
                error(f'[AI][auto-update] check error: {e}')
