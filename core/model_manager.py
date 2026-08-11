"""三级模型配置管理器：上游 → 渠道 → 角色

JSON 结构:
{
  "upstreams": [{"name", "base_url", "api_key", "messages_path"}],
  "channels":  [{"name", "strategy": "fallback|fallback_reset|random|roundrobin", "models": [{"upstream", "model_id"}]}],
  "roles":     {"main": "渠道名", "tiered": "渠道名", "agent": "渠道名", "tasker": "渠道名", "vision": "渠道名"}
}

Persistent key ``dev_agent`` remains a supported legacy alias for ``tasker``.
"""

import json
import random as _random
from pathlib import Path
from typing import Optional

from pack.console_logger import error, info, warn

ROLE_LABELS = {
    'main': '主AI',
    'tiered': '分级AI',
    'tiered_chat': '分级·聊天',
    'tiered_exec': '分级·执行',
    'tiered_decision': '分级·决策',
    'agent': 'Agent',
    'tasker': 'Tasker',
    'vision': '视觉',
}
# 分级 AI 三渠道的回退链：各自绑定渠道 → tiered → main。
TIER_ROLE_FALLBACK = {
    'tiered_chat': 'tiered',
    'tiered_exec': 'tiered',
    'tiered_decision': 'tiered',
}
VALID_CHANNEL_STRATEGIES = {'fallback', 'fallback_reset', 'random', 'roundrobin'}
VALID_UPSTREAM_PROTOCOLS = {'anthropic', 'completions', 'responses'}
UPSTREAM_PROTOCOL_PATHS = {
    'anthropic': '/messages',
    'completions': '/chat/completions',
    'responses': '/responses',
}


