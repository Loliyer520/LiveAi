import asyncio
import copy
import html
import json
import os
import random
import re
import shlex
import sys
import threading
import time
from datetime import datetime

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None

try:
    import httpx as _httpx_mod
except ImportError:
    _httpx_mod = None

try:
    import requests as _requests_mod
except ImportError:
    _requests_mod = None
from pack.napcat import NapcatBot
from pack.anthropic_chat_model import AnthropicChatModel, AnthropicReply
from pack.search_service import DoubaoSearchService
from pack.vision_model import OpenAICompatibleVisionModel
from pack.update_service import UpdateService
from pack.console_logger import info, warn, error, debug
from core.logger import get_bot_logger, INFO, WARN, ERROR, CAT_API, CAT_CHAT, CAT_TASK, CAT_AGENT
from core.ai_repository import AIRepository
from core.ai_tools_schema import LOOP_TOOL_NAMES, build_tools
from core.config import AIConfig
from core.dev_agent import run_dev_agent
from core.agent_manager import AgentManager
from core.events import ChatMessage
from core.prompt_store import PromptStore, default_char_prompt
from core.test_command import handle_test_command
from core.model_manager import ModelManager
from tool.ai_toolbox import AIToolbox


# ── 代码块转图：检测/分段纯函数（步骤2，暂不接线到发送主链路）──────────────
# 匹配 Markdown 围栏代码块：```lang\n...代码...\n```
# - 开围栏后 info string 第一个 token 作为语言，空则为 None（交给 Pygments 猜）
# - re.DOTALL 让 . 跨行匹配，(.*?) 非贪婪，避免把多个代码块吞成一个
_CODE_FENCE_RE = re.compile(r'```[ \t]*([^\n`]*)\n(.*?)```', re.DOTALL)


def _extract_code_language(info_string: str | None) -> str | None:
    """从围栏 info string 里取语言标识：第一个 token，空则 None。"""
    info = (info_string or '').strip()
    if not info:
        return None
    token = info.split()[0].strip()
    return token or None


def has_code_block(content: str | None) -> bool:
    """内容里是否存在至少一个围栏代码块。"""
    if not content:
        return False
    return _CODE_FENCE_RE.search(content) is not None


