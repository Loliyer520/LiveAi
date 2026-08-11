import asyncio
import copy
import hashlib
import html
import json
import os
import random
import re
import shlex
import string
import sys
import threading
import time
import uuid
import zlib
from pathlib import Path
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
from core.event_adapters import envelope_from_scope_turn_item
from core.event_batch_coordinator import AtomicTurnBatchCoordinator, CompletedTurn
from core.event_mailbox import InMemoryEventMailbox
from core.character_session import CharacterSessionRegistry
from core.scope_actor_dispatcher import ScopeActorDispatcher
from core.task_ingress_router import TaskIngressRouter
from core.model_completion_service import ModelCompletionService, ModelRequestSnapshot
from core.agent_report_delivery import AgentReportDeliveryService
from core.timed_event_messages import TimedEventMessageFactory
from core.async_execution import AsyncExecutionPool
from core.runtime_scope_observer import RuntimeScopeObserver
from core.transport import NapcatActionTransport, NapcatEventSource
from pack.anthropic_chat_model import AnthropicChatModel, AnthropicReply, configure_speed_stats
from pack.search_service import DoubaoSearchService
from pack.vision_model import OpenAICompatibleVisionModel
from pack.update_service import UpdateService
from pack.console_logger import info, warn, error, debug
from core.logger import get_bot_logger, INFO, WARN, ERROR, CAT_API, CAT_CHAT, CAT_TASK, CAT_AGENT
from core.ai_repository import AIRepository
from core.ai_tools_schema import LOOP_TOOL_NAMES, build_tools, code_mode_tool_names

CODE_MODE_TOOL_NAMES = code_mode_tool_names()
from core.config import AIConfig, SSHProfileConfig, parse_ssh_profiles, save_config_to_yaml
from core.dev_agent import (
    run_dev_agent,
    MAX_ITERATIONS,
    set_blocking_runner,
    validate_ssh_profile,
    _find_in_project,
    _list_local_files,
    _project_root,
    _read_local_file,
)
from core.agent_manager import AgentManager
from core.events import ChatMessage
from core.prompt_store import PromptStore, default_char_prompt
from core.token_usage_store import TokenUsageStore
from core.model_speed_stats import ModelSpeedStats
from core.test_command import handle_test_command
from core.model_manager import ModelManager
from tool.ai_toolbox import AIToolbox
from pack.txt2wav import Txt2WavError, create_default_txt2wav_service


# ── 代码块转图：检测/分段纯函数（步骤2，暂不接线到发送主链路）──────────────
# 匹配 Markdown 围栏代码块：```lang\n...代码...\n```
# - 开围栏后 info string 第一个 token 作为语言，空则为 None（交给 Pygments 猜）
# - re.DOTALL 让 . 跨行匹配，(.*?) 非贪婪，避免把多个代码块吞成一个
_CODE_FENCE_RE = re.compile(r'```[ \t]*([^\n`]*)\n(.*?)```', re.DOTALL)


class ScopeSendLedger:
    """按 scope 存活、带 TTL 的发送动作账本，用于拦截真实重复发送。

    早期账本是每次 `_process_message` 新建的 set，只能防住单次调用链内的双发；
    跨调用的重复（连续两次触发、agent report 重投、事件被重复消费）完全没有
    保护，表现为“同一句话反复说”。这里按 scope 长期保留 key，TTL 之外才允许
    同一句话再发一次（正常复读不该被永久封死）。
    """

    def __init__(self, ttl_seconds: float = 300.0):
        self.ttl_seconds = float(ttl_seconds)
        self._entries: dict[str, float] = {}

    def _purge(self, now: float) -> None:
        for key in [k for k, expiry in self._entries.items() if expiry <= now]:
            self._entries.pop(key, None)

    def __contains__(self, key: str) -> bool:
        now = time.time()
        self._purge(now)
        return key in self._entries

    def add(self, key: str) -> None:
        now = time.time()
        self._purge(now)
        self._entries[key] = now + self.ttl_seconds

    def discard(self, key: str) -> None:
        self._entries.pop(key, None)