class ModelManager:
    def __init__(self, config_path: str = 'data/models_config.json'):
        self.config_path = Path(config_path)
        self.config: dict = {}
        # 运行时状态（不持久化）
        self._rr_counters: dict[str, int] = {}      # round-robin 计数器，key=渠道名
        self._fb_indexes: dict[str, int] = {}        # fallback 当前索引，key=渠道名
        self._request_fb_indexes: dict[str, int] = {}  # fallback_reset 当前请求内索引，key=渠道名
        self._load_and_migrate()

    # ─────────────────────────── 加载 / 保存 ───────────────────────────

    @staticmethod
    def _empty_config() -> dict:
        return {'upstreams': [], 'channels': [], 'roles': {}}

    @staticmethod
    def _normalize_strategy(strategy: str) -> str:
        value = str(strategy or 'fallback').lower()
        return value if value in VALID_CHANNEL_STRATEGIES else 'fallback'

    @staticmethod
    def normalize_upstream_protocol(protocol: str) -> str:
        value = str(protocol or '').strip().lower()
        aliases = {
            'anthropic': 'anthropic',
            'messages': 'anthropic',
            'completions': 'completions',
            'completion': 'completions',
            'chat': 'completions',
            'chat_completions': 'completions',
            'openai': 'completions',
            'responses': 'responses',
            'response': 'responses',
        }
        return aliases.get(value, '')

    @classmethod
    def protocol_from_path(cls, messages_path: str) -> str:
        path = str(messages_path or '').strip().lower()
        if '/responses' in path:
            return 'responses'
        if '/chat/completions' in path:
            return 'completions'
        return 'anthropic'

    @classmethod
    def endpoint_path_for_protocol(cls, protocol: str) -> str:
        normalized = cls.normalize_upstream_protocol(protocol) or 'anthropic'
        return UPSTREAM_PROTOCOL_PATHS[normalized]

    @classmethod
    def _normalize_upstream(cls, upstream: dict) -> tuple[dict, bool]:
        item = dict(upstream or {})
        base_url = str(item.get('base_url') or '').strip().rstrip('/')
        protocol = cls.normalize_upstream_protocol(item.get('protocol'))
        legacy_path = str(item.get('messages_path') or '').strip()
        changed = False

        if not protocol:
            protocol = cls.protocol_from_path(legacy_path)
            suffix = cls.endpoint_path_for_protocol(protocol)
            path = legacy_path.rstrip('/')
            prefix = path[:-len(suffix)] if path.lower().endswith(suffix) else ''
            if prefix and not base_url.lower().endswith(prefix.lower()):
                base_url = f'{base_url}{prefix}'
            changed = True

        if item.get('base_url') != base_url:
            item['base_url'] = base_url
            changed = True
        if item.get('protocol') != protocol:
            item['protocol'] = protocol
            changed = True
        if 'messages_path' in item:
            item.pop('messages_path', None)
            changed = True
        return item, changed

    def _load_and_migrate(self):
        if not self.config_path.exists():
            warn(f'[ModelManager] 配置文件不存在，使用空配置: {self.config_path}')
            self.config = self._empty_config()
            return
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            self.config = self._migrate(raw)
            u = len(self.config.get('upstreams') or [])
            c = len(self.config.get('channels') or [])
            info(f'[ModelManager] 已加载 upstreams={u} channels={c}')
        except Exception as exc:
            error(f'[ModelManager] 加载失败: {exc}')
            self.config = self._empty_config()

    def _migrate(self, raw: dict) -> dict:
        """将旧格式（channels含base_url字段）自动迁移到新格式"""
        if 'upstreams' in raw:
            cfg = self._empty_config()
            cfg.update(raw)
            roles = cfg.setdefault('roles', {})
            if 'tasker' not in roles and 'dev_agent' in roles:
                roles['tasker'] = roles['dev_agent']
            normalized_upstreams = []
            changed = False
            for upstream in cfg.get('upstreams') or []:
                normalized, item_changed = self._normalize_upstream(upstream)
                normalized_upstreams.append(normalized)
                changed = changed or item_changed
            cfg['upstreams'] = normalized_upstreams
            if changed:
                try:
                    self.config_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.config_path, 'w', encoding='utf-8') as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                except Exception as exc:
                    error(f'[ModelManager] 上游协议迁移写回失败: {exc}')
            return cfg

        # ── 旧格式迁移 ──
        warn('[ModelManager] 检测到旧版配置格式，自动迁移…')
        old_channels: list[dict] = raw.get('channels') or []
        upstreams = []
        for ch in old_channels:
            name = str(ch.get('name') or '').strip()
            if not name:
                continue
            upstreams.append({
                'name': name,
                'base_url': str(ch.get('base_url') or '').strip().rstrip('/'),
                'api_key': str(ch.get('api_key') or '').strip(),
                'messages_path': str(ch.get('messages_path') or '').strip(),
            })

        def _old_models_to_new(role_cfg: dict) -> list[dict]:
            result = []
            for item in (role_cfg.get('models') or []):
                try:
                    idx = int(item.get('channel'))
                except (TypeError, ValueError):
                    continue
                if idx < 0 or idx >= len(old_channels):
                    continue
                up_name = str(old_channels[idx].get('name') or '').strip()
                model_id = str(item.get('model_name') or item.get('model_id') or '').strip()
                if up_name and model_id:
                    result.append({'upstream': up_name, 'model_id': model_id})
            return result

        channels = []
        roles = {}

        for role_key, label in [('main', '主AI渠道'), ('tiered', '分级AI渠道'), ('vision', '视觉渠道')]:
            role_cfg = raw.get(role_key) or {}
            models = _old_models_to_new(role_cfg)
            if models:
                ch_name = label
                strategy = self._normalize_strategy(role_cfg.get('strategy') or 'fallback')
                channels.append({'name': ch_name, 'strategy': strategy, 'models': models})
                roles[role_key] = ch_name

        # agent / tasker 默认继承 main 渠道；dev_agent 为历史兼容键。
        if 'main' in roles:
            roles.setdefault('agent', roles['main'])
            roles.setdefault('tasker', roles.get('dev_agent', roles['main']))
            roles.setdefault('dev_agent', roles['main'])

        upstreams = [self._normalize_upstream(item)[0] for item in upstreams]
        cfg = {'upstreams': upstreams, 'channels': channels, 'roles': roles}
        # 保存迁移后的新格式
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            warn('[ModelManager] 迁移完成，已写回新格式')
        except Exception as exc:
            error(f'[ModelManager] 迁移写回失败: {exc}')
        return cfg

    def _save(self) -> tuple[bool, str]:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            return True, f'已保存到 {self.config_path}'
        except Exception as exc:
            error(f'[ModelManager] 保存失败: {exc}')
            return False, f'保存失败: {exc}'

    def reload_config(self):
        self._load_and_migrate()

    # ─────────────────────────── 查找工具 ───────────────────────────

    def _find_upstream(self, name: str) -> Optional[dict]:
        name = str(name or '').strip()
        for u in (self.config.get('upstreams') or []):
            if str(u.get('name') or '').strip() == name:
                return u
        return None

    def _find_channel(self, name: str) -> Optional[dict]:
        name = str(name or '').strip()
        for ch in (self.config.get('channels') or []):
            if str(ch.get('name') or '').strip() == name:
                return ch
        return None

    def _resolve_upstream_idx(self, target: str) -> int:
        target = str(target or '').strip()
        upstreams = self.config.get('upstreams') or []
        if target.isdigit():
            idx = int(target)
            if 0 <= idx < len(upstreams):
                return idx
        for i, u in enumerate(upstreams):
            if str(u.get('name') or '').strip() == target:
                return i
        return -1

    def _resolve_channel_idx(self, target: str) -> int:
        target = str(target or '').strip()
        channels = self.config.get('channels') or []
        if target.isdigit():
            idx = int(target)
            if 0 <= idx < len(channels):
                return idx
        for i, ch in enumerate(channels):
            if str(ch.get('name') or '').strip() == target:
                return i
        return -1

    # ─────────────────────────── 模型解析 ───────────────────────────

    def _pick_model_from_channel(self, channel: dict, *, advance: bool = True) -> Optional[dict]:
        models = channel.get('models') or []
        if not models:
            return None
        strategy = self._normalize_strategy(channel.get('strategy') or 'fallback')
        ch_name = str(channel.get('name') or '')

        if strategy == 'random':
            entry = _random.choice(models)
        elif strategy == 'roundrobin':
            idx = self._rr_counters.get(ch_name, 0) % len(models)
            if advance:
                self._rr_counters[ch_name] = idx + 1
            entry = models[idx]
        elif strategy == 'fallback_reset':
            idx = self._request_fb_indexes.get(ch_name, 0) % len(models)
            entry = models[idx]
        else:  # fallback
            idx = self._fb_indexes.get(ch_name, 0) % len(models)
            entry = models[idx]

        upstream_name = str(entry.get('upstream') or '').strip()
        model_id = str(entry.get('model_id') or '').strip()
        upstream = self._find_upstream(upstream_name)
        if not upstream or not model_id:
            return None
        protocol = self.normalize_upstream_protocol(upstream.get('protocol')) or self.protocol_from_path(upstream.get('messages_path'))
        messages_path = self.endpoint_path_for_protocol(protocol)
        return {
            'base_url': str(upstream.get('base_url') or '').strip().rstrip('/'),
            'api_key': str(upstream.get('api_key') or '').strip(),
            'model_name': model_id,
            'messages_path': messages_path,
            'protocol': 'anthropic' if protocol == 'anthropic' else 'openai',
            'display_name': f'{upstream_name}/{model_id}',
            'channel_name': ch_name,
            'upstream_name': upstream_name,
        }

    def _resolve_role_channel_name(self, role: str) -> Optional[str]:
        """解析角色绑定的渠道名（只读）。

        分级 AI 三渠道支持回退链：tiered_chat/exec/decision → tiered → main，
        这样只需 /role set tiered <渠道> 即可三渠道共用，再按需单独覆盖。
        """
        role = str(role or '').strip()
        roles = self.config.get('roles') or {}
        ch_name = str(roles.get(role) or '').strip()
        if not ch_name:
            fallback = TIER_ROLE_FALLBACK.get(role)
            if fallback:
                ch_name = str(roles.get(fallback) or '').strip()
            if not ch_name and role != 'main':
                ch_name = str(roles.get('main') or '').strip()
        return ch_name or None

    def _resolve_role_binding_detail(self, role: str) -> tuple[str, str, str]:
        """只读：解析角色渠道绑定的来源详情。

        返回 (explicit_channel, effective_channel, fallback_from)：
        - explicit_channel：角色显式绑定的渠道名（可能为空）；
        - effective_channel：回退链解析后的实际生效渠道（可能为空=未配置）；
        - fallback_from：生效渠道的回退来源角色名（显式绑定时为空）。
        """
        role = str(role or '').strip()
        roles = self.config.get('roles') or {}
        explicit = str(roles.get(role) or '').strip()
        if explicit:
            return explicit, explicit, ''
        fallback = TIER_ROLE_FALLBACK.get(role)
        if fallback:
            ch = str(roles.get(fallback) or '').strip()
            if ch:
                return '', ch, fallback
        if role != 'main':
            ch = str(roles.get('main') or '').strip()
            if ch:
                return '', ch, 'main'
        return '', '', ''

    def get_model_for_role(self, role: str, *, advance: bool = True) -> Optional[dict]:
        """获取指定角色的模型配置，应用渠道轮询策略。

        advance=False 时只读解析（roundrobin 不推进计数器），供状态展示使用，
        避免查看行为改变真实请求的轮询状态。
        """
        role = str(role or '').strip()
        ch_name = self._resolve_role_channel_name(role)
        channel = self._find_channel(ch_name) if ch_name else None
        if not channel:
            channels = self.config.get('channels') or []
            if channels:
                channel = channels[0]
        if not channel:
            return None
        return self._pick_model_from_channel(channel, advance=advance)

    def sync_channel_index(self, channel: str, model_id: str) -> None:
        """模型切换后，把渠道的轮询/回退索引同步到该模型的位置。

        防止 main 运行时单例模型与 model_manager 索引失同步（日志/展示
        显示旧模型、fallback 起点脱节）。找不到对应模型时重置为 0。
        """
        ch_name = str(channel or '').strip()
        if not ch_name:
            return
        models = (self._find_channel(ch_name) or {}).get('models') or []
        idx = 0
        for i, m in enumerate(models):
            if str(m.get('model_id') or '').strip() == str(model_id or '').strip():
                idx = i % len(models)
                break
        self._fb_indexes[ch_name] = idx
        self._request_fb_indexes[ch_name] = idx
        self._rr_counters[ch_name] = idx

    def begin_request(self, role: str):
        """为一次新的请求初始化临时轮询状态。"""
        ch_name = self._resolve_role_channel_name(role)
        channel = self._find_channel(ch_name) if ch_name else None
        if not channel:
            return
        strategy = self._normalize_strategy(channel.get('strategy') or 'fallback')
        if strategy == 'fallback_reset':
            self._request_fb_indexes[str(ch_name)] = 0

    def notify_failure(self, role: str):
        """fallback 策略：通知当前模型失败，切换到下一个。"""
        ch_name = self._resolve_role_channel_name(role)
        channel = self._find_channel(ch_name) if ch_name else None
        if not channel:
            return
        models = channel.get('models') or []
        if not models:
            return
        strategy = self._normalize_strategy(channel.get('strategy') or 'fallback')
        if strategy == 'fallback':
            cur = self._fb_indexes.get(ch_name, 0)
            self._fb_indexes[ch_name] = (cur + 1) % len(models)
            warn(f'[ModelManager] fallback: {ch_name} 切换到索引 {self._fb_indexes[ch_name]}')
        elif strategy == 'fallback_reset':
            cur = self._request_fb_indexes.get(ch_name, 0)
            self._request_fb_indexes[ch_name] = (cur + 1) % len(models)
            warn(f'[ModelManager] fallback_reset: {ch_name} 本轮切换到索引 {self._request_fb_indexes[ch_name]}')

    # ── 兼容旧接口 ──
    def get_current_model(self) -> Optional[dict]:
        return self.get_model_for_role('main')

    def get_role_model(self, role: str) -> Optional[dict]:
        role = str(role or '').strip()
        if role == 'tasker':
            roles = self.config.get('roles') or {}
            if 'tasker' not in roles and 'dev_agent' in roles:
                role = 'dev_agent'
        return self.get_model_for_role(role)

    def get_vision_model(self) -> Optional[dict]:
        return self.get_model_for_role('vision')

    def get_role_channel_name(self, role: str) -> Optional[str]:
        """只读：返回角色绑定的渠道名，不修改任何状态。"""
        return self._resolve_role_channel_name(role)

    def resolve_channel_models(self, channel_name: str) -> list[dict]:
        """只读：返回渠道内所有模型的完整配置（含凭据，仅供内部使用）。"""
        channel = self._find_channel(channel_name)
        if not channel:
            return []
        result = []
        for entry in (channel.get('models') or []):
            upstream_name = str(entry.get('upstream') or '').strip()
            model_id = str(entry.get('model_id') or '').strip()
            upstream = self._find_upstream(upstream_name)
            if not upstream or not model_id:
                continue
            protocol = self.normalize_upstream_protocol(upstream.get('protocol')) or self.protocol_from_path(upstream.get('messages_path'))
            messages_path = self.endpoint_path_for_protocol(protocol)
            result.append({
                'base_url': str(upstream.get('base_url') or '').strip().rstrip('/'),
                'api_key': str(upstream.get('api_key') or '').strip(),
                'model_name': model_id,
                'messages_path': messages_path,
                'protocol': 'anthropic' if protocol == 'anthropic' else 'openai',
                'display_name': f'{upstream_name}/{model_id}',
                'channel_name': str(channel.get('name') or '').strip(),
                'upstream_name': upstream_name,
            })
        return result

    def resolve_exact_model(self, channel: str, upstream: str, model_id: str) -> Optional[dict]:
        """只读：精确匹配 channel+upstream+model_id，不修改任何状态。"""
        for cfg in self.resolve_channel_models(channel):
            if cfg['upstream_name'] == upstream and cfg['model_name'] == model_id:
                return cfg
        return None

    def switch_model(self, target: str, persist: bool = False) -> tuple[bool, str]:
        """将 main 角色的渠道切换至指定渠道（兼容旧接口）。"""
        idx = self._resolve_channel_idx(target)
        if idx < 0:
            return False, f'未找到渠道: {target}'
        ch_name = str((self.config['channels'][idx]).get('name') or '').strip()
        roles = self.config.setdefault('roles', {})
        roles['main'] = ch_name
        if persist:
            ok, msg = self._save()
            if not ok:
                return False, msg
        return True, f'主AI 渠道已切换到: {ch_name}'

    def switch_next_model(self):
        return None

    # ─────────────────────────── 展示 ───────────────────────────

    @staticmethod
    def _mask(value: str) -> str:
        value = str(value or '')
        if not value:
            return '(空)'
        if len(value) <= 8:
            return '*' * len(value)
        return f'{value[:4]}{"*" * (len(value) - 8)}{value[-4:]}'

    def get_summary_text(self) -> str:
        current = self.get_current_model()
        if not current:
            return '当前没有可用模型。'
        upstreams = len(self.config.get('upstreams') or [])
        channels = len(self.config.get('channels') or [])
        tier_parts = []
        for role in ('tiered', 'tiered_chat', 'tiered_exec', 'tiered_decision'):
            model = self.get_model_for_role(role, advance=False)
            tier_parts.append(f'{role}:{model["display_name"] if model else "(无模型)"}')
        return (
            f'当前主AI: {current["display_name"]}\n'
            f'分级分流: ' + ' | '.join(tier_parts) + '\n'
            f'上游数: {upstreams} | 渠道数: {channels}'
        )

    def list_upstreams_text(self) -> str:
        upstreams = self.config.get('upstreams') or []
        if not upstreams:
            return '暂无上游，可用 /upstream add 添加。'
        lines = ['上游列表:']
        for i, u in enumerate(upstreams):
            protocol = self.normalize_upstream_protocol(u.get('protocol')) or self.protocol_from_path(u.get('messages_path'))
            lines.append(
                f'  {i}. {u.get("name")} | url={u.get("base_url")} | '
                f'key={self._mask(u.get("api_key",""))} | protocol={protocol}'
            )
        return '\n'.join(lines)

    def list_channels_text(self) -> str:
        channels = self.config.get('channels') or []
        if not channels:
            return '暂无渠道，可用 /channel add 添加。'
        lines = ['渠道列表:']
        for i, ch in enumerate(channels):
            strategy = self._normalize_strategy(ch.get('strategy', 'fallback'))
            models = ch.get('models') or []
            model_text = ', '.join(f'{m["upstream"]}/{m["model_id"]}' for m in models) or '(无模型)'
            lines.append(f'  {i}. {ch.get("name")} [{strategy}] → {model_text}')
        return '\n'.join(lines)

    def list_roles_text(self) -> str:
        roles = self.config.get('roles') or {}
        lines = ['角色-渠道映射（分级回退链: tiered_* → tiered → main）:']
        for role, label in ROLE_LABELS.items():
            explicit, effective, fb_from = self._resolve_role_binding_detail(role)
            model = self.get_model_for_role(role, advance=False)
            model_text = model['display_name'] if model else '(无模型)'
            if explicit:
                lines.append(f'  {label} ({role}): {explicit} [{model_text}]')
            elif effective:
                lines.append(f'  {label} ({role}): {effective} [{model_text}] (回退自 {fb_from})')
            else:
                lines.append(f'  {label} ({role}): (未配置) [{model_text}]')
        return '\n'.join(lines)

    def list_models(self) -> str:
        return self.get_summary_text() + '\n\n' + self.list_channels_text()

    def list_channels(self) -> str:
        return self.list_channels_text()

    # ─────────────────────────── 上游 CRUD ───────────────────────────

    def add_upstream(self, *, name: str, base_url: str, api_key: str, protocol: str = '', messages_path: str = '') -> tuple[bool, str]:
        name = str(name or '').strip()
        base_url = str(base_url or '').strip().rstrip('/')
        protocol_text = str(protocol or '').strip()
        normalized_protocol = self.normalize_upstream_protocol(protocol_text)
        if not normalized_protocol and not protocol_text:
            normalized_protocol = self.protocol_from_path(messages_path) if messages_path else 'anthropic'
        if not name or not base_url:
            return False, '至少需要 name 和 base_url。'
        if not normalized_protocol:
            return False, 'protocol 仅支持 anthropic、completions、responses。'
        upstreams = self.config.setdefault('upstreams', [])
        if any(str(u.get('name') or '') == name for u in upstreams):
            return False, f'上游名称已存在: {name}'
        upstreams.append({'name': name, 'base_url': base_url, 'api_key': str(api_key or '').strip(), 'protocol': normalized_protocol})
        ok, msg = self._save()
        if not ok:
            upstreams.pop()
            return False, msg
        return True, f'已添加上游 {name}。'

    def update_upstream(self, target: str, **fields) -> tuple[bool, str]:
        idx = self._resolve_upstream_idx(target)
        if idx < 0:
            return False, f'未找到上游: {target}'
        u = self.config['upstreams'][idx]
        old_name = str(u.get('name') or '')
        if 'name' in fields and fields['name']:
            new_name = str(fields['name']).strip()
            if any(i != idx and str(x.get('name') or '') == new_name for i, x in enumerate(self.config['upstreams'])):
                return False, f'上游名称已存在: {new_name}'
            # 同步渠道里的 upstream 引用
            for ch in (self.config.get('channels') or []):
                for m in (ch.get('models') or []):
                    if str(m.get('upstream') or '') == old_name:
                        m['upstream'] = new_name
            # 同步角色里的渠道名不受影响（角色引用渠道名，不引用上游名）
            u['name'] = new_name
        if 'base_url' in fields and fields['base_url'] is not None:
            u['base_url'] = str(fields['base_url']).strip().rstrip('/')
        if 'api_key' in fields and fields['api_key'] is not None:
            u['api_key'] = str(fields['api_key']).strip()
        if 'protocol' in fields and fields['protocol'] is not None:
            u['protocol'] = fields['protocol']
        elif 'messages_path' in fields and fields['messages_path'] is not None:
            legacy = dict(u)
            legacy.pop('protocol', None)
            legacy['messages_path'] = str(fields['messages_path']).strip()
            normalized, _changed = self._normalize_upstream(legacy)
            u.clear()
            u.update(normalized)
        u.pop('messages_path', None)
        ok, msg = self._save()
        if not ok:
            return False, msg
        return True, f'已更新上游 {target}。'

    def remove_upstream(self, target: str) -> tuple[bool, str]:
        idx = self._resolve_upstream_idx(target)
        if idx < 0:
            return False, f'未找到上游: {target}'
        removed = self.config['upstreams'].pop(idx)
        ok, msg = self._save()
        if not ok:
            self.config['upstreams'].insert(idx, removed)
            return False, msg
        return True, f'已删除上游 {removed.get("name", target)}。'

    # ─────────────────────────── 渠道 CRUD ───────────────────────────

    @staticmethod
    def _parse_channel_models(models_str: str) -> list[dict]:
        """解析 'upstream:model_id,upstream2:model_id2' 格式。"""
        result = []
        for item in str(models_str or '').split(','):
            item = item.strip()
            if ':' not in item:
                continue
            upstream, model_id = item.split(':', 1)
            upstream, model_id = upstream.strip(), model_id.strip()
            if upstream and model_id:
                result.append({'upstream': upstream, 'model_id': model_id})
        return result

    def add_channel(self, *, name: str, strategy: str = 'fallback', models=None, **_) -> tuple[bool, str]:
        name = str(name or '').strip()
        if not name:
            return False, '渠道名称不能为空。'
        channels = self.config.setdefault('channels', [])
        if any(str(ch.get('name') or '') == name for ch in channels):
            return False, f'渠道名称已存在: {name}'
        strategy = self._normalize_strategy(strategy)
        if isinstance(models, str):
            models = self._parse_channel_models(models)
        channels.append({'name': name, 'strategy': strategy, 'models': models or []})
        ok, msg = self._save()
        if not ok:
            channels.pop()
            return False, msg
        return True, f'已添加渠道 {name}。'

    def update_channel(self, target: str, **fields) -> tuple[bool, str]:
        idx = self._resolve_channel_idx(target)
        if idx < 0:
            return False, f'未找到渠道: {target}'
        ch = self.config['channels'][idx]
        old_name = str(ch.get('name') or '')
        if 'name' in fields and fields['name']:
            new_name = str(fields['name']).strip()
            if any(i != idx and str(x.get('name') or '') == new_name for i, x in enumerate(self.config['channels'])):
                return False, f'渠道名称已存在: {new_name}'
            # 同步角色引用
            for role, rch in list((self.config.get('roles') or {}).items()):
                if rch == old_name:
                    self.config['roles'][role] = new_name
            ch['name'] = new_name
        if 'strategy' in fields and fields['strategy'] is not None:
            ch['strategy'] = self._normalize_strategy(fields['strategy'])
        if 'models' in fields and fields['models'] is not None:
            m = fields['models']
            if isinstance(m, str):
                m = self._parse_channel_models(m)
            ch['models'] = m if isinstance(m, list) else []
        ok, msg = self._save()
        if not ok:
            return False, msg
        return True, f'已更新渠道 {target}。'

    def remove_channel(self, target: str) -> tuple[bool, str]:
        idx = self._resolve_channel_idx(target)
        if idx < 0:
            return False, f'未找到渠道: {target}'
        removed = self.config['channels'].pop(idx)
        removed_name = str(removed.get('name') or '')
        for role, rch in list((self.config.get('roles') or {}).items()):
            if rch == removed_name:
                del self.config['roles'][role]
        ok, msg = self._save()
        if not ok:
            self.config['channels'].insert(idx, removed)
            return False, msg
        return True, f'已删除渠道 {removed_name}。'

    def add_model_to_channel(self, channel: str, upstream: str, model_id: str) -> tuple[bool, str]:
        idx = self._resolve_channel_idx(channel)
        if idx < 0:
            return False, f'未找到渠道: {channel}'
        ch = self.config['channels'][idx]
        entry = {'upstream': upstream.strip(), 'model_id': model_id.strip()}
        ch.setdefault('models', []).append(entry)
        ok, msg = self._save()
        if not ok:
            ch['models'].pop()
            return False, msg
        return True, f'已向渠道 {channel} 添加模型 {upstream}/{model_id}。'

    def remove_model_from_channel(self, channel: str, index: int) -> tuple[bool, str]:
        idx = self._resolve_channel_idx(channel)
        if idx < 0:
            return False, f'未找到渠道: {channel}'
        models = self.config['channels'][idx].get('models') or []
        if index < 0 or index >= len(models):
            return False, f'模型索引越界: {index}'
        removed = models.pop(index)
        ok, msg = self._save()
        if not ok:
            models.insert(index, removed)
            return False, msg
        return True, f'已删除渠道模型 {removed}。'

    # ─────────────────────────── 角色映射 ───────────────────────────

    def set_role(self, role: str, channel: str) -> tuple[bool, str]:
        role = str(role or '').strip()
        if role == 'dev_agent':
            role = 'tasker'
        channel = str(channel or '').strip()
        if role not in ROLE_LABELS:
            return False, f'未知角色: {role}，可用: {", ".join(ROLE_LABELS.keys())}'
        if channel and not self._find_channel(channel):
            return False, f'渠道不存在: {channel}'
        roles = self.config.setdefault('roles', {})
        if channel:
            roles[role] = channel
        else:
            roles.pop(role, None)
        ok, msg = self._save()
        if not ok:
            return False, msg
        label = ROLE_LABELS.get(role, role)
        return True, f'{label} 角色已绑定渠道: {channel or "(已清除)"}。'