def split_code_block_segments(content: str | None) -> list[dict]:
    """把一段文本按代码块切成有序段列表，保持原文顺序。

    返回元素形如：
      {'kind': 'text', 'text': 原始文本片段}
      {'kind': 'code', 'language': str|None, 'code': 代码正文, 'raw': 含围栏的原文}

    规则：
      - 代码块之间/前后的纯文本各自成为一个 text 段（去空后为空的片段丢弃）
      - 多个代码块 → 多个 code 段，顺序穿插，位置与原文一致
      - 'raw' 保留含围栏的原始文本，供降级时原样发送
      - 无代码块时返回单个 text 段（或空列表）
    """
    text = content or ''
    segments: list[dict] = []
    last = 0
    for m in _CODE_FENCE_RE.finditer(text):
        start, end = m.span()
        if start > last:
            pre = text[last:start]
            if pre.strip():
                segments.append({'kind': 'text', 'text': pre})
        code = m.group(2)
        # 去掉代码正文末尾恰好一个换行（闭合围栏前的换行），保留内部结构
        if code.endswith('\n'):
            code = code[:-1]
        segments.append({
            'kind': 'code',
            'language': _extract_code_language(m.group(1)),
            'code': code,
            'raw': m.group(0),
        })
        last = end
    if last < len(text):
        tail = text[last:]
        if tail.strip():
            segments.append({'kind': 'text', 'text': tail})
    return segments


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
        self.model_manager = ModelManager(self.config.models_config_path)
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
        self._pending_scope_tasks = {}
        self._dev_agent_tasks = set()
        # 新版常驻 agent 管理器（与旧版一次性 dev_agent task 并行、互不影响）。
        # report_notifier 指向本类的 _on_agent_report_pending：agent 产生纯文本
        # 挂起内容时被触发，据 AI 忙/闲决定"立即投递给会话AI"或"延后到下次触发"。
        # 事件循环引用在 _run_loop 里通过 set_loop 登记（那时 loop 才建好）。
        self.agent_manager = AgentManager(report_notifier=self._on_agent_report_pending)
        self._resolving_display_names = set()
        self._pending_self_interrupts = {}
        self._message_epoch = 0
        self._stale_message_max_age: float = 120.0  # 超过此秒数的旧消息不再触发回复
        self._group_reply_windows: dict[str, dict] = {}
        # 本次触发消息里的图片引用，按 scope_key 暂存，供 view_image 工具按需解析。
        # 每个 scope 同一时刻只有一个 turn 在跑（scope 锁保证），故直接覆盖即可。
        self._turn_image_refs: dict[str, list[str]] = {}
        # 收藏表情缓存：list_stickers 拉取后按序缓存 URL，供 send_sticker/annotate_sticker 按序号定位。
        self._sticker_cache: list[str] = []
        self._sticker_cache_at: float = 0.0
        self._sticker_cache_ttl = 300.0  # 5分钟内多个会话共用同一份缓存，避免重复请求 NapCat
        self._recurring_tasks: dict[str, dict] = {}
        self._recurring_tasks_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'recurring_tasks.json',
        )
        self._load_recurring_tasks()
        # ── 定期情报轮状态机 ──────────────────────────────────────────
        # 主AI 每 4 小时主动发起一轮情报收集。每个进行中的情报轮在此登记：
        #   round_id -> {
        #       'status': 'collecting' | 'finalizing' | 'done',
        #       'started_at': float, 'deadline': float,
        #       'waiting': set(scope_key),   # 尚未回报的会话
        #       'received': {scope_key: report_text},  # 已回报的会话
        #       'scopes': [(scope_type, scope_id), ...],  # 本轮全部目标会话
        #   }
        self._intelligence_rounds: dict[str, dict] = {}
        # 情报轮参数：活跃判定窗口、回报超时、cron 触发表
        self._intel_active_window = 4 * 3600      # 最近 4 小时有活动视为活跃
        self._intel_report_timeout = 5 * 60       # 单轮回报 5 分钟超时兜底
        self._intel_schedule = '0 */4 * * *'      # 每 4 小时整点触发
        self._intel_next_run: float = 0.0
        # 初始化模型配置（用新的 ModelManager 替换旧的 profile 系统）
        current_model = self.model_manager.get_current_model()
        if current_model:
            info(f'[AI] 使用模型配置 {current_model["display_name"]}')
            self._update_model_from_config(current_model)
        else:
            warn('[AI] models_config.json 为空，使用传入的默认模型')
        # 更新 vision 模型（优先从 roles.vision 取，回退到 vision 段）
        vision_config = self.model_manager.get_role_model('vision')
        roles = self.model_manager.config.get('roles') or {}
        if 'vision' not in roles:
            vision_config = self.model_manager.get_vision_model()
        if vision_config:
            self.vision_model = OpenAICompatibleVisionModel(
                base_url=vision_config['base_url'],
                api_key=vision_config['api_key'],
                model_name=vision_config['model_name'],
            )

    def reload_models_config(self) -> dict:
        """热重载 models_config.json"""
        self.model_manager.reload_config()
        current = self.model_manager.get_current_model()
        if current:
            self._update_model_from_config(current)
            info(f'[AI] models_config.json 已热加载，当前模型: {current["display_name"]}')
            return {'loaded': True, 'current': current['display_name']}
        else:
            return {'loaded': False, 'message': 'models_config.json 无有效渠道'}

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
        # 把事件循环引用交给常驻 agent 管理器，供 send_to_agent 跨线程安全投递。
        self.agent_manager.set_loop(self.loop)
        for _ in range(max(1, self.config.worker_count)):
            self.loop.create_task(self._worker())
        self.loop.create_task(self._restore_scheduled_tasks())
        self.loop.create_task(self._recurring_scheduler_loop())
        self.loop.create_task(self._auto_update_check_loop())
        self.loop.create_task(self._intelligence_scheduler_loop())
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

    # 新版常驻 agent 上报的【兜底】接收 scope：origin_scope 缺失/为空/格式不合法时，
    # 回退投递给 master 会话AI。有 origin_scope 时按其解析出的 scope 分组分发（见 _flush_agent_reports）。
    _AGENT_REPORT_SCOPE_TYPE = 'master'
    _AGENT_REPORT_SCOPE_ID = '0'

    def _on_agent_report_pending(self) -> None:
        """AgentManager 的 report_notifier 回调：有新的 agent 挂起内容待上报时被触发。

        触发方是 agent 常驻循环所在事件循环线程（run_agent_loop 里 _emit_agent_message
        调用 on_agent_message → append 到待上报队列 → 触发本回调）。这里判定会话AI忙/闲：
        - AI 空闲（目标 scope 不在 _active_scope_turns 里）：立即取走待上报内容，
          组装成一条 source='agent_message' 的 ChatMessage 触发会话AI。
        - AI 忙（目标 scope 正在生成）：什么都不做，内容留在待上报队列里，
          等本轮生成结束、_run_message_turn 释放 scope 后的 flush 再带上，
          或下次该 scope 被触发时一起带上。
        """
        try:
            self._flush_agent_reports(only_if_idle=True)
        except Exception as exc:
            error(f'[AI] _on_agent_report_pending 处理失败: {exc}')

    def _flush_agent_reports(self, only_if_idle: bool = True) -> None:
        """把 AgentManager 待上报队列里的 agent 挂起内容按 origin_scope 分组投递给对应会话AI。

        每条 pending report 带有 origin_scope（形如 'group:123'），标识真正创建该 agent
        的会话。这里按 origin_scope 分组，每组分别投递到对应 scope；origin_scope 为空/None
        的回退到 master:0。

        only_if_idle=True 时，仅把【空闲】scope 的分组投递出去；【忙碌】scope 的分组原样
        放回待上报队列（requeue_pending_reports），等该 scope 下次空闲时补投，确保不丢失。
        only_if_idle=False 时对所有目标 scope 无条件投递（用于 scope 刚释放的场景）。
        同一 scope 下多个 agent 的内容合并成一条消息，每段前缀【agent#id】标清各自来源。
        """
        mgr = getattr(self, 'agent_manager', None)
        if mgr is None or not mgr.has_pending_reports():
            return
        reports = mgr.drain_pending_reports()
        if not reports:
            return

        # 按 origin_scope 分组。key 用解析出的 (scope_type, scope_id)，None/空回退 master:0。
        grouped: dict[tuple[str, str], list[dict]] = {}
        for item in reports:
            scope_type, scope_id = self._parse_agent_report_scope(item.get('origin_scope'))
            grouped.setdefault((scope_type, scope_id), []).append(item)

        deferred: list[dict] = []
        for (scope_type, scope_id), items in grouped.items():
            scope_key = self._scope_key(scope_type, scope_id)
            if only_if_idle and scope_key in self._active_scope_turns:
                # 该 scope 忙：本组内容留到队列，等它空闲再补投。
                deferred.extend(items)
                continue
            self._deliver_agent_reports_to_scope(scope_type, scope_id, items)

        if deferred:
            # 忙碌 scope 的内容放回队列，不丢失，下次 flush（本轮释放后 or 新触发）补投。
            mgr.requeue_pending_reports(deferred)

    def _parse_agent_report_scope(self, origin_scope) -> tuple[str, str]:
        """把 origin_scope 字符串（'scope_type:scope_id'）解析成 (scope_type, scope_id)。

        为空/None 或格式不合法时回退到 master:0，保证兜底通路不被破坏。
        """
        raw = str(origin_scope or '').strip()
        if not raw or ':' not in raw:
            return self._AGENT_REPORT_SCOPE_TYPE, self._AGENT_REPORT_SCOPE_ID
        scope_type, _, scope_id = raw.partition(':')
        scope_type = scope_type.strip()
        scope_id = scope_id.strip()
        if not scope_type or scope_id == '':
            return self._AGENT_REPORT_SCOPE_TYPE, self._AGENT_REPORT_SCOPE_ID
        return scope_type, scope_id

    def _deliver_agent_reports_to_scope(self, scope_type: str, scope_id: str, items: list[dict]) -> None:
        """把一个 scope 下的一批 agent 挂起内容合并成一条消息投递给该会话AI。"""
        if not items:
            return
        lines = []
        for item in items:
            agent_id = str(item.get('agent_id') or '?')
            text = str(item.get('text') or '').strip()
            lines.append(f'【agent#{agent_id}】\n{text}')
        body = '\n\n'.join(lines)
        wrapped = (
            '【内部系统通知：以下是后台常驻 agent 的挂起内容（提问/汇报/进度），不是任何人直接对你说的话，'
            '仅供你参考决策。每段以【agent#编号】标注来源。请结合语境自主判断是否处理、是否需要通过 '
            'send_to_agent 给对应 agent 下达进一步指示，或是否需要转达给相关的人。】\n\n'
            f'{body}'
        )
        try:
            chat_id = 0 if scope_type == 'master' else int(scope_id)
        except (TypeError, ValueError):
            # scope_id 不是合法整数：回退 master:0，避免构造 ChatMessage 抛异常丢内容。
            scope_type = self._AGENT_REPORT_SCOPE_TYPE
            chat_id = 0
        report_message = ChatMessage(
            chat_type=scope_type,
            chat_id=chat_id,
            user_id=0,
            text=wrapped,
            raw_message=wrapped,
            sender={'nickname': '常驻agent系统', 'user_id': 0},
            message_id=None,
            mentions_self=True,
            timestamp=time.time(),
            raw_data={'source': 'agent_message', 'agent_count': len(items)},
        )
        self._submit_message(report_message)

    def get_runtime_status(self) -> dict:
        current = self.model_manager.get_current_model()
        mm_config = self.model_manager.config or {}
        channels = mm_config.get('channels') or []
        main_channel = str((mm_config.get('roles') or {}).get('main') or '').strip()
        available = [
            {
                'index': idx,
                'display_name': str(ch.get('name') or ''),
                'model_id': ', '.join(
                    f'{m.get("upstream")}/{m.get("model_id")}' for m in (ch.get('models') or [])
                ),
                'base_url': str(ch.get('strategy') or 'fallback'),
                'active': str(ch.get('name') or '').strip() == main_channel,
            }
            for idx, ch in enumerate(channels)
        ]
        return {
            'enabled': self.config.enabled,
            'ready': bool(self.loop and self.queue),
            'active_profile': current['display_name'] if current else 'none',
            'active_model': current['model_name'] if current else 'none',
            'active_label': current['display_name'] if current else 'none',
            'queue_size': self.queue.qsize() if self.queue else 0,
            'worker_count': max(1, self.config.worker_count),
            'scheduled_alarm_count': len(self._scheduled_alarm_ids),
            'available_models': available,
        }

    def switch_model_profile(self, requested: str) -> tuple[bool, str]:
        """兼容旧 API，实际调用 ModelManager"""
        success, msg = self.model_manager.switch_model(requested)
        if success:
            current = self.model_manager.get_current_model()
            if current:
                self._update_model_from_config(current)
        return success, msg

    def _update_model_from_config(self, model_config: dict):
        """根据 ModelManager 提供的配置更新 self.model"""
        self.model = AnthropicChatModel(
            base_url=model_config['base_url'],
            api_key=model_config['api_key'],
            model_name=model_config['model_name'],
            messages_path=model_config['messages_path'],
        )
        # 让常驻 agent 的无工具总结 AI（summarize_agent）复用同一个模型实例。
        try:
            self.agent_manager.set_model(self.model)
        except Exception:
            pass

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = str(value or '')
        if not value:
            return ''
        if len(value) <= 8:
            return '*' * len(value)
        return f'{value[:4]}{"*" * (len(value) - 8)}{value[-4:]}'

    def get_model_profiles_info(self) -> list[dict]:
        """从 ModelManager 新结构构造返回值"""
        result = []
        channels = self.model_manager.config.get('channels') or []
        upstreams_map = {u['name']: u for u in (self.model_manager.config.get('upstreams') or [])}
        current = self.model_manager.get_current_model()
        current_display = current['display_name'] if current else ''

        for ch in channels:
            for m in (ch.get('models') or []):
                upstream_name = str(m.get('upstream') or '')
                model_id = str(m.get('model_id') or '')
                upstream = upstreams_map.get(upstream_name) or {}
                display = f'{upstream_name}/{model_id}'
                result.append({
                    'name': display,
                    'label': model_id,
                    'active': display == current_display,
                    'base_url': str(upstream.get('base_url') or ''),
                    'model_name': model_id,
                    'messages_path': str(upstream.get('messages_path') or '') or '/v1/messages',
                    'api_key_set': bool(upstream.get('api_key')),
                    'api_key_masked': self._mask_secret(upstream.get('api_key', '')),
                    'overridden_fields': [],
                })
        return result

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
            {'command': '/model', 'aliases': ['/model list', '/model reload'], 'scope': 'admin', 'description': '管理员查看/重载模型配置'},
            {'command': '/upstream', 'aliases': ['/upstream list', '/upstream add', '/upstream remove'], 'scope': 'admin', 'description': '管理 API 上游（base_url/api_key）'},
            {'command': '/channel', 'aliases': ['/channel list', '/channel add', '/channel remove'], 'scope': 'admin', 'description': '管理渠道（模型池 + 轮询策略）'},
            {'command': '/role', 'aliases': ['/role list', '/role set main <渠道名>'], 'scope': 'admin', 'description': '为 AI 角色绑定渠道'},
            {'command': '/test', 'aliases': ['/test all', '/test alls'], 'scope': 'admin', 'description': '测试渠道/模型可用性，无参数列出列表'},
            {'command': '/stop', 'aliases': [], 'scope': 'admin', 'description': '管理员立即结束整个 Python 进程'},
            {'command': '/restart', 'aliases': [], 'scope': 'admin', 'description': '管理员原地重启 bot 自身 Python 进程'},
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
        if str(message.user_id) == str(self.bot.self_id):
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

    async def _get_stickers(self, force: bool = False) -> list[str]:
        """账号级共享的收藏表情缓存，避免不同会话短时间内重复拉取同一份列表。"""
        now = time.time()
        if not force and self._sticker_cache and (now - self._sticker_cache_at) < self._sticker_cache_ttl:
            return self._sticker_cache
        stickers = list(await asyncio.to_thread(self.bot.fetch_custom_face))
        self._sticker_cache = stickers
        self._sticker_cache_at = now
        return self._sticker_cache

    def _build_trigger_message_entry(self, message: ChatMessage, cleaned: str) -> dict:
        return {
            'user_id': message.user_id,
            'nickname': message.nickname,
            'text': cleaned or message.text,
            'raw_message': message.raw_message,
            'message_id': message.message_id,
            'timestamp': message.timestamp,
            'source_label': self._message_source_label(message),
            'source_kind': self._message_source_kind(message),
            'raw_source': message.raw_data.get('source'),
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
            if scope_key not in self._pending_scope_turns:
                self._pending_scope_turns[scope_key] = []
            _pending_len = len(self._pending_scope_turns[scope_key])
            self._pending_scope_turns[scope_key].append({
                'kind': 'message',
                'message': item['message'],
                'cleaned': item['cleaned'],
                'agent_id': item['agent_id'],
                'scope_key': scope_key,
                'deferred_count': 1,
                'trigger_messages': list(item.get('trigger_messages') or []),
            })
            get_bot_logger().info(CAT_CHAT, scope_key, f'消息排队合并: 会话正忙, 当前排队数={_pending_len + 1}')
            return False
        self._active_scope_turns.add(scope_key)
        return True

    def _release_scope_turn(self, item: dict) -> dict | None:
        scope_key = str(item.get('scope_key') or '')
        if not scope_key:
            return None
        pending = None
        while True:
            pending_list = self._pending_scope_turns.get(scope_key)
            if not pending_list:
                break
            pending = pending_list.pop(0)
            if not pending_list:
                del self._pending_scope_turns[scope_key]
            # 跳过已过期的延迟消息，避免突然回复旧内容
            if self._is_message_stale(pending.get('message')):
                info(f'[AI][release] dropping stale pending message scope={scope_key}')
                pending = None
                continue
            break
        if pending:
            history_seed = item.get('followup_history_seed')
            if history_seed:
                pending['history_seed'] = [dict(entry) for entry in history_seed]
            return pending
        if self._promote_pending_scope_task(scope_key):
            return None
        self._active_scope_turns.discard(scope_key)
        return None

    def _take_pending_scope_turn(self, item: dict) -> dict | None:
        scope_key = str(item.get('scope_key') or '')
        if not scope_key:
            return None
        while True:
            pending_list = self._pending_scope_turns.get(scope_key)
            if not pending_list:
                return None
            pending = pending_list.pop(0)
            if not pending_list:
                del self._pending_scope_turns[scope_key]
            # 跳过已过期的延迟消息
            if self._is_message_stale(pending.get('message')):
                info(f'[AI][take_pending] dropping stale pending message scope={scope_key}')
                continue
            return pending

    def _scope_key_for_task(self, task: dict) -> str | None:
        """返回 task turn 会向其生成/发送消息的目标 scope_key；非发送类 task 返回 None。"""
        kind = task.get('kind')
        payload = task.get('payload') or {}
        if kind in ('delegate_to_child', 'followup_to_child'):
            scope_type = str(payload.get('target_scope_type') or 'private')
            scope_id = str(payload.get('target_scope_id') or '').strip()
        elif kind == 'message_scope':
            scope_type = str(payload.get('target_scope_type') or payload.get('scope_type') or '').strip()
            scope_id = str(payload.get('target_scope_id') or payload.get('scope_id') or '').strip()
        else:
            return None
        if not scope_type or not scope_id:
            return None
        return self._scope_key(scope_type, scope_id)

    def _reserve_task_scope(self, scope_key: str, item: dict) -> bool:
        """为 task turn 占用目标 scope 会话锁。若忙则按 FIFO 延后，返回 False。"""
        if scope_key in self._active_scope_turns:
            info(f'[AI][reserve] scope busy, deferring task: {scope_key}')
            self._pending_scope_tasks.setdefault(scope_key, []).append({
                'kind': 'task',
                'task_id': item['task_id'],
                'message_epoch': int(item.get('message_epoch', self._message_epoch)),
            })
            return False
        self._active_scope_turns.add(scope_key)
        return True

    def _promote_pending_scope_task(self, scope_key: str) -> bool:
        """scope 空闲后，若有延后的 task 则重新入队并保持 scope 占用，返回是否已提升。"""
        queue = self._pending_scope_tasks.get(scope_key)
        while queue:
            pending = queue.pop(0)
            if not queue:
                self._pending_scope_tasks.pop(scope_key, None)
            if self._is_epoch_stale(pending.get('message_epoch')):
                continue
            pending['scope_prereserved'] = True
            self.queue.put_nowait(pending)
            return True
        self._pending_scope_tasks.pop(scope_key, None)
        return False

    def _release_task_scope(self, scope_key: str):
        """task turn 结束后释放 scope；优先让延后的 message，其次 task 接手。"""
        if not scope_key:
            return
        pending = None
        pending_list = self._pending_scope_turns.get(scope_key)
        if pending_list:
            pending = pending_list.pop(0)
            if not pending_list:
                del self._pending_scope_turns[scope_key]
        if pending:
            self.queue.put_nowait(pending)
            return
        if self._promote_pending_scope_task(scope_key):
            return
        self._active_scope_turns.discard(scope_key)

    def _cancel_active_requests(self):
        self._message_epoch += 1
        self._pending_scope_turns.clear()
        self._pending_scope_tasks.clear()
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

    def _is_message_stale(self, message) -> bool:
        """检查消息时间戳是否超过最大允许时效。"""
        ts = getattr(message, 'timestamp', None)
        if ts is None:
            return False
        return (time.time() - float(ts)) > self._stale_message_max_age

    async def _run_message_turn(self, item: dict):
        try:
            await self._process_message(item)
        finally:
            followup = self._release_scope_turn(item)
            if followup:
                await self.queue.put(followup)
            else:
                # 本轮结束且该 scope 无后续排队：若期间有 agent 挂起内容因 AI 忙
                # 被延后，此刻趁 scope 空出把它们一并投递（方向A的"忙碌延后"落地）。
                try:
                    self._flush_agent_reports(only_if_idle=True)
                except Exception as exc:
                    error(f'[AI] 释放 scope 后 flush agent 上报失败: {exc}')

    async def _handle_model_command(self, message: ChatMessage, cleaned: str):
        if not self._is_admin_message(message):
            self.bot.send_text(message.chat_type, message.chat_id, '这个指令你先别动。')
            return

        try:
            parts = shlex.split(cleaned)
        except ValueError as exc:
            self.bot.send_text(message.chat_type, message.chat_id, f'模型指令解析失败: {exc}')
            return

        if len(parts) == 1:
            self.bot.send_text(
                message.chat_type,
                message.chat_id,
                f"{self.model_manager.get_summary_text()}\n\n{self.model_manager.list_models()}\n\n用法:\n{self._model_command_help_text()}",
            )
            return

        sub = str(parts[1] or '').strip().lower()

        if sub in {'help', '?', 'h'}:
            self.bot.send_text(message.chat_type, message.chat_id, self._model_command_help_text())
            return

        if sub in {'list', 'ls'}:
            self.bot.send_text(message.chat_type, message.chat_id, self.model_manager.list_models())
            return

        if sub in {'current', 'status'}:
            self.bot.send_text(message.chat_type, message.chat_id, self.model_manager.get_summary_text())
            return

        if sub == 'reload':
            result = self.reload_models_config()
            msg = f"模型配置已重载，当前模型: {result.get('current')}" if result.get('loaded') else str(result.get('message') or '重载失败')
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        if sub in {'switch', 'use'}:
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标模型，例如：/model switch 0')
                return
            success, msg = self.model_manager.switch_model(parts[2], persist=True)
            if success:
                current = self.model_manager.get_current_model()
                if current:
                    self._update_model_from_config(current)
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        if sub in {'channel', 'channels'}:
            await self._handle_model_channel_command(message, parts[2:])
            return

        success, msg = self.model_manager.switch_model(parts[1], persist=True)
        if success:
            current = self.model_manager.get_current_model()
            if current:
                self._update_model_from_config(current)
        self.bot.send_text(message.chat_type, message.chat_id, msg)

    def _model_command_help_text(self) -> str:
        return (
            '模型管理指令:\n'
            '/model\n'
            '/model list\n'
            '/model current\n'
            '/model reload\n'
            '/model switch <序号或名称>\n'
            '/model channel list\n'
            '/model channel add name=<名称> base_url=<地址> api_key=<密钥> models=<显示名:模型ID,模型ID2> [messages_path=/v1/messages]\n'
            '/model channel update <序号或名称> key=value ...\n'
            '/model channel remove <序号或名称>'
        )

    @staticmethod
    def _parse_model_kv_args(items: list[str]) -> tuple[dict, list[str]]:
        kv: dict[str, str] = {}
        unknown: list[str] = []
        for item in items:
            if '=' not in item:
                unknown.append(item)
                continue
            key, value = item.split('=', 1)
            key = str(key or '').strip().lower()
            if not key:
                unknown.append(item)
                continue
            kv[key] = value.strip()
        return kv, unknown

    async def _handle_model_channel_command(self, message: ChatMessage, args: list[str]):
        if not args:
            self.bot.send_text(message.chat_type, message.chat_id, self.model_manager.list_channels())
            return

        action = str(args[0] or '').strip().lower()
        if action in {'list', 'ls'}:
            self.bot.send_text(message.chat_type, message.chat_id, self.model_manager.list_channels())
            return

        if action == 'add':
            kv, unknown = self._parse_model_kv_args(args[1:])
            if unknown:
                self.bot.send_text(message.chat_type, message.chat_id, f'无法识别的参数: {" ".join(unknown)}')
                return
            success, msg = self.model_manager.add_channel(
                name=kv.get('name', ''),
                base_url=kv.get('base_url', ''),
                api_key=kv.get('api_key', ''),
                messages_path=kv.get('messages_path', '/v1/messages'),
                models=kv.get('models', ''),
            )
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        if action in {'update', 'edit'}:
            if len(args) < 2:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标渠道，例如：/model channel update 0 name=xxx')
                return
            target = args[1]
            kv, unknown = self._parse_model_kv_args(args[2:])
            if unknown:
                self.bot.send_text(message.chat_type, message.chat_id, f'无法识别的参数: {" ".join(unknown)}')
                return
            success, msg = self.model_manager.update_channel(
                target,
                name=kv.get('name'),
                base_url=kv.get('base_url'),
                api_key=kv.get('api_key'),
                messages_path=kv.get('messages_path'),
                models=kv.get('models') if 'models' in kv else None,
            )
            if success:
                current = self.model_manager.get_current_model()
                if current:
                    self._update_model_from_config(current)
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        if action in {'remove', 'rm', 'del', 'delete'}:
            if len(args) < 2:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标渠道，例如：/model channel remove 0')
                return
            success, msg = self.model_manager.remove_channel(args[1])
            if success:
                current = self.model_manager.get_current_model()
                if current:
                    self._update_model_from_config(current)
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        self.bot.send_text(
            message.chat_type,
            message.chat_id,
            '不支持的渠道操作。\n可用子命令: list / add / update / remove',
        )

    def _is_admin_message(self, message: ChatMessage) -> bool:
        return int(message.user_id or 0) == int(self.config.admin_qq)

    # ─── 上游命令 /upstream ───

    async def _handle_upstream_command(self, message: ChatMessage, cleaned: str):
        import shlex
        try:
            parts = shlex.split(cleaned)
        except ValueError as exc:
            self.bot.send_text(message.chat_type, message.chat_id, f'指令解析失败: {exc}')
            return

        sub = str(parts[1] if len(parts) > 1 else '').strip().lower()

        if not sub or sub in {'list', 'ls'}:
            self.bot.send_text(message.chat_type, message.chat_id, self.model_manager.list_upstreams_text())
            return

        if sub == 'add':
            kv, _ = self._parse_model_kv_args(parts[2:])
            ok, msg = self.model_manager.add_upstream(
                name=kv.get('name', ''), base_url=kv.get('base_url', ''),
                api_key=kv.get('api_key', ''), messages_path=kv.get('messages_path', ''),
            )
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        if sub in {'update', 'edit'}:
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标，例: /upstream update deepseek api_key=sk-new')
                return
            kv, _ = self._parse_model_kv_args(parts[3:])
            ok, msg = self.model_manager.update_upstream(parts[2], **kv)
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        if sub in {'remove', 'rm', 'del'}:
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标，例: /upstream remove deepseek')
                return
            ok, msg = self.model_manager.remove_upstream(parts[2])
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        help_text = (
            '上游管理指令:\n'
            '/upstream list\n'
            '/upstream add name=<名称> base_url=<地址> api_key=<密钥> [messages_path=]\n'
            '/upstream update <名称> key=value ...\n'
            '/upstream remove <名称>'
        )
        self.bot.send_text(message.chat_type, message.chat_id, help_text)

    # ─── 渠道命令 /channel ───

    async def _handle_channel_command(self, message: ChatMessage, cleaned: str):
        import shlex
        try:
            parts = shlex.split(cleaned)
        except ValueError as exc:
            self.bot.send_text(message.chat_type, message.chat_id, f'指令解析失败: {exc}')
            return

        sub = str(parts[1] if len(parts) > 1 else '').strip().lower()

        if not sub or sub in {'list', 'ls'}:
            self.bot.send_text(message.chat_type, message.chat_id, self.model_manager.list_channels_text())
            return

        if sub == 'add':
            kv, _ = self._parse_model_kv_args(parts[2:])
            ok, msg = self.model_manager.add_channel(
                name=kv.get('name', ''), strategy=kv.get('strategy', 'fallback'),
                models=kv.get('models', ''),
            )
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        if sub in {'update', 'edit'}:
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标，例: /channel update 主力渠道 strategy=random')
                return
            kv, _ = self._parse_model_kv_args(parts[3:])
            ok, msg = self.model_manager.update_channel(parts[2], **kv)
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        if sub in {'remove', 'rm', 'del'}:
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标，例: /channel remove 主力渠道')
                return
            ok, msg = self.model_manager.remove_channel(parts[2])
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        if sub in {'addmodel', 'addm'}:
            if len(parts) < 5:
                self.bot.send_text(message.chat_type, message.chat_id, '用法: /channel addmodel <渠道> <上游> <模型ID>')
                return
            ok, msg = self.model_manager.add_model_to_channel(parts[2], parts[3], parts[4])
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        help_text = (
            '渠道管理指令:\n'
            '/channel list\n'
            '/channel add name=<名称> [strategy=fallback|random|roundrobin] [models=上游:模型ID,...]\n'
            '/channel update <名称> key=value ...\n'
            '/channel remove <名称>\n'
            '/channel addmodel <渠道名> <上游名> <模型ID>'
        )
        self.bot.send_text(message.chat_type, message.chat_id, help_text)

    # ─── 角色命令 /role ───

    async def _handle_role_command(self, message: ChatMessage, cleaned: str):
        import shlex
        try:
            parts = shlex.split(cleaned)
        except ValueError as exc:
            self.bot.send_text(message.chat_type, message.chat_id, f'指令解析失败: {exc}')
            return

        sub = str(parts[1] if len(parts) > 1 else '').strip().lower()

        if not sub or sub in {'list', 'ls'}:
            self.bot.send_text(message.chat_type, message.chat_id, self.model_manager.list_roles_text())
            return

        if sub == 'set':
            if len(parts) < 4:
                self.bot.send_text(message.chat_type, message.chat_id, '用法: /role set <角色> <渠道名>\n角色可用: main tiered agent dev_agent vision')
                return
            ok, msg = self.model_manager.set_role(parts[2], parts[3])
            if ok:
                self.reload_models_config()
            self.bot.send_text(message.chat_type, message.chat_id, msg)
            return

        help_text = (
            '角色管理指令:\n'
            '/role list\n'
            '/role set <角色> <渠道名>\n'
            '角色: main / tiered / agent / dev_agent / vision'
        )
        self.bot.send_text(message.chat_type, message.chat_id, help_text)

    # ─── 测试命令 /test ───

    async def _handle_test_command(self, message: ChatMessage, cleaned: str):
        if not self._is_admin_message(message):
            self.bot.send_text(message.chat_type, message.chat_id, '这个指令你先别动。')
            return

        await handle_test_command(message, cleaned, self.model_manager, self.bot)



    def _is_master_message(self, message: ChatMessage) -> bool:
        """检查消息是否来自主人"""
        master_qq = int(getattr(self.config, 'master_qq', 0))
        if master_qq == 0:
            return False
        return int(message.user_id or 0) == master_qq

    # 号主 QQ：命令类指令（/、# 开头）只允许该账号触发
    _COMMAND_MASTER_QQ = 241898129

    def _is_command_master(self, message: ChatMessage) -> bool:
        """命令权限校验：仅号主本人（QQ 241898129）可触发斜杠/井号命令。"""
        try:
            return int(message.user_id or 0) == self._COMMAND_MASTER_QQ
        except (TypeError, ValueError):
            return False

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

        if command == '/restart':
            self._cancel_active_requests()
            self.bot.send_text(message.chat_type, message.chat_id, '正在重启…')
            warn('[AI] /restart requested, re-executing process in place')
            # 原地替换当前进程重启，不依赖外部守护进程
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            # 使用绝对路径的 main.py，避免相对路径因工作目录变化而失败
            _repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            _main_script = os.path.join(_repo_dir, 'main.py')
            try:
                os.execv(sys.executable, [sys.executable, _main_script])
            except Exception as _e:
                error(f'[AI] /restart execv failed: {_e}')
                self.bot.send_text(message.chat_type, message.chat_id, f'重启失败: {_e}')
            return

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
        _MAX_FALLBACK_ATTEMPTS = 3
        _tool_count = len(tools) if tools else 0
        _msg_count = len(messages) if messages else 0
        for attempt in range(_MAX_FALLBACK_ATTEMPTS):
            current = self.model_manager.get_current_model()
            _model_name = current['display_name'] if current else 'unknown'
            _api_url = f'{self.model.base_url}{self.model.messages_path}'
            info(
                f'[AI][api] request model={_model_name} '
                f'url={_api_url} '
                f'messages={_msg_count} tools={_tool_count} '
                f'temp={temperature} attempt={attempt + 1}/{_MAX_FALLBACK_ATTEMPTS}'
            )
            _api_start = time.perf_counter()
            try:
                _reply = await asyncio.to_thread(
                    self.model.complete,
                    system_blocks,
                    messages,
                    tools,
                    self.model.model_name,
                    temperature,
                )
                _api_ms = int((time.perf_counter() - _api_start) * 1000)
                _text_len = len(_reply.text) if _reply and _reply.text else 0
                _tool_calls = len(_reply.tool_calls) if _reply and _reply.tool_calls else 0
                info(
                    f'[AI][api] response model={_model_name} '
                    f'ms={_api_ms} '
                    f'text_len={_text_len} tool_calls={_tool_calls} '
                    f'stop={_reply.stop_reason if _reply else "none"}'
                )
                get_bot_logger().info(CAT_API, '', f'API 调用成功 model={_model_name} ms={_api_ms}ms text_len={_text_len} tool_calls={_tool_calls} stop={_reply.stop_reason if _reply else "none"}')
                return _reply
            except Exception as exc:
                _api_ms = int((time.perf_counter() - _api_start) * 1000)
                _exc_name = type(exc).__name__
                # 判定是否为可 fallback 的上游异常：httpx 超时 / requests 超时 / anthropic 5xx
                _is_httpx_timeout = (
                    _httpx_mod is not None and isinstance(exc, _httpx_mod.TimeoutException)
                ) or _exc_name == 'TimeoutException'
                _is_requests_timeout = (
                    _requests_mod is not None and isinstance(exc, _requests_mod.exceptions.Timeout)
                ) or _exc_name == 'Timeout'
                _is_api_5xx = False
                if (
                    _anthropic is not None and isinstance(exc, _anthropic.APIStatusError)
                ) or _exc_name == 'APIStatusError':
                    _status = getattr(exc, 'status_code', 0) or 0
                    if 500 <= _status < 600:
                        _is_api_5xx = True
                # RuntimeError 里也可能包装了上游 5xx（如 502 Bad Gateway），按消息内容判定
                _is_runtime_5xx = (
                    _exc_name == 'RuntimeError'
                    and 'status=5' in str(exc)
                )
                _is_fallbackable = _is_httpx_timeout or _is_requests_timeout or _is_api_5xx or _is_runtime_5xx

                error(
                    '[AI][model] request failed '
                    f"model={current['display_name'] if current else 'unknown'} "
                    f"base_url={self.model.base_url} "
                    f'fallbackable={_is_fallbackable} '
                    f'attempt={attempt + 1}/{_MAX_FALLBACK_ATTEMPTS} '
                    f'error={exc}'
                )
                get_bot_logger().error(CAT_API, '', f'API 调用异常 model={_model_name} ms={_api_ms}ms fallbackable={_is_fallbackable} attempt={attempt+1}/{_MAX_FALLBACK_ATTEMPTS} error={_exc_name}: {exc}')

                if not _is_fallbackable or attempt >= _MAX_FALLBACK_ATTEMPTS - 1:
                    raise

                # 通知 ModelManager 推进 fallback 索引，再用新配置重建 self.model
                self.model_manager.notify_failure('main')
                next_model = self.model_manager.get_current_model()
                if next_model:
                    info(
                        f'[AI][model] fallback switching to {next_model["display_name"]}'
                    )
                    self._update_model_from_config(next_model)
                else:
                    warn('[AI][model] fallback: no next model available')
        return None
    async def _enqueue_message(self, message: ChatMessage):
        scope_type = message.chat_type
        scope_id = str(message.chat_id)
        info(
            f'[AI][recv] scope={scope_type}:{scope_id} '
            f'user={message.nickname}({message.user_id}) '
            f'mid={message.message_id} '
            f'text_len={len(message.text or "")} '
            f'source={self._message_source_label(message)} '
            f'mentions_self={message.mentions_self}'
        )
        agent = await asyncio.to_thread(self.repo.get_or_create_agent, scope_type, scope_id)
        cleaned = self._clean_text(message)
        source_kind = self._message_source_kind(message)
        source_label = self._message_source_label(message)

        # 命令权限统一收口：以 / 或 # 开头的命令类消息只允许号主(241898129)触发。
        # 其他任何人（私聊/群）发命令一律静默 return，不回应。仅拦截真实用户来源，
        # 内部来源（后台任务回执、admin_webui）不受影响，避免误伤自然聊天照常处理。
        if (
            str(cleaned or '').startswith(('/', '#'))
            and source_kind not in ('internal_task', 'admin_webui')
            and not self._is_command_master(message)
        ):
            return

        if cleaned in {'/on', '/off', '/clean', '/stop', '/restart'}:
            await self._handle_power_command(message, cleaned)
            return

        if cleaned.startswith('/model'):
            await self._handle_model_command(message, cleaned)
            return

        if cleaned.startswith('/upstream'):
            await self._handle_upstream_command(message, cleaned)
            return

        if cleaned.startswith('/channel'):
            await self._handle_channel_command(message, cleaned)
            return

        if cleaned.startswith('/role'):
            await self._handle_role_command(message, cleaned)
            return

        if cleaned.startswith('/test'):
            await self._handle_test_command(message, cleaned)
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

        _has_pending = await asyncio.to_thread(
            self.repo.append_message,
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
            self.config.diary_size,
        )
        if _has_pending:
            await self._maybe_schedule_diary_summarization(scope_type, scope_id)
        await asyncio.to_thread(
            self.repo.touch_user_identity, message.user_id, message.nickname, scope_type, scope_id
        )
        agent = await asyncio.to_thread(self.repo.get_or_create_agent, scope_type, scope_id)
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

        # 意图检测器已移除：闹钟/联系/全局设定/状态查询等一律交给 AI 自主判断，
        # 通过它自己的工具（create_task、notify_master 等）执行，符合"AI 完全自主运行"的初衷。

        # 过滤已过期的旧消息：如果消息时间戳距离当前太久，直接丢弃不触发回复
        if self._is_message_stale(message):
            info(
                f'[AI][recv] stale message dropped '
                f'age={time.time() - float(message.timestamp):.0f}s '
                f'scope={scope_type}:{scope_id}'
            )
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
        self.repo.append_message(scope_type, scope_id, entry, self.config.history_limit, self.config.diary_size)
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
                    debug(f'[AI][worker] stale epoch, skipping kind={kind}')
                    continue
                debug(
                    f'[AI][worker] dequeue kind={kind} '
                    f'queue_size={self.queue.qsize()}'
                )
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
        # _record_outbound_message 现为 async（append_message 已移出事件循环）。
        # 本方法是同步的指令快通道，用 fire-and-forget 调度落库，不阻塞指令响应。
        if self.loop is not None:
            self.loop.create_task(
                self._record_outbound_message(message.chat_type, str(message.chat_id), text)
            )

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
        # 内部来源：dev_agent 任务汇报，需正常唤醒 AI，不能被系统昵称判断误伤
        if message.user_id == 0 and message.raw_data.get('source') == 'dev_agent_task_report':
            return 'internal_task'
        # 内部来源：新版常驻 agent 挂起内容上报，同样按内部任务处理正常唤醒 AI
        if message.user_id == 0 and message.raw_data.get('source') == 'agent_message':
            return 'internal_task'
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
            'internal_task': '后台任务',
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
            get_bot_logger().info(CAT_CHAT, f'{message.chat_type}:{message.chat_id}', f'AI 触发: private 私聊, user={message.nickname}({message.user_id})')
            return True
        if message.mentions_self:
            info(f'[AI][trigger] mentions_self=True scope={message.chat_type}:{message.chat_id} user={message.user_id}')
            get_bot_logger().info(CAT_CHAT, f'{message.chat_type}:{message.chat_id}', f'AI 触发: @提及, user={message.nickname}({message.user_id})')
            return True
        lowered = cleaned.lower()
        if any(word.lower() in lowered for word in agent.trigger_words):
            get_bot_logger().info(CAT_CHAT, f'{message.chat_type}:{message.chat_id}', f'AI 触发: 触发词, user={message.nickname}({message.user_id}) word={next(w for w in agent.trigger_words if w.lower() in lowered)}')
            return True
        result = random.random() < agent.trigger_rate
        if not result and message.chat_type == 'group':
            debug(f'[AI][trigger] skipped by trigger_rate={agent.trigger_rate} scope={message.chat_type}:{message.chat_id}')
        elif result:
            get_bot_logger().info(CAT_CHAT, f'{message.chat_type}:{message.chat_id}', f'AI 触发: 随机概率 rate={agent.trigger_rate:.2f}, user={message.nickname}({message.user_id})')
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
        _proc_start = time.perf_counter()
        _deferred = int(item.get('deferred_count') or 0)
        _trig_count = len(item.get('trigger_messages') or [])
        info(
            f'[AI][process] start scope={scope_type}:{scope_id} '
            f'user={message.nickname}({message.user_id}) '
            f'mid={message.message_id} '
            f'text_len={len(cleaned)} '
            f'deferred={_deferred} '
            f'trigger_msgs={_trig_count}'
        )
        get_bot_logger().info(CAT_CHAT, f'{scope_type}:{scope_id}', f'AI 对话开始: 触发消息数={_trig_count}, 排队合并数={_deferred}, user={message.nickname}({message.user_id})')
        agent = await asyncio.to_thread(self.repo.get_or_create_agent, scope_type, scope_id)
        self._maybe_resolve_display_name(scope_type, scope_id, agent)
        trigger_messages = list(item.get('trigger_messages') or [self._build_trigger_message_entry(message, cleaned)])
        _diary_ctx = await asyncio.to_thread(self.repo.get_diary_context, scope_type, scope_id)
        _diary_summaries = _diary_ctx['summaries']
        history = self._flatten_diary_context(_diary_ctx)
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
        global_identity_context = self._build_global_identity_context_for_message(message, combined_trigger_text or cleaned)
        # 图片不再自动解析：把本次触发的图片引用按 scope 暂存，交给 AI 用 view_image 工具按需查看。
        scope_key = self._scope_key(scope_type, scope_id)
        if image_refs:
            self._turn_image_refs[scope_key] = list(image_refs)
            info(
                '[AI][image] detected '
                f'count={len(image_refs)} scope={scope_type}:{scope_id} '
                f'source={self._message_source_label(message)} '
                f'refs={self._summarize_image_refs(image_refs)}'
            )
        else:
            self._turn_image_refs.pop(scope_key, None)
        # image_context 现在只是"有几张图可看"的提示，具体内容由 view_image 拉取。
        image_context = f'本次消息包含 {len(image_refs)} 张图片' if image_refs else None

        # 群上下文：群人数、群主、管理员、成员列表
        group_context = await self._build_group_context(scope_type, scope_id)

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
                group_context,
                int(item.get('deferred_count') or 0),
                agent.display_name,
                _diary_summaries,
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
            history = self._flatten_diary_context(await asyncio.to_thread(self.repo.get_diary_context, scope_type, scope_id))
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
        outbound_entry = await self._record_outbound_message(
            message.chat_type,
            str(message.chat_id),
            reply,
            generation_ms=generation_ms,
            think_note=think_note,
            tool_context_messages=(reply_bundle or {}).get('tool_context_messages'),
        )
        _proc_ms = int((time.perf_counter() - _proc_start) * 1000)
        info(
            f'[AI][process] done scope={scope_type}:{scope_id} '
            f'ms={_proc_ms} '
            f'reply_len={len(reply)} '
            f'gen_ms={generation_ms} '
            f'think_len={len(think_note)}'
        )
        get_bot_logger().info(CAT_CHAT, f'{scope_type}:{scope_id}', f'AI 对话完成: ms={_proc_ms}ms reply_len={len(reply)} gen_ms={generation_ms}')
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
            '3. 如果用户要求更新程序、检查版本、重启系统等系统级操作，必须调用 notify_master 转交主AI处理（request_type: system_operation），不要用 dev_agent 手动操作。',
            '4. 如果 AI 工具备忘里存在跨会话协作上下文，对方追问时必须诚实说明来源；如果追问进度，要优先问主AI。',
            '5. 【关键】凡是遇到不知道、不清楚、不了解、缺少背景、跨会话信息可能不一致、事实可能过期、工具结果看不懂、或任何不确定的情况，必须立即调用 notify_master 联系主AI同步，不要硬猜、不要装懂、不要编造，防止情报差和错误传递。与主AI保持高频沟通是你的核心能力。',
            '6. 如果当前消息像是在补全上一条，例如只发QQ号、只补一句内容、只说”他回了”，你要结合最近上下文理解。',
            '7. 如果消息来源不是QQ好友私聊或正常群消息，不要把它当普通网友闲聊。',
            '8. 私聊回复尽量自然，不要带@，也不要每次都叫对方名字。',
            '9. 群聊如果还没混熟、关系网还浅、没有稳定互动对象，就默认少说话，先观察。',
            '10. 群聊里除非被点名、被@、被直接问到、能提供明确帮助，或者这句接得特别自然，否则宁可不说。',
            '11. 带一点轻微社恐，怕尴尬；如果感觉会硬插话、会掉地上、会显得没混熟，就别发。',
            '12. 信息已经很明确时，优先用短句和省略句，不要赘述同一个对象或问题。',
            '13. 默认分成 1 到 3 行短消息，每行不要太长。',
            '14. 语气要普通、陈述式、去AI化，不要写得像助手、客服、总结器。',
            '15. 幽默要克制，能不用就不用；如果用了，只能是顺手的冷幽默，不要油腻抖机灵。',
            '16. 不要主动用 emoji；真要用，也只能用 😡 或 🤓。',
            '17. 如果当前消息与旧任务无关，不要主动重复播报旧任务。',
            '18. 注意聊天记录里的时间锚点和间隔；如果已经隔了很久，要意识到语境可能已经变了。',
            '19. 不要假设自己刚刚做过现实行为；如果没在QQ里真实完成，就别说已经做了。',
            '20. 如果当前消息提到了你在其他会话认识的人，优先参考全局共同体记忆，不要装作完全不认识。',
            '21. 别人问你刚才在干嘛时，只能回答看消息、回消息、看群、发呆这类低风险内容；不确定就直接承认不确定。',
            '22. 被质疑时先收一下，优先承认记错或说得不准，不要暴躁，不要越圆越假。',
            '23. 需要查 AI 工具备忘时，先调用 memory_list / memory_get 工具读取，不要假设备忘内容。',
            '24. 遇到你不确定、或有时效性的问题（新闻、行情、近期事件等），调用 web_search 工具查一下，不要凭印象瞎编。',
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
                    '发言方式：【关键】要发消息给用户，必须调用 send_message 工具，'
                    '你直接输出的普通文字不会被发送。'
                    '如果需要思考分析，用 <thinking>...</thinking> 包裹写在 send_message 的 content 里，'
                    '这部分会自动过滤掉不会发给用户，用户只看到思考标签外的正常内容。'
                    '如果觉得现在不该说话，调用 stay_silent 工具结束本回合——'
                    '不要把想说的话写成普通文字或塞进思考区来“假装沉默”，'
                    '真要说就 send_message，真不想说就 stay_silent，二者选其一。',
                ]
            )
        return [
            {
                'type': 'text',
                'text': '\n'.join(parts),
            }
        ]

    @staticmethod
    def _stamp_cache_control_on_message(message: dict) -> None:
        """No-op: cache_control removed for CCM compatibility."""
        return

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
        group_context: str = '',
        deferred_count: int = 0,
        display_name: str = '',
        diary_summaries: list[dict] | None = None,
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
                    group_context,
                    deferred_count,
                    display_name,
                    diary_summaries,
                ),
            }
        )
        messages = self._build_char_prefill_messages(persona)
        messages += self._build_tool_prefill_messages()

        # --- Prompt cache breakpoint 3/4: end of static prefill ---
        # Mark the last prefill message so char_prefill + tool_prefill form a
        # cacheable prefix.  If prefill is empty (no persona), skip gracefully.
        if messages:
            self._stamp_cache_control_on_message(messages[-1])

        history_messages = self._build_role_based_history_messages(history)

        # --- Prompt cache breakpoint 4/4: rolling history prefix ---
        # Place a breakpoint a few messages before the end of history so that
        # appending new messages doesn't immediately invalidate the cache.
        _HISTORY_CACHE_TAIL_BUFFER = 4  # keep last N messages uncached
        if len(history_messages) > _HISTORY_CACHE_TAIL_BUFFER:
            bp_idx = len(history_messages) - 1 - _HISTORY_CACHE_TAIL_BUFFER
            self._stamp_cache_control_on_message(history_messages[bp_idx])

        messages += history_messages
        messages.append(
            {
                'role': 'user',
                'content': self._build_trigger_user_message(trigger_messages),
            }
        )
        return {'system': system_blocks, 'messages': messages}

    def _build_tool_prefill_messages(self) -> list[dict]:
        """注入一段伪造对话，强化模型记住必须用 send_message 工具发消息。"""
        return [
            {
                'role': 'user',
                'content': (
                    '请牢记一条核心规则：你输出的普通文字不会发送给用户。'
                    '想要发消息，必须调用 send_message 工具，传入 content 参数。'
                    '如果需要先思考，把思考写进 send_message 的 content 里，并用 <thinking>...</thinking> 包裹；'
                    '系统会自动过滤 thinking 标签，用户只会看到标签外的正常回复。'
                    '如果你决定不回复，什么都不做即可，不要把理由写成普通文字输出。'
                ),
            },
            {
                'role': 'assistant',
                'content': (
                    '好的，我记住了。'
                    '在接下来的所有对话中，我将严格遵守：'
                    '只要需要发消息，必须调用 send_message 工具；'
                    '如果需要思考，我会把思考放在 send_message content 的 <thinking>...</thinking> 内；'
                    '真正想发给用户看的话，写在 thinking 标签外面。'
                    '如果我决定不回复，我会直接结束本轮，不会把不回复的理由写成普通文字。'
                ),
            },
        ]

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
        group_context: str = '',
        deferred_count: int = 0,
        display_name: str = '',
        diary_summaries: list[dict] | None = None,
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
        ]
        if group_context:
            parts += [
                '',
                '群信息:',
                group_context,
            ]
        if diary_summaries:
            parts += ['', '历史记忆摘要（从旧到新，每段约50条消息的浓缩）:']
            for s in diary_summaries:
                parts.append(f'【第{int(s.get("index", 0)) + 1}段】{str(s.get("text") or "")[:600]}')
        parts += [
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
            parts.extend([
                '',
                f'本次消息包含图片（{image_context}）：如果需要看懂图片内容才能好好回应，'
                '调用 view_image 工具查看（index 从 1 开始，按消息里图片出现顺序）；'
                '纯表情、跟你无关的图不用看。',
            ])
        file_refs = self._extract_file_refs(message.raw_message or '')
        if file_refs:
            file_lines = []
            for f in file_refs:
                size_str = f' ({f["file_size"] // 1024}KB)' if f.get('file_size') else ''
                file_lines.append(f'  - 文件名: {f["file_name"]}{size_str}  file_id: {f["file_id"]}')
            parts.extend(['', '消息中包含以下文件（如需下载请调用 download_file 工具）：', '\n'.join(file_lines)])
        return '\n'.join(parts)

    def _is_internal_tool_report_item(self, item: dict) -> bool:
        """判断一条历史/触发条目是否为内部工具回执（dev_agent 任务结果 / 常驻 agent 汇报 /
        notify_master 回传等）。与 _message_source_kind 的判定保持一致：
        source_kind=='internal_task'，或原始来源属于内部来源，或 user_id==0。"""
        source_kind = str(item.get('source_kind') or '').strip()
        if source_kind == 'internal_task':
            return True
        raw_source = item.get('raw_source')
        if raw_source is None:
            raw = item.get('raw_data')
            if isinstance(raw, dict):
                raw_source = raw.get('source')
        if str(raw_source or '').strip() in ('dev_agent_task_report', 'agent_message'):
            return True
        try:
            if int(item.get('user_id')) == 0:
                return True
        except (TypeError, ValueError):
            pass
        return False

    def _xml_attr_escape(self, value) -> str:
        return (
            str(value if value is not None else '')
            .replace('&', '&amp;')
            .replace('"', '&quot;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
        )

    def _sanitize_block_body(self, text) -> str:
        """轻量兜底：只对正文中可能与 <user_msg>/<tool_report> 包裹标签混淆的
        特定标签串做替换，把其起始的 `<` 换成全角 `＜`，避免正文里恰好出现这些
        标签导致模型解析块边界错乱。不做全量 XML 转义，保留其余代码/符号可读性。"""
        s = str(text if text is not None else '')
        for token in ('</user_msg>', '</tool_report>', '<user_msg', '<tool_report'):
            s = s.replace(token, '＜' + token[1:])
        return s

    def _wrap_user_msg_block(self, items: list[dict]) -> str:
        """把一段连续的真实用户消息合并成一个 <user_msg> 块。逐条保留原有
        `HH:MM 昵称(uid): 内容` 行，块属性 from/time 取第一条。"""
        if not items:
            return ''
        first = items[0]
        nickname = first.get('nickname', first.get('user_id'))
        first_ts = self._coerce_timestamp(first.get('timestamp'))
        time_str = self._format_message_clock(first_ts)
        inner = '\n'.join(self._sanitize_block_body(self._format_history_item_for_user_message(it)) for it in items)
        return (
            f'<user_msg from="{self._xml_attr_escape(nickname)}" '
            f'time="{self._xml_attr_escape(time_str)}">{inner}</user_msg>'
        )

    def _wrap_tool_report_block(self, item: dict, note: str = '') -> str:
        """把一条内部工具回执包成独立的 <tool_report> 块。source 取该条 nickname，
        time 取该条 timestamp。note 非空时作为额外属性附加（用于触发轮定性提示）。"""
        source = item.get('nickname', item.get('user_id'))
        current_ts = self._coerce_timestamp(item.get('timestamp'))
        time_str = self._format_message_clock(current_ts)
        text = self._sanitize_block_body(item.get('text') or '')
        note_attr = f' note="{self._xml_attr_escape(note)}"' if note else ''
        return (
            f'<tool_report source="{self._xml_attr_escape(source)}" '
            f'time="{self._xml_attr_escape(time_str)}"{note_attr}>{text}</tool_report>'
        )

    def _render_pending_user_segment(self, pending_items: list[dict]) -> str:
        """把一段（跨到下一个 assistant 之前）累积的条目按时间顺序渲染成一个
        role='user' 的字符串：真实用户消息合并进 <user_msg>，内部回执各自独立成
        <tool_report>，两类块按原始时序穿插排列。"""
        blocks: list[str] = []
        user_run: list[dict] = []
        for it in pending_items:
            if self._is_internal_tool_report_item(it):
                if user_run:
                    blocks.append(self._wrap_user_msg_block(user_run))
                    user_run = []
                blocks.append(self._wrap_tool_report_block(it))
            else:
                user_run.append(it)
        if user_run:
            blocks.append(self._wrap_user_msg_block(user_run))
        return '\n'.join(blocks)

    def _build_role_based_history_messages(self, history: list[dict]) -> list[dict]:
        messages: list[dict] = []
        pending_items: list[dict] = []

        def flush_pending():
            nonlocal pending_items
            if pending_items:
                messages.append({'role': 'user', 'content': self._render_pending_user_segment(pending_items)})
                pending_items = []

        for item in history:
            user_id = str(item.get('user_id') or '').strip()
            tool_context_messages = self._normalize_tool_context_messages(item.get('tool_context_messages'))
            text = str(item.get('text') or '').strip()
            if user_id and user_id == str(self.bot.self_id):
                # 遇到 assistant 条目：先把累积的 user 段（内部回执+用户消息按时序）落盘
                flush_pending()
                if tool_context_messages:
                    messages.extend(tool_context_messages)
                    continue
                if not text:
                    continue
                messages.append({'role': 'assistant', 'content': text})
                continue
            if not text:
                continue
            pending_items.append(item)
        flush_pending()
        return messages

    def _build_trigger_user_message(self, trigger_messages: list[dict]) -> str:
        items = [item for item in trigger_messages if str(item.get('text') or '').strip()]
        if not items:
            return '暂无新消息'
        blocks: list[str] = []
        user_run: list[dict] = []
        for it in items:
            if self._is_internal_tool_report_item(it):
                if user_run:
                    blocks.append(self._wrap_user_msg_block(user_run))
                    user_run = []
                # 触发轮的内部回执额外附上定性提示，避免模型把系统内部异步结果当成用户发言外发
                blocks.append(self._wrap_tool_report_block(
                    it,
                    note='这是系统内部异步结果，默认只更新记忆、消化即可，非必要不要调用 send_message 对外发送',
                ))
            else:
                user_run.append(it)
        if user_run:
            blocks.append(self._wrap_user_msg_block(user_run))
        return '\n'.join(blocks) if blocks else '暂无新消息'

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
        _tool_start = time.perf_counter()
        info(
            f'[AI][tool] exec scope={scope_type}:{scope_id} '
            f'agent={agent_id} tool={name} '
            f'input_keys={list(tool_input.keys())}'
        )
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
                version_info = await self.update_service.get_version_info()
                result = json.dumps(version_info, ensure_ascii=False, indent=2)
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
        elif name == 'download_file':
            file_id = str(tool_input.get('file_id') or '').strip()
            file_name = str(tool_input.get('file_name') or 'file').strip()
            if not file_id:
                result = 'error: file_id 为空，无法下载文件。'
            else:
                try:
                    file_info = self.bot.get_file(file_id)
                except Exception as e:
                    file_info = None
                    result = f'获取文件信息失败: {e}'
                if file_info is not None:
                    size = file_info.get('size') or 0
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
                        src_path = file_info.get('file') or ''
                        try:
                            if src_path and pathlib.Path(src_path).exists():
                                shutil.copy2(src_path, dest_path)
                            elif file_info.get('url'):
                                await asyncio.to_thread(_urllib_req.urlretrieve, file_info['url'], str(dest_path))
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
        elif name == 'create_agent':
            instruction = str(tool_input.get('instruction') or '').strip()
            if not instruction:
                result = 'error: instruction 为空，未创建 agent。'
            else:
                # 记录创建该 agent 的会话 scope，供后续按 scope 投递上报（投递逻辑下个任务接）。
                origin_scope = f'{scope_type}:{scope_id}' if scope_type and str(scope_id) != '' else None
                try:
                    new_agent_id = self.agent_manager.create_agent(instruction, origin_scope=origin_scope)
                    # 启动常驻循环：使用 roles.agent 独立模型配置
                    role_model_config = self.model_manager.get_role_model('agent')
                    if role_model_config:
                        agent_model = AnthropicChatModel(
                            base_url=role_model_config['base_url'],
                            api_key=role_model_config['api_key'],
                            model_name=role_model_config['model_name'],
                            messages_path=role_model_config['messages_path'],
                        )
                    else:
                        agent_model = self.model
                    agent_task = self.loop.create_task(
                        self.agent_manager.run_agent_loop(
                            new_agent_id,
                            agent_model,
                            self._get_github_api_token(),
                            prompt_path=self.config.agent_prompt_path,
                            on_agent_message=self.agent_manager.on_agent_message,
                        )
                    )
                    self.agent_manager.register_agent_task(new_agent_id, agent_task)
                    result = f'已创建常驻 agent，agent_id: {new_agent_id}，已开始执行任务。'
                except Exception as exc:
                    result = f'创建 agent 失败: {exc}'
        elif name == 'send_to_agent':
            target_agent_id = str(tool_input.get('agent_id') or '').strip()
            message = str(tool_input.get('message') or '').strip()
            if not target_agent_id:
                result = 'error: agent_id 为空，未发送。'
            elif not message:
                result = 'error: message 为空，未发送。'
            else:
                ok = self.agent_manager.send_to_agent(target_agent_id, {'role': 'user', 'content': message})
                result = f'已向 agent {target_agent_id} 发送消息。' if ok else f'向 agent {target_agent_id} 发送失败（可能不存在或队列异常）。'
        elif name == 'peek_agent':
            target_agent_id = str(tool_input.get('agent_id') or '').strip()
            if not target_agent_id:
                result = 'error: agent_id 为空，无法查看进度。'
            else:
                result = await self.agent_manager.summarize_agent(target_agent_id, 'progress')
        elif name == 'list_agents':
            agents = self.agent_manager.list_agents()
            if not agents:
                result = '当前没有常驻 agent。'
            else:
                lines = [f'共 {len(agents)} 个常驻 agent：']
                for item in agents:
                    lines.append(
                        f"- {item.get('agent_id')} | {item.get('status')} | "
                        f"消息数:{item.get('message_count')} | "
                        f"来源:{item.get('origin_scope') or '未知'} | "
                        f"{item.get('instruction_summary') or ''}"
                    )
                result = '\n'.join(lines)
        elif name == 'destroy_agent':
            target_agent_id = str(tool_input.get('agent_id') or '').strip()
            summarize = bool(tool_input.get('summarize', False))
            if not target_agent_id:
                result = 'error: agent_id 为空，未销毁。'
            else:
                destroy_result = await self.agent_manager.destroy_agent(target_agent_id, summarize)
                removed = destroy_result.get('removed')
                summary = destroy_result.get('summary')
                if removed:
                    result = f'agent {target_agent_id} 已销毁。'
                    if summary:
                        result += f'\n销毁前总结：\n{summary}'
                else:
                    result = f'agent {target_agent_id} 不存在或已被移除。'
        elif name == 'view_image':
            scope_key = self._scope_key(scope_type, scope_id)
            refs = self._turn_image_refs.get(scope_key) or []
            if not refs:
                result = '本次消息里没有可查看的图片。'
            else:
                try:
                    index = int(tool_input.get('index') or 1)
                except (TypeError, ValueError):
                    index = 1
                if index < 1 or index > len(refs):
                    result = f'图片序号超出范围（共 {len(refs)} 张，index 从 1 开始）。'
                else:
                    question = str(tool_input.get('question') or '').strip()
                    prompt = question or '请详细描述图片，尤其关注人物、文字、场景、动作、情绪和梗。'
                    try:
                        desc = await asyncio.to_thread(
                            self.vision_model.describe_images,
                            [refs[index - 1]],
                            prompt,
                        )
                        result = desc.strip() if desc else '图片解析结果为空。'
                    except Exception as exc:
                        result = f'图片解析失败: {exc}'
        elif name == 'list_stickers':
            try:
                stickers = await self._get_stickers(force=bool(tool_input.get('refresh')))
            except Exception as exc:
                stickers = None
                result = f'获取收藏表情失败: {exc}'
            if stickers is not None:
                if not stickers:
                    result = '你的账号还没有收藏任何表情。'
                else:
                    notes = self.repo.get_setting('sticker_notes', {}) or {}
                    lines = [f'共 {len(stickers)} 个收藏表情：']
                    for i, url in enumerate(stickers, start=1):
                        note = str(notes.get(url) or '').strip()
                        note_str = note if note else '（无备注，建议用 annotate_sticker 打备注）'
                        lines.append(f'{i}. {note_str}')
                    result = '\n'.join(lines)
        elif name == 'annotate_sticker':
            try:
                stickers = await self._get_stickers()
            except Exception:
                stickers = []
            try:
                index = int(tool_input.get('index') or 0)
            except (TypeError, ValueError):
                index = 0
            note = str(tool_input.get('note') or '').strip()
            if not stickers:
                result = '拿不到收藏表情列表，无法打备注，请先调用 list_stickers。'
            elif index < 1 or index > len(stickers):
                result = f'表情序号超出范围（共 {len(stickers)} 个，index 从 1 开始）。'
            elif not note:
                result = 'note 为空，未保存备注。'
            else:
                url = stickers[index - 1]
                notes = dict(self.repo.get_setting('sticker_notes', {}) or {})
                notes[url] = note
                self.repo.set_setting('sticker_notes', notes)
                result = f'已给第 {index} 个表情打备注：{note}'
        elif name == 'send_sticker':
            try:
                stickers = await self._get_stickers()
            except Exception:
                stickers = []
            try:
                index = int(tool_input.get('index') or 0)
            except (TypeError, ValueError):
                index = 0
            if not stickers:
                result = '拿不到收藏表情列表，无法发送，请先调用 list_stickers。'
            elif index < 1 or index > len(stickers):
                result = f'表情序号超出范围（共 {len(stickers)} 个，index 从 1 开始）。'
            else:
                url = stickers[index - 1]
                try:
                    target_id = int(scope_id)
                except (TypeError, ValueError):
                    target_id = scope_id
                try:
                    await asyncio.to_thread(self.bot.send_image, scope_type, target_id, url)
                    result = f'已发送第 {index} 个表情。'
                except Exception as exc:
                    result = f'发送表情失败: {exc}'
        elif name == 'send_local_image':
            import base64 as _base64
            import pathlib as _pathlib
            raw_path = str(tool_input.get('path') or '').strip()
            caption = str(tool_input.get('caption') or '').strip() or None
            ALLOWED_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
            MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
            if not raw_path:
                result = 'error: path 为空，未发送。'
            else:
                proj_root = _pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                allowed_dir = (proj_root / 'data' / 'images').resolve()
                candidate = _pathlib.Path(raw_path)
                if not candidate.is_absolute():
                    candidate = allowed_dir / candidate
                try:
                    resolved = candidate.resolve()
                except Exception as exc:
                    resolved = None
                    result = f'error: 路径解析失败: {exc}'
                if resolved is not None:
                    # 白名单前缀校验：resolve() 后必须仍落在 data/images 内，防 .. 与软链接逃逸
                    if not (resolved == allowed_dir or allowed_dir in resolved.parents):
                        result = 'error: 只允许发送项目 data/images/ 目录内的图片，路径被拒绝。'
                    elif not resolved.exists() or not resolved.is_file():
                        result = 'error: 文件不存在或不是普通文件。'
                    elif resolved.suffix.lower() not in ALLOWED_EXTS:
                        result = f'error: 不支持的图片格式（仅支持 {", ".join(sorted(ALLOWED_EXTS))}）。'
                    else:
                        try:
                            file_size = resolved.stat().st_size
                        except Exception:
                            file_size = 0
                        if file_size > MAX_IMAGE_SIZE:
                            result = f'error: 图片过大（{file_size // 1024 // 1024}MB），超过 10MB 限制。'
                        else:
                            try:
                                data_bytes = resolved.read_bytes()
                                b64 = _base64.b64encode(data_bytes).decode('ascii')
                                file_arg = f'base64://{b64}'
                                try:
                                    target_id = int(scope_id)
                                except (TypeError, ValueError):
                                    target_id = scope_id
                                response = await asyncio.to_thread(
                                    self.tools.send_chat_image, scope_type, target_id, file_arg, caption
                                )
                                mid = None
                                if isinstance(response, dict):
                                    mid = (response.get('data') or {}).get('message_id')
                                if mid is not None:
                                    result = f'已发送图片 {resolved.name}，message_id: {mid}'
                                else:
                                    result = f'已发送图片 {resolved.name}。'
                            except Exception as exc:
                                result = f'发送图片失败: {exc}'
        elif name == 'manage_upstream':
            action = str(tool_input.get('action') or '').strip()
            if action == 'list':
                result = self.model_manager.list_upstreams_text()
            elif action == 'add':
                _ok, result = self.model_manager.add_upstream(
                    name=str(tool_input.get('name') or '').strip(),
                    base_url=str(tool_input.get('base_url') or '').strip(),
                    api_key=str(tool_input.get('api_key') or '').strip(),
                    messages_path=str(tool_input.get('messages_path') or '').strip(),
                )
            elif action == 'update':
                _fields = {k: v for k, v in tool_input.items() if k not in ('action', 'name') and v is not None}
                _ok, result = self.model_manager.update_upstream(str(tool_input.get('name') or '').strip(), **_fields)
            elif action == 'remove':
                _ok, result = self.model_manager.remove_upstream(str(tool_input.get('name') or '').strip())
            elif action == 'balance':
                import requests as _requests
                _base_url = str(tool_input.get('base_url') or '').strip().rstrip('/')
                _api_key = str(tool_input.get('api_key') or '').strip()
                if not _base_url:
                    _uname = str(tool_input.get('name') or '').strip()
                    for _u in (self.model_manager.config.get('upstreams') or []):
                        if _u.get('name') == _uname:
                            _base_url = str(_u.get('base_url') or '').strip().rstrip('/')
                            _api_key = str(_u.get('api_key') or _api_key).strip()
                            break
                if not _base_url:
                    result = '缺少 base_url，无法查询余额。'
                else:
                    _headers = {'Authorization': f'Bearer {_api_key}', 'x-api-key': _api_key, 'Accept': 'application/json'}
                    _candidates = [_base_url]
                    for _sfx in ('/anthropic', '/v1', '/compatible-mode/v1'):
                        if _base_url.endswith(_sfx):
                            _candidates.append(_base_url[:-len(_sfx)])
                    _bal_paths = ['/dashboard/billing/credit_grants', '/v1/dashboard/billing/credit_grants', '/dashboard/billing/subscription']
                    _found = False
                    for _cb in _candidates:
                        for _path in _bal_paths:
                            _url = _cb.rstrip('/') + _path
                            try:
                                _resp = await asyncio.to_thread(_requests.get, _url, headers=_headers, timeout=10)
                                if _resp.status_code == 200:
                                    try:
                                        _data = _resp.json()
                                    except ValueError:
                                        continue
                                    _total = _data.get('total_granted') or _data.get('hard_limit_usd')
                                    _used = _data.get('total_used')
                                    _rem = _data.get('total_available') or _data.get('soft_limit_usd')
                                    if _total is not None or _rem is not None:
                                        _parts = []
                                        if _rem is not None:
                                            _parts.append(f'剩余 ${float(_rem):.2f}')
                                        if _used is not None:
                                            _parts.append(f'已用 ${float(_used):.2f}')
                                        if _total is not None:
                                            _parts.append(f'总额 ${float(_total):.2f}')
                                        result = ' | '.join(_parts) if _parts else str(_data)
                                        _found = True
                                        break
                            except Exception:
                                continue
                        if _found:
                            break
                    if not _found:
                        result = '该上游不支持余额查询（未找到标准接口）。'
            else:
                result = f'manage_upstream: 未知 action={action!r}，可用: list add update remove balance'
        elif name == 'manage_channel':
            action = str(tool_input.get('action') or '').strip()
            if action == 'list':
                result = self.model_manager.list_channels_text()
            elif action == 'add':
                _ok, result = self.model_manager.add_channel(
                    name=str(tool_input.get('name') or '').strip(),
                    strategy=str(tool_input.get('strategy') or 'fallback').strip(),
                )
            elif action == 'update':
                _fields = {k: v for k, v in tool_input.items() if k not in ('action', 'name') and v is not None}
                _ok, result = self.model_manager.update_channel(str(tool_input.get('name') or '').strip(), **_fields)
            elif action == 'remove':
                _ok, result = self.model_manager.remove_channel(str(tool_input.get('name') or '').strip())
                if _ok:
                    _cur = self.model_manager.get_current_model()
                    if _cur:
                        self._update_model_from_config(_cur)
            elif action == 'addmodel':
                _ok, result = self.model_manager.add_model_to_channel(
                    str(tool_input.get('name') or '').strip(),
                    str(tool_input.get('upstream') or '').strip(),
                    str(tool_input.get('model_id') or '').strip(),
                )
            elif action == 'removemodel':
                try:
                    _midx = int(tool_input.get('model_index') or 0)
                except (TypeError, ValueError):
                    _midx = 0
                _ok, result = self.model_manager.remove_model_from_channel(
                    str(tool_input.get('name') or '').strip(), _midx
                )
            else:
                result = f'manage_channel: 未知 action={action!r}，可用: list add update remove addmodel removemodel'
        elif name == 'manage_role':
            action = str(tool_input.get('action') or '').strip()
            if action == 'list':
                result = self.model_manager.list_roles_text()
            elif action == 'set':
                _ok, result = self.model_manager.set_role(
                    str(tool_input.get('role') or '').strip(),
                    str(tool_input.get('channel') or '').strip(),
                )
                if _ok:
                    self.reload_models_config()
            else:
                result = f'manage_role: 未知 action={action!r}，可用: list set'
        elif name == 'query_logs':
            from core.logger import query_logs_text
            count = int(tool_input.get('count') or 20)
            priority = int(tool_input.get('priority') or 0)
            scope_key = str(tool_input.get('scope_key') or '').strip()
            if not scope_key:
                scope_key = f'{scope_type}:{scope_id}'
            result = query_logs_text(count=count, priority=priority, scope_key=scope_key)
        elif name == 'manage_mute':
            if scope_type != 'group':
                result = 'manage_mute: 禁言工具仅限群聊场景使用。'
            else:
                action = str(tool_input.get('action') or 'status').strip()
                group_id = int(scope_id)
                bot_qq = self.bot.self_id
                # 获取 bot 自身在群里的角色
                try:
                    bot_info = self.bot.get_group_member_info(group_id, bot_qq)
                    bot_role = bot_info.get('role', 'member')
                except Exception as e:
                    bot_role = 'unknown'
                    result = f'manage_mute: 无法获取 bot 自身角色信息: {e}'
                if bot_role == 'unknown' and action != 'status':
                    pass  # result already set above
                elif action == 'status':
                    lines = [f'bot 自身角色: {bot_role}（QQ: {bot_qq}）']
                    target_id = tool_input.get('target_user_id')
                    if target_id:
                        try:
                            target_info = self.bot.get_group_member_info(group_id, int(target_id))
                            lines.append(f'目标用户 {target_id} 角色: {target_info.get("role", "unknown")}')
                        except Exception as e:
                            lines.append(f'查询目标用户 {target_id} 失败: {e}')
                    result = '\n'.join(lines)
                elif bot_role not in ('owner', 'admin'):
                    result = f'manage_mute: 权限不足。bot 当前在该群的角色为 {bot_role}，需要管理员或群主权限才能执行禁言操作。'
                elif action not in ('ban', 'unban'):
                    result = f'manage_mute: 未知 action={action!r}，可用: ban unban status'
                else:
                    target_id = tool_input.get('target_user_id')
                    if not target_id:
                        result = 'manage_mute: ban/unban 操作必须提供 target_user_id（要禁言的群成员 QQ 号）'
                    else:
                        target_id = int(target_id)
                        # 查询目标用户角色，做权限层级检查
                        try:
                            target_info = self.bot.get_group_member_info(group_id, target_id)
                            target_role = target_info.get('role', 'member')
                        except Exception as e:
                            result = f'manage_mute: 查询目标用户 {target_id} 失败: {e}'
                            target_role = None
                        if target_role is not None:
                            if target_role == 'owner':
                                result = f'manage_mute: 无法 {action} 群主（owner）。'
                            elif bot_role == 'admin' and target_role == 'admin' and action == 'ban':
                                result = 'manage_mute: 管理员无法禁言其他管理员，仅群主有此权限。'
                            else:
                                if action == 'ban':
                                    duration = int(tool_input.get('duration') or 60)
                                    # 限制最大 30 天
                                    max_duration = 2592000
                                    if duration > max_duration:
                                        duration = max_duration
                                    if duration < 0:
                                        duration = 0
                                else:  # unban
                                    duration = 0
                                try:
                                    self.bot.set_group_ban(group_id, target_id, duration)
                                    if action == 'ban' and duration > 0:
                                        mins = duration // 60
                                        secs = duration % 60
                                        time_str = f'{mins}分{secs}秒' if mins > 0 else f'{secs}秒'
                                        result = f'已禁言用户 {target_id}（角色: {target_role}），时长 {time_str}（{duration}秒）。'
                                    elif action == 'ban' and duration == 0:
                                        result = f'已解除用户 {target_id} 的禁言（角色: {target_role}）。'
                                    else:
                                        result = f'已解除用户 {target_id} 的禁言（角色: {target_role}）。'
                                except Exception as e:
                                    result = f'manage_mute: 禁言操作失败: {e}'
        else:
            result = f'未知 AI 工具: {name}'
        _tool_ms = int((time.perf_counter() - _tool_start) * 1000)
        _result_preview = self._short_text(result, 80)
        info(
            f'[AI][tool] done scope={scope_type}:{scope_id} '
            f'tool={name} ms={_tool_ms} '
            f'result_len={len(result)} preview={_result_preview}'
        )
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
        try:
            reply = await asyncio.to_thread(
                self.model.complete,
                self._static_system_blocks('你是一个搜索结果摘要助手，只根据给定的搜索数据做客观摘要，不要编造信息。'),
                [{'role': 'user', 'content': summary_prompt}],
                None,
                self.model.model_name,
                0.3,
            )
        except Exception as exc:
            return f'搜索结果摘要生成失败: {exc}'
        summary = (reply.text if reply else '').strip()
        return summary or '摘要为空。'

    async def _record_turn_log(
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
        await asyncio.to_thread(
            self.repo.add_turn_log,
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
        _allow_cfg = scope_type == 'master' or (scope_type == 'private' and str(scope_id) == str(self.config.admin_qq))
        tools = build_tools(
            allow_notify_master=allow_notify_master,
            allow_tasks=allow_tasks,
            immediate_mode=live_message is not None,
            allow_config_tools=_allow_cfg,
            include_group_management=(scope_type == 'group'),
        )
        scope_key = self._scope_key(scope_type, scope_id) if live_message is not None else None
        tool_iterations: list[dict] = []
        started_at = time.perf_counter()
        used_tools = False
        sent_entries: list[dict] = []
        tool_context_messages: list[dict] = []
        fallback_prompted = False
        openai_tool_guidance = False
        max_iterations = 8 if live_message is not None else 6
        _turn_kind = (turn_meta or {}).get('turn_kind', 'unknown')
        _tool_count = len(tools) if tools else 0
        info(
            f'[AI][turn] start scope={scope_type}:{scope_id} '
            f'agent={agent_id} kind={_turn_kind} '
            f'messages={len(model_messages)} tools={_tool_count} '
            f'live={live_message is not None} max_iter={max_iterations}'
        )
        for _ in range(max_iterations):
            if self._is_epoch_stale(run_epoch):
                return {'message': '', 'think_note': '', 'tool_context_messages': tool_context_messages}, int((time.perf_counter() - started_at) * 1000), used_tools
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
                        allow_config_tools=_allow_cfg,
                        include_group_management=(scope_type == 'group'),
                    )
                    forced_digest_round = True
            reply = await self._complete_chat(system_blocks, model_messages, round_tools, temperature)
            generation_ms = int((time.perf_counter() - started_at) * 1000)
            if self._is_epoch_stale(run_epoch):
                return {'message': '', 'think_note': '', 'tool_context_messages': tool_context_messages}, generation_ms, used_tools
            if not reply or (not reply.text and not reply.tool_calls):
                await self._record_turn_log(
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
                return {'message': '', 'think_note': '', 'tool_context_messages': tool_context_messages}, generation_ms, used_tools
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
                    await self._record_turn_log(
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
                    info(
                        f'[AI][turn] done scope={scope_type}:{scope_id} '
                        f'kind={_turn_kind} ms={generation_ms} '
                        f'reply_len={len(final_reply)} iterations={len(tool_iterations)}'
                    )
                    return {'message': final_reply, 'think_note': think_note, 'tool_context_messages': tool_context_messages}, generation_ms, used_tools
                used_tools = True
                result_blocks: list[dict] = []
                iteration_calls: list[dict] = []
                for call in reply.tool_calls:
                    try:
                        if call.name in LOOP_TOOL_NAMES:
                            result = await self._run_ai_tool_call(scope_type, scope_id, agent_id, call.name, call.input)
                        else:
                            result = '本轮先处理查询类工具，这个操作未执行；如仍需要，请在拿到查询结果后的最终回复里再调用。'
                    except Exception as exc:
                        warn(
                            f'[AI][tool] 工具调用异常 scope={scope_type}:{scope_id} '
                            f'tool={call.name} error={type(exc).__name__}: {exc}'
                        )
                        result = f'工具 {call.name} 执行异常: {type(exc).__name__}: {exc}'
                    result_blocks.append(
                        {
                            'type': 'tool_result',
                            'tool_use_id': call.call_id,
                            'content': result if result is not None else '（工具无返回）',
                        }
                    )
                    iteration_calls.append({'name': call.name, 'input': call.input, 'result': result})
                tool_iterations.append(
                    {
                        'assistant_text': reply.text,
                        'tool_calls': iteration_calls,
                    }
                )
                assistant_content = self._filter_thinking_blocks(reply.raw_content)
                model_messages.append({'role': 'assistant', 'content': assistant_content})
                model_messages.append({'role': 'user', 'content': result_blocks})
                continue

            # live_message 不为空：主链路，工具调用边执行边发送
            if not reply.tool_calls:
                if forced_digest_round:
                    # 这一轮被临时摘掉了发送类工具，只是让模型先消化中断提醒；
                    # 模型没调用工具 = 已经消化完毕，进入下一轮恢复正常工具集重新决策。
                    model_messages.append({'role': 'assistant', 'content': self._filter_thinking_blocks(reply.raw_content)})
                    model_messages.append({'role': 'user', 'content': '好的，现在可以正常回复了。'})
                    continue

                # ── 兜底逻辑：模型未调用 send_message，re-prompt 让模型重新决策 ──────────
                # 模型可能是：①忘记调用工具（某些模型偶发）②主动选择不回复（把理由写进文本）
                # 直接自动发送文本会把模型的内心独白泄露给用户，因此改为 re-prompt。
                # 模型重新决策后：如果决定回复 → 调用 send_message；如果决定不回复 → 什么都不做，轮次正常结束。
                if not sent_entries and reply.text.strip():
                    if fallback_prompted:
                        # 已经 re-prompt 过一次，模型仍不调工具，静默结束本轮
                        break
                    warn('[AI][fallback] 模型未调用 send_message，re-prompt 重新决策')
                    fallback_prompted = True
                    model_messages.append({'role': 'assistant', 'content': self._filter_thinking_blocks(reply.raw_content)})
                    model_messages.append({'role': 'user', 'content': '你刚才输出了普通文字，但普通文字不会发送给用户。如果你想说这些话，请调用 send_message 工具发送；如果决定不回复，请调用 stay_silent 工具结束本回合。'})
                    continue

                final_reply = '\n'.join(entry['text'] for entry in sent_entries)
                think_note = self._normalize_think_note(reply.text)
                await self._record_turn_log(
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
                info(
                    f'[AI][turn] done scope={scope_type}:{scope_id} '
                    f'kind={_turn_kind} ms={generation_ms} '
                    f'reply_len={len(final_reply)} iterations={len(tool_iterations)}'
                )
                return {'message': final_reply, 'think_note': think_note, 'tool_context_messages': tool_context_messages}, generation_ms, used_tools

            result_blocks = []
            iteration_calls = []
            end_turn_requested = False
            for call in reply.tool_calls:
                try:
                    if call.name == 'stay_silent':
                        # 保持沉默：这一轮到此结束，不做实际工作。给一个占位 tool_result
                        # 保证 tool_use/tool_result 配对，随后终结本回合。
                        result = '好的，本回合保持沉默，不发消息。'
                        end_turn_requested = True
                    elif call.name in LOOP_TOOL_NAMES:
                        result = await self._run_ai_tool_call(scope_type, scope_id, agent_id, call.name, call.input)
                        used_tools = True
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
                        used_tools = True
                except Exception as exc:
                    warn(
                        f'[AI][tool] 工具调用异常 scope={scope_type}:{scope_id} '
                        f'tool={call.name} error={type(exc).__name__}: {exc}'
                    )
                    result = f'工具 {call.name} 执行异常: {type(exc).__name__}: {exc}'
                    used_tools = True
                result_blocks.append(
                    {
                        'type': 'tool_result',
                        'tool_use_id': call.call_id,
                        'content': result if result is not None else '（工具无返回）',
                    }
                )
                iteration_calls.append({'name': call.name, 'input': call.input, 'result': result})
            tool_iterations.append(
                {
                    'assistant_text': reply.text,
                    'tool_calls': iteration_calls,
                }
            )
            assistant_content = self._filter_thinking_blocks(reply.raw_content)
            model_messages.append({'role': 'assistant', 'content': assistant_content})
            model_messages.append({'role': 'user', 'content': result_blocks})
            tool_context_messages.append({'role': 'assistant', 'content': copy.deepcopy(assistant_content)})
            tool_context_messages.append({'role': 'user', 'content': copy.deepcopy(result_blocks)})

            # OpenAI 协议模型引导：工具结果返回后追加提醒，防止模型陷入查询→查询循环
            if getattr(self.model, 'is_openai_protocol', False) and not sent_entries and not end_turn_requested:
                if not openai_tool_guidance:
                    openai_tool_guidance = True
                    model_messages.append({'role': 'user', 'content': '以上是工具执行结果。请基于这些信息，调用 send_message 工具向用户发送最终回复；如果判断不需要回复，请调用 stay_silent 工具结束本回合。'})

            if end_turn_requested:
                final_reply = '\n'.join(entry['text'] for entry in sent_entries)
                think_note = self._normalize_think_note(reply.text)
                await self._record_turn_log(
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
                info(
                    f'[AI][turn] done scope={scope_type}:{scope_id} '
                    f'kind={_turn_kind} ms={generation_ms} '
                    f'reply_len={len(final_reply)} iterations={len(tool_iterations)} '
                    f'stay_silent=True'
                )
                return {'message': final_reply, 'think_note': think_note, 'tool_context_messages': tool_context_messages}, generation_ms, used_tools

            scope_key = self._scope_key(scope_type, scope_id)
            pending = None
            pending_list = self._pending_scope_turns.get(scope_key)
            if pending_list:
                pending = pending_list.pop(0)
                if not pending_list:
                    del self._pending_scope_turns[scope_key]
            if pending:
                model_messages.append({'role': 'user', 'content': self._build_pending_fold_reminder(pending)})

        # OpenAI 协议模型兜底：loop_guard 触发前做最后一次 re-prompt，给模型一次强制决定的机会
        if live_message is not None and getattr(self.model, 'is_openai_protocol', False):
            model_messages.append({'role': 'user', 'content': '你已经进行了多轮工具调用但没有发送任何回复。现在你必须做出最终决定：调用 send_message 向用户发送最终回复，或调用 stay_silent 结束本回合。不要再调用其他查询工具。'})
            try:
                _final_reply = await self._complete_chat(system_blocks, model_messages, tools, temperature)
                if _final_reply and _final_reply.tool_calls:
                    for call in _final_reply.tool_calls:
                        if call.name == 'stay_silent':
                            final_reply = '\n'.join(entry['text'] for entry in sent_entries)
                            _loop_guard_ms = int((time.perf_counter() - started_at) * 1000)
                            await self._record_turn_log(
                                scope_type, scope_id, agent_id, model_messages,
                                raw_reply=json.dumps(_final_reply.raw_content, ensure_ascii=False),
                                final_reply=final_reply, temperature=temperature,
                                turn_meta=turn_meta, tool_iterations=tool_iterations,
                                generation_ms=_loop_guard_ms,
                            )
                            return {'message': final_reply, 'think_note': '', 'tool_context_messages': tool_context_messages}, _loop_guard_ms, used_tools
                        elif call.name == 'send_message':
                            self._execute_live_action_tool_call(
                                scope_type, scope_id, agent_id, live_message,
                                call, context, allow_notify_master, allow_tasks, sent_entries,
                            )
                            final_reply = '\n'.join(entry['text'] for entry in sent_entries)
                            _loop_guard_ms = int((time.perf_counter() - started_at) * 1000)
                            await self._record_turn_log(
                                scope_type, scope_id, agent_id, model_messages,
                                raw_reply=json.dumps(_final_reply.raw_content, ensure_ascii=False),
                                final_reply=final_reply, temperature=temperature,
                                turn_meta=turn_meta, tool_iterations=tool_iterations,
                                generation_ms=_loop_guard_ms,
                            )
                            return {'message': final_reply, 'think_note': '', 'tool_context_messages': tool_context_messages}, _loop_guard_ms, used_tools
                elif _final_reply and _final_reply.text.strip():
                    final_reply = _final_reply.text.strip()
                    _loop_guard_ms = int((time.perf_counter() - started_at) * 1000)
                    await self._record_turn_log(
                        scope_type, scope_id, agent_id, model_messages,
                        raw_reply=json.dumps(_final_reply.raw_content, ensure_ascii=False),
                        final_reply=final_reply, temperature=temperature,
                        turn_meta=turn_meta, tool_iterations=tool_iterations,
                        generation_ms=_loop_guard_ms,
                    )
                    return {'message': final_reply, 'think_note': '', 'tool_context_messages': tool_context_messages}, _loop_guard_ms, used_tools
            except Exception as _exc:
                warn(f'[AI][turn] loop_guard final re-prompt failed: {_exc}')

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
        _loop_guard_ms = int((time.perf_counter() - started_at) * 1000)
        await self._record_turn_log(
            scope_type,
            scope_id,
            agent_id,
            model_messages,
            raw_reply='[loop_guard]',
            final_reply=final_reply,
            temperature=temperature,
            turn_meta=turn_meta,
            tool_iterations=tool_iterations,
            generation_ms=_loop_guard_ms,
        )
        warn(
            f'[AI][turn] loop_guard scope={scope_type}:{scope_id} '
            f'agent={agent_id} ms={_loop_guard_ms} '
            f'iterations={len(tool_iterations)}'
        )
        return {'message': final_reply, 'think_note': '', 'tool_context_messages': tool_context_messages}, _loop_guard_ms, used_tools

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
        # 后台链路里 send_message 会被聚合成本回合的最终回复文本。若模型在同一批里
        # 既发了消息又创建了 set_alarm，则那条聚合回复已相当于确认，无需系统再补发；
        # 若模型没发消息，则仍需 _handle_set_alarm 发系统确认，否则用户收不到任何反馈。
        model_sent_message = any(
            getattr(call, 'name', '') == 'send_message'
            and str((getattr(call, 'input', None) or {}).get('content') or '').strip()
            for call in tool_calls
        )

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
                    if kind == 'set_alarm' and model_sent_message:
                        payload.setdefault('direct_ack_sent', True)
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

    def _strip_send_message_thinking(self, content: str) -> str:
        content = str(content or '')
        content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
        return content.strip()

    def _filter_thinking_blocks(self, raw_content):
        """过滤中转站/平台附加的 extended thinking block，避免回填历史时干扰模型。"""
        if isinstance(raw_content, list):
            filtered = [b for b in raw_content if not (isinstance(b, dict) and b.get('type') == 'thinking')]
            # 如果过滤后只剩一个 text block，展开为字符串（更通用）
            if len(filtered) == 1 and isinstance(filtered[0], dict) and filtered[0].get('type') == 'text':
                return filtered[0].get('text', '')
            return filtered if filtered else ''
        return raw_content

    def _normalize_tool_context_messages(self, messages) -> list[dict]:
        normalized: list[dict] = []
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get('role') or '').strip()
            if role not in {'user', 'assistant'}:
                continue
            normalized.append(
                {
                    'role': role,
                    'content': copy.deepcopy(item.get('content')),
                }
            )
        return normalized

    def _send_scope_message(self, message: ChatMessage, content: str) -> list[dict]:
        content = self._strip_send_message_thinking(content)
        content = re.sub(r'\[\[.*?\]\]', '', content).strip()
        if message.chat_type == 'private':
            content = re.sub(r'\[CQ:at,qq=\d+\]', '', content).strip()
        # 无代码块：走原逻辑，行为与改动前完全一致。
        if not has_code_block(content):
            return self._send_text_lines(message, content)
        # 有代码块：按原文顺序分段发送，text 段照原逻辑发，code 段渲染成图片发。
        # 整个分段流程再包一层兜底：任何意外都回退到原样逐行发送，绝不吞消息。
        try:
            segments = split_code_block_segments(content)
        except Exception as exc:
            warn(f'[AI][code2img] 分段失败，回退纯文本发送: {exc}')
            return self._send_text_lines(message, content)
        entries: list[dict] = []
        for seg in segments:
            if seg.get('kind') == 'code':
                img_entry = self._try_send_code_image(
                    message,
                    seg.get('code') or '',
                    seg.get('language'),
                    seg.get('raw') or '',
                )
                if img_entry is not None:
                    entries.append(img_entry)
                    continue
                # 渲染或发图失败：降级为原样发送该段 raw 文本（含 ``` 围栏）。
                entries.extend(self._send_text_lines(message, seg.get('raw') or ''))
            else:
                entries.extend(self._send_text_lines(message, seg.get('text') or ''))
        return entries

    def _send_text_lines(self, message: ChatMessage, content: str) -> list[dict]:
        """把一段纯文本按原逻辑拆行并逐行发送。等价于原 _send_scope_message 的发送循环。"""
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

    def _try_send_code_image(
        self,
        message: ChatMessage,
        code: str,
        language: str | None,
        raw: str = '',
    ) -> dict | None:
        """把一段代码渲染成 PNG 并通过发图链路发送。

        成功返回 entry（entry['text'] 保留代码块 raw 原文，含 ``` 围栏，
        供下游拼历史/turn_log 无损保留上下文），发送成功后删除临时图；
        任何环节失败返回 None，由调用方降级为原样发文本。
        """
        if not (code or '').strip():
            return None
        out_path = None
        try:
            from core.code2img import render_code_to_image
            import base64 as _base64
            fname = f'codeblk_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.png'
            out_path = render_code_to_image(code, language=language, out_path=fname)
            data_bytes = None
            with open(out_path, 'rb') as fp:
                data_bytes = fp.read()
            b64 = _base64.b64encode(data_bytes).decode('ascii')
            file_arg = f'base64://{b64}'
            try:
                target_id = int(message.chat_id)
            except (TypeError, ValueError):
                target_id = message.chat_id
            response = self.bot.send_image(message.chat_type, target_id, file_arg)
            message_id = None
            if isinstance(response, dict):
                message_id = (response.get('data') or {}).get('message_id')
            # 发送成功后删除临时图；删除失败只记录，不影响已发送结果。
            self._safe_remove_file(out_path)
            # entry['text'] 保留 raw 原文（含 ``` 围栏），下游拼历史无损。
            return {'text': raw or code, 'message_id': message_id}
        except Exception as exc:
            warn(f'[AI][code2img] 渲染或发图失败，降级为文本: {exc}')
            if out_path:
                self._safe_remove_file(out_path)
            return None

    def _safe_remove_file(self, path) -> None:
        try:
            os.remove(path)
        except Exception as exc:
            warn(f'[AI][code2img] 删除临时图失败（忽略）: {exc}')
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
        info(
            f'[AI][live_tool] scope={scope_type}:{scope_id} '
            f'agent={agent_id} tool={call.name} '
            f'input_keys={list(tool_input.keys())}'
        )
        if call.name == 'send_message':
            content = str(tool_input.get('content') or '').strip()
            entries = self._send_scope_message(message, content)
            if not entries:
                return '内容为空或清理后为空，未发送。'
            sent_entries.extend(entries)
            ids = ', '.join(str(entry['message_id']) for entry in entries if entry.get('message_id') is not None)
            suffix = f'，message_id: {ids}' if ids else ''
            # 关键修复：在返回结果中包含实际发送的内容，避免 AI 因看不到自己刚发的消息而重复发送
            sent_text = '\n'.join(entry['text'] for entry in entries)
            return f'已发送 {len(entries)} 条消息{suffix}。发送内容：\n{sent_text}'
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
            if kind == 'set_alarm':
                # 主聊天链路：子AI本轮已用 send_message 自然回复，无需 _handle_set_alarm
                # 再往会话灌一条机器人腔的“闹钟已设定”确认。
                payload.setdefault('direct_ack_sent', True)
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
        _known_restricted = {'notify_master', 'create_task'}
        if call.name in _known_restricted:
            return f'工具 {call.name} 当前未启用（权限开关关闭），本次未执行。'
        return f'未知或不可用的工具: {call.name}，本次未执行。'

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

        _task_kind = task.get('kind', '?')
        _task_start = time.perf_counter()
        _task_scope = str(task.get('origin_scope') or '')
        info(
            f'[AI][task] start task_id={task_id} kind={_task_kind} '
            f'source={task.get("source_agent", "?")} '
            f'status={task.get("status", "?")}'
        )
        get_bot_logger().info(CAT_TASK, _task_scope, f'任务开始: task_id={task_id} kind={_task_kind} source={task.get("source_agent", "?")}')

        # 发送类 task 需占用目标 scope 会话锁，避免与 message turn 并发写同一会话。
        scope_key = self._scope_key_for_task(task)
        prereserved = bool(item.get('scope_prereserved'))
        if scope_key and not prereserved:
            if not self._reserve_task_scope(scope_key, item):
                # scope 忙：已按 FIFO 压入 pending，等空闲后 _promote 重新入队，本次不执行。
                return
        try:
            await self._dispatch_task(task, task_id, run_epoch=run_epoch)
        except Exception as exc:
            _task_ms = int((time.perf_counter() - _task_start) * 1000)
            warn(f'[AI][task] error task_id={task_id} kind={_task_kind} ms={_task_ms} error={exc}')
            get_bot_logger().error(CAT_TASK, _task_scope, f'任务异常: task_id={task_id} kind={_task_kind} ms={_task_ms}ms error={type(exc).__name__}: {exc}')
        finally:
            # 覆盖正常返回/异常/prereserved 等所有退出路径，确保占用的锁被释放。
            _task_ms = int((time.perf_counter() - _task_start) * 1000)
            info(
                f'[AI][task] done task_id={task_id} kind={_task_kind} ms={_task_ms}'
            )
            get_bot_logger().info(CAT_TASK, _task_scope, f'任务完成: task_id={task_id} kind={_task_kind} ms={_task_ms}ms')
            if scope_key:
                self._release_task_scope(scope_key)

    async def _dispatch_task(self, task: dict, task_id, run_epoch: int):
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

        if kind == 'summarize_diary':
            result = await self._handle_summarize_diary(task)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'meta_summarize_diary':
            result = await self._handle_meta_summarize_diary(task)
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'intelligence_round':
            result = await self._handle_intelligence_round(task)
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

    @staticmethod
    def _flatten_diary_context(diary_ctx: dict) -> list[dict]:
        result = []
        for d in (diary_ctx.get('window') or []):
            result.extend(d.get('messages') or [])
        result.extend(diary_ctx.get('current') or [])
        return result

    async def _maybe_schedule_diary_summarization(self, scope_type: str, scope_id: str):
        pending = await asyncio.to_thread(self.repo.get_pending_diary, scope_type, scope_id)
        if not pending:
            return
        agent_id = self.repo.get_or_create_agent(scope_type, scope_id).agent_id
        task = self.tools.create_task(
            agent_id,
            'summarize_diary',
            {'scope_type': scope_type, 'scope_id': scope_id, 'diary_index': pending['index']},
        )
        await self.queue.put({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})

    async def _handle_summarize_diary(self, task: dict) -> str:
        payload = task.get('payload') or {}
        scope_type = str(payload.get('scope_type') or '').strip()
        scope_id = str(payload.get('scope_id') or '').strip()
        diary_index = int(payload.get('diary_index') or 0)
        if not scope_type or not scope_id:
            return '缺少 scope 参数。'
        pending = await asyncio.to_thread(self.repo.get_pending_diary, scope_type, scope_id)
        if not pending or int(pending.get('index') or 0) != diary_index:
            return f'diary #{diary_index} 不在待总结队列中，可能已处理。'
        messages = pending.get('messages') or []
        if not messages:
            needs_meta = await asyncio.to_thread(self.repo.store_diary_summary, scope_type, scope_id, diary_index, '（空日记段，无内容）')
            if needs_meta:
                await self._maybe_schedule_meta_summarization(scope_type, scope_id)
            return f'diary #{diary_index} 为空，已略过。'
        prompt = self._build_diary_summary_prompt(scope_type, scope_id, messages)
        try:
            reply = await self._complete_chat(
                self._static_system_blocks(self._diary_summary_system_prompt()),
                [{'role': 'user', 'content': prompt}],
                None,
                0.3,
            )
        except Exception as exc:
            error(f'[AI][diary] summarize failed scope={scope_type}:{scope_id} index={diary_index} error={exc}')
            return f'总结 diary #{diary_index} 失败: {exc}'
        summary = (reply.text if reply else '').strip()
        if not summary:
            return f'diary #{diary_index} 总结结果为空。'
        needs_meta = await asyncio.to_thread(self.repo.store_diary_summary, scope_type, scope_id, diary_index, summary)
        info(f'[AI][diary] summarized scope={scope_type}:{scope_id} index={diary_index} chars={len(summary)}')
        if needs_meta:
            await self._maybe_schedule_meta_summarization(scope_type, scope_id)
        return f'已总结 diary #{diary_index}。'

    async def _maybe_schedule_meta_summarization(self, scope_type: str, scope_id: str):
        """当日记摘要超过上限时，调度一个元总结任务。"""
        candidates = await asyncio.to_thread(self.repo.get_meta_summary_candidates, scope_type, scope_id)
        if not candidates:
            return
        agent_id = self.repo.get_or_create_agent(scope_type, scope_id).agent_id
        task = self.tools.create_task(
            agent_id,
            'meta_summarize_diary',
            {'scope_type': scope_type, 'scope_id': scope_id},
        )
        await self.queue.put({'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch})

    async def _handle_meta_summarize_diary(self, task: dict) -> str:
        """将最旧的 50 条日记摘要合并为一条元总结。"""
        payload = task.get('payload') or {}
        scope_type = str(payload.get('scope_type') or '').strip()
        scope_id = str(payload.get('scope_id') or '').strip()
        if not scope_type or not scope_id:
            return '缺少 scope 参数。'
        candidates = await asyncio.to_thread(self.repo.get_meta_summary_candidates, scope_type, scope_id)
        if not candidates:
            return '没有需要元总结的日记摘要（可能已被其他任务处理）。'
        prompt = self._build_meta_summary_prompt(scope_type, scope_id, candidates)
        try:
            reply = await self._complete_chat(
                self._static_system_blocks(self._meta_summary_system_prompt()),
                [{'role': 'user', 'content': prompt}],
                None,
                0.3,
            )
        except Exception as exc:
            error(f'[AI][diary] meta-summarize failed scope={scope_type}:{scope_id} error={exc}')
            return f'元总结失败: {exc}'
        summary = (reply.text if reply else '').strip()
        if not summary:
            return '元总结结果为空。'
        await asyncio.to_thread(self.repo.store_meta_summary, scope_type, scope_id, summary)
        info(f'[AI][diary] meta-summarized scope={scope_type}:{scope_id} chars={len(summary)}')
        return f'已合并 {len(candidates)} 条日记摘要为一条元总结。'

    def _build_meta_summary_prompt(self, scope_type: str, scope_id: str, candidates: list[dict]) -> str:
        lines = [f'以下是会话 {scope_type}:{scope_id} 的 {len(candidates)} 条历史日记摘要（从旧到新），请将它们合并浓缩为一条更精炼的元总结：', '']
        for s in candidates:
            idx = int(s.get('index', 0)) + 1
            text = str(s.get('text') or '')[:400]
            lines.append(f'【第{idx}段】{text}')
        lines += ['', '请用简洁的中文段落将上述所有摘要浓缩为一条元总结，覆盖关键人物、重要事件、关系变化和核心结论，长度控制在500字以内。']
        return '\n'.join(lines)

    def _meta_summary_system_prompt(self) -> str:
        return (
            '你在为一个AI聊天机器人做日记摘要的二次浓缩（元总结）。'
            '任务是将多段历史日记摘要合并为一条更精炼的元总结，以便AI在未来对话中能快速回顾很久以前的事。'
            '总结要涵盖：主要人物及关系变化、重要事件、话题演变、AI的关键决策和结论。'
            '用第三人称描述，保留关键细节，省略重复和无意义内容。'
        )


    def _build_diary_summary_prompt(self, scope_type: str, scope_id: str, messages: list[dict]) -> str:
        lines = [f'以下是会话 {scope_type}:{scope_id} 的一段历史对话（共 {len(messages)} 条），请进行浓缩总结：', '']
        for msg in messages:
            src = str(msg.get('source_label') or msg.get('nickname') or msg.get('role') or '未知')
            text = str(msg.get('text') or '')[:300]
            if text:
                lines.append(f'[{src}]: {text}')
        lines += ['', '请用简洁的中文段落总结上述对话的核心内容、重要事件、话题走向及关键结论，长度控制在400字以内。']
        return '\n'.join(lines)

    def _diary_summary_system_prompt(self) -> str:
        return (
            '你在为一个AI聊天机器人总结历史对话记录。'
            '任务是将一段对话历史提炼成简洁摘要，以便AI在未来的对话中能快速回顾过去发生的事。'
            '总结要涵盖：对话主要话题、重要事件、涉及人物及关系、AI的关键行为和结论。'
            '用第三人称描述，保留关键细节，省略无意义寒暄。'
        )

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

    async def _build_group_context(self, scope_type: str, scope_id: str) -> str:
        """构建群聊前置上下文：群人数、群主、管理员列表、成员列表。

        群人数 < 20 时列出全员；否则只列近期发言过的成员。

        注意：NapCat 的 get_group_info 不返回 owner_id/admins，
        owner/admin 信息只能从 get_group_member_list 的 role 字段获取。
        """
        if scope_type != 'group':
            return ''
        try:
            members = await asyncio.to_thread(self.bot.get_group_member_list, int(scope_id))
            member_count = len(members)

            # 从成员列表中提取 owner 和 admin（通过 role 字段）
            owner_id: str | None = None
            owner_nick: str = ''
            admin_list: list[dict] = []
            member_map: dict[str, str] = {}
            for m in members:
                uid = str(m.get('user_id') or '')
                nick = str(m.get('nickname') or m.get('card') or '')
                role = str(m.get('role') or '').lower()
                if uid:
                    member_map[uid] = nick
                if role == 'owner':
                    owner_id = uid
                    owner_nick = nick
                elif role == 'admin':
                    admin_list.append({'user_id': uid, 'nickname': nick})

            parts = [f'群人数: {member_count}']

            # 群主
            if owner_id:
                parts.append(f'群主: {owner_nick or owner_id}({owner_id})')

            # 管理员
            if admin_list:
                admin_lines: list[str] = []
                for a in admin_list:
                    display = a['nickname'] or a['user_id']
                    admin_lines.append(f'  - {display}({a["user_id"]})')
                parts.append('管理员:' + '\n' + '\n'.join(admin_lines))
            else:
                parts.append('管理员: 无')

            # 成员列表
            if member_count < 20:
                parts.append('群成员（全员）:')
                for m in members:
                    uid = str(m.get('user_id') or '')
                    nick = member_map.get(uid) or str(uid)
                    parts.append(f'  - {nick}({uid})')
            else:
                # 大群：从近期消息历史提取发言者
                recent_msgs = await asyncio.to_thread(self.repo.list_messages, scope_type, scope_id)
                speakers: dict[str, str] = {}
                for msg in recent_msgs[-300:]:
                    uid = str(msg.get('user_id') or '')
                    nick = str(msg.get('nickname') or '')
                    if uid and uid not in speakers:
                        speakers[uid] = nick
                # 确保 owner 和 admin 在列表里
                if owner_id:
                    speakers.setdefault(owner_id, owner_nick or owner_id)
                for a in admin_list:
                    aid = a['user_id']
                    if aid and aid not in speakers:
                        speakers[aid] = a['nickname'] or aid
                parts.append(f'近期发言成员（共{len(speakers)}人）:')
                for uid, nick in speakers.items():
                    parts.append(f'  - {nick}({uid})')

            return '\n'.join(parts)
        except Exception as exc:
            warn(f'[AI][group_context] failed scope={scope_type}:{scope_id} error={exc}')
            return ''

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
            allow_config_tools=True,
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
                    result_blocks.append({'type': 'tool_result', 'tool_use_id': call.call_id, 'content': result if result is not None else '（工具无返回）'})
                master_messages.append({'role': 'assistant', 'content': self._filter_thinking_blocks(master_reply.raw_content)})
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
            if str(item.get('user_id')) == str(self.bot.self_id):
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
            await self._record_outbound_message('private', target_qq, content)

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
                await self._record_outbound_message(scope_type, str(scope_id), fail_text)
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
        # 情报查询：主AI 定期情报轮向子AI 拉取会话事件摘要，子AI 只回报、不发消息给用户。
        intel_query = bool(payload.get('intel_query'))
        intel_round_id = str(payload.get('intel_round_id') or '')

        agent = self.repo.get_or_create_agent(target_scope_type, target_scope_id)
        _dctx = self.repo.get_diary_context(target_scope_type, target_scope_id)
        history = self._flatten_diary_context(_dctx)
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
            intel_query=intel_query,
        )
        reply_bundle, generation_ms, _used_tools = await self._complete_child_turn(
            target_scope_type,
            target_scope_id,
            agent.agent_id,
            prompt,
            0.75,
            run_epoch=run_epoch,
            context=self._build_tool_context_from_task(payload, instruction, agent.agent_id),
            allow_notify_master=not (callback_only or intel_query),
            allow_tasks=not (callback_only or intel_query),
            turn_meta={
                'turn_kind': 'intel_query' if intel_query else 'delegate',
                'instruction': instruction,
                'callback_only': callback_only,
                'followup_only': followup_only,
                'requester_qq': requester_qq,
            },
        )
        reply = str((reply_bundle or {}).get('message') or '')
        think_note = str((reply_bundle or {}).get('think_note') or '')

        # 情报查询分支：把子AI 的会话事件摘要回报给情报轮状态机，绝不发消息给用户。
        if intel_query:
            report_text = (reply or think_note or '').strip()
            await self._report_child_result(
                agent.agent_id,
                {
                    'result_type': 'intel_report',
                    'intel_round_id': intel_round_id,
                    'target_scope_type': target_scope_type,
                    'target_scope_id': target_scope_id,
                    'instruction': instruction,
                    'intel_report': report_text,
                    'trace_id': payload.get('trace_id'),
                },
            )
            return f'情报查询完成 {target_scope_type}:{target_scope_id}，已回报 {len(report_text)} 字。'

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
        await self._record_outbound_message(
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

        # 情报回报：来自定期情报轮的子AI 摘要，交给情报轮状态机处理，不走跨会话回信链路。
        if result_type == 'intel_report':
            return await self._handle_child_intelligence_report(payload)

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
        await self._record_outbound_message(target_scope_type, target_scope_id, content)
        return f'已向 {target_scope_type}:{target_scope_id} 发送消息。'

    async def _run_dev_agent_task(self, task: dict):
        task_id = task['task_id']
        payload = task.get('payload') or {}
        # 兼容回退：依次尝试 task / content / description
        raw_task = payload.get('task') or payload.get('content') or payload.get('description') or ''
        raw_task = str(raw_task).strip()
        # 若取到的值形似 JSON dict，尝试解析并从中提取 task
        if raw_task.startswith('{'):
            try:
                parsed = json.loads(raw_task)
                if isinstance(parsed, dict):
                    raw_task = str(parsed.get('task') or raw_task).strip()
                    if not payload.get('github_repo') and parsed.get('github_repo'):
                        payload['github_repo'] = parsed['github_repo']
            except (json.JSONDecodeError, TypeError):
                pass
        task_desc = raw_task
        github_repo = str(payload.get('github_repo') or '').strip()
        source_agent = str(task.get('source_agent') or '')
        delivery_done = False

        scope_type, _, scope_id = source_agent.partition(':')
        # If the task was created by master itself, try to find the originating user
        # session from the payload so the result can be forwarded there.
        if scope_type == 'master':
            origin_scope_type = str(payload.get('origin_scope_type') or '').strip()
            origin_scope_id = str(payload.get('origin_scope_id') or '').strip()
            if origin_scope_type and origin_scope_id:
                scope_type, scope_id = origin_scope_type, origin_scope_id

        async def finish_trigger(summary: dict):
            nonlocal delivery_done
            if delivery_done:
                return
            delivery_done = True
            status = str(summary.get('status') or 'done').strip() or 'done'
            result = str(summary.get('result') or '').strip() or 'Dev agent 已结束，但没有返回结果。'
            self.repo.update_task(task_id, status, result)
            self.repo.add_note(
                'master',
                'global',
                f'Dev agent 任务完成 [{task_id}] ({status}): {task_desc}\n结果: {result}',
            )
            if scope_type and scope_id and scope_type != 'master':
                self._deliver_task_report_message(scope_type, scope_id, task_id, result)

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
            result = f'缺少任务描述 (task)，未执行。实际收到的 payload 键: {list(payload.keys())}'
        else:
            try:
                role_model_config = self.model_manager.get_role_model('dev_agent')
                if role_model_config:
                    dev_model = AnthropicChatModel(
                        base_url=role_model_config['base_url'],
                        api_key=role_model_config['api_key'],
                        model_name=role_model_config['model_name'],
                        messages_path=role_model_config['messages_path'],
                    )
                else:
                    dev_model = self.model
                result = await run_dev_agent(
                    dev_model,
                    self._get_github_api_token(),
                    task_desc,
                    github_repo=github_repo,
                    prompt_path=self.config.dev_agent_prompt_path,
                    on_finished=finish_trigger,
                )
            except Exception as exc:
                result = f'Dev agent 执行异常: {exc}'
        if not delivery_done:
            await finish_trigger(
                {
                    'status': 'failed' if result.startswith('Dev agent 执行异常') else 'done',
                    'result': result,
                }
            )

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
        intel_query: bool = False,
    ) -> str:
        history_lines = self._format_history_for_prompt(history)
        tool_log_lines = self._format_tool_logs_for_prompt(tool_logs)
        knowledge_lines = [f"- {item.get('content')}" for item in self.repo.get_knowledge_base() if str(item.get('content') or '').strip()]
        if intel_query:
            action_prompt = (
                "这是一次内部情报查询工单，不是新的人设，也不是要你发消息给用户。"
                "你仍然是完整个体在这个会话里的分身，现在需要向自己的调度层回报本会话最近的情况。"
                "【重要】不要发消息给用户，不要调用 send_message；直接用普通文字输出你的情报摘要即可。"
                "请基于本会话最近的聊天记录，简明扼要地回报以下内容："
                "①最近与角色性格/人设相关的事件或表现；"
                "②本会话里人物关系、好感、态度的变化；"
                "③值得主AI 关注的新情况或潜在需求。"
                "如果本会话最近没有值得一提的事件，就直接回一句“无重要情报”。"
                "只回报事实与观察，不要编造，控制在 200 字以内。"
            )
        elif callback_only:
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
        await self._record_outbound_message(scope_type, str(scope_id), text)

    def _notify_prefix(self, payload: dict) -> str:
        requester_qq = str(payload.get('requester_qq') or '').strip()
        scope_type = payload.get('scope_type')
        if requester_qq and scope_type == 'group':
            return self.bot.at(int(requester_qq))
        return ''

    @staticmethod
    def _coerce_timestamp(value) -> float:
        """将 Unix 时间戳(float/int/数字字符串)或 ISO 8601 字符串统一转换为 Unix 时间戳(float)。"""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip()
            try:
                return float(text)
            except ValueError:
                pass
            iso = text.replace('Z', '+00:00') if text.endswith('Z') else text
            dt = datetime.fromisoformat(iso)
            return dt.timestamp()
        raise TypeError(f'无法解析时间戳: {value!r}')

    def _resolve_alarm_due_at(self, payload: dict) -> float | None:
        if payload.get('due_at') is not None:
            return self._coerce_timestamp(payload['due_at'])
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
        tool_context_messages: list[dict] | None = None,
    ) -> dict:
        item = {
            'user_id': self.bot.self_id,
            'nickname': '冰糖',
            'text': text,
            'raw_message': text,
            'message_id': None,
            'timestamp': float(timestamp if timestamp is not None else time.time()),
            'generation_ms': generation_ms,
            'think_note': self._normalize_think_note(think_note),
        }
        normalized_tool_context = self._normalize_tool_context_messages(tool_context_messages)
        if normalized_tool_context:
            item['tool_context_messages'] = normalized_tool_context
        return item

    async def _record_outbound_message(
        self,
        scope_type: str,
        scope_id: str,
        text: str,
        generation_ms: int | None = None,
        think_note: str = '',
        tool_context_messages: list[dict] | None = None,
    ) -> dict:
        item = self._build_outbound_message_entry(
            text,
            generation_ms=generation_ms,
            think_note=think_note,
            tool_context_messages=tool_context_messages,
        )
        _has_pending = await asyncio.to_thread(
            self.repo.append_message,
            scope_type,
            scope_id,
            item,
            self.config.history_limit,
            self.config.diary_size,
        )
        if _has_pending:
            await self._maybe_schedule_diary_summarization(scope_type, scope_id)
        if scope_type == 'group':
            scope_key = f'{scope_type}:{scope_id}'
            self._arm_group_reply_window(scope_key, scope_id)
        return item

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
            # 仅当窗口仍是本 runner 创建的那个实例时才清除，
            # 防止 _arm_group_reply_window 取消旧任务后新建的窗口被误删。
            if self._group_reply_windows.get(scope_key) is window:
                self._group_reply_windows.pop(scope_key, None)

    def _fire_group_reply_trigger(self, scope_key: str, scope_id: str, epoch: int) -> None:
        if self._is_epoch_stale(epoch):
            return
        # 如果该 scope 正在处理消息或有积压的延迟消息，不触发防抖回复
        # 避免 AI 在处理完积压消息后"冷不丁"接续旧对话
        if scope_key in self._active_scope_turns or scope_key in self._pending_scope_turns:
            debug(f'[AI][debounce] scope busy/pending, suppressing trigger for {scope_key}')
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

    # ── 定期情报轮（每 4 小时主AI 主动情报收集与分发） ──────────────────
    async def _intelligence_scheduler_loop(self) -> None:
        """按 cron(默认每4小时) 触发一轮 intelligence_round 任务。"""
        try:
            self._intel_next_run = self._calc_next_cron_run(self._intel_schedule)
        except Exception as e:
            error(f'[AI][intel] 初始化 cron 失败: {e}')
            self._intel_next_run = time.time() + self._intel_active_window
        while True:
            try:
                await asyncio.sleep(30)
                now = time.time()
                if now < (self._intel_next_run or 0):
                    continue
                # 计算下一次触发时间，避免重复触发
                try:
                    self._intel_next_run = self._calc_next_cron_run(self._intel_schedule, now)
                except Exception:
                    self._intel_next_run = now + self._intel_active_window
                task = self.tools.create_task(
                    'master:global',
                    'intelligence_round',
                    {'triggered_at': now, 'source': 'intel_scheduler'},
                )
                await self.queue.put(
                    {'kind': 'task', 'task_id': task.task_id, 'message_epoch': self._message_epoch}
                )
                info(f"[AI][intel] 情报轮已触发 task={task.task_id[:8]}, 下次={self._intel_next_run:.0f}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                error(f'[AI][intel] scheduler error: {e}')

    def _discover_active_scopes(self) -> list[tuple[str, str]]:
        """遍历已知 agent，筛出最近 _intel_active_window 秒内有消息活动的会话。"""
        now = time.time()
        active: list[tuple[str, str]] = []
        for agent in self.repo.list_agents():
            scope_type = str(agent.get('scope_type') or '').strip()
            scope_id = str(agent.get('scope_id') or '').strip()
            if not scope_type or not scope_id:
                continue
            if scope_type == 'master':
                continue
            # 先用 agent.updated_at 做便宜的预筛，再用真实消息时间戳确认
            if (now - float(agent.get('updated_at') or 0.0)) > self._intel_active_window:
                continue
            try:
                messages = self.repo.list_messages(scope_type, scope_id)
            except Exception:
                messages = []
            latest_ts = 0.0
            for item in messages:
                ts = self._coerce_timestamp(item.get('timestamp')) or 0.0
                if ts > latest_ts:
                    latest_ts = ts
            if latest_ts and (now - latest_ts) <= self._intel_active_window:
                active.append((scope_type, scope_id))
        return active

    async def _handle_intelligence_round(self, task: dict) -> str:
        """情报轮主流程：发现活跃会话 -> 向各子AI 发情报查询 -> 登记状态机等回报。"""
        round_id = str(task.get('task_id') or uuid.uuid4().hex[:12])
        active_scopes = self._discover_active_scopes()
        if not active_scopes:
            self.repo.add_note('master', 'global', '[情报轮] 本轮未发现最近4小时内活跃的会话，跳过。')
            return '情报轮：无活跃会话，跳过。'

        now = time.time()
        deadline = now + self._intel_report_timeout
        waiting = {self._scope_key(st, sid) for st, sid in active_scopes}
        self._intelligence_rounds[round_id] = {
            'status': 'collecting',
            'started_at': now,
            'deadline': deadline,
            'waiting': set(waiting),
            'received': {},
            'scopes': list(active_scopes),
        }

        intel_instruction = (
            '这是一次内部情报查询：请回报本会话最近与角色性格、人设表现、'
            '人物关系与态度变化相关的事件摘要，不要发消息给用户。'
        )
        for scope_type, scope_id in active_scopes:
            query_task = self.tools.create_task(
                'master:global',
                'delegate_to_child',
                {
                    'target_scope_type': scope_type,
                    'target_scope_id': scope_id,
                    'instruction': intel_instruction,
                    'callback_only': True,
                    'intel_query': True,
                    'intel_round_id': round_id,
                    'requester_name': '定期情报轮',
                    'trace_id': round_id,
                },
            )
            self.queue.put_nowait(
                {'kind': 'task', 'task_id': query_task.task_id, 'message_epoch': self._message_epoch}
            )

        # 超时兜底：到 deadline 仍未收齐则强制进入汇总
        self.loop.create_task(self._intelligence_round_timeout(round_id))
        self.repo.add_note(
            'master',
            'global',
            f'[情报轮] 已向 {len(active_scopes)} 个活跃会话发出情报查询，round={round_id[:8]}。',
        )
        return f'情报轮已启动：向 {len(active_scopes)} 个活跃会话发出情报查询。'

    async def _intelligence_round_timeout(self, round_id: str) -> None:
        """5 分钟超时兜底：仍在收集中就用已收到的回报强制汇总。"""
        state = self._intelligence_rounds.get(round_id)
        if not state:
            return
        delay = max(1.0, float(state.get('deadline') or 0.0) - time.time())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        state = self._intelligence_rounds.get(round_id)
        if not state or state.get('status') != 'collecting':
            return
        missing = len(state.get('waiting') or set())
        info(f'[AI][intel] round={round_id[:8]} 超时兜底，仍缺 {missing} 个回报，强制汇总。')
        await self._finalize_intelligence_round(round_id, reason='timeout')

    async def _handle_child_intelligence_report(self, payload: dict) -> str:
        """接收子AI 的情报回报，更新状态机；收齐则进入汇总。"""
        round_id = str(payload.get('intel_round_id') or payload.get('trace_id') or '')
        scope_type = str(payload.get('target_scope_type') or '').strip()
        scope_id = str(payload.get('target_scope_id') or '').strip()
        report_text = str(payload.get('intel_report') or '').strip()
        state = self._intelligence_rounds.get(round_id)
        if not state:
            # 轮次已结束或超时清理，仅记录
            return f'情报回报到达，但情报轮 {round_id[:8]} 已结束，忽略。'
        scope_key = self._scope_key(scope_type, scope_id)
        state['received'][scope_key] = report_text
        state['waiting'].discard(scope_key)
        if not state['waiting'] and state.get('status') == 'collecting':
            await self._finalize_intelligence_round(round_id, reason='all_reported')
        return f'情报回报已登记 {scope_key}（剩余 {len(state["waiting"])} 个待回报）。'

    async def _finalize_intelligence_round(self, round_id: str, reason: str = '') -> None:
        """汇总分析所有回报，更新用户画像与主AI备忘，并分发给所有子AI。"""
        state = self._intelligence_rounds.get(round_id)
        if not state or state.get('status') != 'collecting':
            return
        state['status'] = 'finalizing'
        received = dict(state.get('received') or {})
        # 过滤掉无实质内容的回报
        meaningful = {
            k: v for k, v in received.items()
            if v and v not in {'无重要情报', '无', '无重要情报。'}
        }
        if not meaningful:
            state['status'] = 'done'
            self.repo.add_note('master', 'global', f'[情报轮] round={round_id[:8]} 无实质情报，结束（{reason}）。')
            self._intelligence_rounds.pop(round_id, None)
            return

        prompt = self._build_intelligence_analysis_prompt(meaningful, reason)
        summary = ''
        try:
            reply = await self._complete_chat(
                self._static_system_blocks(self._master_system_prompt()),
                [{'role': 'user', 'content': prompt}],
                None,
                0.3,
            )
            summary = (reply.text if reply else '').strip()
        except Exception as e:
            error(f'[AI][intel] 汇总 LLM 调用失败 round={round_id[:8]}: {e}')

        if summary:
            self.repo.add_note('master', 'global', f'[情报轮汇总] {summary}')
            admin_qq = str(getattr(self.config, 'admin_qq', 0) or '').strip()
            if admin_qq and admin_qq != '0':
                try:
                    self.repo.add_user_fact(
                        admin_qq,
                        f'[情报轮画像] {summary[:200]}',
                        source_scope_type='master',
                        source_scope_id='global',
                        source_agent='intelligence_round',
                    )
                except Exception as e:
                    warn(f'[AI][intel] add_user_fact 失败: {e}')
            await self._distribute_intelligence(summary, state)
        else:
            self.repo.add_note('master', 'global', f'[情报轮] round={round_id[:8]} 汇总为空，未分发。')

        state['status'] = 'done'
        self._intelligence_rounds.pop(round_id, None)

    def _build_intelligence_analysis_prompt(self, reports: dict[str, str], reason: str = '') -> str:
        """构造汇总分析 prompt：让主AI 汇总各会话回报、分析性格与关系变化。"""
        lines = []
        for scope_key, text in reports.items():
            lines.append(f'【{scope_key}】\n{text}')
        joined = '\n\n'.join(lines)
        return (
            f'当前时间: {self._now_text()}\n'
            '你是主AI，刚刚完成了一轮定期情报收集。以下是各活跃会话的子AI 回报的情报摘要：\n\n'
            f'{joined}\n\n'
            '请你综合分析并输出一份简洁的情报汇总，包含：\n'
            '1. 各角色/会话的性格模式与人设表现；\n'
            '2. 值得注意的人物关系、好感或态度变化；\n'
            '3. 对号主本人有价值的新画像信息（若有）；\n'
            '4. 需要各子AI 后续注意或消化的要点。\n'
            '控制在 300 字以内，用陈述式条理表达，不要编造未提及的信息。'
        )

    async def _distribute_intelligence(self, summary: str, state: dict) -> None:
        """把汇总后的情报摘要分发给本轮所有活跃会话的子AI 消化（callback_only，不发用户）。"""
        distribute_instruction = (
            '这是主AI 汇总后回传给你的最新情报摘要，供你消化理解，不需要发消息给用户：\n'
            f'{summary}\n'
            '请结合本会话语境记住这些要点即可。'
        )
        for scope_type, scope_id in state.get('scopes') or []:
            dist_task = self.tools.create_task(
                'master:global',
                'delegate_to_child',
                {
                    'target_scope_type': scope_type,
                    'target_scope_id': scope_id,
                    'instruction': distribute_instruction,
                    'callback_only': True,
                    'intel_query': True,
                    'intel_round_id': f'distribute',
                    'requester_name': '情报分发',
                    'trace_id': f'intel_distribute',
                },
            )
            self.queue.put_nowait(
                {'kind': 'task', 'task_id': dist_task.task_id, 'message_epoch': self._message_epoch}
            )

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