# #region debug-point helpers:ssh-agent-report-timeout
def _debug_report_agent_runtime(hypothesis_id: str, location: str, msg: str, data: dict | None = None, *, run_id: str = 'pre-fix') -> None:
    try:
        _p = os.path.join(os.getcwd(), '.dbg', 'ssh-agent-report-timeout.env')
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
        bot: NapcatActionTransport,
        repo: AIRepository,
        model: AnthropicChatModel,
        vision_model: OpenAICompatibleVisionModel,
    ):
        self.config = config
        self.bot = bot
        self.event_source: NapcatEventSource | None = bot if hasattr(bot, 'on_group_message') else None
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
        self._task_ingress_router = None
        self.thread = None
        self.ready = threading.Event()
        self._recent_message_keys = {}
        self._recent_lock = threading.Lock()
        self._scheduled_alarm_ids = set()
        self._event_mailbox = InMemoryEventMailbox()
        self._turn_batch_coordinator = AtomicTurnBatchCoordinator(self._event_mailbox)
        self._character_sessions = CharacterSessionRegistry(mailbox=self._event_mailbox)
        self._scope_dispatcher = ScopeActorDispatcher(
            mailbox=self._event_mailbox,
            sessions=self._character_sessions,
            consume=self._consume_scope_item,
            is_stale=lambda item: self._is_epoch_stale(item.get('message_epoch')),
            on_idle=self._on_scope_idle,
        )
        self._background_task_limit = max(1, self.config.background_workers)
        self._background_task_semaphore: asyncio.Semaphore | None = None
        # scope_key -> 发送动作账本（带 TTL），跨 _process_message 调用存活
        self._scope_send_ledgers: dict[str, ScopeSendLedger] = {}
        self._chat_model_pool = AsyncExecutionPool(
            'liveai-chat-model', max(1, self.config.chat_model_workers)
        )
        self._runtime_io_pool = AsyncExecutionPool(
            'liveai-runtime-io', max(8, self.config.chat_model_workers)
        )
        self._background_pool = AsyncExecutionPool(
            'liveai-background', self._background_task_limit
        )
        self._model_completion = ModelCompletionService(
            get_client=lambda: self.model,
            default_pool=self._chat_model_pool,
        )
        self._agent_report_delivery = AgentReportDeliveryService(
            self._AGENT_REPORT_SCOPE_TYPE,
            self._AGENT_REPORT_SCOPE_ID,
        )
        self._timed_event_messages = TimedEventMessageFactory()
        set_blocking_runner(self._background_pool.run)
        self._runtime_scope_observer = RuntimeScopeObserver(
            is_active=self._character_sessions.is_active,
            mailbox=self._event_mailbox,
            pending_task_count=self._character_sessions.pending_task_count,
            queue_size=self._scope_dispatcher.active_actor_count,
        )
        self._dev_agent_tasks = set()
        # 跟踪每个 scope 的重试次数和当前使用的模型
        self._scope_retry次数 = {}  # {scope_key: retry_count}
        self._scope_current_model = {}  # {scope_key: model_name}
        # Persisted token counters are intentionally separate from the much larger AI state.
        token_usage_path = str(Path(self.config.storage_path).with_name('token_usage.json'))
        self.token_usage_store = TokenUsageStore(token_usage_path)
        speed_stats_path = str(Path(self.config.storage_path).with_name('model_speed_stats.json'))
        self.model_speed_stats = ModelSpeedStats(speed_stats_path)
        configure_speed_stats(self.model_speed_stats)
        # Per-scope reasoning level. Runtime-only by design; restart resets to off.
        self._scope_thinking_levels: dict[str, str] = {}
        # Per-scope session mode: 'chat' or 'code'. Runtime-only, default chat.
        self._scope_session_modes: dict[str, str] = {}
        # code 模式下连续多少轮没用过 code 专属工具；到阈值就提示切回 chat 省 token。
        self._scope_code_idle_turns: dict[str, int] = {}
        # 本轮已执行完的工具名。模型侧调用失败时本轮会被整体丢弃，但工具的副作用
        # 已经落盘了；异常处理要靠这份记录把"已经做过什么"写回历史，否则模型下一轮
        # 完全不知道自己动过手（曾导致批量销毁 agent 后自称"不是我删的"）。
        self._scope_executed_tools: dict[str, list[str]] = {}
        # 常驻 agent 管理器（与一次性 tasker 并行；tasker 内部仍使用 legacy dev_agent 实现名）。
        # report_notifier 指向本类的 _on_agent_report_pending：agent 产生纯文本
        # 挂起内容时被触发，据 AI 忙/闲决定"立即投递给会话AI"或"延后到下次触发"。
        # 事件循环引用在 _run_loop 里通过 set_loop 登记（那时 loop 才建好）。
        self.agent_manager = AgentManager(report_notifier=self._on_agent_report_pending)
        self.agent_manager.set_blocking_runner(self._background_pool.run)
        # Agent calls bypass _complete_chat; account them globally without assigning
        # them to whichever QQ scope happens to be active.
        try:
            self.agent_manager.token_usage_store = self.token_usage_store
        except AttributeError:
            pass
        self._resolving_display_names = set()
        self._scope_direct_agents: dict[str, str] = {}
        self._pending_send_message_persona_notices: dict[str, bool] = {}
        self._txt2wav_service = None
        self._message_epoch = 0
        self._stale_message_max_age: float = 120.0  # 超过此秒数的旧消息不再触发回复
        self._group_reply_windows: dict[str, dict] = {}
        # 本次触发消息里的图片引用，按 scope_key 暂存，供 view_image 工具按需解析。
        # 每个 scope 同一时刻只有一个 turn 在跑（scope 锁保证），故直接覆盖即可。
        self._turn_image_refs: dict[str, list[str]] = {}
        # 当前 turn 内可见消息的四位短 ID -> 原 message_id 映射，供 reply/recall/view_image 复用。
        self._turn_message_ref_maps: dict[str, dict[str, dict]] = {}
        # 收藏表情缓存：按序缓存结构化条目，优先保留稳定 emoji_id，避免 URL 刷新后备注和发送漂移。
        self._sticker_cache: list[dict[str, str]] = []
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
        # ── 静默上报巡检 ──────────────────────────────────────────────────
        self._silence_report_window: int = 600    # 静默阈值：10 分钟
        self._scope_last_user_msg_at: dict[str, float] = {}   # scope_key -> 最后真实用户消息时间
        self._scope_silence_fired: set[str] = set()           # 已发过静默提示的 scope
        # 初始化模型配置（用新的 ModelManager 替换旧的 profile 系统）
        self._active_main_model: dict | None = None
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
        # 对已有运行态 agent：有效 binding 重建 client，失效 binding 保留旧 client 并警告。
        for record in self.agent_manager.list_agents():
            agent_id = record.get('agent_id')
            if not agent_id:
                continue
            full = self.agent_manager.get_agent(agent_id)
            binding = (full or {}).get('model_binding')
            if not binding:
                continue
            cfg = self.model_manager.resolve_exact_model(
                binding.get('channel', ''),
                binding.get('upstream', ''),
                binding.get('model_id', ''),
            )
            if cfg:
                new_client = AnthropicChatModel(
                    base_url=cfg['base_url'],
                    api_key=cfg['api_key'],
                    model_name=cfg['model_name'],
                    messages_path=cfg['messages_path'],
                )
                self.agent_manager.register_agent_client(agent_id, new_client)
                info(f'[AI] agent {agent_id} client 已按 binding 重建: {cfg["display_name"]}')
            else:
                warn(f'[AI] agent {agent_id} binding 已失效，保留旧 client: {binding}')
        if current:
            return {'loaded': True, 'current': current['display_name']}
        return {'loaded': False, 'message': 'models_config.json 无有效渠道'}

    def _build_restored_agent_client(self, record: dict):
        binding = (record or {}).get('model_binding') or {}
        if binding:
            cfg = self.model_manager.resolve_exact_model(
                binding.get('channel', ''),
                binding.get('upstream', ''),
                binding.get('model_id', ''),
            )
            if cfg:
                return AnthropicChatModel(
                    base_url=cfg['base_url'],
                    api_key=cfg['api_key'],
                    model_name=cfg['model_name'],
                    messages_path=cfg['messages_path'],
                )
            warn(f'[AI] 恢复 agent 时 binding 已失效，回退默认 agent 模型: {binding}')

        role_model_config = self.model_manager.get_role_model('agent')
        if role_model_config:
            return AnthropicChatModel(
                base_url=role_model_config['base_url'],
                api_key=role_model_config['api_key'],
                model_name=role_model_config['model_name'],
                messages_path=role_model_config['messages_path'],
            )
        return self.model

    def _ensure_agent_loop_running(self, agent_id: str, record: dict | None = None) -> dict:
        """确保指定 agent 有存活的常驻循环；必要时按持久化上下文重新拉起。"""
        agent_id = str(agent_id or '').strip()
        if not agent_id:
            return {'ok': False, 'started': False, 'error': 'missing agent_id'}
        loop = self.loop
        if loop is None:
            return {'ok': False, 'started': False, 'error': 'runtime loop not ready'}
        existing_task = self.agent_manager.get_agent_task(agent_id)
        if existing_task is not None and not existing_task.done():
            return {'ok': True, 'started': False, 'error': None}
        full_record = record or self.agent_manager.get_agent(agent_id) or {}
        if not full_record:
            return {'ok': False, 'started': False, 'error': 'agent_not_found'}
        try:
            agent_model = self.agent_manager.get_agent_client(agent_id) or self._build_restored_agent_client(full_record)
            self.agent_manager.register_agent_client(agent_id, agent_model)
            agent_task = loop.create_task(
                self.agent_manager.run_agent_loop(
                    agent_id,
                    agent_model,
                    self._get_github_api_token(),
                    prompt_path=self.config.agent_prompt_path,
                    ssh_profiles=self._get_ssh_profiles_map(),
                    on_agent_message=self.agent_manager.on_agent_message,
                )
            )
            self.agent_manager.register_agent_task(agent_id, agent_task)
            info(f'[AI] 已重新拉起常驻 agent: {agent_id} status={full_record.get("status") or "unknown"}')
            return {'ok': True, 'started': True, 'error': None}
        except Exception as exc:
            warn(f'[AI] 重新拉起常驻 agent 失败 {agent_id}: {exc}')
            return {'ok': False, 'started': False, 'error': str(exc)}

    async def _restore_persisted_agents(self):
        """启动时恢复持久化的常驻 agent 运行循环。"""
        restored = 0
        skipped = 0
        profiles = self._get_ssh_profiles_map()
        for item in self.agent_manager.list_agents():
            agent_id = str(item.get('agent_id') or '').strip()
            if not agent_id:
                continue
            status = str(item.get('status') or '').strip().lower()
            if status == 'error':
                skipped += 1
                continue
            existing_task = self.agent_manager.get_agent_task(agent_id)
            if existing_task is not None and not existing_task.done():
                skipped += 1
                continue
            full_record = self.agent_manager.get_agent(agent_id) or {}
            try:
                agent_model = self._build_restored_agent_client(full_record)
                self.agent_manager.register_agent_client(agent_id, agent_model)
                agent_task = self.loop.create_task(
                    self.agent_manager.run_agent_loop(
                        agent_id,
                        agent_model,
                        self._get_github_api_token(),
                        prompt_path=self.config.agent_prompt_path,
                        ssh_profiles=profiles,
                        on_agent_message=self.agent_manager.on_agent_message,
                    )
                )
                self.agent_manager.register_agent_task(agent_id, agent_task)
                restored += 1
                info(f'[AI] 已恢复常驻 agent: {agent_id} status={status or "unknown"}')
            except Exception as exc:
                warn(f'[AI] 恢复常驻 agent 失败 {agent_id}: {exc}')
        if restored or skipped:
            info(f'[AI] 常驻 agent 恢复完成 restored={restored} skipped={skipped}')

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.ready.clear()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=3)

    def stop(self, timeout: float = 5.0) -> None:
        loop = self.loop
        thread = self.thread
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._shutdown_runtime_loop(), loop)
            future.result(timeout=timeout)
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self.agent_manager.set_blocking_runner(None)
        set_blocking_runner(None)
        self._chat_model_pool.close()
        self._runtime_io_pool.close()
        self._background_pool.close()
        self.thread = None
        self.loop = None
        self.queue = None
        self._task_ingress_router = None
        self.ready.clear()

    async def _shutdown_runtime_loop(self) -> None:
        await self._scope_dispatcher.close()
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.agent_manager.set_loop(None)

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()  # task ingress; chat messages bypass it
        self._background_task_semaphore = asyncio.Semaphore(self._background_task_limit)
        self._task_ingress_router = TaskIngressRouter(
            queue=self.queue,
            dispatcher=self._scope_dispatcher,
            load_task=lambda task_id: self._runtime_io_pool.run(self.repo.get_task, task_id),
            resolve_scope=self._scope_key_for_task,
            is_stale=lambda item: self._is_epoch_stale(item.get('message_epoch')),
            on_error=lambda exc: error(f'[AI][task_router] {exc}'),
        )
        self.loop.set_default_executor(self._runtime_io_pool.executor)
        self.agent_manager.set_loop(self.loop)
        self.loop.create_task(self._route_task_queue_drain())
        self.loop.create_task(self._restore_scheduled_tasks())
        self.loop.create_task(self._restore_persisted_agents())
        self.loop.create_task(self._recurring_scheduler_loop())
        self.loop.create_task(self._auto_update_check_loop())
        self.loop.create_task(self._intelligence_scheduler_loop())
        self.loop.create_task(self._silence_report_scheduler_loop())
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def register(self):
        if self.event_source is None:
            raise RuntimeError('当前运行模式未配置 NapCat 入站事件源')
        self.event_source.on_group_message(self.handle_group_message)
        self.event_source.on_private_message(self.handle_private_message)
        self.event_source.on_self_message(self.handle_self_message)
        self.event_source.on_friend_request(self.handle_friend_request)
        self.event_source.on_group_request(self.handle_group_request)

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

    def handle_friend_request(self, event: dict):
        """NapCat 好友申请事件：实时唤醒主 AI，消除"申请到了但 AI 不知道"的同步空窗。"""
        if not self.loop or not self.queue:
            self.start()
        if not self.loop:
            return
        self._submit_request_event_message('friend', event)

    def handle_group_request(self, event: dict):
        """NapCat 加群/入群邀请事件：实时唤醒主 AI 处理。"""
        if not self.loop or not self.queue:
            self.start()
        if not self.loop:
            return
        self._submit_request_event_message('group', event)

    def _submit_request_event_message(self, request_type: str, event: dict):
        """把 QQ 申请事件包装成 master scope 内部通知消息，走 mailbox 唤醒主 AI 回合。"""
        if self.bot is None or str(event.get('user_id') or '') == str(getattr(self.bot, 'self_id', '')):
            return
        user_id = str(event.get('user_id') or '')
        comment = str(event.get('comment') or '').strip()
        nickname = str(event.get('nickname') or '').strip()
        if request_type == 'friend':
            label = f'QQ {user_id}' + (f'（{nickname}）' if nickname else '')
            text = (
                f'【系统通知】收到新的好友申请：{label}，验证消息：{comment or "（无）"}。'
                '请用 qq_list_friend_requests 查看申请详情与 flag 后再决定是否同意/拒绝；'
                '如需主动向对方发消息，请用 send_private_message 工具，不要向本通知发普通消息。'
            )
        else:
            group_id = str(event.get('group_id') or '')
            sub_type = str(event.get('sub_type') or 'add')
            sub_label = '入群邀请' if sub_type == 'invite' else '加群申请'
            text = (
                f'【系统通知】收到新的{sub_label}：申请人 QQ {user_id}，目标群 {group_id}，'
                f'验证消息：{comment or "（无）"}。'
                '请用 qq_list_group_requests 查看详情与 flag 后再决定是否同意/拒绝。'
            )
        request_message = ChatMessage(
            chat_type='master',
            chat_id=0,
            user_id=0,
            text=text,
            raw_message=text,
            sender={'nickname': '系统通知', 'user_id': 0},
            message_id=None,
            mentions_self=True,  # 强制触发主 AI 回合
            timestamp=time.time(),
            raw_data={
                'source': 'qq_request_event',
                'request_type': request_type,
                'flag': event.get('flag'),
                'user_id': user_id,
                'group_id': event.get('group_id'),
            },
        )
        self._submit_message(request_message)

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
        """把一次性 tasker 等后台任务的原始汇报喂给会话 AI，由它决定如何转达。"""
        result = str(result or '').strip()
        if not result or not self.loop or not self.queue:
            return
        wrapped = (
            '【内部系统通知：以下是一次性后台 tasker 执行完成后的原始技术汇报，不是任何人直接对你说的话，仅供你参考决策。'
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
            # 让常驻 agent 的普通上报在当前会话生成中也能立即进入同 scope 的 mailbox，
            # 由工具循环中的“流式补喂”逻辑统一并流给模型，而不是等整轮结束后再补发。
            self._flush_agent_reports(only_if_idle=False)
        except Exception as exc:
            error(f'[AI] _on_agent_report_pending 处理失败: {exc}')

    def _get_agent_report_delivery(self) -> AgentReportDeliveryService:
        service = getattr(self, '_agent_report_delivery', None)
        if service is None:
            service = AgentReportDeliveryService(
                self._AGENT_REPORT_SCOPE_TYPE,
                self._AGENT_REPORT_SCOPE_ID,
            )
            self._agent_report_delivery = service
        return service

    def _get_timed_event_messages(self) -> TimedEventMessageFactory:
        factory = getattr(self, '_timed_event_messages', None)
        if factory is None:
            factory = TimedEventMessageFactory()
            self._timed_event_messages = factory
        return factory

    def _get_model_validation_service(self):
        svc = getattr(self, '_model_validation_service', None)
        if svc is None:
            from core.model_validation_service import ModelValidationService
            svc = ModelValidationService(self.model_manager)
            self._model_validation_service = svc
        return svc

    def _is_admin_user(self, user_id) -> bool:
        try:
            return str(user_id) == str(self.config.admin_qq)
        except Exception:
            return False

    def _flush_agent_reports(self, only_if_idle: bool = True) -> None:
        # #region debug-point A:flush-agent-reports
        try:
            pending = getattr(self, 'agent_manager', None).peek_pending_reports() if getattr(self, 'agent_manager', None) else []
            _debug_report_agent_runtime(
                'A',
                'core.ai_runtime:_flush_agent_reports',
                'flush agent reports invoked',
                {
                    'only_if_idle': bool(only_if_idle),
                    'pending_count': len(pending or []),
                    'pending_scopes': [str(item.get('origin_scope') or '') for item in (pending or [])[:8]],
                },
            )
        except Exception:
            pass
        # #endregion
        self._get_agent_report_delivery().flush(
            getattr(self, 'agent_manager', None),
            is_scope_active=self._scope_turn_is_active,
            deliver=self._deliver_agent_reports_to_scope,
            only_if_idle=only_if_idle,
        )

    def _parse_agent_report_scope(self, origin_scope) -> tuple[str, str]:
        return self._get_agent_report_delivery().parse_scope(origin_scope)

    def _deliver_agent_reports_to_scope(self, scope_type: str, scope_id: str, items: list[dict]) -> None:
        # #region debug-point A:deliver-agent-reports
        _debug_report_agent_runtime(
            'A',
            'core.ai_runtime:_deliver_agent_reports_to_scope',
            'deliver agent reports to scope',
            {
                'scope_type': str(scope_type or ''),
                'scope_id': str(scope_id or ''),
                'item_count': len(items or []),
                'agent_ids': [str(item.get('agent_id') or '') for item in (items or [])],
                'origin_scopes': [str(item.get('origin_scope') or '') for item in (items or [])],
            },
        )
        # #endregion
        scope_key = self._scope_key(scope_type, str(scope_id))
        direct_agent_id = str(self._scope_direct_agents.get(scope_key) or '').strip()
        direct_items: list[dict] = []
        relay_items: list[dict] = []
        for item in items or []:
            report_type = str(item.get('report_type') or 'message').strip() or 'message'
            if direct_agent_id and str(item.get('agent_id') or '').strip() == direct_agent_id:
                direct_items.append(dict(item))
            elif report_type != 'progress':
                relay_items.append(dict(item))
        if direct_items:
            self._deliver_direct_agent_reports_to_scope(
                scope_type,
                str(scope_id),
                direct_agent_id,
                direct_items,
            )
        message = self._get_agent_report_delivery().build_message(scope_type, scope_id, relay_items)
        if message is not None:
            self._submit_message(message)

    def get_runtime_status(self) -> dict:
        current = getattr(self, '_active_main_model', None) or self.model_manager.get_current_model()
        mm_config = getattr(self.model_manager, 'config', {}) or {}
        get_role_channel_name = getattr(self.model_manager, 'get_role_channel_name', None)
        if callable(get_role_channel_name):
            main_channel = get_role_channel_name('main') or ''
        else:
            main_channel = str((mm_config.get('roles') or {}).get('main') or '').strip()
        resolve_channel_models = getattr(self.model_manager, 'resolve_channel_models', None)
        if callable(resolve_channel_models):
            channel_models = resolve_channel_models(main_channel)
        else:
            channel = next(
                (item for item in (mm_config.get('channels') or []) if str(item.get('name') or '').strip() == main_channel),
                None,
            )
            channel_models = [
                {
                    'display_name': f'{m.get("upstream")}/{m.get("model_id")}',
                    'model_name': str(m.get('model_id') or ''),
                    'upstream_name': str(m.get('upstream') or ''),
                    'channel_name': main_channel,
                }
                for m in ((channel or {}).get('models') or [])
                if m.get('model_id')
            ]
        current_display = str((current or {}).get('display_name') or '')
        available = [
            {
                'display_name': item['display_name'],
                'model_id': item['display_name'],
                'raw_model_id': item['model_name'],
                'upstream': item['upstream_name'],
                'channel': item['channel_name'],
                'active': item['display_name'] == current_display,
            }
            for item in channel_models
        ]
        return {
            'enabled': self.config.enabled,
            'ready': bool(self.loop and self.queue),
            'active_profile': current['display_name'] if current else 'none',
            'active_model': current['model_name'] if current else 'none',
            'active_label': current['display_name'] if current else 'none',
            'queue_size': self._event_mailbox.pending_count(),
            'worker_count': self._scope_dispatcher.active_actor_count(),
            'active_actor_count': self._scope_dispatcher.active_actor_count(),
            'task_ingress_size': self.queue.qsize() if self.queue else 0,
            'chat_model_workers': self.config.chat_model_workers,
            'background_workers': self.config.background_workers,
            'scheduled_alarm_count': len(self._scheduled_alarm_ids),
            'available_models': available,
        }

    def switch_model_profile(self, requested: str) -> tuple[bool, str]:
        """兼容旧 API，切换主 AI 渠道。"""
        success, msg = self.model_manager.switch_model(requested)
        if success:
            current = self.model_manager.get_current_model()
            if current:
                self._update_model_from_config(current)
        return success, msg

    def switch_channel_model(self, channel: str, upstream: str, model_id: str) -> tuple[bool, str]:
        """在主 AI 当前渠道内精确切换运行时模型。"""
        main_channel = self.model_manager.get_role_channel_name('main') or ''
        if channel != main_channel:
            return False, f'只能切换当前渠道 {main_channel} 内的模型。'
        model_config = self.model_manager.resolve_exact_model(channel, upstream, model_id)
        if not model_config:
            return False, '当前渠道中未找到指定模型。'
        self._update_model_from_config(model_config)
        # 同步渠道轮询/回退索引，避免 main 运行时单例与索引失同步
        # （日志/展示显示旧模型、fallback 起点脱节）。
        sync = getattr(self.model_manager, 'sync_channel_index', None)
        if callable(sync):
            sync(main_channel, model_config['model_name'])
        return True, f'已切换到 {model_config["display_name"]}'

    def _update_model_from_config(self, model_config: dict):
        """根据 ModelManager 提供的配置更新 self.model"""
        self._active_main_model = dict(model_config)
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
                protocol = self.model_manager.normalize_upstream_protocol(upstream.get('protocol')) or self.model_manager.protocol_from_path(upstream.get('messages_path'))
                result.append({
                    'name': display,
                    'label': model_id,
                    'active': display == current_display,
                    'base_url': str(upstream.get('base_url') or ''),
                    'model_name': model_id,
                    'messages_path': self.model_manager.endpoint_path_for_protocol(protocol),
                    'api_key_set': bool(upstream.get('api_key')),
                    'api_key_masked': self._mask_secret(upstream.get('api_key', '')),
                    'overridden_fields': [],
                })
        return result

    def get_command_catalog(self) -> list[dict]:
        return [
            {'command': '#help', 'aliases': ['#指令', '#菜单', '#命令'], 'scope': 'all', 'description': '查看当前可用指令列表'},
            {'command': '#status', 'aliases': ['#状态'], 'scope': 'all', 'description': '查看当前会话和运行时状态'},
            {'command': '#speed', 'aliases': ['#速度'], 'scope': 'all', 'description': '查看所有模型的首字延迟和平均调用时长'},
            {'command': '#profile', 'aliases': ['#画像', '#资料'], 'scope': 'all', 'description': '查看当前会话的触发词、画像和来源'},
            {'command': '#impression', 'aliases': ['#印象'], 'scope': 'all', 'description': '查看当前会话的长期印象'},
            {'command': '#notes', 'aliases': ['#备注', '#记忆'], 'scope': 'all', 'description': '查看当前会话 AI 工具备忘'},
            {'command': '#tasks', 'aliases': ['#任务', '#任务列表'], 'scope': 'all', 'description': '查看当前会话最近任务'},
            {'command': '#task <任务ID>', 'aliases': ['#任务 <任务ID>', '#ai-task <任务ID>'], 'scope': 'all', 'description': '查询指定任务详情'},
            {'command': '#into [agent_id|off]', 'aliases': [], 'scope': 'all', 'description': '进入当前会话的 agent 直连模式；后续普通消息直接发给该 agent'},
            {'command': '#refresh-impression', 'aliases': ['#刷新印象'], 'scope': 'all', 'description': '手动提交一次印象刷新任务'},
            {'command': '#clear', 'aliases': ['#clear-chat', '#清空聊天记录'], 'scope': 'all', 'description': '清空当前会话活动消息、近期原始窗口和待压缩原文（保留历史摘要/备注/审计日志）'},
            {'command': '#clear-notes', 'aliases': ['#清空备注'], 'scope': 'all', 'description': '只清空当前会话 AI 工具备忘'},
            {'command': '#clear-memory', 'aliases': ['#clear-all', '#清空记忆'], 'scope': 'all', 'description': '清空当前会话聊天记录、AI 工具备忘和工具记录'},
            {'command': '/thinking [off|low|medium|high]', 'aliases': ['/thinking'], 'scope': 'all', 'description': '查看或设置当前私聊/群聊的模型思考等级（重启恢复 low）'},
            {'command': '/trigger [0~0.30]', 'aliases': ['/trigger'], 'scope': 'admin', 'description': '查看或设置全局随机触发概率，并同步现有会话'},
            {'command': '/model', 'aliases': ['/model list', '/model reload'], 'scope': 'admin', 'description': '管理员查看/重载模型配置'},
            {'command': '/upstream', 'aliases': ['/upstream list', '/upstream add', '/upstream remove'], 'scope': 'admin', 'description': '管理 API 上游（base_url/api_key）'},
            {'command': '/channel', 'aliases': ['/channel list', '/channel add', '/channel remove'], 'scope': 'admin', 'description': '管理渠道（模型池 + 轮询策略）'},
            {'command': '/role', 'aliases': ['/role list', '/role set main <渠道名>'], 'scope': 'admin', 'description': '为 AI 角色绑定渠道'},
            {'command': '/ssh', 'aliases': ['/ssh list', '/ssh add', '/ssh test <profile_id>'], 'scope': 'admin', 'description': '管理 SSH 服务器配置并验证连通性'},
            {'command': '/test', 'aliases': ['/test all', '/test alls'], 'scope': 'admin', 'description': '测试渠道/模型可用性，无参数列出列表'},
            {'command': '/silent', 'aliases': [], 'scope': 'admin', 'description': '管理员开启静默模式，仅保留与主人的私聊在线'},
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

    async def _get_stickers(self, force: bool = False) -> list[dict[str, str]]:
        """账号级共享的收藏表情缓存，避免不同会话短时间内重复拉取同一份列表。"""
        now = time.time()
        if not force and self._sticker_cache and (now - self._sticker_cache_at) < self._sticker_cache_ttl:
            return self._sticker_cache
        stickers = list(await asyncio.to_thread(self.bot.fetch_custom_face))
        self._sticker_cache = stickers
        self._sticker_cache_at = now
        return self._sticker_cache

    @staticmethod
    def _sticker_note_key(sticker: dict[str, str]) -> str:
        emoji_id = str(sticker.get('emoji_id') or '').strip()
        package_id = str(sticker.get('emoji_package_id') or '').strip()
        key = str(sticker.get('key') or '').strip()
        if emoji_id:
            return 'mface:' + '|'.join(part for part in [emoji_id, package_id, key] if part)
        url = str(sticker.get('url') or '').strip()
        return f'url:{url}' if url else ''

    @classmethod
    def _get_sticker_note(cls, notes: dict, sticker: dict[str, str]) -> str:
        stable_key = cls._sticker_note_key(sticker)
        legacy_key = str(sticker.get('url') or '').strip()
        for candidate in [stable_key, legacy_key]:
            if not candidate:
                continue
            note = str(notes.get(candidate) or '').strip()
            if note:
                return note
        return ''

    @staticmethod
    def _sticker_preview_url(sticker: dict[str, str]) -> str:
        return str(sticker.get('url') or '').strip()

    def _build_trigger_message_entry(self, message: ChatMessage, cleaned: str) -> dict:
        entry = {
            'user_id': message.user_id,
            'nickname': message.nickname,
            'text': self._mark_mentions_self(message, cleaned or message.text),
            'raw_message': message.raw_message,
            'message_id': message.message_id,
            'timestamp': message.timestamp,
            'source_label': self._message_source_label(message),
            'source_kind': self._message_source_kind(message),
            'raw_source': message.raw_data.get('source'),
        }
        annotated, _ref_map = self._annotate_message_refs(message.chat_type, str(message.chat_id), [entry])
        return annotated[0] if annotated else entry

    def _dedupe_trigger_message_entries(self, entries: list[dict] | None) -> list[dict]:
        result: list[dict] = []
        seen: set[tuple] = set()
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            key = (
                str(entry.get('message_id') or ''),
                str(entry.get('message_ref') or ''),
                str(entry.get('raw_message') or ''),
                str(entry.get('text') or ''),
                str(entry.get('timestamp') or ''),
                str(entry.get('source_label') or ''),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(dict(entry))
        return result

    def get_character_session(self, scope_type: str, scope_id: str):
        """Return a shadow session identity; runtime ownership stays unchanged."""
        return self._character_sessions.get_or_create(scope_type, scope_id)

    def get_character_session_by_key(self, scope_key: str):
        scope_type, separator, scope_id = str(scope_key or '').partition(':')
        if not separator or not scope_type or not scope_id:
            raise ValueError('scope_key must be <scope_type>:<scope_id>')
        return self.get_character_session(scope_type, scope_id)


    def get_character_session_snapshots(self) -> tuple[dict, ...]:
        """Read shadow observations without changing runtime/status ownership."""
        return tuple(
            snapshot.to_dict() for snapshot in self._character_sessions.snapshots()
        )

    def observe_runtime_scope(self, scope_type: str, scope_id: str) -> dict:
        """Best-effort read of current owners; does not change status/WebUI."""
        return self._runtime_scope_observer.observe(scope_type, scope_id).to_dict()

    def observe_runtime_scope_by_key(self, scope_key: str) -> dict:
        return self._runtime_scope_observer.observe_key(scope_key).to_dict()

    def _scope_turn_is_active(self, scope_key: str) -> bool:
        return self._character_sessions.is_active(scope_key)

    def _scope_turn_has_pending(self, scope_key: str) -> bool:
        return self._event_mailbox.pending_count(scope_key) > 0

    def _scope_turn_is_busy(self, scope_key: str) -> bool:
        return self._scope_turn_is_active(scope_key) or self._scope_turn_has_pending(scope_key)

    def _activate_scope_turn(self, scope_key: str) -> None:
        # Preserve legacy set.add() semantics: repeated activation is idempotent.
        self._character_sessions.activate(scope_key)

    def _deactivate_scope_turn(self, scope_key: str) -> None:
        self._character_sessions.deactivate(scope_key)

    def _append_pending_scope_turn(self, scope_key: str, item: dict) -> int:
        envelope = envelope_from_scope_turn_item(item)
        if envelope.scope_key != scope_key:
            raise ValueError(f'pending scope mismatch: {envelope.scope_key} != {scope_key}')
        pending_count_before = self._event_mailbox.pending_count(scope_key)
        self._event_mailbox.append(envelope, transient=item)
        return pending_count_before

    def _pending_scope_turn_count(self, scope_key: str) -> int:
        return self._event_mailbox.pending_count(scope_key)

    def _pop_pending_scope_turn(self, scope_key: str) -> dict | None:
        entry = self._event_mailbox.pop_scope_entry(scope_key)
        if entry is None:
            return None
        if entry.transient is None:
            raise RuntimeError(f'pending mailbox entry missing transient item: {scope_key}')
        return entry.transient

    def _pop_next_live_pending_scope_turn(self, scope_key: str) -> dict | None:
        while True:
            pending = self._pop_pending_scope_turn(scope_key)
            if pending is None:
                return None
            if not self._is_message_stale(pending.get('message')):
                return pending

    def _drain_live_tool_scope_turn(self, scope_key: str) -> dict | None:
        """在工具循环中一次性取走当前 scope 已积压的全部 mailbox 事件。

        这里与 turn 结束后的 follow-up merge 使用同一套 batch 协调器，保证
        mailbox 的 FIFO 批量摄入规则在 mid-turn / post-turn 完全一致。
        """
        batch = self._turn_batch_coordinator.drain_scope_followup(
            scope_key,
            is_stale=lambda pending: self._is_message_stale((pending or {}).get('message')),
        )
        if batch is None:
            return None
        pending = dict(batch.turn_item)
        pending['scope_key'] = scope_key
        if not self._followup_has_actionable_event(pending):
            info(f'[AI][mailbox] skip silent-only tool followup scope={scope_key}')
            return None
        return pending

    def _followup_batch_items(self, item: dict) -> list[dict]:
        batch_items = item.get('batch_items')
        if isinstance(batch_items, list) and batch_items:
            return [dict(entry) for entry in batch_items if isinstance(entry, dict)]
        return [dict(item)]

    def _is_silent_message_item(self, item: dict) -> bool:
        return str(item.get('kind') or 'message') == 'message' and bool(item.get('silent_event'))

    def _followup_has_actionable_event(self, item: dict) -> bool:
        return any(
            not self._is_silent_message_item(entry)
            for entry in self._followup_batch_items(item)
        )

    def _merge_followup_items(self, scope_key: str, *segments: dict | None) -> dict | None:
        merged_items: list[dict] = []
        history_seed = None
        turn_metadata = None
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_items = segment.get('batch_items')
            if isinstance(segment_items, list) and segment_items:
                merged_items.extend(
                    dict(entry) for entry in segment_items if isinstance(entry, dict)
                )
            else:
                merged_items.append(dict(segment))
            if history_seed is None and segment.get('history_seed') is not None:
                history_seed = [dict(entry) for entry in (segment.get('history_seed') or [])]
            if turn_metadata is None and segment.get('turn_metadata') is not None:
                turn_metadata = dict(segment.get('turn_metadata') or {})
        if not merged_items:
            return None

        representative = dict(merged_items[-1])
        trigger_messages = []
        deferred_count = 0
        mailbox_event_ids: list[str] = []
        mailbox_sequences: list[int] = []
        for entry in merged_items:
            current_trigger_messages = entry.get('trigger_messages') or []
            if current_trigger_messages:
                trigger_messages.extend(copy.deepcopy(current_trigger_messages))
            else:
                message = entry.get('message')
                if isinstance(message, ChatMessage):
                    trigger_messages.append(
                        self._build_trigger_message_entry(
                            message,
                            entry.get('cleaned') or message.text,
                        )
                    )
            deferred_count += max(1, int(entry.get('deferred_count') or 0))
            for event_id in entry.get('mailbox_event_ids') or []:
                text = str(event_id or '').strip()
                if text:
                    mailbox_event_ids.append(text)
            for sequence in entry.get('mailbox_sequences') or []:
                if isinstance(sequence, int):
                    mailbox_sequences.append(sequence)

        representative.update({
            'scope_key': scope_key,
            'deferred_count': deferred_count,
            'trigger_messages': self._dedupe_trigger_message_entries(trigger_messages),
            'batch_items': [dict(entry) for entry in merged_items],
            'mailbox_event_ids': mailbox_event_ids,
            'mailbox_sequences': mailbox_sequences,
        })
        if history_seed is not None:
            representative['history_seed'] = history_seed
        if turn_metadata is not None:
            representative['turn_metadata'] = turn_metadata
        representative['batch_metadata'] = {
            'event_count': len(merged_items),
            'event_ids': list(mailbox_event_ids),
            'sequences': list(mailbox_sequences),
            'first_sequence': mailbox_sequences[0] if mailbox_sequences else None,
            'last_sequence': mailbox_sequences[-1] if mailbox_sequences else None,
        }
        return representative

    def _fold_silent_followup_head(self, scope_key: str, item: dict) -> dict | None:
        batch = self._turn_batch_coordinator.drain_scope_followup(
            scope_key,
            is_stale=lambda pending: self._is_message_stale((pending or {}).get('message')),
        )
        merged = self._merge_followup_items(
            scope_key,
            item,
            dict(batch.turn_item) if batch is not None else None,
        )
        if merged is None:
            return None
        if not self._followup_has_actionable_event(merged):
            info(f'[AI][mailbox] consumed silent-only batch scope={scope_key}')
            return None
        return merged

    def _clear_scope_turn_coordination(self) -> None:
        if hasattr(self, '_scope_dispatcher'):
            self._scope_dispatcher.clear_runtime_state()
            return
        self._event_mailbox.clear()
        self._character_sessions.clear_active()

    def _reserve_scope_turn(self, item: dict) -> bool:
        scope_key = self._scope_key(
            item['message'].chat_type,
            str(item['message'].chat_id),
        )
        item['scope_key'] = scope_key
        item.setdefault('deferred_count', 0)
        item.setdefault('trigger_messages', [])
        if self._scope_turn_is_active(scope_key):
            info(f'[AI][reserve] scope busy, deferring: {scope_key}')
            pending_count_before = self._append_pending_scope_turn(scope_key, {
                'kind': 'message',
                'message': item['message'],
                'cleaned': item['cleaned'],
                'agent_id': item['agent_id'],
                'scope_key': scope_key,
                'deferred_count': 1,
                'trigger_messages': list(item.get('trigger_messages') or []),
            })
            get_bot_logger().info(CAT_CHAT, scope_key, f'消息排队合并: 会话正忙, 当前排队数={pending_count_before + 1}')
            return False
        self._activate_scope_turn(scope_key)
        return True

    def _release_scope_turn(self, item: dict) -> dict | None:
        scope_key = str(item.get('scope_key') or '')
        if not scope_key:
            return None
        pending = self._pop_next_live_pending_scope_turn(scope_key)
        if pending:
            history_seed = item.get('followup_history_seed')
            if history_seed:
                pending['history_seed'] = [dict(entry) for entry in history_seed]
            return pending
        if self._promote_pending_scope_task(scope_key):
            return None
        self._deactivate_scope_turn(scope_key)
        return None

    def _take_pending_scope_turn(self, item: dict) -> dict | None:
        scope_key = str(item.get('scope_key') or '')
        if not scope_key:
            return None
        return self._pop_next_live_pending_scope_turn(scope_key)

    def _has_completed_turn_commit(self, item: dict) -> bool:
        evidence = item.get('turn_commit_evidence') or {}
        return bool(
            item.get('followup_history_seed') is not None
            and evidence.get('outbound_history_committed')
            and evidence.get('turn_log_committed')
            and evidence.get('turn_metadata_committed')
        )

    def _merge_followup_after_turn(self, item: dict, completed: bool) -> dict | None:
        scope_key = str(item.get('scope_key') or '')
        if not scope_key:
            return None
        history_seed = tuple(
            dict(entry) for entry in (item.get('followup_history_seed') or [])
        )
        metadata = dict(item.get('completed_turn_metadata') or {}) if completed else None
        batch = self._turn_batch_coordinator.drain_scope_followup(
            scope_key,
            history_seed=history_seed,
            metadata=metadata,
            is_stale=lambda pending: self._is_message_stale(
                (pending or {}).get('message')
            ),
        )
        if batch is not None:
            followup = dict(batch.turn_item)
            followup['scope_key'] = scope_key
            if not self._followup_has_actionable_event(followup):
                info(f'[AI][mailbox] skip silent-only followup scope={scope_key}')
                if completed and self._promote_pending_scope_task(scope_key):
                    return None
                return None
            return followup
        if completed and self._promote_pending_scope_task(scope_key):
            return None
        return None

    def _handoff_completed_scope_turn(self, item: dict) -> dict | None:
        scope_key = str(item.get('scope_key') or '')
        if not scope_key:
            return None
        completed = CompletedTurn(
            scope_key=scope_key,
            history_seed=tuple(
                dict(entry) for entry in (item.get('followup_history_seed') or [])
            ),
            metadata=dict(item.get('completed_turn_metadata') or {}),
        )
        batch = self._turn_batch_coordinator.drain_after_completed_turn(
            completed,
            is_stale=lambda pending: self._is_message_stale(
                (pending or {}).get('message')
            ),
        )
        if batch is not None:
            followup = batch.turn_item
            followup['scope_key'] = scope_key
            return followup
        if self._promote_pending_scope_task(scope_key):
            return None
        self._deactivate_scope_turn(scope_key)
        return None

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
        if self._scope_turn_is_active(scope_key):
            info(f'[AI][reserve] scope busy, deferring task: {scope_key}')
            self._character_sessions.append_pending_task(scope_key, {
                'kind': 'task',
                'task_id': item['task_id'],
                'message_epoch': self._resolve_message_epoch(item.get('message_epoch')),
            })
            return False
        self._activate_scope_turn(scope_key)
        return True

    def _promote_pending_scope_task(self, scope_key: str) -> bool:
        """scope 空闲后，若有延后的 task 则重新入队并保持 scope 占用，返回是否已提升。"""
        while True:
            pending = self._character_sessions.promote_pending_task_if_mailbox_empty(
                scope_key
            )
            if pending is None:
                return False
            if self._is_epoch_stale(pending.get('message_epoch')):
                continue
            pending['scope_prereserved'] = True
            self.queue.put_nowait(pending)
            return True

    def _release_task_scope(self, scope_key: str):
        """task turn 结束后由 actor 继续处理同 scope 的消息或任务。"""
        if not scope_key:
            return
        if hasattr(self, '_scope_dispatcher'):
            self._scope_dispatcher.wake(scope_key)
            return
        pending = self._pop_pending_scope_turn(scope_key)
        if pending:
            self.queue.put_nowait(pending)
            return
        if self._promote_pending_scope_task(scope_key):
            return
        self._deactivate_scope_turn(scope_key)

    def _submit_runtime_task(self, task_id: str, *, message_epoch: int | None = None) -> None:
        """统一把任务送回 ingress/router，避免绕过 scope dispatcher。"""
        if not self.queue:
            raise RuntimeError('runtime queue is not ready')
        task_id = str(task_id or '').strip()
        if not task_id:
            raise ValueError('task_id must be non-empty')
        self.queue.put_nowait({
            'kind': 'task',
            'task_id': task_id,
            'message_epoch': int(self._message_epoch if message_epoch is None else message_epoch),
        })

    def _cancel_active_requests(self):
        self._message_epoch += 1
        self._clear_scope_turn_coordination()
        self._character_sessions.clear_pending_tasks()
        for window in list(self._group_reply_windows.values()):
            t = window.get('task')
            if t and not t.done():
                t.cancel()
        self._group_reply_windows.clear()

    def _is_epoch_stale(self, epoch: int | None) -> bool:
        if epoch is None:
            return False
        return int(epoch) != int(self._message_epoch)

    def _resolve_message_epoch(self, epoch: int | None) -> int:
        """Normalize nullable runtime epochs.

        Some handoff items explicitly carry ``message_epoch=None``. Using
        ``dict.get('message_epoch', self._message_epoch)`` is not sufficient in
        that case because ``get`` only falls back when the key is missing.
        Treating ``None``/empty as "current epoch" keeps mailbox/task handoff
        resilient without changing stale-check semantics for real values.
        """
        if epoch in (None, ''):
            return int(self._message_epoch)
        return int(epoch)

    def _is_message_stale(self, message) -> bool:
        """检查消息时间戳是否超过最大允许时效。"""
        ts = getattr(message, 'timestamp', None)
        if ts is None:
            return False
        return (time.time() - float(ts)) > self._stale_message_max_age

    async def _run_message_turn(self, item: dict):
        try:
            await self._process_message(item)
        except Exception as exc:
            error(f'[AI] _process_message 异常中断 scope={item.get("message")!r}: {type(exc).__name__}: {exc}')
            # 异常保护：尝试持久化上下文，防止已发送消息/工具调用记录丢失
            try:
                message = item.get('message')
                if message is not None and hasattr(message, 'chat_type') and hasattr(message, 'chat_id'):
                    # 模型侧这一轮被丢弃了，但工具的副作用已经生效。必须把已执行的工具
                    # 写进历史，否则模型下一轮会以为自己什么都没做。
                    _executed = self._describe_executed_tools(message.chat_type, str(message.chat_id))
                    _interrupt_note = f'[系统] 上轮 AI 处理异常中断: {type(exc).__name__}'
                    if _executed:
                        _interrupt_note += (
                            f'\n中断前这些工具已经执行完并生效了: {_executed}。'
                            f'\n这一轮的模型回复丢失了，但上面的操作是真做过的，不要当成没发生；'
                            f'如需确认结果请重新查询，不要重复执行。'
                        )
                    self.repo.append_message(
                        message.chat_type,
                        str(message.chat_id),
                        {
                            'user_id': self.bot.self_id,
                            'nickname': '冰糖',
                            'text': _interrupt_note,
                            'raw_message': _interrupt_note,
                            'message_id': None,
                            'timestamp': time.time(),
                            'source_label': 'system-error',
                        },
                        self.config.history_limit,
                        self.config.diary_size,
                    )
                    # 判断是否为"软错误"（可恢复的概率性故障）：空内容、超时、5xx、连接错误
                    # 这类错误已经过 _complete_chat 内部 3 次 fallback 重试，仍失败时只记日志不通知号主
                    _exc_str = str(exc).lower()
                    _exc_name = type(exc).__name__
                    _is_soft_error = (
                        (_exc_name == 'RuntimeError' and '空内容' in _exc_str)
                        or 'timeout' in _exc_str
                        or 'timed out' in _exc_str
                        or 'status=5' in _exc_str  # 5xx errors
                        or 'connection' in _exc_str
                        or 'overloaded' in _exc_str
                        or 'rate limit' in _exc_str
                        or '429' in _exc_str
                        or '502' in _exc_str
                        or '503' in _exc_str
                        or '504' in _exc_str
                    )
                    # 软错误默认静默是为了防刷屏，但如果工具已经执行过，就意味着
                    # 有副作用悄悄生效且没人被告知。这种必须报，无论错误软不软。
                    if _executed:
                        _is_soft_error = False
                    if not _is_soft_error:
                        # 只有"硬错误"（认证失败、配置错误等不可恢复错误）才通知管理员
                        try:
                            current_model = self.model_manager.get_current_model()
                            model_name = current_model['display_name'] if current_model else 'unknown'
                            admin_notice = (
                                f'[AI异常通知]\n'
                                f'会话: {message.chat_type}:{message.chat_id}\n'
                                f'模型: {model_name}\n'
                                f'错误: {type(exc).__name__}: {str(exc)[:200]}\n'
                                + (f'中断前已生效的工具: {_executed}\n' if _executed else '')
                                + f'时间: {time.strftime("%Y-%m-%d %H:%M:%S")}'
                            )
                            self.bot.send_private_text(241898129, admin_notice)
                        except Exception as _notify_exc:
                            error(f'[AI] 管理员通知发送失败: {_notify_exc}')
                    else:
                        # 软错误：只记日志，不通知号主，避免刷屏
                        warn(f'[AI] 软错误（已重试耗尽，不通知管理员）scope={message.chat_type}:{message.chat_id} error={exc}')
            except Exception as _persist_exc:
                error(f'[AI] 异常保护持久化也失败: {_persist_exc}')
        # 生成期积压的事件在本轮结束后合并成一批 followup（只触发一次 AI）。
        # task 晋升与 scope 释放由 actor 循环独占，这里只处理消息批次。
        completed = (
            not self._is_epoch_stale(item.get('message_epoch'))
            and self._has_completed_turn_commit(item)
        )
        return self._merge_followup_after_turn(item, completed)

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
            '/model channel add name=<名称> base_url=<含版本路径的地址> api_key=<密钥> models=<显示名:模型ID,模型ID2> [messages_path=/messages]\n'
            '/model channel update <序号或名称> key=value ...\n'
            '/model channel remove <序号或名称>\n'
            '分级分流: /role list 查看各角色实际生效渠道；/test role <角色> 或 /test roles 按角色测试渠道'
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

    @staticmethod
    def _parse_command_bool(value, default: bool = True) -> bool:
        if value is None:
            return bool(default)
        text = str(value).strip().lower()
        if not text:
            return bool(default)
        if text in {'1', 'true', 'yes', 'y', 'on', 'strict', '严格'}:
            return True
        if text in {'0', 'false', 'no', 'n', 'off', 'loose', '宽松'}:
            return False
        raise ValueError(f'无法识别的布尔值: {value}')

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
                messages_path=kv.get('messages_path', '/messages'),
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
                api_key=kv.get('api_key', ''), protocol=kv.get('protocol', ''),
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
            '/upstream add name=<名称> base_url=<含版本路径的地址> api_key=<密钥> protocol=<anthropic|completions|responses>\n'
            '/upstream update <名称> key=value ...（可修改 protocol）\n'
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
            '/channel add name=<名称> [strategy=fallback|fallback_reset|random|roundrobin] [models=上游:模型ID,...]\n'
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
                self.bot.send_text(message.chat_type, message.chat_id, '用法: /role set <角色> <渠道名>\n角色可用: main tiered tiered_chat tiered_exec tiered_decision agent tasker vision（旧 dev_agent 输入仍兼容）')
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
            '角色: main / tiered / tiered_chat(聊天) / tiered_exec(执行) / tiered_decision(决策) / agent / tasker / vision（旧 dev_agent 输入仍兼容）'
        )
        self.bot.send_text(message.chat_type, message.chat_id, help_text)

    # ─── SSH 命令 /ssh ───

    def _ssh_command_help_text(self) -> str:
        return (
            'SSH 管理指令:\n'
            '/ssh list\n'
            '/ssh add profile_id=<ID> target=<user@host或Host别名> [root_dir=~] [port=22] [identity_file=] [password=] [shell=bash] [strict_host_key_checking=true]\n'
            '/ssh update <ID> key=value ...\n'
            '/ssh remove <ID>\n'
            '/ssh test <ID>\n'
            '/ssh validate <ID>'
        )

    async def _handle_ssh_command(self, message: ChatMessage, cleaned: str):
        import shlex
        try:
            parts = shlex.split(cleaned)
        except ValueError as exc:
            self.bot.send_text(message.chat_type, message.chat_id, f'指令解析失败: {exc}')
            return

        sub = str(parts[1] if len(parts) > 1 else '').strip().lower()
        if not sub or sub in {'list', 'ls'}:
            self.bot.send_text(message.chat_type, message.chat_id, self._format_ssh_profiles_list())
            return

        if sub in {'help', '?', 'h'}:
            self.bot.send_text(message.chat_type, message.chat_id, self._ssh_command_help_text())
            return

        if sub == 'add':
            kv, unknown = self._parse_model_kv_args(parts[2:])
            if unknown:
                self.bot.send_text(message.chat_type, message.chat_id, f'无法识别的参数: {" ".join(unknown)}')
                return
            profile_id = str(kv.get('profile_id') or kv.get('id') or '').strip()
            if not profile_id:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少 profile_id，例: /ssh add profile_id=prod target=root@example.com root_dir=/srv/app')
                return
            existing = self._get_ssh_profiles_map()
            if profile_id in existing:
                self.bot.send_text(message.chat_type, message.chat_id, f'SSH profile {profile_id} 已存在。')
                return
            try:
                strict_host_key_checking = self._parse_command_bool(kv.get('strict_host_key_checking'), True)
            except ValueError as exc:
                self.bot.send_text(message.chat_type, message.chat_id, str(exc))
                return
            parsed = parse_ssh_profiles([{
                'profile_id': profile_id,
                'target': kv.get('target'),
                'root_dir': kv.get('root_dir', '~'),
                'port': kv.get('port', 22),
                'identity_file': kv.get('identity_file', ''),
                'password': kv.get('password', ''),
                'shell': kv.get('shell', 'bash'),
                'strict_host_key_checking': strict_host_key_checking,
            }], warn_prefix='ssh_command.add')
            if not parsed:
                self.bot.send_text(message.chat_type, message.chat_id, 'SSH profile 参数无效，至少需要 profile_id 和 target。')
                return
            profiles = list(existing.values())
            profiles.append(parsed[0])
            self._save_ssh_profiles(profiles)
            self.bot.send_text(message.chat_type, message.chat_id, f'SSH profile {profile_id} 已添加。')
            return

        if sub in {'update', 'edit'}:
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标，例: /ssh update prod root_dir=/srv/app shell=bash')
                return
            profile_id = str(parts[2] or '').strip()
            profile_map = self._get_ssh_profiles_map()
            current = profile_map.get(profile_id)
            if current is None:
                self.bot.send_text(message.chat_type, message.chat_id, f'SSH profile {profile_id} 不存在。')
                return
            kv, unknown = self._parse_model_kv_args(parts[3:])
            if unknown:
                self.bot.send_text(message.chat_type, message.chat_id, f'无法识别的参数: {" ".join(unknown)}')
                return
            payload = self._ssh_profile_to_payload(current)
            for key in ('target', 'root_dir', 'port', 'identity_file', 'password', 'shell'):
                if key in kv:
                    payload[key] = kv.get(key)
            if 'strict_host_key_checking' in kv:
                try:
                    payload['strict_host_key_checking'] = self._parse_command_bool(kv.get('strict_host_key_checking'), True)
                except ValueError as exc:
                    self.bot.send_text(message.chat_type, message.chat_id, str(exc))
                    return
            parsed = parse_ssh_profiles([payload], warn_prefix='ssh_command.update')
            if not parsed:
                self.bot.send_text(message.chat_type, message.chat_id, f'SSH profile {profile_id} 更新后的配置无效。')
                return
            profile_map[profile_id] = parsed[0]
            self._save_ssh_profiles(list(profile_map.values()))
            self.bot.send_text(message.chat_type, message.chat_id, f'SSH profile {profile_id} 已更新。')
            return

        if sub in {'remove', 'rm', 'del', 'delete'}:
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标，例: /ssh remove prod')
                return
            profile_id = str(parts[2] or '').strip()
            profile_map = self._get_ssh_profiles_map()
            if profile_id not in profile_map:
                self.bot.send_text(message.chat_type, message.chat_id, f'SSH profile {profile_id} 不存在。')
                return
            del profile_map[profile_id]
            self._save_ssh_profiles(list(profile_map.values()))
            self.bot.send_text(message.chat_type, message.chat_id, f'SSH profile {profile_id} 已删除。')
            return

        if sub in {'test', 'validate', 'check'}:
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '缺少目标，例: /ssh test prod')
                return
            profile_id = str(parts[2] or '').strip()
            profile = self._get_ssh_profiles_map().get(profile_id)
            if profile is None:
                self.bot.send_text(message.chat_type, message.chat_id, f'SSH profile {profile_id} 不存在。')
                return
            result = await asyncio.to_thread(validate_ssh_profile, profile)
            lines = [
                f'SSH 验证结果: {"成功" if result.get("ok") else "失败"}',
                f'profile_id: {result.get("profile_id") or profile_id}',
                f'target: {result.get("target") or profile.target}',
                f'root_dir: {result.get("root_dir") or profile.root_dir}',
            ]
            if result.get('remote_pwd'):
                lines.append(f'remote_pwd: {result.get("remote_pwd")}')
            if 'root_exists' in result:
                lines.append(f'root_exists: {bool(result.get("root_exists"))}')
            if result.get('error'):
                lines.append(f'error: {result.get("error")}')
            self.bot.send_text(message.chat_type, message.chat_id, '\n'.join(lines))
            return

        self.bot.send_text(message.chat_type, message.chat_id, self._ssh_command_help_text())

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

    def _is_master_private_message(self, message: ChatMessage) -> bool:
        if str(getattr(message, 'chat_type', '')) != 'private':
            return False
        master_qq = str(getattr(self.config, 'master_qq', 0) or '').strip()
        if not master_qq or master_qq == '0':
            return False
        chat_id = str(getattr(message, 'chat_id', '') or '').strip()
        user_id = str(getattr(message, 'user_id', '') or '').strip()
        return chat_id == master_qq or user_id == master_qq

    def _is_message_allowed_by_power_mode(self, message: ChatMessage) -> bool:
        if not bool(getattr(self.config, 'enabled', True)):
            return False
        if not bool(getattr(self.config, 'silent_mode', False)):
            return True
        return self._is_master_private_message(message)

    # 号主 QQ：命令类指令（/、# 开头）只允许该账号触发
    _COMMAND_MASTER_QQ = 241898129

    def _is_command_master(self, message: ChatMessage) -> bool:
        """命令权限校验：仅号主本人（QQ 241898129）可触发斜杠/井号命令。"""
        try:
            return int(message.user_id or 0) == self._COMMAND_MASTER_QQ
        except (TypeError, ValueError):
            return False

    def _is_tasker_authorized(self, scope_type: str, scope_id: str) -> bool:
        """私聊场景下 tasker 只能由管理员账号发起，群聊暂不限制。"""
        if str(scope_type) != 'private':
            return True
        return str(scope_id) == str(self.config.admin_qq)

    # Legacy internal alias: persisted/tool kind remains dev_agent for compatibility.
    def _is_dev_agent_authorized(self, scope_type: str, scope_id: str) -> bool:
        return self._is_tasker_authorized(scope_type, scope_id)

    @staticmethod
    def _normalize_task_kind(kind: str) -> str:
        """Map public tasker terminology to the legacy persisted kind."""
        normalized = str(kind or '').strip()
        return 'dev_agent' if normalized == 'tasker' else normalized

    @staticmethod
    def _task_kind_label(kind: str) -> str:
        """Return model/user-visible terminology without rewriting persisted history."""
        return 'tasker' if str(kind or '').strip() in {'dev_agent', 'tasker'} else str(kind or '')

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
            self.config.silent_mode = False
            self.bot.send_text(message.chat_type, message.chat_id, 'AI 已开启。')
            return

        if command == '/off':
            self.config.enabled = False
            self.config.silent_mode = False
            self._cancel_active_requests()
            self.bot.send_text(message.chat_type, message.chat_id, 'AI 已关闭，后续普通消息将不再触发。')
            return

        if command == '/silent':
            self.config.enabled = True
            self.config.silent_mode = True
            self._cancel_active_requests()
            self.bot.send_text(
                message.chat_type,
                message.chat_id,
                'AI 已进入静默模式：仅保留与主人的私聊在线，其他会话不再触发。用 /on 可恢复。',
            )
            return

        if command == '/clean':
            self.repo.reset_all()
            self._cancel_active_requests()
            self._recent_message_keys.clear()
            self._scheduled_alarm_ids.clear()
            self.config.enabled = True
            self.config.silent_mode = False
            self.bot.send_text(
                message.chat_type,
                message.chat_id,
                'AI 已重置：全部对话、印象、备注、工具记录、上下文快照、任务和关系数据已清空，并恢复为开启状态。',
            )
            return

    def _get_scope_thinking_level(self, scope_type: str, scope_id: str) -> str:
        return self._scope_thinking_levels.get(self._scope_key(scope_type, scope_id), 'low')

    def _get_scope_session_mode(self, scope_type: str, scope_id: str) -> str:
        return self._scope_session_modes.get(self._scope_key(scope_type, scope_id), 'chat')

    def _code_idle_turns(self) -> dict:
        counters = getattr(self, '_scope_code_idle_turns', None)
        if counters is None:
            counters = {}
            self._scope_code_idle_turns = counters
        return counters

    def _set_scope_session_mode(self, scope_type: str, scope_id: str, mode: str) -> None:
        scope_key = self._scope_key(scope_type, scope_id)
        self._scope_session_modes[scope_key] = mode
        # 换模式就重新计数，否则切回 code 的第一轮就可能被劝退。
        self._code_idle_turns().pop(scope_key, None)

    def _note_session_mode_activity(
        self,
        scope_type: str,
        scope_id: str,
        tool_iterations: list[dict] | None,
        turn_kind: str = 'message',
    ) -> None:
        """记录本轮有没有用到 code 专属工具：用了就归零，没用就累加。"""
        scope_key = self._scope_key(scope_type, scope_id)
        counters = self._code_idle_turns()
        if getattr(self, '_scope_session_modes', {}).get(scope_key) != 'code':
            counters.pop(scope_key, None)
            return
        # 只数真实对话轮；agent 汇报等内部触发轮不该把会话推向"该切回 chat"。
        if turn_kind != 'message':
            return
        for iteration in (tool_iterations or []):
            for call in ((iteration or {}).get('tool_calls') or []):
                if str((call or {}).get('name') or '') in CODE_MODE_TOOL_NAMES:
                    counters[scope_key] = 0
                    return
        counters[scope_key] = counters.get(scope_key, 0) + 1

    def _consume_code_mode_switch_hint(self, scope_type: str, scope_id: str) -> str:
        """闲够阈值就取出一次提示；取走即归零，避免每轮都念。"""
        scope_key = self._scope_key(scope_type, scope_id)
        if getattr(self, '_scope_session_modes', {}).get(scope_key) != 'code':
            return ''
        counters = self._code_idle_turns()
        idle = counters.get(scope_key, 0)
        if idle < self.CODE_MODE_IDLE_TURN_LIMIT:
            return ''
        counters[scope_key] = 0
        return (
            f'[系统提示] 本会话已连续 {idle} 轮没有用到任何 code 专属工具（派发 agent、搜索、记忆、日志等）。'
            'code 模式的工具表比 chat 大得多，一直挂着很费 token。'
            '如果接下来只是普通聊天，调用 set_session_mode 把 mode 设为 chat；'
            '如果马上还要干活就忽略这条，继续用 code。'
            '这条提示只对你可见，不要发给用户，也不要在聊天里提模式这回事。'
        )

    async def _handle_thinking_command(self, message: ChatMessage, cleaned: str):
        parts = str(cleaned or '').strip().lower().split()
        scope_key = self._scope_key(message.chat_type, str(message.chat_id))
        if len(parts) == 1:
            current = self._scope_thinking_levels.get(scope_key, 'low')
            self._send_chat_reply(
                message,
                f'当前思考等级：{current}\n用法：/thinking off|low|medium|high',
            )
            return
        if len(parts) != 2 or parts[1] not in {'off', 'low', 'medium', 'high'}:
            self._send_chat_reply(message, '用法：/thinking off|low|medium|high')
            return
        level = parts[1]
        self._scope_thinking_levels[scope_key] = level
        self._send_chat_reply(message, f'当前会话思考等级已设为：{level}（仅内存保存，重启恢复 low）')

    async def _handle_trigger_command(self, message: ChatMessage, cleaned: str):
        if not self._is_admin_message(message):
            self.bot.send_text(message.chat_type, message.chat_id, '这个指令你先别动。')
            return

        parts = str(cleaned or '').strip().lower().split()
        current = float(
            getattr(
                self.config,
                'global_trigger_rate',
                getattr(self.repo, 'default_trigger_rate', 0.0),
            )
        )
        usage = (
            f'当前全局随机触发概率：{current:.3f}\n'
            '说明：仅影响群聊随机触发，私聊仍默认触发。\n'
            '用法：/trigger 0.08'
        )
        if len(parts) == 1:
            self._send_chat_reply(message, usage)
            return
        if len(parts) != 2:
            self._send_chat_reply(message, usage)
            return
        try:
            rate = float(parts[1])
        except (TypeError, ValueError):
            self._send_chat_reply(message, '设置失败：rate 必须是数字，范围 0 ~ 0.30。')
            return
        if not (0.0 <= rate <= 0.30):
            self._send_chat_reply(message, '设置失败：rate 超出范围，只能在 0 ~ 0.30 之间，可设置为 0。')
            return

        updated_count, persisted = self._apply_global_trigger_rate(rate)
        result = (
            f'全局随机触发概率已设为：{rate:.3f}。\n'
            f'已同步现有会话：{updated_count} 个；新会话默认继承该值。\n'
            '私聊默认仍会触发，这个值主要影响群聊随机触发。'
        )
        if not persisted:
            result += '\n注意：写入 config.yaml 失败，当前仅在本次运行内生效。'
        self._send_chat_reply(message, result)

    def _apply_global_trigger_rate(self, rate: float) -> tuple[int, bool]:
        """把随机触发概率作为全局默认值落地：内存 + 现有会话画像 + config.yaml。

        只改现有会话画像不够——新会话是按 config 的 global_trigger_rate 播种的，
        不写 config.yaml 的话重启后新建会话会回到旧值。
        """
        rate = float(rate)
        self.config.global_trigger_rate = rate
        self.repo.default_trigger_rate = rate
        updated_count = self.repo.update_all_agent_trigger_rates(rate)
        persisted = save_config_to_yaml({'ai': {'global_trigger_rate': rate}})
        return updated_count, persisted

    def _snapshot_for_role(self, role: str, model_config: dict | None):
        """构造本次请求的模型快照。

        main 角色沿用运行时单例 self.model（含会话/回退同步）；tiered 子渠道为
        每个请求构建独立 AnthropicChatModel 快照，避免多个 scope 并发互相覆盖
        单例导致模型串线。model_config 为空时回退单例快照。
        """
        if role == 'main' or not model_config:
            return self._model_completion.snapshot()
        client = AnthropicChatModel(
            base_url=model_config['base_url'],
            api_key=model_config['api_key'],
            model_name=model_config['model_name'],
            messages_path=model_config['messages_path'],
        )
        return ModelRequestSnapshot(
            client=client,
            model_name=client.model_name,
            api_url=f'{client.base_url}{client.messages_path}',
        )

    async def _complete_chat(
        self,
        system_blocks: list[dict],
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        scope_key: str = None,
        execution_pool: AsyncExecutionPool | None = None,
        role: str = 'main',
    ) -> AnthropicReply | None:
        _MAX_FALLBACK_ATTEMPTS = 3
        _tool_count = len(tools) if tools else 0
        _msg_count = len(messages) if messages else 0
        begin_request = getattr(self.model_manager, 'begin_request', None)
        if callable(begin_request):
            begin_request(role)
        # 角色模型解析：main 走旧接口（含运行时单例同步），tiered 子渠道按需解析。
        if role == 'main':
            def _role_model():
                return self.model_manager.get_current_model()
        else:
            def _role_model():
                return self.model_manager.get_model_for_role(role)
        # 初始化重试计数
        if scope_key:
            self._scope_retry次数[scope_key] = 0
            current = _role_model()
            self._scope_current_model[scope_key] = current['display_name'] if current else 'unknown'

        last_exc: Exception | None = None
        for attempt in range(_MAX_FALLBACK_ATTEMPTS):
            current = _role_model()
            _model_name = current['display_name'] if current else 'unknown'
            if scope_key:
                self._scope_current_model[scope_key] = _model_name
            request_snapshot = self._snapshot_for_role(role, current)
            _timed_out_once = False  # 本候选模型是否已做过一次 timeout 同模型重试
            # 内层最多执行两次：首次 timeout 后同模型重试一次
            for _inner in range(2):
                info(
                    f'[AI][api] request model={_model_name} '
                    f'url={request_snapshot.api_url} '
                    f'messages={_msg_count} tools={_tool_count} '
                    f'temp={temperature} attempt={attempt + 1}/{_MAX_FALLBACK_ATTEMPTS}'
                    + (f' inner_retry={_inner}' if _inner else '')
                )
                _api_start = time.perf_counter()
                try:
                    _reply = await self._model_completion.complete(
                        request_snapshot,
                        system_blocks,
                        messages,
                        tools,
                        temperature,
                        thinking=self._scope_thinking_levels.get(scope_key, 'low') if scope_key else 'low',
                        execution_pool=execution_pool,
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
                    if _reply is not None:
                        self.token_usage_store.record(
                            _reply.input_tokens,
                            _reply.output_tokens,
                            estimated=bool(_reply.usage_estimated),
                            model=_model_name,
                            scope_key=scope_key,
                        )
                    return _reply
                except Exception as exc:
                    last_exc = exc
                    _api_ms = int((time.perf_counter() - _api_start) * 1000)
                    _exc_name = type(exc).__name__
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
                    _is_runtime_5xx = (
                        _exc_name == 'RuntimeError'
                        and 'status=5' in str(exc)
                    )
                    _is_empty_content = (
                        _exc_name == 'RuntimeError'
                        and '空内容' in str(exc)
                    )
                    # 连接类瞬态错误（连接重置/拒绝/代理断开等）：与 agent 链路
                    # _is_retryable_api_error 对齐，纳入 fallback，避免一次瞬态失败直接 abort 整轮。
                    _conn_error_keywords = (
                        'connection reset', 'connection aborted', 'connection refused',
                        'remote end closed connection', 'max retries exceeded',
                        'connecterror', 'proxyerror', 'remoteprotocolerror',
                        'temporarily unavailable', 'network is unreachable', 'broken pipe',
                    )
                    _is_conn_error = (
                        _exc_name in ('ConnectionError', 'ConnectionResetError', 'ProxyError', 'RemoteDisconnected', 'ConnectError', 'RemoteProtocolError')
                        or any(k in str(exc).lower() for k in _conn_error_keywords)
                    )
                    _is_fallbackable = _is_httpx_timeout or _is_requests_timeout or _is_api_5xx or _is_runtime_5xx or _is_empty_content or _is_conn_error
                    _is_timeout = _is_httpx_timeout or _is_requests_timeout

                    error(
                        '[AI][model] request failed '
                        f"model={current['display_name'] if current else 'unknown'} "
                        f"base_url={request_snapshot.client.base_url} "
                        f'fallbackable={_is_fallbackable} '
                        f'attempt={attempt + 1}/{_MAX_FALLBACK_ATTEMPTS} '
                        f'error={exc}'
                    )
                    get_bot_logger().error(CAT_API, '', f'API 调用异常 model={_model_name} ms={_api_ms}ms fallbackable={_is_fallbackable} attempt={attempt+1}/{_MAX_FALLBACK_ATTEMPTS} error={_exc_name}: {exc}')

                    if not _is_fallbackable:
                        raise

                    # 首次 timeout：同模型重试一次，不推进 fallback
                    if _is_timeout and not _timed_out_once:
                        _timed_out_once = True
                        info(f'[AI][model] timeout retry same model={_model_name}')
                        if scope_key:
                            self._scope_retry次数[scope_key] = (self._scope_retry次数.get(scope_key) or 0) + 1
                        await asyncio.sleep(2)
                        continue  # inner retry

                    # 非 timeout 或已重试过：跳出内层，推进 fallback
                    break

            # 内层两次都失败，检查是否还有 fallback 候选
            if attempt >= _MAX_FALLBACK_ATTEMPTS - 1:
                if last_exc is not None:
                    raise last_exc
                raise RuntimeError('模型重试耗尽，但未捕获到具体异常。')

            if scope_key:
                self._scope_retry次数[scope_key] = attempt + 1

            self.model_manager.notify_failure(role)
            next_model = _role_model()
            if next_model:
                info(f'[AI][model] fallback switching to {next_model["display_name"]}')
                if role == 'main':
                    # main 沿用旧行为：同步运行时单例 self.model，供后续回合直接使用。
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
        cleaned = self._clean_text(message)
        source_kind = self._message_source_kind(message)
        source_label = self._message_source_label(message)

        # 静默巡检：记录真实用户消息时间，清除已触发标记
        if source_kind not in ('internal_task', 'admin_webui', 'system_private') and message.user_id != 0:
            _sk = self._scope_key(scope_type, scope_id)
            self._scope_last_user_msg_at[_sk] = time.time()
            self._scope_silence_fired.discard(_sk)

        # 先判定“是否应完全屏蔽”，再决定是否接触 agent 状态或写入长期消息历史。
        # `system_private` 这类官方/系统消息不应进入 AI 上下文，也不应在之后被
        # history_seed / diary 重放，否则就会出现“本轮触发没看到，后面复盘却看到了”
        # 的上下文分叉。
        if self._should_ignore_message(message):
            self.repo.add_note(scope_type, scope_id, f'识别到非普通聊天来源消息: {source_label}')
            return

        agent = await asyncio.to_thread(self.repo.get_or_create_agent, scope_type, scope_id)

        # 命令权限统一收口：以 / 或 # 开头的命令类消息只允许号主(241898129)触发。
        # 其他任何人（私聊/群）发命令一律静默 return，不回应。仅拦截真实用户来源，
        # 内部来源（后台任务回执、admin_webui）不受影响，避免误伤自然聊天照常处理。
        if (
            str(cleaned or '').startswith(('/', '#'))
            and source_kind not in ('internal_task', 'admin_webui')
            and not self._is_command_master(message)
        ):
            return

        if cleaned in {'/on', '/off', '/silent', '/clean', '/stop', '/restart'}:
            await self._handle_power_command(message, cleaned)
            return

        if re.fullmatch(r'/thinking(?:\s+.*)?', str(cleaned or '').strip(), flags=re.IGNORECASE):
            await self._handle_thinking_command(message, cleaned)
            return

        if re.fullmatch(r'/trigger(?:\s+.*)?', str(cleaned or '').strip(), flags=re.IGNORECASE):
            await self._handle_trigger_command(message, cleaned)
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

        if cleaned.startswith('/ssh'):
            await self._handle_ssh_command(message, cleaned)
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

        if cleaned in {'#speed', '#速度'}:
            self._send_speed_stats(message)
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

        if cleaned == '#into' or cleaned.startswith('#into '):
            await self._handle_into_command(message, cleaned)
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
            self._send_chat_reply(message, '当前会话的活动消息、近期原始窗口和待压缩原文已清空；历史摘要、备注和审计工具日志仍保留。')
            return

        if cleaned in {'#clear-notes', '#清空备注'}:
            self.repo.clear_notes(scope_type, scope_id)
            self._send_chat_reply(message, '这段会话的 AI 工具备忘已经清空了。')
            return

        if cleaned in {'#clear-all', '#clear-memory', '#清空记忆'}:
            self.repo.clear_memory(scope_type, scope_id)
            self._send_chat_reply(message, '这段会话的聊天记录、AI 工具备忘和工具记录都清掉了。')
            return

        if not self._is_message_allowed_by_power_mode(message):
            return

        inbound_entry = self._register_persistent_message_ref(
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
        )
        _has_pending = await asyncio.to_thread(
            self.repo.append_message,
            scope_type,
            scope_id,
            inbound_entry,
            self.config.history_limit,
            self.config.diary_size,
        )
        if _has_pending:
            await self._maybe_schedule_diary_summarization(scope_type, scope_id)
        await asyncio.to_thread(
            self.repo.touch_user_identity, message.user_id, message.nickname, scope_type, scope_id
        )
        agent = await asyncio.to_thread(self.repo.get_or_create_agent, scope_type, scope_id)

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

        if source_kind not in ('internal_task', 'admin_webui') and message.user_id != 0:
            if await self._route_into_agent_message(message, cleaned):
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
        scope_key = self._scope_key(item['message'].chat_type, str(item['message'].chat_id))
        item['scope_key'] = scope_key
        item.setdefault('deferred_count', 0)
        item.setdefault('trigger_messages', [])
        if self._scope_turn_is_active(scope_key):
            self._append_pending_scope_turn(scope_key, {
                'kind': 'message',
                'message': item['message'],
                'cleaned': item['cleaned'],
                'agent_id': item['agent_id'],
                'scope_key': scope_key,
                'deferred_count': 1,
                'trigger_messages': list(item.get('trigger_messages') or []),
            })
            self._scope_dispatcher.wake(scope_key)
            return
        self._scope_dispatcher.submit_event(
            envelope_from_scope_turn_item(item), item
        )

    async def _enqueue_self_message(self, message: ChatMessage):
        text = str(message.text or '').strip()
        if not text:
            return
        message.raw_data = dict(message.raw_data or {})
        message.raw_data['source'] = 'self_other_device'
        scope_type = message.chat_type
        scope_id = str(message.chat_id)
        source_kind = self._message_source_kind(message)
        source_label = self._message_source_label(message)
        entry = {
            'user_id': self.bot.self_id,
            'nickname': '冰糖',
            'text': text,
            'raw_message': message.raw_message,
            'message_id': message.message_id,
            'timestamp': message.timestamp,
            'source_kind': source_kind,
            'source_label': source_label,
        }
        entry = self._register_persistent_message_ref(scope_type, scope_id, entry)
        self.repo.append_message(scope_type, scope_id, entry, self.config.history_limit, self.config.diary_size)
        scope_key = self._scope_key(scope_type, scope_id)
        agent = await asyncio.to_thread(self.repo.get_or_create_agent, scope_type, scope_id)
        item = {
            'kind': 'message',
            'message': message,
            'cleaned': text,
            'agent_id': agent.agent_id,
            'scope_key': scope_key,
            'message_epoch': self._message_epoch,
            'deferred_count': 1,
            'trigger_messages': [self._build_trigger_message_entry(message, text)],
            'silent_event': True,
        }
        if self._scope_turn_is_active(scope_key):
            self._append_pending_scope_turn(scope_key, item)
            self._scope_dispatcher.wake(scope_key)
            info(f'[AI][self_message] queued silent followup scope={scope_key} text={text[:40]}')
            return
        self._scope_dispatcher.submit_event(
            envelope_from_scope_turn_item(item), item
        )
        info(f'[AI][self_message] queued silent event scope={scope_key} text={text[:40]}')

    async def _route_task_queue_drain(self):
        """兼容入口；后台任务路由由 TaskIngressRouter 独占。"""
        if self._task_ingress_router is None:
            raise RuntimeError('task ingress router is not initialized')
        await self._task_ingress_router.run()

    async def _consume_scope_item(self, scope_key: str, item: dict) -> None:
        kind = item.get('kind')
        try:
            if kind == 'message':
                followup = item
                while followup is not None:
                    if not self._followup_has_actionable_event(followup):
                        followup = self._fold_silent_followup_head(scope_key, followup)
                        continue
                    followup = await self._run_message_turn(followup)
            elif kind == 'task' and scope_key.startswith('task:'):
                async with self._background_task_semaphore:
                    await self._process_task(item)
            elif kind == 'task':
                await self._process_task(item)
        except Exception as exc:
            error(f'[AI][scope_actor] scope={scope_key} kind={kind} error={exc}')

    def _on_scope_idle(self, _scope_key: str) -> None:
        try:
            self._flush_agent_reports(only_if_idle=True)
        except Exception as exc:
            error(f'[AI] scope idle flush agent reports failed: {exc}')

    def _send_chat_reply(self, message: ChatMessage, text: str):
        self.bot.send_text(message.chat_type, message.chat_id, text)
        # _record_outbound_message 现为 async（append_message 已移出事件循环）。
        # 本方法是同步的指令快通道，用 fire-and-forget 调度落库，不阻塞指令响应。
        if self.loop is not None:
            self.loop.create_task(
                self._record_outbound_message(message.chat_type, str(message.chat_id), text)
            )

    def _send_speed_stats(self, message: ChatMessage) -> None:
        text = self.model_speed_stats.format_text()
        raw = f'```text\n{text}\n```'
        image_entry = self._try_send_code_image(message, text, 'text', raw=raw)
        if image_entry is None:
            self._send_chat_reply(message, text)

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
        diary_ctx = self.repo.get_diary_context(scope_type, scope_id)
        messages = diary_ctx.get('current') or []
        diary_window = diary_ctx.get('window') or []
        diary_pending = diary_ctx.get('pending') or []
        diary_summaries = diary_ctx.get('summaries') or []
        recent_raw_count = len(messages) + sum(len(item.get('messages') or []) for item in diary_window)
        pending_raw_count = sum(len(item.get('messages') or []) for item in diary_pending)
        audit_tool_log_count = len(self.tools.list_tool_uses(scope_type, scope_id))
        notes = self.repo.list_notes(scope_type, scope_id)
        recent_task = self._list_recent_agent_tasks(agent.agent_id, limit=1)

        # 构建 scope_key
        scope_key = f"{scope_type}:{scope_id}"

        # 检查当前会话状态
        is_generating = self._scope_turn_is_active(scope_key)
        current_model = self._scope_current_model.get(scope_key, '')
        retry_count = self._scope_retry次数.get(scope_key, 0)

        # 获取排队消息数
        pending_count = self._pending_scope_turn_count(scope_key)

        lines = [
            f'会话: {scope_type}:{scope_id}',
            f'来源: {source_label}',
        ]

        # 添加 AI 状态信息
        if is_generating:
            lines.append(f'AI 状态: 生成中')
            if current_model:
                lines.append(f'当前模型: {current_model}')
            if retry_count > 0:
                lines.append(f'重试次数: {retry_count}')
        else:
            lines.append(f'AI 状态: 空闲')
        direct_agent_id = str(self._scope_direct_agents.get(scope_key) or '').strip()
        if direct_agent_id:
            direct_record = self.agent_manager.get_agent(direct_agent_id) or {}
            direct_status = str(direct_record.get('status') or 'unknown')
            lines.append(f'Agent直连: {direct_agent_id} | {direct_status}')
        else:
            lines.append('Agent直连: 未进入')

        usage_snapshot = self.token_usage_store.snapshot(scope_key)
        usage = usage_snapshot.get('last')
        if usage:
            input_tokens = usage.get('input_tokens')
            output_tokens = usage.get('output_tokens')
            input_text = str(input_tokens) if input_tokens is not None else '不可用'
            output_text = str(output_tokens) if output_tokens is not None else '不可用'
            estimate_mark = '（估算）' if usage.get('estimated') else ''
            lines.append(f'最近 Token{estimate_mark}: 输入 {input_text} / 输出 {output_text}')
        else:
            lines.append('最近 Token: 暂无')

        scope_usage = usage_snapshot.get('scope')
        if scope_usage:
            scope_total = scope_usage['input_tokens'] + scope_usage['output_tokens']
            scope_mark = '（含估算）' if scope_usage.get('estimated_call_count') else ''
            lines.append(
                f"当前会话累计 Token{scope_mark}: 输入 {scope_usage['input_tokens']} / "
                f"输出 {scope_usage['output_tokens']} / 合计 {scope_total}"
            )
        else:
            lines.append('当前会话累计 Token: 输入 0 / 输出 0 / 合计 0')

        global_usage = usage_snapshot['global']
        global_total = global_usage['input_tokens'] + global_usage['output_tokens']
        global_mark = '（含估算）' if global_usage.get('estimated_call_count') else ''
        lines.append(
            f"全局累计 Token{global_mark}: 输入 {global_usage['input_tokens']} / "
            f"输出 {global_usage['output_tokens']} / 合计 {global_total}"
        )

        # 添加排队消息数
        if pending_count > 0:
            lines.append(f'排队消息: {pending_count}')

        # 添加原有信息
        lines.extend([
            f"全局模型: {runtime['active_profile']} -> {runtime['active_model']}",
            f"邮箱待处理: {runtime['queue_size']} | 会话Actor: {runtime['active_actor_count']} | 后台入口: {runtime['task_ingress_size']} | 闹钟: {runtime['scheduled_alarm_count']}",
            f"累计消息数: {int(agent.message_count or 0)} | 活动消息: {len(messages)} | 近期原始上下文: {recent_raw_count}",
            f"历史摘要: {len(diary_summaries)} | 待压缩段: {len(diary_pending)}（{pending_raw_count} 条）",
            f"备注: {len(notes)} | 审计工具日志: {audit_tool_log_count}（不注入模型背景）",
            f"印象更新时间: {self._format_ts_text(agent.impression_updated_at) or '暂无'}",
        ])

        if recent_task:
            item = recent_task[0]
            lines.append(f"最近任务: {item.get('task_id')} {self._task_kind_label(item.get('kind'))} / {item.get('status')}")
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
            lines.append(f"- {item.get('task_id')} | {self._task_kind_label(item.get('kind'))} | {item.get('status')} | {result}")
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
                f"类型: {self._task_kind_label(task.get('kind'))}",
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

    def _list_scope_agents(self, scope_type: str, scope_id: str) -> list[dict]:
        origin_scope = self._scope_key(scope_type, scope_id)
        return [
            dict(item)
            for item in self.agent_manager.list_agents()
            if str(item.get('origin_scope') or '').strip() == origin_scope
        ]

    def _format_scope_agents_text(self, scope_type: str, scope_id: str) -> str:
        agents = self._list_scope_agents(scope_type, scope_id)
        if not agents:
            return '当前会话没有可进入的常驻 agent。'
        lines = [f'当前会话共 {len(agents)} 个常驻 agent：']
        for item in agents:
            lines.append(
                f"- {item.get('agent_id')} | {item.get('status')} | "
                f"目录:{item.get('cwd') or '/'} | "
                f"{item.get('instruction_summary') or ''}"
            )
        return '\n'.join(lines)

    def _resolve_into_target_agent(self, scope_type: str, scope_id: str, target: str) -> tuple[dict | None, str | None]:
        target = str(target or '').strip()
        agents = self._list_scope_agents(scope_type, scope_id)
        if not agents:
            return None, '当前会话没有可进入的常驻 agent。'
        if not target:
            current = str(self._scope_direct_agents.get(self._scope_key(scope_type, scope_id)) or '').strip()
            if current:
                record = self.agent_manager.get_agent(current)
                if record:
                    return record, None
            if len(agents) == 1:
                record = self.agent_manager.get_agent(str(agents[0].get('agent_id') or ''))
                return record, None
            return None, f'{self._format_scope_agents_text(scope_type, scope_id)}\n请用 #into <agent_id> 指定要进入的 agent。'
        if target.lower() in {'latest', 'last', 'recent'}:
            record = self.agent_manager.get_agent(str(agents[0].get('agent_id') or ''))
            return record, None
        record = self.agent_manager.get_agent(target)
        if not record:
            return None, f'agent {target} 不存在。'
        if str(record.get('origin_scope') or '').strip() != self._scope_key(scope_type, scope_id):
            return None, f'agent {target} 不属于当前会话，不能进入直连。'
        return record, None

    def _bind_scope_direct_agent(self, scope_type: str, scope_id: str, agent_id: str) -> None:
        self._scope_direct_agents[self._scope_key(scope_type, scope_id)] = str(agent_id or '').strip()

    def _unbind_scope_direct_agent(self, scope_type: str, scope_id: str) -> str:
        return str(self._scope_direct_agents.pop(self._scope_key(scope_type, scope_id), '') or '').strip()

    async def _handle_into_command(self, message: ChatMessage, cleaned: str) -> None:
        scope_type = message.chat_type
        scope_id = str(message.chat_id)
        arg = str(cleaned[len('#into'):].strip() if cleaned.startswith('#into') else '')
        if arg.lower() in {'off', 'exit', 'quit', 'close'}:
            previous = self._unbind_scope_direct_agent(scope_type, scope_id)
            if previous:
                self._send_chat_reply(message, f'已退出 agent 直连模式：{previous}')
            else:
                self._send_chat_reply(message, '当前会话本来就不在 agent 直连模式。')
            return
        if arg.lower() in {'list', 'ls'}:
            self._send_chat_reply(message, self._format_scope_agents_text(scope_type, scope_id))
            return
        record, error_text = self._resolve_into_target_agent(scope_type, scope_id, arg)
        if error_text:
            self._send_chat_reply(message, error_text)
            return
        agent_id = str((record or {}).get('agent_id') or '').strip()
        if not agent_id:
            self._send_chat_reply(message, '目标 agent 记录不完整，无法进入直连。')
            return
        self._bind_scope_direct_agent(scope_type, scope_id, agent_id)
        restart_result = self._ensure_agent_loop_running(agent_id, record)
        suffix = ''
        if not restart_result.get('ok'):
            suffix = f"\n注意：agent 执行循环当前未成功拉起：{restart_result.get('error') or 'unknown error'}"
        self._send_chat_reply(
            message,
            f'已进入 agent 直连模式：{agent_id}\n后续普通消息会直接发给该 agent；发送 `#into off` 退出。{suffix}',
        )

    async def _route_into_agent_message(self, message: ChatMessage, cleaned: str) -> bool:
        scope_type = message.chat_type
        scope_id = str(message.chat_id)
        scope_key = self._scope_key(scope_type, scope_id)
        agent_id = str(self._scope_direct_agents.get(scope_key) or '').strip()
        if not agent_id:
            return False
        ok = self.agent_manager.send_to_agent(
            agent_id,
            {'role': 'user', 'content': cleaned},
        )
        if not ok:
            self._unbind_scope_direct_agent(scope_type, scope_id)
            self._send_chat_reply(message, f'当前直连 agent {agent_id} 不可用，已自动退出 #into 模式。')
            return True
        restart_result = self._ensure_agent_loop_running(agent_id)
        if not restart_result.get('ok'):
            self._unbind_scope_direct_agent(scope_type, scope_id)
            self._send_chat_reply(
                message,
                f'agent {agent_id} 消息已入队，但执行循环拉起失败，已退出 #into 模式：{restart_result.get("error") or "unknown error"}',
            )
            return True
        return True

    def _deliver_direct_agent_reports_to_scope(
        self,
        scope_type: str,
        scope_id: str,
        agent_id: str,
        items: list[dict],
    ) -> None:
        if not items:
            return
        try:
            chat_id = 0 if scope_type == 'master' else int(scope_id)
        except (TypeError, ValueError):
            warn(f'[AI][into] invalid direct agent scope={scope_type}:{scope_id} agent={agent_id}')
            return
        synthetic = ChatMessage(
            chat_type=scope_type,
            chat_id=chat_id,
            user_id=0,
            text='',
            raw_message='',
            sender={'nickname': f'agent:{agent_id}', 'user_id': 0},
            message_id=None,
            mentions_self=False,
            timestamp=time.time(),
            raw_data={'source': 'direct_agent_message', 'agent_id': agent_id},
        )
        source_label = f'常驻Agent直连({agent_id})'
        for item in items:
            text = str(item.get('text') or '').strip()
            if not text:
                continue
            sent_entries = self._send_scope_message(synthetic, text)
            for entry in sent_entries:
                outbound = self._build_outbound_message_entry(
                    entry.get('text') or '',
                    timestamp=item.get('ts'),
                    message_id=entry.get('message_id'),
                    source_label=source_label,
                    raw_message=entry.get('raw_message'),
                )
                self._append_outbound_message_now(scope_type, scope_id, outbound)

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

    _MENTION_SELF_MARK = '[有人@了你] '

    def _mark_mentions_self(self, message: ChatMessage, text: str) -> str:
        """给被 @ 的群消息补一个显式标记。

        _clean_text 会剥掉 [CQ:at,qq=自己] 供斜杠命令解析，而模型只看得到条目的
        text 字段（不看 raw_message），结果被 @ 这件事对模型完全不可见，它无法
        分辨该不该接话。这里把它显式写回可见文本。
        """
        text = str(text or '')
        if message.chat_type != 'group' or not message.mentions_self:
            return text
        if text.startswith(self._MENTION_SELF_MARK):
            return text
        return f'{self._MENTION_SELF_MARK}{text}'

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
        # 内部来源：tasker 汇报。source 值保留 legacy 名称以兼容历史记录。
        if message.user_id == 0 and message.raw_data.get('source') == 'dev_agent_task_report':
            return 'internal_task'
        # 内部来源：新版常驻 agent 挂起内容上报，同样按内部任务处理正常唤醒 AI
        if message.user_id == 0 and message.raw_data.get('source') == 'agent_message':
            return 'internal_task'
        # 内部来源：QQ 好友/群申请事件通知，按内部任务语义正常唤醒主 AI
        if message.user_id == 0 and message.raw_data.get('source') == 'qq_request_event':
            return 'internal_task'
        if message.raw_data.get('source') == 'self_other_device':
            return 'self_other_device'
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
            'self_other_device': '本人-其他设备',
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
        message: ChatMessage = item['message']
        if not self._is_message_allowed_by_power_mode(message):
            return
        run_epoch = self._resolve_message_epoch(item.get('message_epoch'))
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
        trigger_messages = self._dedupe_trigger_message_entries(
            list(item.get('trigger_messages') or [self._build_trigger_message_entry(message, cleaned)])
        )
        _diary_ctx = await asyncio.to_thread(self.repo.get_diary_context, scope_type, scope_id)
        _diary_summaries = _diary_ctx['summaries']
        history = self._flatten_diary_context(_diary_ctx)
        history_seed = item.get('history_seed')
        if history_seed:
            history_before_trigger = [dict(entry) for entry in history_seed]
        else:
            history_before_trigger = self._strip_trigger_entries_from_history(history, trigger_messages)
        tool_logs = None  # 独立日志仅用于审计；工具上下文从聊天条目的 tool_context_messages 恢复
        combined_trigger_text = '\n'.join(
            str(entry.get('text') or '').strip()
            for entry in trigger_messages
            if str(entry.get('text') or '').strip()
        )
        image_refs: list[str] = []
        seen_image_refs: set[str] = set()
        history_before_trigger, trigger_messages = self._prepare_visible_message_refs(
            scope_type,
            scope_id,
            history_before_trigger,
            trigger_messages,
        )
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
        live_send_action_ledger = self._get_scope_send_ledger(scope_type, scope_id)
        tool_round = 0  # 工具回合计数：>0 表示本轮为工具 checkpoint 后的续跑（混合场景）
        while True:
            _inject_persona = (tool_round == 0) and not any(
                self._is_internal_tool_report_item(entry) for entry in trigger_messages
            )
            _session_mode = self._get_scope_session_mode(scope_type, scope_id)
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
                inject_persona=_inject_persona,
                chat_mode=(_session_mode == 'chat'),
                # 只在首个回合提示；工具续跑轮再提会打断模型手上的活。
                mode_hint=self._consume_code_mode_switch_hint(scope_type, scope_id) if tool_round == 0 else '',
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
                    'combined_trigger_chars': sum(
                        len(str(entry.get('text') or '').strip()) for entry in trigger_messages
                    ),
                    'has_agent_message': any(
                        self._is_internal_tool_report_item(entry) for entry in trigger_messages
                    ),
                    'resumed_from_tool_turn': tool_round > 0,
                },
                live_message=message,
                live_send_action_ledger=live_send_action_ledger,
            )
            if not used_tools:
                break
            pending = None
            post_send_pending = (reply_bundle or {}).get('post_send_pending')
            if isinstance(post_send_pending, dict):
                pending = dict(post_send_pending)
            else:
                pending = self._drain_live_tool_scope_turn(scope_key)
            if not pending:
                break

            # 消息循环重跑：将上一轮已处理的消息（含 AI 回复）移动到背景历史中，
            # 并将新到的消息作为下一轮的 trigger_messages。
            # 这样模型能看到自己刚刚发了什么，不会因为上下文断层而重复回复。
            
            # 1. 收集本轮产生的所有 outbound (已发出的 AI 消息)
            iteration_outbound = [
                dict(entry) for entry in ((reply_bundle or {}).get('live_outbound_entries') or [])
            ]
            iteration_checkpoint = (reply_bundle or {}).get('live_tool_checkpoint_entry')
            
            # 2. 更新 history_before_trigger：包含上一轮的 triggers 和 AI replies
            history_before_trigger.extend(trigger_messages)
            if isinstance(iteration_checkpoint, dict):
                history_before_trigger.append(dict(iteration_checkpoint))
            # 去除内部状态标记，避免泄漏到历史实体中
            clean_outbound = []
            for entry in iteration_outbound:
                clean_entry = dict(entry)
                clean_entry.pop('_history_committed', None)
                clean_entry.pop('tool_checkpoint_id', None)
                clean_outbound.append(clean_entry)
                
            history_before_trigger.extend(clean_outbound)

            # 3. 设置新的 trigger_messages
            message = pending['message']
            cleaned = pending['cleaned'] or message.text
            source_label = self._message_source_label(message)
            trigger_messages = self._dedupe_trigger_message_entries(
                list(pending.get('trigger_messages') or [])
            )
            
            # 4. 更新 item 状态
            item['message'] = message
            item['cleaned'] = cleaned
            item['agent_id'] = pending.get('agent_id') or item['agent_id']
            item['deferred_count'] = int(item.get('deferred_count') or 0) + max(
                1,
                int(pending.get('deferred_count') or 0),
            )
            
            # 5. 重新加载基础背景（主要是为了刷新摘要和窗口，但保持 history_before_trigger 的增量逻辑）
            _diary_ctx = await asyncio.to_thread(self.repo.get_diary_context, scope_type, scope_id)
            _diary_summaries = _diary_ctx['summaries']
            history_before_trigger, trigger_messages = self._prepare_visible_message_refs(
                scope_type,
                scope_id,
                history_before_trigger,
                trigger_messages,
            )
            
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
            tool_round += 1
        item['turn_result'] = dict(reply_bundle or {})
        reply = str((reply_bundle or {}).get('message') or '')
        think_note = str((reply_bundle or {}).get('think_note') or '')
        live_tool_context_checkpointed = bool(
            (reply_bundle or {}).get('live_tool_context_checkpointed')
        )
        live_tool_checkpoint_entry = (
            dict((reply_bundle or {}).get('live_tool_checkpoint_entry') or {})
            if isinstance((reply_bundle or {}).get('live_tool_checkpoint_entry'), dict)
            else None
        )
        tool_context_messages = None if live_tool_context_checkpointed else (
            (reply_bundle or {}).get('tool_context_messages')
        )
        live_outbound_entries = [
            dict(entry) for entry in ((reply_bundle or {}).get('live_outbound_entries') or [])
            if isinstance(entry, dict)
        ]
        checkpoint_history_entries: list[dict] = []
        if live_tool_checkpoint_entry:
            checkpoint_history_entries.append(dict(live_tool_checkpoint_entry))
        if not reply:
            if tool_context_messages and not self._is_epoch_stale(run_epoch):
                # 本轮虽然没有对外文本，但工具协议仍需成为会话历史的一部分，
                # 以便下一轮能连续看到调用与结果；该空文本条目不会被当作发言展示。
                outbound_entry = await self._record_outbound_message(
                    message.chat_type,
                    str(message.chat_id),
                    '',
                    generation_ms=generation_ms,
                    think_note=think_note,
                    tool_context_messages=tool_context_messages,
                )
                checkpoint_history_entries = [dict(outbound_entry)]
            if checkpoint_history_entries:
                item['followup_history_seed'] = [
                    *[dict(entry) for entry in history_before_trigger],
                    *[dict(entry) for entry in trigger_messages],
                    *[dict(entry) for entry in checkpoint_history_entries],
                ]
                item['turn_commit_evidence'] = {
                    'outbound_history_committed': True,
                    'turn_log_committed': bool((item.get('turn_result') or {}).get('turn_log_committed')),
                    'turn_metadata_committed': (item.get('turn_result') or {}).get('turn_metadata') is not None,
                }
                item['completed_turn_metadata'] = copy.deepcopy(
                    (item.get('turn_result') or {}).get('turn_metadata') or {}
                )
            return
        if self._is_epoch_stale(run_epoch):
            return
        reply = self._finalize_reply(message, reply)
        if not reply:
            return
        if self._is_epoch_stale(run_epoch):
            return
        outbound_history_entries: list[dict] = []
        missing_live_history_entries: list[dict] = []
        for entry in live_outbound_entries:
            copied = dict(entry)
            committed = bool(copied.pop('_history_committed', True))
            if committed:
                outbound_history_entries.append(copied)
            else:
                missing_live_history_entries.append(copied)
        for entry in missing_live_history_entries:
            try:
                await asyncio.to_thread(
                    self._append_outbound_message_now,
                    message.chat_type,
                    str(message.chat_id),
                    dict(entry),
                )
                outbound_history_entries.append(dict(entry))
            except Exception as exc:
                warn(
                    f'[AI][persist] live outbound recovery failed '
                    f'scope={message.chat_type}:{message.chat_id} '
                    f'message_id={entry.get("message_id")} error={exc}'
                )
        normalized_tool_context = (
            self._normalize_tool_context_messages(tool_context_messages)
            if tool_context_messages else None
        )
        if outbound_history_entries and normalized_tool_context:
            outbound_history_entries[0]['tool_context_messages'] = copy.deepcopy(normalized_tool_context)
            attach_tool_context = getattr(self.repo, 'attach_tool_context_to_message', None)
            if callable(attach_tool_context):
                attached = await asyncio.to_thread(
                    attach_tool_context,
                    message.chat_type,
                    str(message.chat_id),
                    outbound_history_entries[0].get('message_id'),
                    outbound_history_entries[0].get('message_ref'),
                    copy.deepcopy(normalized_tool_context),
                )
                if not attached:
                    warn(
                        f'[AI][persist] live tool context attach missed '
                        f'scope={message.chat_type}:{message.chat_id} '
                        f'message_id={outbound_history_entries[0].get("message_id")}'
                    )
        if not outbound_history_entries:
            outbound_entry = await self._record_outbound_message(
                message.chat_type,
                str(message.chat_id),
                reply,
                generation_ms=generation_ms,
                think_note=think_note,
                tool_context_messages=normalized_tool_context,
            )
            outbound_history_entries = [dict(outbound_entry)]
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
            *[dict(entry) for entry in checkpoint_history_entries],
            *[dict(entry) for entry in outbound_history_entries],
        ]
        item['turn_commit_evidence'] = {
            'outbound_history_committed': True,
            'turn_log_committed': bool((item.get('turn_result') or {}).get('turn_log_committed')),
            'turn_metadata_committed': (item.get('turn_result') or {}).get('turn_metadata') is not None,
        }
        item['completed_turn_metadata'] = copy.deepcopy(
            (item.get('turn_result') or {}).get('turn_metadata') or {}
        )
    CHILD_RULES_PROMPT = '\n'.join(
        [
            '规则提醒:',
            '1. 如果用户要你联系别人、转达消息、查别处情况，应调用 notify_master 工具联系主AI协调对应会话的子AI。',
            '2. 如果用户要你定闹钟或提醒，应调用 create_task 工具创建 set_alarm 任务。',
            '3. 如果用户要求更新程序、检查版本、重启系统等系统级操作，必须调用 notify_master 转交主AI处理（request_type: system_operation），不要用 tasker 手动操作。',
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
            '25. 群聊多人聊天时，先判断每条消息的对话对象再决定要不要接：看是谁发的、在回谁（@了谁、引用了哪条短ID、上一句接的是谁），不要看到有人说话就当成在叫自己、问自己，也不要替别人抢答。只回应明确指向你、或你确实接得上且自然的对话。',
            '请判断是否该回复；如果回复，尽量像自然网友。',
        ]
    )

    def _identity_prompt_block(self) -> str:
        """运行时身份基线：自己的 QQ 与号主 QQ，防止把号主 QQ 当成自己账号。

        prompt 文件里只硬编码了号主 QQ，没有 bot 自身 QQ。AI 被问"你的QQ号"、
        "这个账号"时，唯一见过的 QQ 数字就是号主号，容易把号主 QQ 当成自己。
        这里用运行时真实值动态注入，避免在 prompt 文件里再硬编码一份 QQ。
        """
        bot_qq = str(getattr(self.bot, 'self_id', 0) or 0).strip()
        if bot_qq in ('', '0'):
            bot_qq = str(getattr(self.config, 'self_id', 0) or 0).strip()
        if bot_qq in ('', '0'):
            return ''
        lines = [f'- 你的 QQ 号（机器人自身账号）是 {bot_qq}，被问到"你的QQ号/这个账号"时以此为准。']
        master_qq = str(getattr(self.config, 'master_qq', 0) or 0).strip()
        if master_qq in ('', '0'):
            master_qq = str(getattr(self.config, 'admin_qq', 0) or 0).strip()
        if master_qq not in ('', '0'):
            lines.append(f'- 号主（主人）QQ 是 {master_qq}，是拥有你这个账号的人，与机器人（{bot_qq}）是两个不同账号，不要混淆。')
        return '\n【身份基线】\n' + '\n'.join(lines)

    def _system_prompt(self) -> str:
        return self.prompt_store.staff_system_prompt() + self._identity_prompt_block()

    def _static_system_blocks(self, base_prompt: str, persona: str | None = None, persona_position: str = 'inline', chat_mode: bool = False) -> list[dict]:
        """组装静态 system 块。

        persona_position:
        - 'inline'（默认，旧行为）：人设、CHILD_RULES、发言方式紧随 base_prompt。
        - 'last'：人设块单独成一个 system 块返回（列表最后一项），调用方要把动态背景块
          插到它前面。人设排在背景之后才真的是"最后"，否则会被大段背景信息稀释掉
          表达约束——这也是之前 AI 不走人设说话的主因。

        chat_mode: True 时过滤掉 CHILD_RULES 中不适用于纯聊天的规则，并注入聊天模式职责与风格块。"""
        parts = [base_prompt]
        persona_tail: list[str] | None = None
        if persona is not None:
            rules = self.CHILD_RULES_PROMPT
            if chat_mode:
                skip_prefixes = ('2. ', '23. ', '24. ')
                rules = '\n'.join(
                    line for line in self.CHILD_RULES_PROMPT.split('\n')
                    if not line.strip().startswith(skip_prefixes)
                )
            # 规则留在可缓存的首块；人设与风格约束单独成块，便于挪到背景之后。
            parts.extend(['', rules])
            persona_block = [
                '',
                'AI人设与对话要求（说话语气、用词、节奏必须贴合此人设；前面的工作指令只是行为逻辑，不改变说话风格）:',
                persona or default_char_prompt(),
                '',
                self._chat_focus_block() if chat_mode else '',
                self._chat_style_block() if chat_mode else '',
                '',
                '发言方式：【关键】要发消息给用户，必须调用 send_message 工具，'
                '你直接输出的普通文字不会被发送。'
                '如果需要思考分析，用 <thinking>...</thinking> 包裹写在 send_message 的 content 里，'
                '这部分会自动过滤掉不会发给用户，用户只看到思考标签外的正常内容。'
                '如果觉得现在不该说话，调用 stay_silent 工具结束本回合——'
                '不要把想说的话写成普通文字或塞进思考区来“假装沉默”，'
                '真要说就 send_message，真不想说就 stay_silent，二者选其一。',
            ]
            if persona_position == 'last':
                persona_tail = persona_block
            else:
                parts.extend(persona_block)
        system_blocks = [
            {
                'type': 'text',
                'text': '\n'.join(parts),
            }
        ]
        self._stamp_cache_control_on_text_block(system_blocks[0])
        if persona_tail:
            system_blocks.append({'type': 'text', 'text': '\n'.join(persona_tail)})
        return system_blocks

    @staticmethod
    def _prompt_file_block(filename: str) -> str:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'prompt', filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return ''

    @classmethod
    def _chat_style_block(cls) -> str:
        return cls._prompt_file_block('chat_style.txt')

    @classmethod
    def _chat_focus_block(cls) -> str:
        return cls._prompt_file_block('chat_focus.txt')

    @staticmethod
    def _stamp_cache_control_on_text_block(block: dict) -> None:
        if not isinstance(block, dict):
            return
        if block.get('type') != 'text':
            return
        if not str(block.get('text') or '').strip():
            return
        block['cache_control'] = {'type': 'ephemeral'}

    @staticmethod
    def _stamp_cache_control_on_message(message: dict) -> None:
        """Mark the last text block in a message as an Anthropic prompt cache breakpoint."""
        if not isinstance(message, dict):
            return
        content = message.get('content')
        if isinstance(content, str):
            text = str(content or '')
            if not text.strip():
                return
            message['content'] = [
                {
                    'type': 'text',
                    'text': text,
                    'cache_control': {'type': 'ephemeral'},
                }
            ]
            return
        if not isinstance(content, list):
            return
        for index in range(len(content) - 1, -1, -1):
            block = content[index]
            if isinstance(block, str):
                text = str(block or '')
                if not text.strip():
                    continue
                content[index] = {
                    'type': 'text',
                    'text': text,
                    'cache_control': {'type': 'ephemeral'},
                }
                return
            if not isinstance(block, dict):
                continue
            if block.get('type') != 'text':
                continue
            if not str(block.get('text') or '').strip():
                continue
            block['cache_control'] = {'type': 'ephemeral'}
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
        inject_persona: bool = True,
        chat_mode: bool = False,
        mode_hint: str = '',
    ) -> dict:
        # inject_persona=False 时（含工具/agent 等混合触发），不注入人设性格块，
        # 避免人设腔干扰工具处理语境；人设移到 system 最后也是只在纯用户消息时才有。
        system_blocks = self._static_system_blocks(
            self._system_prompt(),
            persona if inject_persona else None,
            persona_position='last',
            chat_mode=chat_mode,
        )
        # 人设块要压在背景块之后，否则大段背景会把表达约束冲淡。
        persona_tail_block = system_blocks.pop() if len(system_blocks) > 1 else None
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
        if persona_tail_block is not None:
            system_blocks.append(persona_tail_block)
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
        trigger_content = self._build_trigger_user_message(trigger_messages)
        if mode_hint:
            # 跟在触发消息之后，不进 system 块，免得污染缓存前缀。
            trigger_content = f'{trigger_content}\n\n{mode_hint}'
        messages.append({'role': 'user', 'content': trigger_content})
        return {'system': system_blocks, 'messages': messages, 'inject_persona': inject_persona}

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

    def _default_knowledge_lines(self) -> list[str]:
        return [f"- {item.get('content')}" for item in self.repo.get_knowledge_base() if str(item.get('content') or '').strip()]

    def _build_mounted_knowledge_prompt_lines(self, scope_type: str, scope_id: str) -> list[str]:
        lines: list[str] = []
        for kb_id in self.repo.get_scope_knowledge_mounts(scope_type, scope_id):
            if kb_id == getattr(self.repo, 'DEFAULT_KNOWLEDGE_BASE_ID', ''):
                continue
            info = self.repo.get_knowledge_base_info(kb_id)
            if not info:
                continue
            entries = self.repo.get_knowledge_entries(kb_id)
            lines.append(f"【{info.get('name') or kb_id} | {kb_id}】{info.get('description') or '无描述'}")
            if entries:
                lines.extend(
                    f"- ({entry.get('entry_id')}) {str(entry.get('content') or '').strip()}"
                    for entry in entries
                    if str(entry.get('content') or '').strip()
                )
            else:
                lines.append('- （空）')
        return lines

    def _build_knowledge_admin_summary(self) -> str:
        bases = self.repo.list_knowledge_bases()
        if not bases:
            return '暂无知识库。'
        lines = ['知识库概览:']
        for kb in bases:
            lines.append(
                f"- {kb.get('name')} ({kb.get('kb_id')}) | 条目{kb.get('entry_count')} | {kb.get('description') or '无描述'}"
            )
        mounted_lines = ['挂载情况:']
        for agent in self.repo.list_agents():
            mounts = [str(item) for item in list((agent or {}).get('knowledge_mounts') or []) if str(item or '').strip()]
            if not mounts:
                continue
            mounted_lines.append(
                f"- {agent.get('scope_type')}:{agent.get('scope_id')} -> {', '.join(mounts)}"
            )
        if len(mounted_lines) == 1:
            mounted_lines.append('- 暂无挂载')
        return '\n'.join(lines + [''] + mounted_lines)

    def _build_child_background_prompt(
        self,
        message: ChatMessage,
        impression: str,
        history: list[dict],
        tool_logs: list[dict] | None,
        image_context: str | None,
        global_identity_context: str,
        group_context: str = '',
        deferred_count: int = 0,
        display_name: str = '',
        diary_summaries: list[dict] | None = None,
    ) -> str:
        # 工具协议上下文已经随 assistant 聊天条目的 tool_context_messages
        # 按真实时序进入 history；独立 tool_logs 仅保留审计用途，不再注入提示词。
        recent_think_lines = self._collect_recent_think_notes(history)
        knowledge_lines = self._default_knowledge_lines()
        mounted_knowledge_lines = self._build_mounted_knowledge_prompt_lines(message.chat_type, str(message.chat_id))
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
        bot_qq = str(getattr(self.bot, 'self_id', 0) or 0).strip()
        if bot_qq not in ('', '0'):
            parts.append(f"你的QQ号（机器人自身账号）: {bot_qq}，不要把自己的账号当成号主账号。")
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
        ]
        if mounted_knowledge_lines:
            parts += [
                '',
                '已挂载知识库（这些是当前会话额外可引用的上下文，不等于号主本人事实）:',
                '\n'.join(mounted_knowledge_lines),
            ]
        parts += [
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
        parts.extend([
            '',
            '消息短ID说明:',
            '上下文里形如 [#A1B2] 的四位字母数字就是消息短ID。'
            '需要引用上下文里的某条具体消息时，优先使用这个短ID：'
            'send_message 可传 reply_to_id，recall_message 可传 message_ref，'
            'view_image 也可传 message_ref 来查看那条历史消息里的图片。',
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
        """判断一条历史/触发条目是否为内部工具回执（tasker 结果 / 常驻 agent 汇报 /
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
        for token in ('</user_msg>', '</tool_report>', '</user_invisible>', '</user_visible>',
                      '<user_msg', '<tool_report', '<user_invisible', '<user_visible'):
            s = s.replace(token, '＜' + token[1:])
        return s

    def _format_tool_result_content(self, tool_name: str, result, *, mixed_batch: bool = False) -> str:
        content = str(result if result is not None else '（工具无返回）')
        if mixed_batch and str(tool_name or '') == 'send_message':
            return f'<user_visible>{content}</user_visible>'
        return content

    def _persona_notice_scope_key(self, scope_type: str, scope_id: str, agent_id: str) -> str:
        return f'{scope_type}:{scope_id}:{agent_id}'

    def _mark_send_message_persona_notice(self, scope_type: str, scope_id: str, agent_id: str) -> None:
        pending = getattr(self, '_pending_send_message_persona_notices', None)
        if not isinstance(pending, dict):
            pending = {}
            self._pending_send_message_persona_notices = pending
        pending[
            self._persona_notice_scope_key(scope_type, scope_id, agent_id)
        ] = True

    def _consume_send_message_persona_notice(self, scope_type: str, scope_id: str, agent_id: str) -> bool:
        pending = getattr(self, '_pending_send_message_persona_notices', None)
        if not isinstance(pending, dict):
            return False
        return bool(
            pending.pop(
                self._persona_notice_scope_key(scope_type, scope_id, agent_id),
                False,
            )
        )

    def _render_transient_persona_notice_messages(self, messages: list[dict]) -> list[dict]:
        notice_suffix = '<notice>请回归人设发言！</notice>'
        target_index = None
        target_block_index = None
        for message_index in range(len(messages) - 1, -1, -1):
            item = messages[message_index]
            blocks = item.get('content') if isinstance(item, dict) else None
            if item.get('role') != 'user' or not isinstance(blocks, list):
                continue
            for block_index in range(len(blocks) - 1, -1, -1):
                block = blocks[block_index]
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    target_index = message_index
                    target_block_index = block_index
                    break
            if target_index is not None:
                break
        if target_index is None or target_block_index is None:
            return messages

        rendered = copy.deepcopy(messages)
        target_block = rendered[target_index]['content'][target_block_index]
        original = str(target_block.get('content') or '')
        if notice_suffix not in original:
            target_block['content'] = f'{original}\n{notice_suffix}' if original else notice_suffix
        return rendered

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

    def _wrap_user_invisible_group(self, items: list[dict], note: str = '') -> str:
        """把一段连续的内部触发条目（agent 返回 / 主AI联系 / 闹钟 / 循环任务等系统注入、
        非用户直接发送的消息）合并进同一个 <user_invisible>，内部每条各自成独立
        <tool_report>。<user_invisible> 用于让 AI 知道这段内容用户看不见，是被系统触发
        而非用户当面发言，避免直接回一句“好的”让用户困惑。"""
        if not items:
            return ''
        inner = '\n'.join(self._wrap_tool_report_block(it, note) for it in items)
        return f'<user_invisible>{inner}</user_invisible>'

    def _render_pending_blocks(self, items: list[dict], internal_note: str = '') -> list[str]:
        """把一段按时序累积的条目渲染成块列表，两类分流：
          - 真实用户消息：连续合并进 <user_msg>
          - 内部触发条目（agent 返回 / 主AI联系 / 闹钟 / 循环任务等）：连续合并进同一个
            <user_invisible>，内部每条各自成独立 <tool_report>
        两类块按原始时序穿插排列。internal_note 非空时附加到内部回执块（触发轮定性提示）。"""
        blocks: list[str] = []
        user_run: list[dict] = []
        invisible_run: list[dict] = []

        def flush_user():
            if user_run:
                blocks.append(self._wrap_user_msg_block(user_run))
                user_run.clear()

        def flush_invisible():
            if invisible_run:
                blocks.append(self._wrap_user_invisible_group(invisible_run, internal_note))
                invisible_run.clear()

        for it in items:
            if self._is_internal_tool_report_item(it):
                flush_user()
                invisible_run.append(it)
            else:
                flush_invisible()
                user_run.append(it)
        flush_user()
        flush_invisible()
        return blocks

    def _render_pending_user_segment(self, pending_items: list[dict]) -> str:
        """把一段（跨到下一个 assistant 之前）累积的条目按时间顺序渲染成一个
        role='user' 的字符串。"""
        return '\n'.join(self._render_pending_blocks(pending_items))

    def _build_role_based_history_messages(self, history: list[dict]) -> list[dict]:
        messages: list[dict] = []
        pending_items: list[dict] = []

        def flush_pending():
            nonlocal pending_items
            if pending_items:
                messages.append({'role': 'user', 'content': self._render_pending_user_segment(pending_items)})
                pending_items = []

        idx = 0
        bot_user_id = str(self.bot.self_id)
        while idx < len(history):
            item = history[idx]
            user_id = str(item.get('user_id') or '').strip()
            text = str(item.get('text') or '').strip()
            if user_id and user_id == bot_user_id:
                # 兼容旧数据：live send_message 曾先写正文、后写空 tool checkpoint。
                # 这里把连续 assistant 段重新整理为：tool_context -> assistant 文本。
                flush_pending()
                assistant_batch: list[dict] = []
                pending_tool_context: list[dict] = []
                while idx < len(history):
                    current = history[idx]
                    current_user_id = str(current.get('user_id') or '').strip()
                    if not current_user_id or current_user_id != bot_user_id:
                        break
                    current_tool_context = self._normalize_tool_context_messages(
                        current.get('tool_context_messages')
                    )
                    current_text = str(current.get('text') or '').strip()
                    # 旧数据里可能因为多次 live checkpoint 写出多个空 assistant 条目。
                    # 这里取“最后一份”工具上下文，等价于把同一逻辑 checkpoint 折叠成单条，
                    # 避免模型重建历史时看到早期版本而不是最终版本。
                    if current_tool_context:
                        pending_tool_context = current_tool_context
                    if current_text:
                        assistant_batch.append({'role': 'assistant', 'content': current_text})
                    idx += 1
                if pending_tool_context:
                    messages.extend(pending_tool_context)
                messages.extend(assistant_batch)
                continue
            if text:
                pending_items.append(item)
            idx += 1
        flush_pending()
        return messages

    def _build_trigger_user_message(self, trigger_messages: list[dict]) -> str:
        items = [item for item in trigger_messages if str(item.get('text') or '').strip()]
        if not items:
            return '暂无新消息'
        # 触发轮的内部回执额外附上定性提示，避免模型把系统内部异步结果当成用户发言外发
        blocks = self._render_pending_blocks(
            items,
            internal_note='这是系统内部异步结果，默认只更新记忆、消化即可，非必要不要调用 send_message 对外发送',
        )
        return '\n'.join(blocks) if blocks else '暂无新消息'

    def _format_history_item_for_user_message(self, item: dict) -> str:
        current_ts = self._coerce_timestamp(item.get('timestamp'))
        time_prefix = self._format_message_clock(current_ts)
        user_id = str(item.get('user_id') or '').strip()
        speaker = item.get('nickname', item.get('user_id'))
        source_label = str(item.get('source_label') or '').strip()
        text = str(item.get('text') or '')
        if source_label:
            prefix = f"[#{self._normalize_message_ref(item.get('message_ref'))}] " if self._normalize_message_ref(item.get('message_ref')) else ''
            return f"{prefix}{time_prefix} [{source_label}] {speaker}({user_id or '未知'}): {text}".strip()
        prefix = f"[#{self._normalize_message_ref(item.get('message_ref'))}] " if self._normalize_message_ref(item.get('message_ref')) else ''
        return f"{prefix}{time_prefix} {speaker}({user_id or '未知'}): {text}".strip()

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
        if name == 'set_thinking_level':
            level = str(tool_input.get('level') or '').strip().lower()
            current = self._get_scope_thinking_level(scope_type, scope_id)
            if not level:
                result = f'当前会话思考等级：{current}。可选值：off / low / medium / high。'
            elif level not in {'off', 'low', 'medium', 'high'}:
                result = '设置失败：level 只能是 off、low、medium、high。'
            else:
                scope_key = self._scope_key(scope_type, scope_id)
                self._scope_thinking_levels[scope_key] = level
                result = f'当前会话思考等级已设为：{level}（仅内存保存，重启恢复 low）。'
        elif name == 'set_session_mode':
            mode = str(tool_input.get('mode') or '').strip().lower()
            target_type = str(tool_input.get('target_scope_type') or '').strip()
            target_id = str(tool_input.get('target_scope_id') or '').strip()
            mode_scope_type, mode_scope_id = scope_type, scope_id
            invalid_target = ''
            if target_type or target_id:
                if scope_type != 'master':
                    invalid_target = '仅主 AI 可调控其他会话的模式；子会话分身只能设置当前会话。'
                elif target_type not in {'group', 'private'} or not target_id:
                    invalid_target = '调控其他会话需要同时提供有效的 target_scope_type（group/private）和 target_scope_id。'
                else:
                    mode_scope_type, mode_scope_id = target_type, target_id
            label = (
                '当前会话'
                if (mode_scope_type, mode_scope_id) == (scope_type, scope_id)
                else f'会话 {mode_scope_type}:{mode_scope_id}'
            )
            if invalid_target:
                result = invalid_target
            elif not mode:
                current = self._get_scope_session_mode(mode_scope_type, mode_scope_id)
                result = (
                    f'{label}模式：{current}。可选值：chat / code。'
                    'chat 为纯聊天模式（只盯群、挖情报、用知识库答问），code 为包含任务派发的完整模式。默认 chat。'
                )
            elif mode not in {'chat', 'code'}:
                result = '设置失败：mode 只能是 chat 或 code。'
            else:
                self._set_scope_session_mode(mode_scope_type, mode_scope_id, mode)
                result = f'{label}模式已设为：{mode}（仅内存保存，重启恢复默认 chat）。'
                if (mode_scope_type, mode_scope_id) == (scope_type, scope_id):
                    result += '工具表已同步刷新，本轮接下来就能直接用新模式的工具，不用等下一条消息。'
        elif name == 'set_trigger_rate':
            agent_scope_type = scope_type
            agent_scope_id = scope_id
            target_type = str(tool_input.get('target_scope_type') or '').strip()
            target_id = str(tool_input.get('target_scope_id') or '').strip()
            if target_type or target_id:
                if scope_type != 'master':
                    result = '仅主 AI 可调控其他会话的触发率；子会话分身只能设置当前会话。'
                    agent_scope_type = None
                elif target_type == 'global':
                    agent_scope_type = 'global'
                elif target_type not in {'group', 'private'} or not target_id:
                    result = '调控其他会话需要同时提供有效的 target_scope_type（group/private）和 target_scope_id。'
                    agent_scope_type = None
                else:
                    agent_scope_type, agent_scope_id = target_type, target_id
            if agent_scope_type is None:
                # target 校验失败：result 已带错误信息，跳过后续 rate 处理
                pass
            else:
                is_global = agent_scope_type == 'global'
                agent = None
                if not is_global:
                    agent = self.repo.get_or_create_agent(
                        agent_scope_type,
                        agent_scope_id,
                        role='master' if agent_scope_type == 'master' else 'child',
                    )
                rate_raw = tool_input.get('rate')
                if rate_raw is None or str(rate_raw).strip() == '':
                    if is_global:
                        current = float(getattr(self.config, 'global_trigger_rate', 0.0))
                        result = f'全局默认随机触发概率：{current:.3f}。可设置范围：0 ~ 0.30。'
                    else:
                        result = f'当前会话随机触发概率：{float(agent.trigger_rate):.3f}。可设置范围：0 ~ 0.30。'
                else:
                    try:
                        rate = float(rate_raw)
                    except (TypeError, ValueError):
                        result = '设置失败：rate 必须是数字，范围 0 ~ 0.30。'
                    else:
                        if not (0.0 <= rate <= 0.30):
                            result = '设置失败：rate 超出范围，只能在 0 ~ 0.30 之间，可设置为 0。'
                        elif is_global:
                            updated_count, persisted = self._apply_global_trigger_rate(rate)
                            result = (
                                f'全局默认随机触发概率已设为：{rate:.3f}。'
                                f'已同步现有会话：{updated_count} 个；新会话默认继承该值。'
                                '私聊默认仍会触发，这个值主要影响群聊随机触发。'
                            )
                            if not persisted:
                                result += '注意：写入 config.yaml 失败，重启后会回到旧值，需要人工检查。'
                        else:
                            updated = self.repo.update_agent_trigger_rate(agent_scope_type, agent_scope_id, rate)
                            result = (
                                f'当前会话随机触发概率已设为：{float(updated.trigger_rate):.3f}。'
                                '私聊默认仍会触发，这个值主要影响群聊随机触发。'
                                '注意：这只改了这一个会话；要让新会话也继承，用 target_scope_type="global" 再设一次。'
                            )
        elif name == 'memory_list':
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
        elif name == 'relation_lookup':
            query = str(tool_input.get('query') or '').strip()
            candidates = self.repo.resolve_user_candidates(query, limit=5) if query else []
            if not candidates:
                result = f'关系网中没有找到匹配 "{query}" 的人物档案。'
            else:
                lines = [f'查询 "{query}" 命中 {len(candidates)} 个档案：']
                for prof in candidates:
                    parts = [f"QQ:{prof.get('user_id')}"]
                    aliases = [a for a in (prof.get('aliases') or []) if a]
                    if aliases:
                        parts.append(f"昵称:{','.join(aliases[:3])}")
                    if prof.get('province'):
                        parts.append(f"省份:{prof.get('province')}")
                    if prof.get('impression'):
                        parts.append(f"印象:{self._short_text(prof.get('impression'), 80)}")
                    if prof.get('affinity'):
                        parts.append(f"好感度:{prof.get('affinity')}")
                    if prof.get('admin_note'):
                        parts.append(f"备注:{prof.get('admin_note')}")
                    attrs = prof.get('attributes') or {}
                    if attrs:
                        kv = '，'.join(f"{k}={ (v or {}).get('value','') }" for k, v in list(attrs.items())[:8])
                        parts.append(f"属性:{kv}")
                    facts = [f.get('content') for f in (prof.get('facts') or []) if f.get('content')]
                    if facts:
                        parts.append(f"事实:{'; '.join(facts[-3:])}")
                    lines.append('- ' + ' | '.join(parts))
                result = '\n'.join(lines)
        elif name == 'relation_list':
            kind = str(tool_input.get('kind') or 'user').strip()
            try:
                limit = int(tool_input.get('limit') or 20)
            except (TypeError, ValueError):
                limit = 20
            limit = max(1, min(limit, 50))
            if kind == 'scope':
                rels = self.repo.list_scope_relations()[:limit]
                if not rels:
                    result = '关系网中暂无会话关系记录。'
                else:
                    lines = [f'会话关系（{len(rels)} 条）：']
                    for rel in rels:
                        parts = [f"{rel['scope_type']}:{rel['scope_id']}"]
                        if rel.get('affinity'):
                            parts.append(f"好感度{rel['affinity']}")
                        if rel.get('relevance'):
                            parts.append(f"关联度{rel['relevance']}")
                        if rel.get('admin_note'):
                            parts.append(f"备注:{rel['admin_note']}")
                        if rel.get('impression'):
                            parts.append(f"印象:{self._short_text(rel['impression'], 50)}")
                        lines.append('- ' + ' | '.join(parts))
                    result = '\n'.join(lines)
            else:
                rels = self.repo.list_user_relations()[:limit]
                if not rels:
                    result = '关系网中暂无人物档案。'
                else:
                    lines = [f'人物档案（{len(rels)} 条）：']
                    for rel in rels:
                        parts = [f"QQ:{rel['user_id']}"]
                        if rel.get('aliases'):
                            parts.append(f"昵称:{','.join(rel['aliases'][:2])}")
                        if rel.get('province'):
                            parts.append(f"省份:{rel['province']}")
                        if rel.get('impression'):
                            parts.append(f"印象:{self._short_text(rel['impression'], 50)}")
                        if rel.get('affinity'):
                            parts.append(f"好感度{rel['affinity']}")
                        if rel.get('admin_note'):
                            parts.append(f"备注:{rel['admin_note']}")
                        lines.append('- ' + ' | '.join(parts))
                    result = '\n'.join(lines)
        elif name in {'relation_update_user', 'relation_add_fact'}:
            if scope_type != 'master':
                result = '关系网写入仅限主AI，请通过 notify_master 上报由主AI归档处理。'
            elif name == 'relation_update_user':
                user_id = str(tool_input.get('user_id') or '').strip()
                if not user_id:
                    result = '写入失败：user_id 不能为空。'
                else:
                    self.repo.update_user_intel(
                        user_id,
                        province=tool_input.get('province'),
                        impression=tool_input.get('impression'),
                        attributes=tool_input.get('attributes') if isinstance(tool_input.get('attributes'), dict) else None,
                        source_scope_type=scope_type,
                        source_scope_id=str(scope_id),
                    )
                    affinity = tool_input.get('affinity')
                    admin_note = tool_input.get('admin_note')
                    if affinity is not None or admin_note is not None:
                        self.repo.update_user_relation(
                            user_id,
                            affinity=float(affinity) if affinity is not None else None,
                            admin_note=str(admin_note) if admin_note is not None else None,
                        )
                    result = f'已更新人物档案 QQ:{user_id}。'
            else:  # relation_add_fact
                user_id = str(tool_input.get('user_id') or '').strip()
                fact = str(tool_input.get('fact') or '').strip()
                if not user_id or not fact:
                    result = '写入失败：user_id 和 fact 都不能为空。'
                else:
                    self.repo.add_user_fact(
                        user_id, fact,
                        source_scope_type=scope_type,
                        source_scope_id=str(scope_id),
                        source_agent=agent_id,
                    )
                    result = f'已为 QQ:{user_id} 追加情报：{self._short_text(fact, 60)}'
        elif name == 'manage_knowledge_base':
            can_manage = scope_type == 'master' or (scope_type == 'private' and str(scope_id) == str(self.config.admin_qq))
            if not can_manage:
                result = '知识库管理工具仅允许主AI或管理员授权会话使用。'
            else:
                action = str(tool_input.get('action') or '').strip()
                kb_id = str(tool_input.get('kb_id') or '').strip()
                if action == 'list_bases':
                    bases = self.repo.list_knowledge_bases()
                    if not bases:
                        result = '暂无知识库。'
                    else:
                        lines = [f'共 {len(bases)} 个知识库：']
                        for kb in bases:
                            lines.append(
                                f"- {kb.get('name')} ({kb.get('kb_id')}) | 条目{kb.get('entry_count')} | {kb.get('description') or '无描述'}"
                            )
                        result = '\n'.join(lines)
                elif action == 'create_base':
                    created = self.repo.create_knowledge_base(str(tool_input.get('name') or ''), str(tool_input.get('description') or ''))
                    result = (
                        f"已创建知识库 {created.get('name')} ({created.get('kb_id')})。"
                        if created else '创建失败：知识库名称不能为空，且不能与现有知识库重名。'
                    )
                elif action == 'update_base':
                    updated = self.repo.update_knowledge_base_info(kb_id, tool_input.get('name'), tool_input.get('description'))
                    result = (
                        f"已更新知识库 {updated.get('name')} ({updated.get('kb_id')})。"
                        if updated else '更新失败：kb_id 不存在，或名称为空/重名。'
                    )
                elif action == 'delete_base':
                    result = f'已删除知识库 {kb_id}。' if self.repo.delete_knowledge_base_info(kb_id) else '删除失败：知识库不存在，或该知识库不允许删除。'
                elif action == 'list_entries':
                    kb_info = self.repo.get_knowledge_base_info(kb_id)
                    entries = self.repo.get_knowledge_entries(kb_id)
                    if not kb_info:
                        result = f'没有找到知识库 {kb_id}。'
                    elif not entries:
                        result = f"知识库 {kb_info.get('name')} ({kb_id}) 为空。"
                    else:
                        lines = [f"知识库 {kb_info.get('name')} ({kb_id}) 共 {len(entries)} 条："]
                        for entry in entries:
                            lines.append(f"- {entry.get('entry_id')}: {entry.get('content')}")
                        result = '\n'.join(lines)
                elif action == 'add_entry':
                    created = self.repo.add_knowledge_entry(str(tool_input.get('content') or ''), kb_id)
                    result = (
                        f"已向知识库 {kb_id} 追加条目 {created.get('entry_id')}: {self._short_text(created.get('content'), 80)}"
                        if created else '追加失败：kb_id 不存在或 content 为空。'
                    )
                elif action == 'update_entry':
                    updated = self.repo.update_knowledge_entry(str(tool_input.get('entry_id') or ''), str(tool_input.get('content') or ''), kb_id)
                    result = (
                        f"已更新知识库 {kb_id} 的条目 {updated.get('entry_id')}。"
                        if updated else '更新失败：kb_id/entry_id 不存在，或 content 为空。'
                    )
                elif action == 'delete_entry':
                    entry_id = str(tool_input.get('entry_id') or '').strip()
                    result = f'已删除知识库 {kb_id} 的条目 {entry_id}。' if self.repo.delete_knowledge_entry(entry_id, kb_id) else '删除失败：kb_id 或 entry_id 不存在。'
                elif action == 'list_mounts':
                    target_scope_type = str(tool_input.get('target_scope_type') or scope_type or '').strip()
                    target_scope_id = str(tool_input.get('target_scope_id') or scope_id or '').strip()
                    mounts = self.repo.get_scope_knowledge_mounts(target_scope_type, target_scope_id)
                    if not mounts:
                        result = f'{target_scope_type}:{target_scope_id} 当前没有挂载知识库。'
                    else:
                        result = f"{target_scope_type}:{target_scope_id} 当前挂载: {', '.join(mounts)}"
                elif action in {'mount_base', 'unmount_base'}:
                    target_scope_type = str(tool_input.get('target_scope_type') or '').strip()
                    target_scope_id = str(tool_input.get('target_scope_id') or '').strip()
                    if not target_scope_type or not target_scope_id or not kb_id:
                        result = '挂载失败：kb_id、target_scope_type、target_scope_id 都不能为空。'
                    else:
                        current = self.repo.get_scope_knowledge_mounts(target_scope_type, target_scope_id)
                        desired = list(current)
                        if action == 'mount_base':
                            if kb_id not in desired:
                                desired.append(kb_id)
                        else:
                            desired = [item for item in desired if item != kb_id]
                        updated_mounts = self.repo.set_scope_knowledge_mounts(target_scope_type, target_scope_id, desired)
                        result = (
                            f"已更新 {target_scope_type}:{target_scope_id} 的知识库挂载: {', '.join(updated_mounts) if updated_mounts else '空'}"
                        )
                else:
                    result = f'不支持的知识库操作: {action}'
        elif name == 'request_knowledge_base_update':
            suggestion = str(tool_input.get('suggestion') or '').strip()
            if not suggestion:
                result = '知识库补充请求不能为空。'
            elif scope_type == 'master':
                result = '当前已经是主AI，请直接使用 manage_knowledge_base 操作知识库。'
            else:
                payload = {
                    'scope_type': scope_type,
                    'scope_id': scope_id,
                    'request_type': 'knowledge_base_suggestion',
                    'suggestion': suggestion,
                    'requester_qq': scope_id if scope_type == 'private' else '',
                    'requester_name': '',
                    'source_message': suggestion,
                    'source_label': f'knowledge_request:{scope_type}:{scope_id}',
                    'message_id': '',
                    'trace_id': f'knowledge:{scope_type}:{scope_id}:{int(time.time() * 1000)}',
                }
                task = self.tools.create_task(agent_id, 'notify_master', payload)
                result = f'已创建知识库补充请求 {task.task_id}，主AI会审批并明确回复是否采纳。'
        elif name == 'web_search':
            query = str(tool_input.get('query') or '').strip()
            result = await self._execute_web_search(query, scope_type, scope_id)
        elif name in {
            'qq_add_friend', 'qq_list_friend_requests', 'qq_approve_friend_request', 'qq_reject_friend_request',
            'qq_join_group', 'qq_list_group_requests', 'qq_approve_group_request', 'qq_reject_group_request',
            'qq_sync_contacts',
        }:
            if scope_type != 'master':
                result = 'error: QQ 好友/群请求管理工具仅允许主AI授权路径调用。'
            else:
                try:
                    if name == 'qq_sync_contacts':
                        friends = await asyncio.to_thread(self.tools.get_friend_list) or []
                        groups = await asyncio.to_thread(self.tools.get_group_list) or []
                        added = 0
                        for friend in friends:
                            uid = str(friend.get('user_id') or '')
                            if not uid:
                                continue
                            nickname = str(friend.get('nickname') or '').strip()
                            if not self.repo.get_user_profile(uid):
                                added += 1
                            self.repo.touch_user_identity(uid, nickname, 'private', uid)
                        result = f'好友/群列表同步完成：好友 {len(friends)} 个，群 {len(groups)} 个，本次新增联系人身份 {added} 个。'
                    elif name == 'qq_add_friend':
                        user_id = int(tool_input.get('user_id') or 0)
                        if user_id <= 0:
                            raise ValueError('user_id 必须是正整数 QQ 号')
                        self.tools.request_friend_add(user_id, str(tool_input.get('comment') or ''))
                        result = '主动好友申请已提交。'
                    elif name == 'qq_list_friend_requests':
                        count = max(1, min(100, int(tool_input.get('count') or 50)))
                        result = json.dumps(self.tools.get_friend_requests(count), ensure_ascii=False, indent=2)
                    elif name in {'qq_approve_friend_request', 'qq_reject_friend_request'}:
                        flag = str(tool_input.get('flag') or '').strip()
                        if not flag:
                            raise ValueError('必须提供申请事件中的 flag；不能仅凭 QQ 号审批')
                        approve = name == 'qq_approve_friend_request'
                        self.tools.set_friend_add_request(flag, approve, str(tool_input.get('remark') or ''))
                        result = '好友申请已同意。' if approve else '好友申请已拒绝。'
                    elif name == 'qq_join_group':
                        group_id = int(tool_input.get('group_id') or 0)
                        if group_id <= 0:
                            raise ValueError('group_id 必须是正整数群号')
                        self.tools.request_group_join(group_id, str(tool_input.get('comment') or ''))
                        result = '加群申请已提交。'
                    elif name == 'qq_list_group_requests':
                        count = max(1, min(100, int(tool_input.get('count') or 50)))
                        result = json.dumps(self.tools.get_group_requests(count), ensure_ascii=False, indent=2)
                    else:
                        flag = str(tool_input.get('flag') or '').strip()
                        sub_type = str(tool_input.get('sub_type') or '').strip()
                        if not flag:
                            raise ValueError('必须提供请求事件中的 flag/request_id')
                        if sub_type not in {'add', 'invite'}:
                            raise ValueError('sub_type 必须是 add 或 invite，并应来自请求事件上下文')
                        approve = name == 'qq_approve_group_request'
                        self.tools.set_group_add_request(flag, sub_type, approve, str(tool_input.get('reason') or ''))
                        result = '群请求/邀请已同意。' if approve else '群请求/邀请已拒绝。'
                except Exception as exc:
                    result = f'error: {name} 未执行成功: {exc}'
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
            kind_filter = self._normalize_task_kind(tool_input.get('kind')) or None
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
                    kind = self._task_kind_label(task.get('kind', '?'))
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
                    f"kind: {self._task_kind_label(task.get('kind'))}",
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
                        proj_root = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                        dest_dir = proj_root / 'data' / 'file' / str(scope_id)
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        # 只用 basename 再清洗非法字符，防路径穿越；限制长度防超长文件名。
                        safe_name = os.path.basename(file_name)
                        safe_name = re.sub(r'[\\/:*?"<>|]', '_', safe_name).strip().rstrip('.')
                        if not safe_name:
                            safe_name = 'file'
                        if len(safe_name) > 120:
                            stem, ext = os.path.splitext(safe_name)
                            safe_name = stem[: 120 - len(ext)] + ext
                        dest_path = dest_dir / safe_name
                        src_path = file_info.get('file') or ''
                        try:
                            if src_path and pathlib.Path(src_path).exists():
                                shutil.copy2(src_path, dest_path)
                            elif file_info.get('url'):
                                await asyncio.to_thread(self.bot.download_file_to, file_info['url'], str(dest_path))
                            else:
                                result = '无法获取文件内容：既无本地路径也无下载 URL。'
                                dest_path = None
                            # 下载完成后二次校验实际大小：NapCat 的 size 字段可能缺失/为 0，
                            # 仅靠下载前检查不可靠，超限必须清掉已落盘文件（落盘即清）。
                            if dest_path is not None and dest_path.exists():
                                actual_size = dest_path.stat().st_size
                                if actual_size > MAX_FILE_SIZE:
                                    dest_path.unlink(missing_ok=True)
                                    result = f'文件实际大小（{actual_size // 1024 // 1024}MB）超过 20MB 限制，已删除并跳过。'
                                    dest_path = None
                        except Exception as e:
                            # 落盘即清：保存/下载失败时清理半成品，避免残留垃圾文件。
                            if dest_path is not None:
                                try:
                                    dest_path.unlink(missing_ok=True)
                                except OSError:
                                    pass
                            result = f'文件保存失败: {e}'
                            dest_path = None
                        if dest_path is not None:
                            rel_path = str(dest_path).replace('\\', '/')
                            saved_size = dest_path.stat().st_size
                            if saved_size >= 1024 * 1024:
                                size_desc = f'{saved_size / 1024 / 1024:.1f}MB'
                            elif saved_size >= 1024:
                                size_desc = f'{saved_size // 1024}KB'
                            else:
                                size_desc = f'{saved_size}B'
                            result = f'文件已保存：{rel_path}（大小 {size_desc}，可交给 tasker 或常驻 agent 读取分析）'
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
                result = '\n'.join(lines) + '\n（方括号内的前 8 位 ID 可用于 update/delete 的唯一前缀）'
        elif name == 'update_recurring_task':
            status, detail = self._resolve_recurring_task_key(tool_input.get('task_id'))
            if status != 'ok':
                result = detail
            else:
                task = self._recurring_tasks[detail]
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
                    result = f'任务 {task["id"][:8]} 已更新，下次触发: {next_str}'
                except Exception as e:
                    result = f'更新失败: {e}'
        elif name == 'delete_recurring_task':
            status, detail = self._resolve_recurring_task_key(tool_input.get('task_id'))
            if status != 'ok':
                result = detail
            else:
                task_id = detail
                del self._recurring_tasks[task_id]
                self._save_recurring_tasks()
                result = f'任务 {task_id[:8]} 已删除'
        elif name == 'create_agent':
            instruction = str(tool_input.get('instruction') or '').strip()
            cwd = tool_input.get('cwd', '/')
            read_only = bool(tool_input.get('read_only', False))
            if not instruction:
                result = 'error: instruction 为空，未创建 agent。'
            else:
                # 记录创建该 agent 的会话 scope，供后续按 scope 投递上报（投递逻辑下个任务接）。
                origin_scope = f'{scope_type}:{scope_id}' if scope_type and str(scope_id) != '' else None
                try:
                    new_agent_id = self.agent_manager.create_agent(
                        instruction,
                        origin_scope=origin_scope,
                        cwd=cwd,
                        read_only=read_only,
                    )
                    # 启动常驻循环：使用 roles.agent 独立模型配置
                    role_model_config = self.model_manager.get_role_model('agent')
                    if role_model_config:
                        agent_model = AnthropicChatModel(
                            base_url=role_model_config['base_url'],
                            api_key=role_model_config['api_key'],
                            model_name=role_model_config['model_name'],
                            messages_path=role_model_config['messages_path'],
                        )
                        initial_binding = {
                            'channel': role_model_config.get('channel_name', ''),
                            'upstream': role_model_config.get('upstream_name', ''),
                            'model_id': role_model_config.get('model_name', ''),
                        }
                        self.agent_manager.switch_agent_model_binding(new_agent_id, initial_binding, agent_model)
                    else:
                        agent_model = self.model
                        self.agent_manager.register_agent_client(new_agent_id, agent_model)
                    agent_task = self.loop.create_task(
                        self.agent_manager.run_agent_loop(
                            new_agent_id,
                            agent_model,
                            self._get_github_api_token(),
                            prompt_path=self.config.agent_prompt_path,
                            ssh_profiles=self._get_ssh_profiles_map(),
                            on_agent_message=self.agent_manager.on_agent_message,
                        )
                    )
                    self.agent_manager.register_agent_task(new_agent_id, agent_task)
                    mode_text = '只读' if read_only else '可写'
                    watch_note = self._ensure_agent_watch_timer(scope_type, scope_id)
                    result = f'已创建常驻 agent，agent_id: {new_agent_id}，默认目录: {cwd or "/"}，模式: {mode_text}，已开始执行任务。'
                    if watch_note:
                        result = f'{result}\n{watch_note}'
                except ValueError as exc:
                    result = f'创建 agent 失败: {exc}'
                except Exception as exc:
                    result = f'创建 agent 失败: {exc}'
        elif name == 'list_ssh_profiles':
            result = self._format_ssh_profiles_list()
        elif name == 'create_ssh_agent':
            instruction = str(tool_input.get('instruction') or '').strip()
            ssh_profile_id = str(tool_input.get('ssh_profile_id') or '').strip()
            cwd = tool_input.get('cwd', '/')
            read_only = bool(tool_input.get('read_only', False))
            if not instruction:
                result = 'error: instruction 为空，未创建 ssh agent。'
            elif not ssh_profile_id:
                result = 'error: ssh_profile_id 为空，未创建 ssh agent。'
            else:
                profiles = self._get_ssh_profiles_map()
                profile = profiles.get(ssh_profile_id)
                if profile is None:
                    result = f'创建 ssh agent 失败: 找不到 SSH profile {ssh_profile_id}。'
                else:
                    origin_scope = f'{scope_type}:{scope_id}' if scope_type and str(scope_id) != '' else None
                    try:
                        new_agent_id = self.agent_manager.create_agent(
                            instruction,
                            origin_scope=origin_scope,
                            cwd=cwd,
                            read_only=read_only,
                            target_kind='ssh',
                            ssh_profile_id=ssh_profile_id,
                        )
                        role_model_config = self.model_manager.get_role_model('agent')
                        if role_model_config:
                            agent_model = AnthropicChatModel(
                                base_url=role_model_config['base_url'],
                                api_key=role_model_config['api_key'],
                                model_name=role_model_config['model_name'],
                                messages_path=role_model_config['messages_path'],
                            )
                            initial_binding = {
                                'channel': role_model_config.get('channel_name', ''),
                                'upstream': role_model_config.get('upstream_name', ''),
                                'model_id': role_model_config.get('model_name', ''),
                            }
                            self.agent_manager.switch_agent_model_binding(new_agent_id, initial_binding, agent_model)
                        else:
                            agent_model = self.model
                            self.agent_manager.register_agent_client(new_agent_id, agent_model)
                        agent_task = self.loop.create_task(
                            self.agent_manager.run_agent_loop(
                                new_agent_id,
                                agent_model,
                                self._get_github_api_token(),
                                prompt_path=self.config.agent_prompt_path,
                                ssh_profiles=profiles,
                                on_agent_message=self.agent_manager.on_agent_message,
                            )
                        )
                        self.agent_manager.register_agent_task(new_agent_id, agent_task)
                        mode_text = '只读' if read_only else '可写'
                        watch_note = self._ensure_agent_watch_timer(scope_type, scope_id)
                        result = (
                            f'已创建 SSH 常驻 agent，agent_id: {new_agent_id}，'
                            f'profile: {profile.profile_id}，target: {profile.target}，'
                            f'默认目录: {cwd or "/"}，模式: {mode_text}，已开始执行任务。'
                        )
                        if watch_note:
                            result = f'{result}\n{watch_note}'
                    except ValueError as exc:
                        result = f'创建 ssh agent 失败: {exc}'
                    except Exception as exc:
                        result = f'创建 ssh agent 失败: {exc}'
        elif name == 'send_to_agent':
            target_agent_id = str(tool_input.get('agent_id') or '').strip()
            message = str(tool_input.get('message') or '').strip()
            cwd = tool_input.get('cwd') if 'cwd' in tool_input else None
            read_only = bool(tool_input.get('read_only')) if 'read_only' in tool_input else None
            if not target_agent_id:
                result = 'error: agent_id 为空，未发送。'
            elif not message:
                result = 'error: message 为空，未发送。'
            else:
                before = self.agent_manager.get_agent(target_agent_id) or {}
                try:
                    ok = self.agent_manager.send_to_agent(
                        target_agent_id,
                        {'role': 'user', 'content': message},
                        cwd=cwd,
                        read_only=read_only,
                    )
                except ValueError as exc:
                    result = f'向 agent {target_agent_id} 发送失败: {exc}'
                else:
                    if ok:
                        restart_result = self._ensure_agent_loop_running(target_agent_id)
                        before_status = str(before.get('status') or '').strip().lower()
                        if not restart_result.get('ok'):
                            result = (
                                f'向 agent {target_agent_id} 发送成功，但重新拉起执行循环失败: '
                                f'{restart_result.get("error") or "unknown error"}'
                            )
                        else:
                            if before_status == 'review_required':
                                suffix = '；已从原上下文继续并重置本阶段轮次。'
                            elif before_status == 'error':
                                suffix = '；已从 error 状态恢复，并重新唤醒执行循环。'
                            else:
                                suffix = '。'
                        config_changes = []
                        if cwd is not None:
                            config_changes.append(f'默认目录={cwd or "/"}')
                        if read_only is not None:
                            config_changes.append('模式=只读' if read_only else '模式=可写')
                        if restart_result.get('ok'):
                            config_suffix = f' 已更新{"，".join(config_changes)}。' if config_changes else ''
                            result = f'已向 agent {target_agent_id} 发送消息{suffix}{config_suffix}'
                    else:
                        result = f'向 agent {target_agent_id} 发送失败（agent 不存在或队列异常）。'
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
                    status = str(item.get('status') or '?')
                    detail = str(item.get('error_detail') or '').strip()
                    activity_ts = item.get('last_activity_at') or 0
                    activity = time.strftime('%m-%d %H:%M', time.localtime(activity_ts)) if activity_ts else '未知'
                    status_txt = status
                    if status == 'error':
                        status_txt = f'error({detail[:120] or "无详情"})'
                    lines.append(
                        f"- {item.get('agent_id')} | {status_txt} | 最后活动:{activity} | "
                        f"目标:{'ssh:' + str(item.get('ssh_profile_id') or '') if item.get('target_kind') == 'ssh' else 'local'} | "
                        f"阶段轮次:{item.get('stage_iteration') or 0}/{MAX_ITERATIONS} | "
                        f"复核次数:{item.get('review_count') or 0} | "
                        f"消息数:{item.get('message_count')} | "
                        f"目录:{item.get('cwd') or '/'} | "
                        f"模式:{'只读' if item.get('read_only') else '可写'} | "
                        f"来源:{item.get('origin_scope') or '未知'} | "
                        f"{item.get('instruction_summary') or ''}"
                        + (' | 建议:可发送“继续/纠偏”指令，或 destroy_agent 销毁重建' if status == 'error' else '')
                        + (' | 建议:等待主AI复核' if status == 'review_required' else '')
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
            message_ref = self._normalize_message_ref(tool_input.get('message_ref'))
            refs: list[str] = []
            missing_reason = ''
            if message_ref:
                target = self._lookup_message_ref(scope_type, scope_id, message_ref)
                if not target:
                    missing_reason = f'找不到消息短ID {message_ref} 对应的上下文消息。'
                else:
                    refs = list(target.get('image_refs') or [])
                    if not refs:
                        missing_reason = f'消息 #{message_ref} 里没有图片可查看。'
            else:
                refs = self._turn_image_refs.get(scope_key) or []
                if not refs:
                    missing_reason = '本次消息里没有可查看的图片。'
            if not refs:
                result = missing_reason or '没有可查看的图片。'
            else:
                try:
                    index = int(tool_input.get('index') or 1)
                except (TypeError, ValueError):
                    index = 1
                if index < 1 or index > len(refs):
                    if message_ref:
                        result = f'消息 #{message_ref} 的图片序号超出范围（共 {len(refs)} 张，index 从 1 开始）。'
                    else:
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
                    for i, sticker in enumerate(stickers, start=1):
                        note = self._get_sticker_note(notes, sticker)
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
                sticker = stickers[index - 1]
                note_key = self._sticker_note_key(sticker)
                if not note_key:
                    result = '该表情缺少稳定标识，暂时无法保存备注。'
                else:
                    notes = dict(self.repo.get_setting('sticker_notes', {}) or {})
                    notes[note_key] = note
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
                sticker = stickers[index - 1]
                emoji_id = str(sticker.get('emoji_id') or '').strip()
                emoji_package_id = str(sticker.get('emoji_package_id') or '').strip()
                face_key = str(sticker.get('key') or '').strip()
                summary = str(sticker.get('summary') or '').strip() or '[表情包]'
                preview_url = self._sticker_preview_url(sticker)
                try:
                    target_id = int(scope_id)
                except (TypeError, ValueError):
                    target_id = scope_id
                try:
                    if emoji_id:
                        await asyncio.to_thread(
                            self.bot.send_mface,
                            scope_type,
                            target_id,
                            emoji_id,
                            emoji_package_id,
                            face_key,
                            summary,
                        )
                        result = f'已发送第 {index} 个表情。'
                    elif preview_url:
                        await asyncio.to_thread(self.bot.send_image, scope_type, target_id, preview_url)
                        result = f'已发送第 {index} 个表情（已回退为图片发送）。'
                    else:
                        result = '该表情缺少可发送的 emoji_id 和预览地址，无法发送。'
                except Exception as exc:
                    result = f'发送表情失败: {exc}'
        elif name == 'view_sticker':
            try:
                stickers = await self._get_stickers()
            except Exception:
                stickers = []
            try:
                index = int(tool_input.get('index') or 0)
            except (TypeError, ValueError):
                index = 0
            if not stickers:
                result = '拿不到收藏表情列表，无法查看，请先调用 list_stickers。'
            elif index < 1 or index > len(stickers):
                result = f'表情序号超出范围（共 {len(stickers)} 个，index 从 1 开始）。'
            else:
                sticker = stickers[index - 1]
                url = self._sticker_preview_url(sticker)
                question = str(tool_input.get('question') or '').strip()
                prompt = question or '请描述这个表情包：画的是什么、表达什么情绪、有没有文字或梗、适合什么聊天场景。'
                if not url:
                    result = '该表情缺少预览地址，暂时无法查看。'
                else:
                    try:
                        desc = await asyncio.to_thread(
                            self.vision_model.describe_images,
                            [url],
                            prompt,
                        )
                        result = desc.strip() if desc else '表情解析结果为空。'
                    except Exception as exc:
                        result = f'表情解析失败: {exc}'
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
        elif name in {'send_voice', 'send_local_voice'}:
            import base64 as _base64
            text = str(tool_input.get('text') or '').strip()
            emotion = str(tool_input.get('emotion') or '').strip().lower() or None
            speaker_id = str(tool_input.get('speaker_id') or '').strip() or None
            speed_raw = tool_input.get('speed')
            volume_raw = tool_input.get('volume')
            try:
                speed = float(speed_raw) if speed_raw is not None else 1.0
            except (TypeError, ValueError):
                speed = 1.0
            try:
                volume = float(volume_raw) if volume_raw is not None else 0.0
            except (TypeError, ValueError):
                volume = 0.0
            if not text:
                result = 'error: text 为空，未发送。'
            else:
                try:
                    service = self._get_txt2wav_service()
                    provider_options = {'emotion': emotion} if emotion else None
                    audio_path = await asyncio.to_thread(
                        service.text_to_audio,
                        text,
                        speaker_id=speaker_id,
                        speed=speed,
                        volume=volume,
                        provider_options=provider_options,
                    )
                    data_bytes = Path(audio_path).read_bytes()
                    b64 = _base64.b64encode(data_bytes).decode('ascii')
                    file_arg = f'base64://{b64}'
                    try:
                        target_id = int(scope_id)
                    except (TypeError, ValueError):
                        target_id = scope_id
                    response = await asyncio.to_thread(
                        self.tools.send_chat_record, scope_type, target_id, file_arg
                    )
                    mid = None
                    if isinstance(response, dict):
                        mid = (response.get('data') or {}).get('message_id')
                    if mid is not None:
                        result = f'已发送语音 {Path(audio_path).name}，message_id: {mid}'
                    else:
                        result = f'已发送语音 {Path(audio_path).name}。'
                except Txt2WavError as exc:
                    result = f'发送语音失败: {exc}'
                except Exception as exc:
                    result = f'发送语音失败: {exc}'
        elif name == 'send_file':
            import base64 as _base64
            import pathlib as _pathlib
            raw_path = str(tool_input.get('path') or '').strip()
            display_name = str(tool_input.get('name') or '').strip() or None
            if not raw_path:
                result = 'error: path 为空，未发送。'
            else:
                candidate = _pathlib.Path(raw_path)
                if not candidate.is_absolute():
                    result = 'error: send_file 的 path 必须是服务器本地绝对路径。'
                else:
                    try:
                        resolved = candidate.resolve()
                    except Exception as exc:
                        resolved = None
                        result = f'error: 路径解析失败: {exc}'
                    if resolved is not None:
                        if not resolved.exists() or not resolved.is_file():
                            result = 'error: 文件不存在或不是普通文件。'
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
                                    self.tools.send_chat_file,
                                    scope_type,
                                    target_id,
                                    file_arg,
                                    display_name or resolved.name,
                                )
                                if isinstance(response, dict):
                                    data = response.get('data') or {}
                                    if data.get('message_id') is not None:
                                        result = f'已发送文件 {resolved.name}，message_id: {data.get("message_id")}'
                                    else:
                                        result = f'已发送文件 {resolved.name}。'
                                else:
                                    result = f'已发送文件 {resolved.name}。'
                            except Exception as exc:
                                result = f'发送文件失败: {exc}'
        elif name == 'manage_upstream':
            action = str(tool_input.get('action') or '').strip()
            if action == 'list':
                result = self.model_manager.list_upstreams_text()
            elif action == 'add':
                _ok, result = self.model_manager.add_upstream(
                    name=str(tool_input.get('name') or '').strip(),
                    base_url=str(tool_input.get('base_url') or '').strip(),
                    api_key=str(tool_input.get('api_key') or '').strip(),
                    protocol=str(tool_input.get('protocol') or '').strip(),
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
        elif name == 'manage_ssh_profile':
            _is_admin = scope_type == 'master' or (scope_type == 'private' and self._is_admin_user(scope_id))
            if not _is_admin:
                result = 'error: manage_ssh_profile 仅管理员可用。'
            else:
                action = str(tool_input.get('action') or '').strip()
                stored_profiles = self._get_stored_ssh_profiles()
                profiles = stored_profiles if stored_profiles is not None else list(getattr(self.config, 'ssh_profiles', None) or [])
                profile_map = {profile.profile_id: profile for profile in profiles}
                if action == 'list':
                    result = self._format_ssh_profiles_list()
                elif action == 'add':
                    profile_id = str(tool_input.get('profile_id') or '').strip()
                    if not profile_id:
                        result = 'error: add 时 profile_id 必填。'
                    elif profile_id in profile_map:
                        result = f'error: SSH profile {profile_id} 已存在。'
                    else:
                        parsed = parse_ssh_profiles([{
                            'profile_id': profile_id,
                            'target': tool_input.get('target'),
                            'root_dir': tool_input.get('root_dir', '~'),
                            'port': tool_input.get('port', 22),
                            'identity_file': tool_input.get('identity_file', ''),
                            'shell': tool_input.get('shell', 'bash'),
                            'strict_host_key_checking': tool_input.get('strict_host_key_checking', True),
                        }], warn_prefix='manage_ssh_profile.add')
                        if not parsed:
                            result = 'error: SSH profile 参数无效，至少需要 profile_id 和 target。'
                        else:
                            profiles.append(parsed[0])
                            self._save_ssh_profiles(profiles)
                            result = f'SSH profile {profile_id} 已添加。'
                elif action == 'update':
                    profile_id = str(tool_input.get('profile_id') or '').strip()
                    current = profile_map.get(profile_id)
                    if current is None:
                        result = f'error: SSH profile {profile_id} 不存在。'
                    else:
                        payload = self._ssh_profile_to_payload(current)
                        for key in ('target', 'root_dir', 'port', 'identity_file', 'shell', 'strict_host_key_checking'):
                            if key in tool_input and tool_input.get(key) is not None:
                                payload[key] = tool_input.get(key)
                        parsed = parse_ssh_profiles([payload], warn_prefix='manage_ssh_profile.update')
                        if not parsed:
                            result = f'error: SSH profile {profile_id} 更新后的配置无效。'
                        else:
                            profile_map[profile_id] = parsed[0]
                            self._save_ssh_profiles(list(profile_map.values()))
                            result = f'SSH profile {profile_id} 已更新。'
                elif action == 'remove':
                    profile_id = str(tool_input.get('profile_id') or '').strip()
                    if profile_id not in profile_map:
                        result = f'error: SSH profile {profile_id} 不存在。'
                    else:
                        del profile_map[profile_id]
                        self._save_ssh_profiles(list(profile_map.values()))
                        result = f'SSH profile {profile_id} 已删除。'
                else:
                    result = f'manage_ssh_profile: 未知 action={action!r}，可用: list add update remove'
        elif name == 'validate_model_config':
            _is_admin = scope_type == 'master' or (scope_type == 'private' and self._is_admin_user(scope_id))
            if not _is_admin:
                result = 'error: validate_model_config 仅管理员可用。'
            else:
                _target_type = str(tool_input.get('target_type') or '').strip()
                _channel = str(tool_input.get('channel') or '').strip()
                _upstream = str(tool_input.get('upstream') or '').strip()
                _model_id = str(tool_input.get('model_id') or '').strip()
                _svc = self._get_model_validation_service()
                if _target_type == 'channel':
                    _results = await asyncio.to_thread(_svc.validate_channel, _channel)
                    result = json.dumps(_results, ensure_ascii=False, indent=2)
                elif _target_type == 'model':
                    if not _upstream or not _model_id:
                        result = 'error: 验证单模型时 upstream 和 model_id 必填。'
                    else:
                        _res = await asyncio.to_thread(_svc.validate_model, _channel, _upstream, _model_id)
                        result = json.dumps(_res, ensure_ascii=False, indent=2)
                else:
                    result = f'error: 未知 target_type={_target_type!r}，可用: channel model'
        elif name == 'validate_ssh_profile':
            _is_admin = scope_type == 'master' or (scope_type == 'private' and self._is_admin_user(scope_id))
            if not _is_admin:
                result = 'error: validate_ssh_profile 仅管理员可用。'
            else:
                _profile_id = str(tool_input.get('profile_id') or '').strip()
                _profile = self._get_ssh_profiles_map().get(_profile_id)
                if _profile is None:
                    result = f'error: SSH profile {_profile_id} 不存在。'
                else:
                    _res = await asyncio.to_thread(validate_ssh_profile, _profile)
                    result = json.dumps(_res, ensure_ascii=False, indent=2)
        elif name == 'switch_agent_channel':
            _target_agent_id = str(tool_input.get('agent_id') or '').strip()
            _target_channel = str(tool_input.get('channel') or '').strip()
            if not _target_agent_id or not _target_channel:
                result = 'error: agent_id 和 channel 均为必填。'
            else:
                _agent_rec = self.agent_manager.get_agent(_target_agent_id)
                if not _agent_rec:
                    result = f'error: agent {_target_agent_id} 不存在。'
                else:
                    _is_admin = scope_type == 'master' or (scope_type == 'private' and self._is_admin_user(scope_id))
                    _origin = _agent_rec.get('origin_scope') or ''
                    _current_scope = f'{scope_type}:{scope_id}'
                    if not _is_admin and _origin != _current_scope:
                        result = f'error: 无权切换其他 scope 创建的 agent（origin={_origin}）。'
                    else:
                        _models = self.model_manager.resolve_channel_models(_target_channel)
                        if not _models:
                            result = f'error: 渠道 {_target_channel!r} 不存在或没有有效模型。'
                        else:
                            _cfg = _models[0]
                            _new_client = AnthropicChatModel(
                                base_url=_cfg['base_url'],
                                api_key=_cfg['api_key'],
                                model_name=_cfg['model_name'],
                                messages_path=_cfg['messages_path'],
                            )
                            _prev_binding = _agent_rec.get('model_binding') or {}
                            _prev_channel = _prev_binding.get('channel', '(未知)')
                            _binding = {
                                'channel': _target_channel,
                                'upstream': _cfg['upstream_name'],
                                'model_id': _cfg['model_name'],
                            }
                            _sw = self.agent_manager.switch_agent_model_binding(_target_agent_id, _binding, _new_client)
                            if _sw.get('ok'):
                                _updated = self.agent_manager.get_agent(_target_agent_id) or {}
                                _gen = (_updated.get('model_binding') or {}).get('generation', '?')
                                result = json.dumps({
                                    'ok': True,
                                    'agent_id': _target_agent_id,
                                    'previous_channel': _prev_channel,
                                    'current_channel': _target_channel,
                                    'current_model': _cfg['display_name'],
                                    'generation': _gen,
                                    'effective_from': 'next_model_call',
                                }, ensure_ascii=False)
                            else:
                                result = f'error: 切换失败: {_sw.get("error")}'
        elif name in {'find_in_project', 'list_local_files', 'read_local_file'}:
            if self._get_scope_session_mode(scope_type, scope_id) != 'code':
                result = f'{name}: 只在 code 模式下可用。'
            elif name == 'find_in_project':
                result = await asyncio.to_thread(
                    _find_in_project,
                    _project_root(),
                    str(tool_input.get('name_pattern') or ''),
                    str(tool_input.get('content_query') or ''),
                    bool(tool_input.get('is_regex', False)),
                    str(tool_input.get('subpath') or ''),
                    tool_input.get('max_results') or 40,
                )
            elif name == 'list_local_files':
                result = await asyncio.to_thread(
                    _list_local_files,
                    _project_root(),
                    str(tool_input.get('subpath') or ''),
                )
            else:
                result = await asyncio.to_thread(
                    _read_local_file,
                    _project_root(),
                    str(tool_input.get('path') or ''),
                )
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

    def _get_txt2wav_service(self):
        service = getattr(self, '_txt2wav_service', None)
        if service is None:
            service = create_default_txt2wav_service(
                provider=self.config.tts_provider,
                output_dir='data/tmp',
                api_key=self.config.tts_api_key,
                reference_id=self.config.tts_reference_id,
                base_url=self.config.tts_base_url,
                model=self.config.tts_model,
            )
            self._txt2wav_service = service
        return service

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

    @staticmethod
    def _ssh_profile_to_payload(profile: SSHProfileConfig) -> dict:
        return {
            'profile_id': profile.profile_id,
            'target': profile.target,
            'root_dir': profile.root_dir,
            'port': int(profile.port or 22),
            'identity_file': profile.identity_file,
            'password': getattr(profile, 'password', ''),
            'shell': profile.shell,
            'strict_host_key_checking': bool(profile.strict_host_key_checking),
        }

    def _get_stored_ssh_profiles(self) -> list[SSHProfileConfig] | None:
        raw = self.repo.get_setting('ssh_profiles', None)
        if raw is None:
            return None
        return parse_ssh_profiles(raw, warn_prefix='settings.ssh_profiles')

    def _get_ssh_profiles_map(self) -> dict[str, SSHProfileConfig]:
        stored_profiles = self._get_stored_ssh_profiles()
        profiles = stored_profiles if stored_profiles is not None else (getattr(self.config, 'ssh_profiles', None) or [])
        result: dict[str, SSHProfileConfig] = {}
        for profile in profiles:
            profile_id = str(getattr(profile, 'profile_id', '') or '').strip()
            if profile_id:
                result[profile_id] = profile
        return result

    def _save_ssh_profiles(self, profiles: list[SSHProfileConfig]) -> None:
        profiles = sorted(
            list(profiles or []),
            key=lambda item: str(getattr(item, 'profile_id', '') or '').strip(),
        )
        self.repo.set_setting(
            'ssh_profiles',
            [self._ssh_profile_to_payload(profile) for profile in profiles],
        )

    def _format_ssh_profiles_list(self) -> str:
        profiles = list(self._get_ssh_profiles_map().values())
        if not profiles:
            return '当前未配置任何 SSH profile。'
        lines = [f'共 {len(profiles)} 个 SSH profile：']
        for profile in profiles:
            auth_mode = 'password' if str(getattr(profile, 'password', '') or '').strip() else ('identity' if str(profile.identity_file or '').strip() else 'system')
            lines.append(
                f"- {profile.profile_id} | target:{profile.target} | root:{profile.root_dir} | "
                f"port:{profile.port} | auth:{auth_mode} | shell:{profile.shell} | host_key:{'严格' if profile.strict_host_key_checking else '宽松'}"
            )
        return '\n'.join(lines)

    async def _execute_web_search(self, query: str, scope_type: str = '', scope_id: str = '') -> str:
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
            reply = await self._complete_chat(
                self._static_system_blocks('你是一个搜索结果摘要助手，只根据给定的搜索数据做客观摘要，不要编造信息。'),
                [{'role': 'user', 'content': summary_prompt}],
                None,
                0.3,
                scope_key=self._scope_key(scope_type, scope_id) if scope_type and scope_id else None,
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


    def _turn_result_bundle(
        self,
        bundle: dict,
        *,
        turn_log_committed: bool,
        agent_id: str,
        temperature: float,
        turn_meta: dict | None,
        tool_iterations: list[dict],
        generation_ms: int | None,
        note: str | None = None,
    ) -> dict:
        result = dict(bundle)
        # 兜底：所有 turn 结果统一剥 thinking 标签，防止任何路径把思维链当正文发出。
        # 剥完为空即模型沉默（message=''），调用方按不发送处理。
        result['message'] = self._strip_send_message_thinking(str(result.get('message') or ''))
        result['turn_log_committed'] = bool(turn_log_committed)
        result['turn_metadata'] = None
        if turn_log_committed:
            result['turn_metadata'] = {
                'agent_id': agent_id,
                'temperature': temperature,
                'turn_meta': dict(turn_meta or {}),
                'tool_iterations': copy.deepcopy(tool_iterations),
                'generation_ms': generation_ms,
                'note': note,
            }
        return result
    def _resolve_tiered_role(self, turn_meta: dict | None = None) -> str:
        """分级 AI 三渠道场景判定（决策 > 执行 > 聊天）。

        - tiered_decision：触发批次含 agent/内部消息，或合并触发数>=5，或合并总字数>300；
        - tiered_exec：委派/情报轮等天然工具驱动回合，或工具 checkpoint 续跑（混合场景）；
        - tiered_chat：仅用户消息的普通聊天。
        未单独配置 tiered 子渠道时，ModelManager 经回退链 tiered_* → tiered → main 解析，
        行为与旧版一致。
        """
        meta = turn_meta or {}
        _turn_kind = str(meta.get('turn_kind') or 'unknown')
        if (
            meta.get('has_agent_message')
            or int(meta.get('trigger_count') or 0) >= 5
            or int(meta.get('combined_trigger_chars') or 0) > 300
        ):
            return 'tiered_decision'
        if _turn_kind in ('delegate', 'intel_query') or meta.get('resumed_from_tool_turn'):
            return 'tiered_exec'
        return 'tiered_chat'

    def _executed_tools_store(self) -> dict:
        """惰性取容器：不少测试绕过 __init__ 手搭 runtime，直接取属性会 AttributeError。"""
        store = getattr(self, '_scope_executed_tools', None)
        if store is None:
            store = {}
            self._scope_executed_tools = store
        return store

    def _describe_executed_tools(self, scope_type: str, scope_id: str) -> str:
        """把本轮已执行的工具压成 '名字 x次' 的短摘要，供异常中断说明引用。"""
        names = self._executed_tools_store().get(self._scope_key(scope_type, scope_id)) or []
        if not names:
            return ''
        counts: dict[str, int] = {}
        for name in names:
            if name:
                counts[name] = counts.get(name, 0) + 1
        if not counts:
            return ''
        return '、'.join(
            f'{name} x{count}' if count > 1 else name
            for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
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
        live_send_action_ledger: set[str] | None = None,
    ) -> tuple[dict, int | None, bool]:
        if isinstance(messages, str):
            _session_mode = self._get_scope_session_mode(scope_type, scope_id)
            system_blocks = self._static_system_blocks(self._system_prompt(), chat_mode=(_session_mode == 'chat'))
            model_messages = [{'role': 'user', 'content': messages}]
            _persona_injected = False
        else:
            system_blocks = list(messages.get('system') or [])
            model_messages = [dict(item) for item in (messages.get('messages') or [])]
            _persona_injected = bool(messages.get('inject_persona', True))
        _allow_cfg = scope_type == 'master' or (scope_type == 'private' and str(scope_id) == str(self.config.admin_qq))

        def _build_round_tools(session_mode: str) -> list[dict]:
            return build_tools(
                allow_notify_master=allow_notify_master,
                allow_tasks=allow_tasks,
                immediate_mode=live_message is not None,
                allow_config_tools=_allow_cfg,
                include_group_management=(scope_type == 'group'),
                include_qq_request_management=(scope_type == 'master'),
                include_relation_read=True,
                include_relation_write=(scope_type == 'master'),
                include_knowledge_management=_allow_cfg,
                include_knowledge_request=allow_notify_master and not _allow_cfg,
                chat_mode=(session_mode == 'chat'),
            )

        _session_mode = self._get_scope_session_mode(scope_type, scope_id)
        _tools_session_mode = _session_mode
        tools = _build_round_tools(_session_mode)
        scope_key = self._scope_key(scope_type, scope_id) if live_message is not None else None
        _executed_key = self._scope_key(scope_type, scope_id)
        self._executed_tools_store()[_executed_key] = []
        tool_iterations: list[dict] = []
        started_at = time.perf_counter()
        used_tools = False
        sent_entries: list[dict] = []
        live_outbound_entries: list[dict] = []
        tool_context_messages: list[dict] = []
        live_tool_context_checkpointed = False
        live_tool_checkpoint_entry: dict | None = None
        live_tool_checkpoint_id = uuid.uuid4().hex
        # 后台链路（live_message=None）混合批次里 send_message 的内容聚合缓冲：
        # DIRECTIVE 动作立即执行，send_message 文本先收进这里，回合结束时统一作为回报，
        # 避免“先发确认再干活”的确认消息因模型后续不回调而丢失。
        background_reply_parts: list[str] = []
        fallback_prompted = False
        openai_tool_guidance = False
        transient_persona_notice_pending = (
            self._consume_send_message_persona_notice(scope_type, scope_id, agent_id)
            if _persona_injected else False
        )
        inject_transient_persona_notice_on_next_completion = False
        max_iterations = 8 if live_message is not None else 6
        _turn_kind = (turn_meta or {}).get('turn_kind', 'unknown')
        _tiered_role = self._resolve_tiered_role(turn_meta)
        _tool_count = len(tools) if tools else 0
        info(
            f'[AI][turn] start scope={scope_type}:{scope_id} '
            f'agent={agent_id} kind={_turn_kind} role={_tiered_role} '
            f'messages={len(model_messages)} tools={_tool_count} '
            f'live={live_message is not None} max_iter={max_iterations}'
        )
        for _ in range(max_iterations):
            if self._is_epoch_stale(run_epoch):
                return self._turn_result_bundle({'message': '', 'think_note': '', 'tool_context_messages': tool_context_messages, 'live_tool_context_checkpointed': live_tool_context_checkpointed, 'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry)}, turn_log_committed=False, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=int((time.perf_counter() - started_at) * 1000)), int((time.perf_counter() - started_at) * 1000), used_tools
            # set_session_mode 可能在本轮的上一次迭代里刚被调用过。工具表若不跟着重建，
            # 模型会看到"模式已切到 code"但手上还是 chat 的精简工具表，表现为切了没用。
            _live_session_mode = self._get_scope_session_mode(scope_type, scope_id)
            if _live_session_mode != _tools_session_mode:
                tools = _build_round_tools(_live_session_mode)
                info(
                    f'[AI][turn] tools rebuilt scope={scope_type}:{scope_id} '
                    f'mode={_tools_session_mode}->{_live_session_mode} tools={len(tools) if tools else 0}'
                )
                _tools_session_mode = _live_session_mode
            round_tools = tools
            forced_digest_round = False
            scope_key = f"{scope_type}:{scope_id}"
            request_messages = model_messages
            if inject_transient_persona_notice_on_next_completion:
                request_messages = self._render_transient_persona_notice_messages(model_messages)
                inject_transient_persona_notice_on_next_completion = False
            reply = await self._complete_chat(system_blocks, request_messages, round_tools, temperature, scope_key=scope_key, role=_tiered_role)
            generation_ms = int((time.perf_counter() - started_at) * 1000)
            if self._is_epoch_stale(run_epoch):
                return self._turn_result_bundle({'message': '', 'think_note': '', 'tool_context_messages': tool_context_messages, 'live_tool_context_checkpointed': live_tool_context_checkpointed, 'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry)}, turn_log_committed=False, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=generation_ms), generation_ms, used_tools
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
                return self._turn_result_bundle({'message': '', 'think_note': '', 'tool_context_messages': tool_context_messages, 'live_tool_context_checkpointed': live_tool_context_checkpointed, 'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry)}, turn_log_committed=True, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=generation_ms), generation_ms, used_tools
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
                    if background_reply_parts:
                        # 混合批次里已聚合的 send_message 内容（如“好，我马上去”）并入最终回报
                        parts = list(background_reply_parts)
                        if final_reply:
                            parts.append(final_reply)
                        final_reply = '\n'.join(parts)
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
                    return self._turn_result_bundle({'message': final_reply, 'think_note': think_note, 'tool_context_messages': tool_context_messages, 'live_tool_context_checkpointed': live_tool_context_checkpointed, 'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry)}, turn_log_committed=True, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=generation_ms), generation_ms, used_tools
                used_tools = True
                result_blocks: list[dict] = []
                iteration_calls: list[dict] = []
                mixed_tool_batch = len(reply.tool_calls) > 1
                for call in reply.tool_calls:
                    try:
                        if call.name in LOOP_TOOL_NAMES:
                            result = await self._run_ai_tool_call(scope_type, scope_id, agent_id, call.name, call.input)
                        else:
                            # 混合批次里的 DIRECTIVE 动作立即执行（同 notify_master 循环修复），
                            # 不再用占位提示依赖模型下轮回调，否则确认消息/任务会凭空丢失；
                            # send_message 内容聚合到 background_reply_parts，回合结束统一作为回报。
                            partial = self._apply_directive_tools(
                                scope_type,
                                scope_id,
                                agent_id,
                                [call],
                                context=context,
                                allow_notify_master=allow_notify_master,
                                allow_tasks=allow_tasks,
                            )
                            if partial:
                                background_reply_parts.append(partial)
                            result = partial or '本轮已执行该操作。'
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
                            'content': self._format_tool_result_content(call.name, result, mixed_batch=mixed_tool_batch),
                        }
                    )
                    iteration_calls.append({'name': call.name, 'input': call.input, 'result': result})
                tool_iterations.append(
                    {
                        'assistant_text': reply.text,
                        'tool_calls': iteration_calls,
                    }
                )
                if transient_persona_notice_pending and result_blocks:
                    inject_transient_persona_notice_on_next_completion = True
                    transient_persona_notice_pending = False
                # Anthropic extended thinking 协议：assistant 消息必须原样回传
                # （含 thinking block 与 signature），否则 API 报 400
                # "content[].thinking ... must be passed back"。这里不能再做
                # thinking 过滤，直接深拷贝原始 raw_content。
                assistant_content = copy.deepcopy(reply.raw_content)
                model_messages.append({'role': 'assistant', 'content': assistant_content})
                model_messages.append({'role': 'user', 'content': result_blocks})
                continue

            # live_message 不为空：主链路，工具调用边执行边发送
            if not reply.tool_calls:
                if forced_digest_round:
                    # 这一轮被临时摘掉了发送类工具，只是让模型先消化中断提醒；
                    # 模型没调用工具 = 已经消化完毕，进入下一轮恢复正常工具集重新决策。
                    # 同 5734：回传的 assistant 消息必须保留 thinking block。
                    model_messages.append({'role': 'assistant', 'content': copy.deepcopy(reply.raw_content)})
                    model_messages.append({'role': 'user', 'content': '好的，现在可以正常回复了。'})
                    continue

                # ── 兜底逻辑：模型未调用 send_message，re-prompt 让模型重新决策 ──────────
                # 模型可能是：①忘记调用工具（某些模型偶发）②主动选择不回复（把理由写进文本）
                # 直接自动发送文本会把模型的内心独白泄露给用户，因此改为 re-prompt。
                # 模型重新决策后：如果决定回复 → 调用 send_message；如果决定不回复 → 什么都不做，轮次正常结束。
                if not sent_entries and reply.text.strip():
                    if fallback_prompted:
                        # 已经 re-prompt 过一次仍不调工具，交给循环后的 loop_guard
                        # 做最后一次强制决定，不在这里静默结束。
                        warn('[AI][fallback] re-prompt 后仍为纯文本，转 loop_guard 强制决定')
                        break
                    warn('[AI][fallback] 模型未调用 send_message，re-prompt 重新决策')
                    fallback_prompted = True
                    # 同 5734：回传的 assistant 消息必须保留 thinking block。
                    model_messages.append({'role': 'assistant', 'content': copy.deepcopy(reply.raw_content)})
                    model_messages.append({'role': 'user', 'content': '你刚才输出的那段普通文字并没有被发送——用户完全没看到它（只有调用 send_message 工具发送的内容用户才能看到）。本条系统消息同样只对你可见，用户也看不到。如果你确实想把刚才那些话说给用户，请重新调用 send_message 工具发送；如果决定不回复，请调用 stay_silent 工具结束本回合。'})
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
                return self._turn_result_bundle({'message': final_reply, 'think_note': think_note, 'tool_context_messages': tool_context_messages, 'live_tool_context_checkpointed': live_tool_context_checkpointed, 'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry)}, turn_log_committed=True, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=generation_ms), generation_ms, used_tools

            result_blocks = []
            iteration_calls = []
            end_turn_requested = False
            sent_message_this_round = False
            mixed_tool_batch = len(reply.tool_calls) > 1
            for call in reply.tool_calls:
                sent_count_before = len(sent_entries)
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
                            live_outbound_entries,
                            live_send_action_ledger=live_send_action_ledger,
                        )
                        used_tools = True
                        if call.name == 'send_message' and len(sent_entries) > sent_count_before:
                            sent_message_this_round = True
                except Exception as exc:
                    warn(
                        f'[AI][tool] 工具调用异常 scope={scope_type}:{scope_id} '
                        f'tool={call.name} error={type(exc).__name__}: {exc}'
                    )
                    result = f'工具 {call.name} 执行异常: {type(exc).__name__}: {exc}'
                    used_tools = True
                    if call.name == 'send_message' and len(sent_entries) > sent_count_before:
                        sent_message_this_round = True
                result_blocks.append(
                    {
                        'type': 'tool_result',
                        'tool_use_id': call.call_id,
                        'content': self._format_tool_result_content(call.name, result, mixed_batch=mixed_tool_batch),
                    }
                )
                iteration_calls.append({'name': call.name, 'input': call.input, 'result': result})
            tool_iterations.append(
                {
                    'assistant_text': reply.text,
                    'tool_calls': iteration_calls,
                }
            )
            self._executed_tools_store().setdefault(_executed_key, []).extend(
                str(entry.get('name') or '') for entry in iteration_calls
            )
            if transient_persona_notice_pending and result_blocks:
                inject_transient_persona_notice_on_next_completion = True
                transient_persona_notice_pending = False
            # 同 5734：回传的 assistant 消息必须保留 thinking block；
            # 该 content 同时进入 tool_context 与 live checkpoint，后续仍会回传 API。
            assistant_content = copy.deepcopy(reply.raw_content)
            iteration_tool_context = [
                {'role': 'assistant', 'content': copy.deepcopy(assistant_content)},
                {'role': 'user', 'content': copy.deepcopy(result_blocks)},
            ]
            model_messages.append({'role': 'assistant', 'content': assistant_content})
            model_messages.append({'role': 'user', 'content': result_blocks})
            tool_context_messages.extend(copy.deepcopy(iteration_tool_context))
            if not sent_message_this_round:
                live_tool_checkpoint_entry = self._append_live_tool_checkpoint(
                    scope_type,
                    scope_id,
                    live_tool_checkpoint_id,
                    iteration_tool_context,
                )
                if live_tool_checkpoint_entry:
                    live_tool_context_checkpointed = True

            # OpenAI 协议模型引导：工具结果返回后追加提醒，防止模型陷入查询→查询循环
            if getattr(self.model, 'is_openai_protocol', False) and not sent_entries and not end_turn_requested:
                if not openai_tool_guidance:
                    openai_tool_guidance = True
                    model_messages.append({'role': 'user', 'content': '以上是工具执行结果。请基于这些信息，调用 send_message 工具向用户发送最终回复；如果判断不需要回复，请调用 stay_silent 工具结束本回合。'})

            post_send_pending = None
            if sent_message_this_round and scope_key:
                post_send_pending = self._drain_live_tool_scope_turn(scope_key)
                if post_send_pending:
                    model_messages.append(
                        {'role': 'user', 'content': self._build_pending_fold_reminder(post_send_pending)}
                    )

            # send_message 的 continue_work 参数决定发送后是否保留后续轮次：
            #   - continue_work=true  → 这条只是确认消息，发完本回合继续，模型可在后续
            #     轮次继续调用工具（create_task / create_tasker / notify_master / 查询类等），
            #     完成续期/建任务/委派等“先确认再干活”的操作，不再出现“发完确认就静默”；
            #   - continue_work=false/缺省 → 发送即终结（旧语义），不产生多余轮次。
            # 同批含查询类工具（had_loop_tools）时结果已回填，模型自然继续消费，不受此参数影响。
            # post_send_pending 非空（发送期间有新事件积压）时仍走终结，由 _process_message
            # 续跑循环把新事件作为新一轮 trigger 跨回合续跑，避免回合内上下文膨胀。
            had_loop_tools = any(call.name in LOOP_TOOL_NAMES for call in reply.tool_calls)
            continue_work_requested = any(
                call.name == 'send_message'
                and bool(dict(call.input or {}).get('continue_work'))
                for call in reply.tool_calls
            )
            if end_turn_requested or (
                sent_message_this_round
                and not had_loop_tools
                and not continue_work_requested
            ):
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
                    f'stay_silent={end_turn_requested} sent_message={sent_message_this_round}'
                )
                self._note_session_mode_activity(scope_type, scope_id, tool_iterations, _turn_kind)
                result_bundle = {
                    'message': final_reply,
                    'think_note': think_note,
                    'tool_context_messages': tool_context_messages,
                    'live_tool_context_checkpointed': live_tool_context_checkpointed,
                    'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry),
                    'live_outbound_entries': [dict(entry) for entry in live_outbound_entries],
                }
                if post_send_pending:
                    result_bundle['post_send_pending'] = dict(post_send_pending)
                return self._turn_result_bundle(result_bundle, turn_log_committed=True, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=generation_ms), generation_ms, used_tools

            scope_key = self._scope_key(scope_type, scope_id)
            pending = self._drain_live_tool_scope_turn(scope_key)
            if pending:
                model_messages.append({'role': 'user', 'content': self._build_pending_fold_reminder(pending)})
        # loop_guard 兜底：触发前做最后一次 re-prompt，给模型一次强制决定的机会。
        # 不再限定 OpenAI 协议——Anthropic 渠道在扩展思考下 tool_choice 只能是 auto，
        # 同样会出现“只输出纯文本导致消息发不出去”的静默，需要同一层兜底。
        if live_message is not None:
            model_messages.append({'role': 'user', 'content': '你已经进行了多轮工具调用但没有发送任何回复。现在你必须做出最终决定：调用 send_message 向用户发送最终回复，或调用 stay_silent 结束本回合。不要再调用其他查询工具。'})
            try:
                _final_reply = await self._complete_chat(system_blocks, model_messages, tools, temperature, scope_key=scope_key, role=_tiered_role)
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
                            return self._turn_result_bundle({'message': final_reply, 'think_note': '', 'tool_context_messages': tool_context_messages, 'live_tool_context_checkpointed': live_tool_context_checkpointed, 'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry), 'live_outbound_entries': [dict(entry) for entry in live_outbound_entries]}, turn_log_committed=True, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=_loop_guard_ms, note='tool_loop_guard'), _loop_guard_ms, used_tools
                        elif call.name == 'send_message':
                            self._execute_live_action_tool_call(
                                scope_type, scope_id, agent_id, live_message,
                                call, context, allow_notify_master, allow_tasks, sent_entries, live_outbound_entries,
                                live_send_action_ledger=live_send_action_ledger,
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
                            return self._turn_result_bundle({'message': final_reply, 'think_note': '', 'tool_context_messages': tool_context_messages, 'live_tool_context_checkpointed': live_tool_context_checkpointed, 'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry), 'live_outbound_entries': [dict(entry) for entry in live_outbound_entries]}, turn_log_committed=True, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=_loop_guard_ms, note='tool_loop_guard'), _loop_guard_ms, used_tools
                elif _final_reply and _final_reply.text.strip():
                    _loop_guard_ms = int((time.perf_counter() - started_at) * 1000)
                    warn('[AI][turn] loop_guard 收到普通文本，已阻止其作为真实回复落库/发送')
                    await self._record_turn_log(
                        scope_type, scope_id, agent_id, model_messages,
                        raw_reply=json.dumps(_final_reply.raw_content, ensure_ascii=False),
                        final_reply=None, temperature=temperature,
                        turn_meta=turn_meta, tool_iterations=tool_iterations,
                        generation_ms=_loop_guard_ms,
                    )
                    return self._turn_result_bundle({'message': '', 'think_note': '', 'tool_context_messages': tool_context_messages, 'live_tool_context_checkpointed': live_tool_context_checkpointed, 'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry), 'live_outbound_entries': [dict(entry) for entry in live_outbound_entries]}, turn_log_committed=True, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=_loop_guard_ms, note='tool_loop_guard_plaintext_blocked'), _loop_guard_ms, used_tools
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
        if live_message is not None:
            final_reply = '\n'.join(entry['text'] for entry in sent_entries)
        elif background_reply_parts:
            # 后台链路混合批次聚合的 send_message 内容在 loop_guard 时也不丢弃
            final_reply = '\n'.join(background_reply_parts)
        else:
            final_reply = ''
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
        return self._turn_result_bundle({'message': final_reply, 'think_note': '', 'tool_context_messages': tool_context_messages, 'live_tool_context_checkpointed': live_tool_context_checkpointed, 'live_tool_checkpoint_entry': copy.deepcopy(live_tool_checkpoint_entry), 'live_outbound_entries': [dict(entry) for entry in live_outbound_entries]}, turn_log_committed=True, agent_id=agent_id, temperature=temperature, turn_meta=turn_meta, tool_iterations=tool_iterations, generation_ms=_loop_guard_ms, note='tool_loop_guard'), _loop_guard_ms, used_tools

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
        total_send_message_chars = 0
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
                # 后台链路聚合时同样要剥 thinking 标签：模型会按 prompt 指引把思考
                # 写进 content 的 <thinking>...</thinking> 里（期望系统过滤），
                # 这里漏剥就会把思维链直接发给用户。
                content = self._strip_send_message_thinking(content)
                content = re.sub(r'\[\[.*?\]\]', '', content).strip()
                if content:
                    message_parts.append(content)
                    total_send_message_chars += len(content)
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
            elif call.name in {'create_task', 'create_tasker'} and allow_tasks:
                if call.name == 'create_tasker':
                    kind = 'dev_agent'  # legacy persisted kind
                    content = str(tool_input.get('payload') or tool_input.get('task') or '').strip()
                    if tool_input.get('github_repo'):
                        content = json.dumps({'task': content, 'github_repo': tool_input.get('github_repo')}, ensure_ascii=False)
                else:
                    kind = self._normalize_task_kind(tool_input.get('kind'))
                    content = str(tool_input.get('payload') or '').strip()
                if kind == 'dev_agent' and not self._is_dev_agent_authorized(scope_type, scope_id):
                    self.tools.record_tool_use(
                        scope_type,
                        scope_id,
                        agent_id,
                        f'task:{kind}',
                        content,
                        '拒绝：当前私聊不是管理员账号，无权发起 tasker。',
                        limit=self.config.history_limit,
                    )
                    message_parts.append('（这个操作需要号主本人才能发起，我暂时没法帮你做。）')
                elif kind:
                    payload = self._normalize_task_payload(content, scope_type, scope_id, agent_id, context)
                    if kind == 'set_alarm' and model_sent_message:
                        payload.setdefault('direct_ack_sent', True)
                    task = self.tools.create_tasker(agent_id, payload) if kind == 'dev_agent' else self.tools.create_task(agent_id, kind, payload)
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

        if total_send_message_chars > 20:
            self._mark_send_message_persona_notice(scope_type, scope_id, agent_id)
        return '\n'.join(part for part in message_parts if part).strip()

    def _strip_send_message_thinking(self, content: str) -> str:
        content = str(content or '')
        content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
        return content.strip()

    def _filter_thinking_blocks(self, raw_content):
        """过滤中转站/平台附加的 extended thinking block（仅限展示/审计类场景）。

        警告：绝不能用于“回传给 API 的 assistant 消息”。Anthropic extended thinking
        协议要求 thinking block（含 signature）原样回传，剥掉会触发 400：
        "The `content[].thinking` in the thinking mode must be passed back to the API."
        工具循环回传请直接使用 copy.deepcopy(reply.raw_content)。
        """
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

    def _build_live_tool_checkpoint_entry(
        self,
        checkpoint_id: str,
        tool_context_messages: list[dict] | None,
    ) -> dict | None:
        normalized_tool_context = self._normalize_tool_context_messages(tool_context_messages)
        if not normalized_tool_context:
            return None
        entry = self._build_outbound_message_entry('', tool_context_messages=normalized_tool_context)
        entry['tool_checkpoint_id'] = str(checkpoint_id or '').strip()
        return entry

    def _get_scope_send_ledger(self, scope_type: str, scope_id: str) -> ScopeSendLedger:
        # 惰性建表：测试 harness 用 object.__new__ 构造 runtime，不会跑 __init__。
        store = getattr(self, '_scope_send_ledgers', None)
        if store is None:
            store = {}
            self._scope_send_ledgers = store
        key = self._scope_key(scope_type, scope_id)
        ledger = store.get(key)
        if ledger is None:
            ledger = ScopeSendLedger()
            store[key] = ledger
        return ledger

    def _normalize_live_send_action_key(self, content: str, reply_to_message_id=None) -> str:
        """生成发送动作键。

        这是 scope 级的结构化幂等保护：一旦同一可见 payload 已经真实发出，TTL 内
        就不允许再走一次完全相同的 `send_message`，否则 post-send rerun、事件重投
        或触发重复都会把模型的重复决策变成真实双发。
        """
        sanitized = self._strip_send_message_thinking(str(content or ''))
        sanitized = re.sub(r'\[\[.*?\]\]', '', sanitized).strip()
        if not sanitized:
            return ''
        normalized_lines = [
            line.strip()
            for line in self._split_long_reply_lines(sanitized).split('\n')
            if line.strip()
        ]
        if not normalized_lines:
            return ''
        canonical = '\n'.join(normalized_lines)
        reply_marker = '' if reply_to_message_id in (None, '') else str(reply_to_message_id)
        return hashlib.sha1(f'{reply_marker}\n{canonical}'.encode('utf-8', 'ignore')).hexdigest()

    def _send_scope_message(self, message: ChatMessage, content: str, on_sent_entry=None) -> list[dict]:
        return self._send_scope_message_with_reply(message, content, None, on_sent_entry=on_sent_entry)

    def _send_scope_message_with_reply(
        self,
        message: ChatMessage,
        content: str,
        reply_to_message_id=None,
        on_sent_entry=None,
    ) -> list[dict]:
        content = self._strip_send_message_thinking(content)
        content = re.sub(r'\[\[.*?\]\]', '', content).strip()
        if message.chat_type == 'private':
            content = re.sub(r'\[CQ:at,qq=\d+\]', '', content).strip()
        # 无代码块：走原逻辑，行为与改动前完全一致。
        if not has_code_block(content):
            return self._send_text_lines(
                message,
                content,
                on_sent_entry=on_sent_entry,
                reply_to_message_id=reply_to_message_id,
            )
        # 有代码块：按原文顺序分段发送，text 段照原逻辑发，code 段渲染成图片发。
        # 整个分段流程再包一层兜底：任何意外都回退到原样逐行发送，绝不吞消息。
        try:
            segments = split_code_block_segments(content)
        except Exception as exc:
            warn(f'[AI][code2img] 分段失败，回退纯文本发送: {exc}')
            return self._send_text_lines(
                message,
                content,
                on_sent_entry=on_sent_entry,
                reply_to_message_id=reply_to_message_id,
            )
        entries: list[dict] = []
        pending_reply_id = reply_to_message_id
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
                    if on_sent_entry is not None:
                        on_sent_entry(dict(img_entry))
                    continue
                # 渲染或发图失败：降级为原样发送该段 raw 文本（含 ``` 围栏）。
                entries.extend(
                    self._send_text_lines(
                        message,
                        seg.get('raw') or '',
                        on_sent_entry=on_sent_entry,
                        reply_to_message_id=pending_reply_id,
                    )
                )
                pending_reply_id = None
            else:
                entries.extend(
                    self._send_text_lines(
                        message,
                        seg.get('text') or '',
                        on_sent_entry=on_sent_entry,
                        reply_to_message_id=pending_reply_id,
                    )
                )
                pending_reply_id = None
        return entries

    def _send_text_lines(self, message: ChatMessage, content: str, on_sent_entry=None, reply_to_message_id=None) -> list[dict]:
        """把一段纯文本按原逻辑拆行并逐行发送。等价于原 _send_scope_message 的发送循环。"""
        content = self._split_long_reply_lines(content)
        entries: list[dict] = []
        reply_prefix = f'[CQ:reply,id={reply_to_message_id}]' if reply_to_message_id not in (None, '') else ''
        reply_used = False
        for line in content.split('\n'):
            line = line.strip()
            if not line:
                continue
            outgoing = line
            if reply_prefix and not reply_used:
                outgoing = f'{reply_prefix}{line}'
                reply_used = True
            response = self.bot.send_text(message.chat_type, message.chat_id, outgoing)
            message_id = None
            if isinstance(response, dict):
                message_id = (response.get('data') or {}).get('message_id')
            entry = {'text': line, 'raw_message': outgoing, 'message_id': message_id}
            if reply_used and reply_prefix and outgoing.startswith(reply_prefix):
                entry['reply_to_message_id'] = reply_to_message_id
            entries.append(entry)
            if on_sent_entry is not None:
                on_sent_entry(dict(entry))
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
            from pack.code2img import render_code_to_image
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
        checkpointed_outbound_entries: list[dict] | None = None,
        live_send_action_ledger: set[str] | None = None,
    ) -> str:
        context = context or {}
        checkpointed_outbound_entries = checkpointed_outbound_entries if checkpointed_outbound_entries is not None else []
        tool_input = dict(call.input or {})
        info(
            f'[AI][live_tool] scope={scope_type}:{scope_id} '
            f'agent={agent_id} tool={call.name} '
            f'input_keys={list(tool_input.keys())}'
        )
        if call.name == 'send_message':
            content = str(tool_input.get('content') or '').strip()
            reply_to_ref = self._normalize_message_ref(tool_input.get('reply_to_id'))
            reply_target = None
            if reply_to_ref:
                reply_target = self._lookup_message_ref(scope_type, scope_id, reply_to_ref)
                if not reply_target or reply_target.get('message_id') in (None, ''):
                    return f'找不到消息短ID {reply_to_ref} 对应的原始消息，无法回复。'
            action_key = self._normalize_live_send_action_key(
                content,
                reply_target.get('message_id') if reply_target else None,
            )
            if live_send_action_ledger is not None and action_key and action_key in live_send_action_ledger:
                return (
                    '这条内容刚刚已经发过了，系统拦截了本次重复发送。'
                    '不要重复发送同样的话；要补充新内容请换一句，'
                    '没有新内容要说就调用 stay_silent 结束本回合。'
                )
            # 先占坑再发送：_send_text_lines 是逐行发的，如果发到第 3 行才失败，
            # 前两行已经真的发出去了；此时若还没落账，重试会把整条内容再发一遍。
            # 因此改为发送前预占，仅在“一条都没发出去”时释放，保留全失败可重试。
            action_reserved = False
            if live_send_action_ledger is not None and action_key:
                live_send_action_ledger.add(action_key)
                action_reserved = True

            # sent_entries 跨同一回合的多次 send_message 共享，用长度差判断本次是否真发出。
            sent_entries_before = len(sent_entries)

            def _release_action_key() -> None:
                if action_reserved and live_send_action_ledger is not None and action_key:
                    live_send_action_ledger.discard(action_key)

            def _checkpoint_sent_entry(entry: dict) -> None:
                entry = self._register_turn_message_ref(scope_type, scope_id, dict(entry))
                sent_entries.append(dict(entry))
                persisted_entry = self._build_outbound_message_entry(
                    str(entry.get('text') or ''),
                    timestamp=time.time(),
                    message_id=entry.get('message_id'),
                    source_label='AI-send_message',
                    raw_message=entry.get('raw_message'),
                )
                if entry.get('message_ref'):
                    persisted_entry['message_ref'] = entry['message_ref']
                checkpoint_entry = dict(persisted_entry)
                try:
                    self._append_outbound_message_now(scope_type, scope_id, persisted_entry)
                    checkpoint_entry['_history_committed'] = True
                except Exception as _persist_exc:
                    checkpoint_entry['_history_committed'] = False
                    warn(f'[AI][persist] 即时持久化消息失败（忽略）: {_persist_exc}')
                checkpointed_outbound_entries.append(checkpoint_entry)

            try:
                if reply_target:
                    entries = self._send_scope_message_with_reply(
                        message,
                        content,
                        reply_target.get('message_id'),
                        on_sent_entry=_checkpoint_sent_entry,
                    )
                else:
                    entries = self._send_scope_message(
                        message,
                        content,
                        on_sent_entry=_checkpoint_sent_entry,
                    )
            except Exception:
                # 一条都没发出去才允许重试；已经发出部分行的必须保住占坑，
                # 否则重试会把已送达的内容再发一遍。
                if len(sent_entries) == sent_entries_before:
                    _release_action_key()
                raise
            if not entries:
                _release_action_key()
                return '内容为空或清理后为空，未发送。'
            ids = ', '.join(str(entry['message_id']) for entry in entries if entry.get('message_id') is not None)
            refs = ', '.join(f"#{entry['message_ref']}" for entry in entries if entry.get('message_ref'))
            suffix = f'，message_id: {ids}' if ids else ''
            suffix += f'，短ID: {refs}' if refs else ''
            # 关键修复：在返回结果中包含实际发送的内容，避免 AI 因看不到自己刚发的消息而重复发送
            sent_text = '\n'.join(entry['text'] for entry in entries)
            if sum(len(str(entry.get('text') or '')) for entry in entries) > 20:
                self._mark_send_message_persona_notice(scope_type, scope_id, agent_id)
            result_text = f'已发送 {len(entries)} 条消息{suffix}。发送内容：\n{sent_text}'
            # 【工具调用记录】即时记录 send_message 工具使用，防止崩溃丢失工具调用上下文
            self.tools.record_tool_use(
                scope_type,
                scope_id,
                agent_id,
                'send_message',
                content,
                result_text,
                limit=self.config.history_limit,
            )
            return result_text
        if call.name == 'recall_message':
            message_ref = self._normalize_message_ref(tool_input.get('message_ref'))
            message_id = tool_input.get('message_id')
            if message_ref:
                target = self._lookup_message_ref(scope_type, scope_id, message_ref)
                if not target or target.get('message_id') in (None, ''):
                    return f'找不到消息短ID {message_ref} 对应的原始消息，无法撤回。'
                message_id = target.get('message_id')
            if not message_id:
                return '缺少 message_id，无法撤回。'
            try:
                self.bot.recall_message(message_id, scope_type, int(scope_id))
                # 撤回成功后，同步从本轮已发送列表中剔除，避免收尾时再次补写进历史
                sent_entries[:] = [e for e in sent_entries if str(e.get('message_id')) != str(message_id)]
                if checkpointed_outbound_entries is not None:
                    checkpointed_outbound_entries[:] = [
                        e for e in checkpointed_outbound_entries 
                        if str(e.get('message_id')) != str(message_id)
                    ]
            except Exception as exc:
                return f'撤回失败: {exc}'
            if message_ref:
                return f'已撤回消息 #{message_ref}（message_id: {message_id}）。'
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
        if call.name in {'create_task', 'create_tasker'} and allow_tasks:
            if call.name == 'create_tasker':
                kind = 'dev_agent'  # legacy persisted kind
                content = str(tool_input.get('payload') or tool_input.get('task') or '').strip()
                if tool_input.get('github_repo'):
                    content = json.dumps({'task': content, 'github_repo': tool_input.get('github_repo')}, ensure_ascii=False)
            else:
                kind = self._normalize_task_kind(tool_input.get('kind'))
                content = str(tool_input.get('payload') or '').strip()
            if not kind:
                return '缺少任务类型，未创建。'
            if kind == 'dev_agent' and not self._is_dev_agent_authorized(scope_type, scope_id):
                result = '当前私聊不是管理员账号，无权发起 tasker。'
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
            task = self.tools.create_tasker(agent_id, payload) if kind == 'dev_agent' else self.tools.create_task(agent_id, kind, payload)
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
        _known_restricted = {'notify_master', 'create_task', 'create_tasker'}
        if call.name in _known_restricted:
            return f'工具 {call.name} 当前未启用（权限开关关闭），本次未执行。'
        return f'未知或不可用的工具: {call.name}，本次未执行。'

    def _build_pending_fold_reminder(self, pending: dict) -> str:
        pending_items = pending.get('batch_items') or [pending]
        reminder_entries: list[dict] = []
        for item in pending_items:
            trigger_messages = item.get('trigger_messages') or []
            if trigger_messages:
                reminder_entries.extend(copy.deepcopy(trigger_messages))
                continue
            message = item.get('message')
            if isinstance(message, ChatMessage):
                reminder_entries.append(
                    self._build_trigger_message_entry(
                        message,
                        item.get('cleaned') or message.text,
                    )
                )
        reminder_entries = self._dedupe_trigger_message_entries(reminder_entries)

        if reminder_entries:
            body = self._render_pending_user_segment(reminder_entries)
        else:
            cleaned = str(pending.get('cleaned') or '').strip()
            body = cleaned or '(内容为空)'
        return (
            '补充提醒：生成过程中又收到以下新内容，请把它们和本轮工具结果一起纳入决策：\n'
            f'{body}\n'
            '如果你之前准备发的内容因此过时、冲突或需要改口，可以直接调整；'
            '如果之前已经发出的内容因此需要撤回，可以调用 recall_message。'
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
        run_epoch = self._resolve_message_epoch(item.get('message_epoch'))
        if self._is_epoch_stale(run_epoch):
            return
        task_id = item['task_id']
        task = self.repo.get_task(task_id)
        if not task:
            return
        if task.get('status') in {'done', 'failed'}:
            return

        _task_kind = task.get('kind', '?')
        _task_kind_label = self._task_kind_label(_task_kind)
        _task_start = time.perf_counter()
        _task_scope = str(task.get('origin_scope') or '')
        info(
            f'[AI][task] start task_id={task_id} kind={_task_kind_label} '
            f'source={task.get("source_agent", "?")} '
            f'status={task.get("status", "?")}'
        )
        get_bot_logger().info(CAT_TASK, _task_scope, f'任务开始: task_id={task_id} kind={_task_kind_label} source={task.get("source_agent", "?")}')

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
            self.repo.update_task(task_id, 'failed', f'{type(exc).__name__}: {exc}')
            _task_ms = int((time.perf_counter() - _task_start) * 1000)
            warn(f'[AI][task] error task_id={task_id} kind={_task_kind_label} ms={_task_ms} error={exc}')
            get_bot_logger().error(CAT_TASK, _task_scope, f'任务异常: task_id={task_id} kind={_task_kind_label} ms={_task_ms}ms error={type(exc).__name__}: {exc}')
        finally:
            # 覆盖正常返回/异常/prereserved 等所有退出路径，确保占用的锁被释放。
            _task_ms = int((time.perf_counter() - _task_start) * 1000)
            info(
                f'[AI][task] done task_id={task_id} kind={_task_kind_label} ms={_task_ms}'
            )
            get_bot_logger().info(CAT_TASK, _task_scope, f'任务完成: task_id={task_id} kind={_task_kind_label} ms={_task_ms}ms')
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
            try:
                result = await self._handle_summarize_diary(task)
            except Exception as exc:
                result = f'总结 diary 失败: {type(exc).__name__}: {exc}'
                self.repo.update_task(task_id, 'failed', result)
                return
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'meta_summarize_diary':
            try:
                result = await self._handle_meta_summarize_diary(task)
            except Exception as exc:
                result = f'元总结失败: {type(exc).__name__}: {exc}'
                self.repo.update_task(task_id, 'failed', result)
                return
            self.repo.update_task(task_id, 'done', result)
            return

        if kind == 'intelligence_round':
            result = await self._handle_intelligence_round(task)
            self.repo.update_task(task_id, 'done', result)
            return

        # Legacy persisted kind is dev_agent; accept tasker defensively for imported/new records.
        if kind in {'dev_agent', 'tasker'}:
            await self._run_dev_agent_task(task)
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

    @staticmethod
    def _strip_trigger_entries_from_history(history: list, trigger_messages: list) -> list:
        """从 diary 展开的历史中剔除本次触发消息条目。

        按 message_id 精确匹配：diary 快照可能已包含触发消息（否则按条数
        切尾会重复渲染），也可能完全不包含（否则按条数切尾会误删用户消息）。
        触发消息没有 message_id（系统事件）时回退旧语义：按条数切尾部。
        """
        trigger_ids = {
            str(e.get('message_id') or '')
            for e in trigger_messages
            if str(e.get('message_id') or '')
        }
        if trigger_ids:
            return [
                dict(entry) for entry in history
                if str(entry.get('message_id') or '') not in trigger_ids
            ]
        if trigger_messages and len(history) >= len(trigger_messages):
            return history[:-len(trigger_messages)]
        return history

    async def _maybe_schedule_diary_summarization(self, scope_type: str, scope_id: str):
        pending = await asyncio.to_thread(self.repo.get_pending_diary, scope_type, scope_id)
        if not pending:
            return
        agent_id = self.repo.get_or_create_agent(scope_type, scope_id).agent_id
        payload = {'scope_type': scope_type, 'scope_id': scope_id, 'diary_index': pending['index']}
        task, created = await asyncio.to_thread(
            self.repo.create_unique_task,
            agent_id,
            'summarize_diary',
            payload,
            ('scope_type', 'scope_id', 'diary_index'),
        )
        if not created:
            return
        task_id = task.task_id if hasattr(task, 'task_id') else task['task_id']
        await self.queue.put({'kind': 'task', 'task_id': task_id, 'message_epoch': self._message_epoch})

    async def _handle_summarize_diary(self, task: dict) -> str:
        payload = task.get('payload') or {}
        scope_type = str(payload.get('scope_type') or '').strip()
        scope_id = str(payload.get('scope_id') or '').strip()
        diary_index = int(payload.get('diary_index') or 0)
        if not scope_type or not scope_id:
            raise RuntimeError('缺少 scope 参数。')
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
                scope_key=self._scope_key(scope_type, scope_id),
                execution_pool=self._background_pool,
            )
        except Exception as exc:
            error(f'[AI][diary] summarize failed scope={scope_type}:{scope_id} index={diary_index} error={exc}')
            raise RuntimeError(f'总结 diary #{diary_index} 失败: {exc}') from exc
        summary = (reply.text if reply else '').strip()
        if not summary:
            raise RuntimeError(f'diary #{diary_index} 总结结果为空。')
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
        payload = {'scope_type': scope_type, 'scope_id': scope_id}
        task, created = await asyncio.to_thread(
            self.repo.create_unique_task,
            agent_id,
            'meta_summarize_diary',
            payload,
            ('scope_type', 'scope_id'),
        )
        if not created:
            return
        task_id = task.task_id if hasattr(task, 'task_id') else task['task_id']
        await self.queue.put({'kind': 'task', 'task_id': task_id, 'message_epoch': self._message_epoch})

    async def _handle_meta_summarize_diary(self, task: dict) -> str:
        """将最旧的 50 条日记摘要合并为一条元总结。"""
        payload = task.get('payload') or {}
        scope_type = str(payload.get('scope_type') or '').strip()
        scope_id = str(payload.get('scope_id') or '').strip()
        if not scope_type or not scope_id:
            raise RuntimeError('缺少 scope 参数。')
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
                scope_key=self._scope_key(scope_type, scope_id),
                execution_pool=self._background_pool,
            )
        except Exception as exc:
            error(f'[AI][diary] meta-summarize failed scope={scope_type}:{scope_id} error={exc}')
            raise RuntimeError(f'元总结失败: {exc}') from exc
        summary = (reply.text if reply else '').strip()
        if not summary:
            raise RuntimeError('元总结结果为空。')
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
            tool_context = self._normalize_tool_context_messages(msg.get('tool_context_messages'))
            for context_msg in tool_context:
                role = str(context_msg.get('role') or 'unknown')
                content = context_msg.get('content')
                if isinstance(content, list):
                    chunks = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get('type') == 'tool_use':
                            chunks.append(f"调用工具 {block.get('name')}: {block.get('input')}")
                        elif block.get('type') == 'tool_result':
                            chunks.append(f"工具结果: {block.get('content')}")
                        elif block.get('text'):
                            chunks.append(str(block.get('text')))
                    content = '；'.join(chunks)
                content = str(content or '').strip()[:500]
                if content:
                    lines.append(f'[{src}/{role}/工具上下文]: {content}')
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

    def _normalize_message_ref(self, value) -> str:
        text = ''.join(ch for ch in str(value or '').upper() if ch in string.digits + string.ascii_uppercase)
        return text[:4]

    def _message_ref_alphabet(self) -> str:
        return string.digits + string.ascii_uppercase

    def _encode_message_ref_number(self, number: int, length: int = 4) -> str:
        alphabet = self._message_ref_alphabet()
        base = len(alphabet)
        value = max(0, int(number))
        chars: list[str] = []
        for _ in range(max(1, length)):
            value, remainder = divmod(value, base)
            chars.append(alphabet[remainder])
        encoded = ''.join(reversed(chars))
        if len(encoded) < length:
            encoded = (alphabet[0] * (length - len(encoded))) + encoded
        return encoded[-length:]

    def _compute_message_ref(self, scope_type: str, scope_id: str, item: dict, used_refs: set[str] | None = None) -> str:
        used_refs = used_refs or set()
        raw_seed = (
            f'{scope_type}:{scope_id}:'
            f'{item.get("message_id") or ""}:'
            f'{item.get("timestamp") or ""}:'
            f'{item.get("raw_message") or item.get("text") or ""}'
        )
        for salt in range(512):
            digest = zlib.crc32(f'{raw_seed}|{salt}'.encode('utf-8')) & 0xFFFFFFFF
            ref = self._encode_message_ref_number(digest, 4)
            if ref not in used_refs:
                return ref
        return self._encode_message_ref_number(zlib.crc32(raw_seed.encode('utf-8')) & 0xFFFFFFFF, 4)

    def _annotate_message_refs(self, scope_type: str, scope_id: str, items: list[dict]) -> tuple[list[dict], dict[str, dict]]:
        annotated: list[dict] = []
        ref_map: dict[str, dict] = {}
        used_refs: set[str] = set()
        for item in items or []:
            copied = dict(item or {})
            message_id = copied.get('message_id')
            if message_id not in (None, ''):
                ref = self._normalize_message_ref(copied.get('message_ref'))
                if not ref or ref in used_refs:
                    ref = self._compute_message_ref(scope_type, scope_id, copied, used_refs)
                copied['message_ref'] = ref
                used_refs.add(ref)
                ref_map[ref] = {
                    'message_ref': ref,
                    'message_id': message_id,
                    'text': str(copied.get('text') or ''),
                    'raw_message': str(copied.get('raw_message') or copied.get('text') or ''),
                    'image_refs': self._extract_image_refs(str(copied.get('raw_message') or '')),
                    'timestamp': copied.get('timestamp'),
                    'user_id': copied.get('user_id'),
                    'nickname': copied.get('nickname'),
                }
            annotated.append(copied)
        return annotated, ref_map

    def _prepare_visible_message_refs(
        self,
        scope_type: str,
        scope_id: str,
        history: list[dict],
        trigger_messages: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        combined = [dict(item) for item in (history or [])] + [dict(item) for item in (trigger_messages or [])]
        annotated, ref_map = self._annotate_message_refs(scope_type, scope_id, combined)
        scope_key = self._scope_key(scope_type, scope_id)
        if not hasattr(self, '_turn_message_ref_maps'):
            self._turn_message_ref_maps = {}
        if ref_map:
            self._turn_message_ref_maps[scope_key] = ref_map
        else:
            self._turn_message_ref_maps.pop(scope_key, None)
        history_count = len(history or [])
        return annotated[:history_count], annotated[history_count:]

    def _register_turn_message_ref(self, scope_type: str, scope_id: str, entry: dict) -> dict:
        copied = dict(entry or {})
        message_id = copied.get('message_id')
        if message_id in (None, ''):
            return copied
        scope_key = self._scope_key(scope_type, scope_id)
        ref_map = getattr(self, '_turn_message_ref_maps', None)
        if ref_map is None:
            self._turn_message_ref_maps = {}
            ref_map = self._turn_message_ref_maps
        current_map = dict(ref_map.get(scope_key) or {})
        used_refs = set(current_map.keys())
        ref = self._normalize_message_ref(copied.get('message_ref'))
        if not ref or ref in used_refs:
            ref = self._compute_message_ref(scope_type, scope_id, copied, used_refs)
        copied['message_ref'] = ref
        current_map[ref] = {
            'message_ref': ref,
            'message_id': message_id,
            'text': str(copied.get('text') or ''),
            'raw_message': str(copied.get('raw_message') or copied.get('text') or ''),
            'image_refs': self._extract_image_refs(str(copied.get('raw_message') or '')),
            'timestamp': copied.get('timestamp'),
            'user_id': copied.get('user_id'),
            'nickname': copied.get('nickname'),
        }
        self._turn_message_ref_maps[scope_key] = current_map
        return copied

    def _register_persistent_message_ref(self, scope_type: str, scope_id: str, entry: dict) -> dict:
        copied = dict(entry or {})
        if copied.get('message_id') in (None, ''):
            return copied
        scope_key = self._scope_key(scope_type, scope_id)
        if not hasattr(self, '_turn_message_ref_maps'):
            self._turn_message_ref_maps = {}
        current_map = dict(self._turn_message_ref_maps.get(scope_key) or {})
        if not current_map:
            try:
                history = self.repo.list_messages(scope_type, scope_id)
            except Exception:
                history = []
            _annotated, history_map = self._annotate_message_refs(scope_type, scope_id, history)
            current_map.update({ref: dict(meta) for ref, meta in history_map.items()})
            self._turn_message_ref_maps[scope_key] = current_map
        return self._register_turn_message_ref(scope_type, scope_id, copied)

    def _lookup_message_ref(self, scope_type: str, scope_id: str, message_ref: str) -> dict | None:
        normalized = self._normalize_message_ref(message_ref)
        if not normalized:
            return None
        scope_key = self._scope_key(scope_type, scope_id)
        current_map = getattr(self, '_turn_message_ref_maps', {}).get(scope_key) or {}
        if normalized in current_map:
            return dict(current_map[normalized])
        try:
            history = self.repo.list_messages(scope_type, scope_id)
        except Exception:
            history = []
        _annotated, ref_map = self._annotate_message_refs(scope_type, scope_id, history)
        found = ref_map.get(normalized)
        if found:
            if not hasattr(self, '_turn_message_ref_maps'):
                self._turn_message_ref_maps = {}
            merged_map = dict(current_map)
            merged_map.update({ref: dict(meta) for ref, meta in ref_map.items()})
            self._turn_message_ref_maps[scope_key] = merged_map
        return dict(found) if found else None

    def _summarize_image_refs(self, refs: list[str]) -> str:
        preview = []
        for ref in refs[:3]:
            if len(ref) > 96:
                preview.append(ref[:93] + '...')
            else:
                preview.append(ref)
        return ' | '.join(preview)

    def _master_system_prompt(self) -> str:
        return self.prompt_store.main_system_prompt() + self._identity_prompt_block()

    def _build_master_prompt(self, task: dict) -> str:
        payload = task.get('payload') or {}
        request_type = str(payload.get('request_type') or '').strip()
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
            if rel.get('province'):
                parts.append(f"省份:{rel['province']}")
            if rel.get('impression'):
                parts.append(f"印象:{str(rel['impression'])[:50]}")
            if rel['affinity'] != 0:
                parts.append(f"好感度{rel['affinity']}")
            if rel['admin_note']:
                parts.append(f"备注:{rel['admin_note']}")
            user_relation_lines.append(' | '.join(parts))

        special_guidance = (
            "如果这是跨会话联系请求，优先用 create_task 创建 delegate_to_child 任务，而不是直接 message_scope。"
            "如果这是进度追问，优先依据已有事实回传，不要猜测对方态度。"
            "你输出的普通文字会作为补充情报回传给来源子AI。"
        )
        if request_type == 'knowledge_base_suggestion':
            special_guidance += (
                "如果这是知识库补充请求，你必须明确判断采纳还是拒绝。"
                "采纳时先调用 manage_knowledge_base 完成实际变更；必要时把对应知识库挂载到来源会话。"
                "无论采纳还是拒绝，最后都必须用普通文字明确告知来源子AI结果："
                "是否批准、加到了哪个知识库/哪条、或者为什么拒绝。"
            )

        return (
            f"【当前时间: {self._now_text()}】\n"
            f"来源下级AI: {task.get('source_agent')}\n"
            f"任务类型: {self._task_kind_label(task.get('kind'))}\n"
            f"请求载荷: {json.dumps(payload, ensure_ascii=False)}\n\n"
            f"可能相关的人物/会话:\n{chr(10).join(candidate_lines) if candidate_lines else '暂无'}\n\n"
            f"私聊好友列表:\n{chr(10).join(friend_lines) if friend_lines else '暂无'}\n\n"
            f"群聊列表:\n{chr(10).join(group_lines) if group_lines else '暂无'}\n\n"
            f"群聊/私聊关系网（好感度、关联度、备注）:\n{chr(10).join(scope_relation_lines) if scope_relation_lines else '暂无'}\n\n"
            f"用户关系网（好感度、备注）:\n{chr(10).join(user_relation_lines) if user_relation_lines else '暂无'}\n\n"
            f"{self._build_knowledge_admin_summary()}\n\n"
            f"主AI备忘（带时间标记，注意判断时效性）:\n{chr(10).join(note_lines) if note_lines else '暂无'}\n\n"
            f"{special_guidance}"
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
            include_qq_request_management=True,
            include_relation_read=True,
            include_relation_write=True,
            include_knowledge_management=True,
        )
        created_task_ids = []
        try:
            for _ in range(5):
                master_reply = await self._complete_chat(
                    self._static_system_blocks(self._master_system_prompt()),
                    master_messages,
                    master_tools,
                    0.2,
                    scope_key=self._scope_key('master', '0'),
                )
                if not master_reply:
                    break
                loop_calls = [call for call in master_reply.tool_calls if call.name in LOOP_TOOL_NAMES]
                result_blocks = []
                mixed_tool_batch = len(master_reply.tool_calls) > 1
                for call in master_reply.tool_calls:
                    if call.name in LOOP_TOOL_NAMES:
                        result = await self._run_ai_tool_call('master', 'global', 'master:global', call.name, call.input)
                    elif call.name == 'remember':
                        note = str(dict(call.input or {}).get('note') or '').strip()
                        if note:
                            self.repo.add_note('master', 'global', note)
                            result = '已记录备忘。'
                        else:
                            result = '内容为空，未记录。'
                    elif call.name in {'create_task', 'create_tasker'}:
                        # DIRECTIVE 工具在循环内立即执行并回填结果：不能像 LOOP 一样跳过
                        # 再在循环后补执行，否则多轮混合时（如 [查询工具, create_task] 同批、
                        # 下一轮才结束），中间轮的 create_task 会静默丢失，主 AI 误以为已建任务。
                        tool_input = dict(call.input or {})
                        if call.name == 'create_tasker':
                            kind = 'dev_agent'  # legacy persisted kind
                            task_content = str(tool_input.get('payload') or tool_input.get('task') or '')
                            if tool_input.get('github_repo'):
                                task_content = json.dumps({'task': task_content, 'github_repo': tool_input.get('github_repo')}, ensure_ascii=False)
                        else:
                            kind = self._normalize_task_kind(tool_input.get('kind'))
                            task_content = str(tool_input.get('payload') or '')
                        if not kind:
                            result = '缺少任务类型，未创建。'
                        elif kind == 'dev_agent' and not self._is_dev_agent_authorized(
                            str(payload.get('scope_type') or ''), str(payload.get('scope_id') or ''),
                        ):
                            self.repo.add_note(
                                'master',
                                'global',
                                f"拒绝了来自非管理员私聊的 tasker 请求 scope={payload.get('scope_type')}:{payload.get('scope_id')}",
                            )
                            result = '已拒绝该 tasker 请求（非管理员私聊）。'
                        else:
                            child_task = self.tools.create_task(
                                'master:global',
                                kind,
                                self._normalize_task_payload(
                                    task_content,
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
                            self._submit_runtime_task(child_task.task_id)
                            result = f'已创建任务 {child_task.task_id}。'
                    else:
                        result = '本轮先处理查询/更新类工具，这个操作未执行；如仍需要，请在工具结果后再次调用。'
                    result_blocks.append({'type': 'tool_result', 'tool_use_id': call.call_id, 'content': self._format_tool_result_content(call.name, result, mixed_batch=mixed_tool_batch)})
                # 同 5734：回传的 assistant 消息必须保留 thinking block。
                master_messages.append({'role': 'assistant', 'content': copy.deepcopy(master_reply.raw_content)})
                master_messages.append({'role': 'user', 'content': result_blocks})
                if not loop_calls:
                    break
        except Exception as exc:
            error(f'[AI][master] {exc}')

        followup_text = (master_reply.text if master_reply else '').strip()
        if not followup_text and payload.get('request_type') == 'knowledge_base_suggestion':
            followup_text = '主AI已收到这条知识库补充请求，但这轮还没形成明确的批准或拒绝结论，请稍后重试或直接继续说明。'
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
                self._submit_runtime_task(followup_task.task_id)

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
                self._submit_runtime_task(child_task.task_id)

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
            self._submit_runtime_task(callback_task.task_id)
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
        tool_logs = None  # 审计日志不进入委派提示词，历史工具协议由聊天条目承载
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
            self._submit_runtime_task(callback_task.task_id)
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
            result = str(summary.get('result') or '').strip() or 'Tasker 已结束，但没有返回结果。'
            self.repo.update_task(task_id, status, result)
            self.repo.add_note(
                'master',
                'global',
                f'Tasker 完成 [{task_id}] ({status}): {task_desc}\n结果: {result}',
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
                role_model_config = self.model_manager.get_role_model('tasker')
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
                    prompt_path=self.config.tasker_prompt_path,
                    on_finished=finish_trigger,
                    token_usage_store=self.token_usage_store,
                )
            except Exception as exc:
                result = f'Tasker 执行异常: {exc}'
        if not delivery_done:
            await finish_trigger(
                {
                    'status': 'failed' if result.startswith('Tasker 执行异常') else 'done',
                    'result': result,
                }
            )

    async def _report_child_result(self, source_agent: str, payload: dict):
        report_task = self.tools.create_task(source_agent, 'child_report', payload)
        self._submit_runtime_task(report_task.task_id)

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
        tool_logs: list[dict] | None,
        callback_only: bool,
        followup_only: bool,
        global_identity_context: str,
        intel_query: bool = False,
    ) -> str:
        history_lines = self._format_history_for_prompt(history)
        knowledge_lines = self._default_knowledge_lines()
        mounted_knowledge_lines = self._build_mounted_knowledge_prompt_lines(target_scope_type, str(target_scope_id))
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
        mounted_section = ''
        if mounted_knowledge_lines:
            mounted_section = (
                "已挂载知识库（这些是当前会话额外可引用的上下文，不等于号主本人事实）:\n"
                + '\n'.join(mounted_knowledge_lines)
                + '\n\n'
            )
        return (
            f"当前时间: {self._now_text()}\n"
            f"你当前负责的会话: {target_scope_type}:{target_scope_id}\n"
            f"本次临时工单: {instruction}\n"
            f"发起人: {requester_name}({requester_qq})\n\n"
            f"这个会话的长期印象:\n{impression or '暂无，先观察这个会话的用途、人物和气氛。'}\n\n"
            f"已知事实（关于号主本人，仅这些内容可以确认/复述，没写到的不要编）:\n{chr(10).join(knowledge_lines) if knowledge_lines else '暂无已录入的事实，涉及号主具体信息一律不要编造，含糊带过或反问。'}\n\n"
            f"{mounted_section}"
            f"身份分明的聊天记录:\n{chr(10).join(history_lines) if history_lines else '暂无'}\n\n"
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
            tool_context = self._normalize_tool_context_messages(item.get('tool_context_messages'))
            for context_msg in tool_context:
                content = context_msg.get('content')
                if isinstance(content, list):
                    chunks = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get('type') == 'tool_use':
                            chunks.append(f"调用工具 {block.get('name')}: {block.get('input')}")
                        elif block.get('type') == 'tool_result':
                            chunks.append(f"工具结果: {block.get('content')}")
                        elif block.get('text'):
                            chunks.append(str(block.get('text')))
                    content = '；'.join(chunks)
                content = self._short_text(str(content or '').strip(), 500)
                if content:
                    lines.append(f"{time_prefix} [AI/工具上下文/{context_msg.get('role')}]: {content}".strip())
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
                scope_key=self._scope_key(scope_type, scope_id),
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
                self._submit_runtime_task(task['task_id'])
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
            message = self._get_timed_event_messages().build_alarm_message(
                payload,
                task_id=task_id,
                text=text,
            )
            if message is not None:
                self._submit_message(message)
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
        message_id=None,
        source_label: str | None = None,
        raw_message: str | None = None,
    ) -> dict:
        item = {
            'user_id': self.bot.self_id,
            'nickname': '冰糖',
            'text': text,
            'raw_message': raw_message if raw_message is not None else text,
            'timestamp': float(timestamp if timestamp is not None else time.time()),
            'generation_ms': generation_ms,
            'think_note': self._normalize_think_note(think_note),
        }
        if message_id is not None:
            item['message_id'] = message_id
        else:
            item['_mid_missing'] = True
        if source_label:
            item['source_label'] = source_label
        normalized_tool_context = self._normalize_tool_context_messages(tool_context_messages)
        if normalized_tool_context:
            item['tool_context_messages'] = normalized_tool_context
        return item

    def _append_outbound_message_now(self, scope_type: str, scope_id: str, item: dict) -> None:
        has_pending = self.repo.append_message(
            scope_type,
            scope_id,
            item,
            self.config.history_limit,
            self.config.diary_size,
        )
        if has_pending:
            try:
                asyncio.get_running_loop().create_task(
                    self._maybe_schedule_diary_summarization(scope_type, scope_id)
                )
            except RuntimeError:
                pass
        if scope_type == 'group':
            scope_key = f'{scope_type}:{scope_id}'
            self._arm_group_reply_window(scope_key, scope_id)

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

    def _append_live_tool_checkpoint(
        self,
        scope_type: str,
        scope_id: str,
        checkpoint_id: str,
        tool_context_messages: list[dict] | None,
    ) -> dict | None:
        checkpoint_entry = self._build_live_tool_checkpoint_entry(checkpoint_id, tool_context_messages)
        if not checkpoint_entry:
            return None
        try:
            stored_entry, _has_pending = self.repo.upsert_tool_context_checkpoint(
                scope_type,
                scope_id,
                checkpoint_id,
                checkpoint_entry,
                self.config.history_limit,
                self.config.diary_size,
            )
            if _has_pending:
                try:
                    asyncio.get_running_loop().create_task(
                        self._maybe_schedule_diary_summarization(scope_type, scope_id)
                    )
                except RuntimeError:
                    pass
            return stored_entry
        except Exception as exc:
            warn(
                f'[AI][persist] live tool checkpoint failed '
                f'scope={scope_type}:{scope_id} error={exc}'
            )
            return None

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
        if self._scope_turn_is_busy(scope_key):
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

    # ── agent 巡检定时器（按 scope 共用一个，无 agent 时自清理）──────────────
    CODE_MODE_IDLE_TURN_LIMIT = 20

    AGENT_WATCH_TASK_KIND = 'agent_watch'
    AGENT_WATCH_SCHEDULE = '*/5 * * * *'
    # running/waiting/review_required/error 都还需要上级跟进；idle 是干完待命，不再占定时器。
    AGENT_WATCH_PENDING_STATUSES = ('running', 'waiting', 'review_required', 'error')

    def _find_agent_watch_task(self, scope_key: str) -> dict | None:
        """找该 scope 已有的巡检定时器；有就复用，避免每建一个 agent 开一个定时器。"""
        for task in self._recurring_tasks.values():
            if task.get('kind') != self.AGENT_WATCH_TASK_KIND:
                continue
            if str(task.get('target_scope') or '') == scope_key:
                return task
        return None

    def _scope_agent_snapshot(self, scope_key: str) -> list[dict]:
        """取该 scope 下还需要跟进的 agent 列表。"""
        manager = getattr(self, 'agent_manager', None)
        if manager is None:
            return []
        try:
            agents = manager.list_agents()
        except Exception as exc:
            warn(f'[AI][agent_watch] 读取 agent 列表失败: {exc}')
            return []
        pending = []
        for item in agents:
            if str(item.get('origin_scope') or '') != scope_key:
                continue
            if str(item.get('status') or '') not in self.AGENT_WATCH_PENDING_STATUSES:
                continue
            pending.append(item)
        return pending

    def _build_agent_watch_instruction(self, scope_key: str) -> str | None:
        """生成巡检提示；返回 None 表示该 scope 已无待跟进 agent，定时器该清理了。"""
        pending = self._scope_agent_snapshot(scope_key)
        if not pending:
            return None
        now = time.time()
        lines = [f'当前有 {len(pending)} 个 agent 在跟进中，逐个核对进度：']
        for item in pending:
            agent_id = str(item.get('agent_id') or '')
            status = str(item.get('status') or '?')
            idle_for = now - float(item.get('last_activity_at') or item.get('updated_at') or now)
            stalled = '，疑似卡住' if idle_for >= 900 else ''
            lines.append(
                f"- {agent_id[:8]} 状态={status} 静默={int(idle_for // 60)}分钟{stalled}"
                f" 任务={item.get('instruction_summary') or ''}"
            )
        lines.append(
            '用 peek_agent 看需要细究的那几个，然后把这一批的整体进展合并成一条消息汇报给用户，不要一个 agent 发一条。'
        )
        lines.append(
            'waiting 表示它在等你答复，用 send_to_agent 回它；error 或长时间静默的，判断是重发指令还是 destroy_agent 收掉。'
        )
        return '\n'.join(lines)

    def _ensure_agent_watch_timer(self, scope_type: str, scope_id) -> str:
        """确保该 scope 有且只有一个 agent 巡检定时器，返回给模型看的说明。"""
        if not scope_type or str(scope_id) == '':
            return ''
        scope_key = f'{scope_type}:{scope_id}'
        existing = self._find_agent_watch_task(scope_key)
        if existing is not None:
            if not existing.get('enabled'):
                existing['enabled'] = True
                existing.pop('schedule_error', None)
                try:
                    existing['next_run'] = self._calc_next_cron_run(existing.get('schedule') or self.AGENT_WATCH_SCHEDULE)
                except Exception:
                    existing['next_run'] = time.time() + 300
                self._save_recurring_tasks()
            return f"已复用现有巡检定时器（{existing.get('schedule')}），本 agent 会并入同一条汇报。"
        import uuid as _uuid
        try:
            next_run = self._calc_next_cron_run(self.AGENT_WATCH_SCHEDULE)
        except Exception as exc:
            warn(f'[AI][agent_watch] 定时器创建失败: {exc}')
            return ''
        task_id = str(_uuid.uuid4())
        self._recurring_tasks[task_id] = {
            'id': task_id,
            'kind': self.AGENT_WATCH_TASK_KIND,
            'schedule': self.AGENT_WATCH_SCHEDULE,
            'instruction': '检查本会话所有 agent 的进度并合并汇报。',
            'target_scope': scope_key,
            'enabled': True,
            'created_at': time.time(),
            'last_run': None,
            'next_run': next_run,
            'creator_scope': scope_key,
        }
        self._save_recurring_tasks()
        return f'已自动开启 agent 巡检定时器（{self.AGENT_WATCH_SCHEDULE}），所有 agent 结束后会自动关闭。'

    def _cleanup_agent_watch_timer(self, scope_key: str) -> None:
        task = self._find_agent_watch_task(scope_key)
        if task is None:
            return
        self._recurring_tasks.pop(str(task.get('id') or ''), None)
        self._save_recurring_tasks()
        info(f'[AI][agent_watch] {scope_key} 已无待跟进 agent，巡检定时器已清理')

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

    def _resolve_recurring_task_key(self, raw_id: str) -> tuple[str, str]:
        """按完整 ID 或唯一前缀解析循环任务 key。

        list_recurring_tasks 只展示 id 前 8 位，模型拿缩写来 delete/update 时，
        如果这里只做完整匹配就会一直“任务不存在”，表现为删不掉。
        返回 (status, detail)：('ok', key) / ('not_found', 消息) / ('ambiguous', 消息)。
        """
        raw_id = str(raw_id or '').strip()
        if not raw_id:
            return 'not_found', 'task_id 不能为空'
        if raw_id in self._recurring_tasks:
            return 'ok', raw_id
        matches = [key for key in self._recurring_tasks if key.startswith(raw_id)]
        if len(matches) == 1:
            return 'ok', matches[0]
        if len(matches) > 1:
            return 'ambiguous', (
                f'有 {len(matches)} 个任务以 {raw_id} 开头，'
                '请提供更完整的 task_id（可用 list_recurring_tasks 查看）'
            )
        return 'not_found', f'任务 {raw_id} 不存在'

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
                    if now < next_run:
                        continue
                    # 先计算下一次运行时间：无效 cron 时 next_run 不推进会导致
                    # 每 30 秒重复触发同一任务，这里直接停用并记录错误。
                    try:
                        next_after = self._calc_next_cron_run(task['schedule'], now)
                    except Exception as exc:
                        task['enabled'] = False
                        task['schedule_error'] = f'invalid cron: {exc}'
                        changed = True
                        warn(f"[AI][recurring] task {task.get('id','?')[:8]} 无效 cron schedule，已停用: {exc}")
                        continue
                    try:
                        ok = self._trigger_recurring_task(task)
                        task['last_run'] = now
                        task['next_run'] = next_after
                        changed = True
                        info(f"[AI][recurring] triggered task {task['id'][:8]}, next={task['next_run']:.0f}")
                        if not ok:
                            # 目标 scope 无效导致消息无法投递：任务永远无法成功执行，停用防抖。
                            task['enabled'] = False
                            task['schedule_error'] = 'invalid target_scope'
                            warn(f"[AI][recurring] task {task['id'][:8]} target_scope 无效，已停用")
                    except Exception as e:
                        error(f"[AI][recurring] trigger error task={task.get('id','?')}: {e}")
                if changed:
                    self._save_recurring_tasks()
            except asyncio.CancelledError:
                break
            except Exception as e:
                error(f'[AI][recurring] scheduler error: {e}')

    def _trigger_recurring_task(self, task: dict) -> bool:
        """触发循环任务；返回 True 表示已成功投递，False 表示配置无效未投递。"""
        if task.get('kind') == self.AGENT_WATCH_TASK_KIND:
            scope_key = str(task.get('target_scope') or task.get('creator_scope') or '')
            # 巡检提示带上触发瞬间的真实状态，模型不用先 list_agents 再 peek 才知道该看谁。
            instruction = self._build_agent_watch_instruction(scope_key)
            if instruction is None:
                self._cleanup_agent_watch_timer(scope_key)
                return True
            task = {**task, 'instruction': instruction}
        message = self._get_timed_event_messages().build_recurring_message(task)
        if message is None:
            target_scope = task.get('target_scope') or task.get('creator_scope', '')
            warn(f"[AI][recurring] invalid target_scope: {target_scope}")
            return False
        self._submit_message(message)
        return True

    # ── 静默巡检（每会话10分钟无真实用户消息后发一次情报上报提示） ──────────
    async def _silence_report_scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                now = time.time()
                for scope_key, last_at in list(self._scope_last_user_msg_at.items()):
                    if scope_key in self._scope_silence_fired:
                        continue
                    if now - last_at < self._silence_report_window:
                        continue
                    
                    current_level = self._scope_thinking_levels.get(scope_key, 'low')
                    if current_level != 'low':
                        self._scope_thinking_levels[scope_key] = 'low'
                        info(f'[AI][thinking] 静默重置 scope={scope_key} thinking_level=low')
                    
                    try:
                        scope_type, scope_id = scope_key.split(':', 1)
                    except ValueError:
                        continue
                    if scope_type == 'master':
                        continue
                    text = (
                        '【静默巡检】本会话已超过10分钟没有新消息。'
                        '请回顾本会话最近的聊天内容，提取可沉淀的人物情报（省份、职业、性别、爱好、性格印象、关系态度等），'
                        '通过 notify_master 上报主AI 归档。若无值得上报的情报，调用 stay_silent 结束本轮。'
                    )
                    message = self._get_timed_event_messages().build_silence_report_message(
                        scope_type, scope_id, text=text,
                    )
                    if message is not None:
                        self._scope_silence_fired.add(scope_key)
                        self._submit_message(message)
                        info(f'[AI][silence] 静默上报提示已发送 scope={scope_key}')
            except asyncio.CancelledError:
                break
            except Exception as e:
                error(f'[AI][silence] scheduler error: {e}')

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
                scope_key=self._scope_key('master', '0'),
                execution_pool=self._background_pool,
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
