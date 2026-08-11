import copy
import time
import uuid
from pathlib import Path

from core.ai_types import AgentProfile, PendingTask
from core.turn_log_slim import slim_turn_log, TURN_LOG_LIMIT
from pack.json_store import JsonStore
from pack.scoped_memory_store import ScopedMemoryStore


class AIRepository:
    DEFAULT_KNOWLEDGE_BASE_ID = 'owner_facts'
    DEFAULT_KNOWLEDGE_BASE_NAME = '号主事实'

    def __init__(
        self,
        store: JsonStore,
        memory_store: ScopedMemoryStore | None = None,
        default_trigger_rate: float = 0.0,
    ):
        self.store = store
        self.default_trigger_rate = float(default_trigger_rate)
        # memories 已从主文件拆分到独立目录（方案B）。默认放主文件同级的 memories/ 目录。
        if memory_store is None:
            base_dir = Path(store.file_path).parent / 'memories'
            memory_store = ScopedMemoryStore(str(base_dir))
        self.memory_store = memory_store
        self.store.update(self._ensure_shape)

    def _build_agent_profile_dict(self, scope_type: str, scope_id: str, role: str) -> dict:
        return AgentProfile(
            agent_id=self._agent_key(scope_type, scope_id),
            scope_type=scope_type,
            scope_id=str(scope_id),
            role=role,
            trigger_rate=float(self.default_trigger_rate),
        ).to_dict()

    @staticmethod
    def _ensure_shape(payload: dict):
        payload.setdefault('agents', {})
        payload.setdefault('tasks', {})
        payload.setdefault('relations', {'users': {}, 'scopes': {}})
        payload.setdefault('settings', {})
        payload.setdefault('knowledge_base', [])
        knowledge_bases = payload.setdefault('knowledge_bases', {})
        legacy_items = list(payload.get('knowledge_base') or [])
        default_base = knowledge_bases.get(AIRepository.DEFAULT_KNOWLEDGE_BASE_ID)
        if default_base is None:
            now = time.time()
            default_base = {
                'kb_id': AIRepository.DEFAULT_KNOWLEDGE_BASE_ID,
                'name': AIRepository.DEFAULT_KNOWLEDGE_BASE_NAME,
                'description': '关于号主本人、可被 AI 明确确认和复述的基础事实。',
                'entries': legacy_items,
                'created_at': now,
                'updated_at': now,
            }
            knowledge_bases[AIRepository.DEFAULT_KNOWLEDGE_BASE_ID] = default_base
        else:
            default_base.setdefault('kb_id', AIRepository.DEFAULT_KNOWLEDGE_BASE_ID)
            default_base.setdefault('name', AIRepository.DEFAULT_KNOWLEDGE_BASE_NAME)
            default_base.setdefault('description', '关于号主本人、可被 AI 明确确认和复述的基础事实。')
            default_base.setdefault('entries', legacy_items)
            default_base.setdefault('created_at', time.time())
            default_base.setdefault('updated_at', default_base.get('created_at'))
        payload['knowledge_base'] = list(default_base.get('entries') or [])

    @staticmethod
    def _normalize_knowledge_entry(item: dict) -> dict:
        now = time.time()
        normalized = dict(item or {})
        normalized.setdefault('entry_id', uuid.uuid4().hex[:12])
        normalized['content'] = str(normalized.get('content') or '').strip()
        normalized.setdefault('created_at', now)
        normalized.setdefault('updated_at', normalized.get('created_at'))
        return normalized

    @classmethod
    def _normalize_knowledge_base(cls, kb: dict) -> dict:
        now = time.time()
        normalized = dict(kb or {})
        normalized.setdefault('kb_id', uuid.uuid4().hex[:12])
        normalized.setdefault('name', cls.DEFAULT_KNOWLEDGE_BASE_NAME)
        normalized['name'] = str(normalized.get('name') or cls.DEFAULT_KNOWLEDGE_BASE_NAME).strip()[:60]
        normalized['description'] = str(normalized.get('description') or '').strip()[:300]
        normalized.setdefault('entries', [])
        normalized['entries'] = [
            cls._normalize_knowledge_entry(entry)
            for entry in list(normalized.get('entries') or [])
            if str((entry or {}).get('content') or '').strip()
        ]
        normalized.setdefault('created_at', now)
        normalized.setdefault('updated_at', normalized.get('created_at'))
        return normalized

    @classmethod
    def _sync_legacy_knowledge_base(cls, payload: dict):
        default_base = ((payload.get('knowledge_bases') or {}).get(cls.DEFAULT_KNOWLEDGE_BASE_ID) or {})
        payload['knowledge_base'] = list(default_base.get('entries') or [])

    def list_knowledge_bases(self) -> list[dict]:
        payload = self.store.load()
        items = []
        for kb in (payload.get('knowledge_bases') or {}).values():
            normalized = self._normalize_knowledge_base(kb)
            items.append(
                {
                    'kb_id': normalized['kb_id'],
                    'name': normalized['name'],
                    'description': normalized['description'],
                    'entry_count': len(normalized.get('entries') or []),
                    'created_at': normalized.get('created_at'),
                    'updated_at': normalized.get('updated_at'),
                }
            )
        items.sort(key=lambda item: (item['kb_id'] != self.DEFAULT_KNOWLEDGE_BASE_ID, float(item.get('updated_at') or 0.0), item['name']))
        return items

    def get_knowledge_base_info(self, kb_id: str) -> dict | None:
        payload = self.store.load()
        kb = (payload.get('knowledge_bases') or {}).get(str(kb_id or '').strip())
        if not kb:
            return None
        normalized = self._normalize_knowledge_base(kb)
        return {
            'kb_id': normalized['kb_id'],
            'name': normalized['name'],
            'description': normalized['description'],
            'entry_count': len(normalized.get('entries') or []),
            'created_at': normalized.get('created_at'),
            'updated_at': normalized.get('updated_at'),
        }

    def get_knowledge_entries(self, kb_id: str) -> list[dict]:
        payload = self.store.load()
        kb = (payload.get('knowledge_bases') or {}).get(str(kb_id or '').strip())
        if not kb:
            return []
        normalized = self._normalize_knowledge_base(kb)
        return list(normalized.get('entries') or [])

    def get_setting(self, key: str, default=None):
        payload = self.store.load()
        return (payload.get('settings') or {}).get(key, default)

    def set_setting(self, key: str, value):
        def mutator(payload: dict):
            payload.setdefault('settings', {})[key] = value

        self.store.update(mutator)

    def get_knowledge_base(self) -> list[dict]:
        return self.get_knowledge_entries(self.DEFAULT_KNOWLEDGE_BASE_ID)

    def create_knowledge_base(self, name: str, description: str = '') -> dict | None:
        name = str(name or '').strip()
        description = str(description or '').strip()
        if not name:
            return None

        def mutator(payload: dict):
            knowledge_bases = payload.setdefault('knowledge_bases', {})
            for kb in knowledge_bases.values():
                if str((kb or {}).get('name') or '').strip() == name:
                    return None
            now = time.time()
            item = self._normalize_knowledge_base(
                {
                    'kb_id': uuid.uuid4().hex[:12],
                    'name': name,
                    'description': description,
                    'entries': [],
                    'created_at': now,
                    'updated_at': now,
                }
            )
            knowledge_bases[item['kb_id']] = item
            self._sync_legacy_knowledge_base(payload)
            return {
                'kb_id': item['kb_id'],
                'name': item['name'],
                'description': item['description'],
                'entry_count': 0,
                'created_at': item['created_at'],
                'updated_at': item['updated_at'],
            }

        return self.store.update(mutator)

    def update_knowledge_base_info(self, kb_id: str, name: str | None = None, description: str | None = None) -> dict | None:
        kb_id = str(kb_id or '').strip()
        if not kb_id:
            return None

        def mutator(payload: dict):
            knowledge_bases = payload.setdefault('knowledge_bases', {})
            kb = knowledge_bases.get(kb_id)
            if not kb:
                return None
            if name is not None:
                clean_name = str(name or '').strip()
                if not clean_name:
                    return None
                for other_id, other in knowledge_bases.items():
                    if other_id != kb_id and str((other or {}).get('name') or '').strip() == clean_name:
                        return None
                kb['name'] = clean_name[:60]
            if description is not None:
                kb['description'] = str(description or '').strip()[:300]
            kb['updated_at'] = time.time()
            knowledge_bases[kb_id] = self._normalize_knowledge_base(kb)
            self._sync_legacy_knowledge_base(payload)
            normalized = knowledge_bases[kb_id]
            return {
                'kb_id': normalized['kb_id'],
                'name': normalized['name'],
                'description': normalized['description'],
                'entry_count': len(normalized.get('entries') or []),
                'created_at': normalized.get('created_at'),
                'updated_at': normalized.get('updated_at'),
            }

        return self.store.update(mutator)

    def delete_knowledge_base_info(self, kb_id: str) -> bool:
        kb_id = str(kb_id or '').strip()
        if not kb_id or kb_id == self.DEFAULT_KNOWLEDGE_BASE_ID:
            return False

        def mutator(payload: dict):
            knowledge_bases = payload.setdefault('knowledge_bases', {})
            if kb_id not in knowledge_bases:
                return False
            del knowledge_bases[kb_id]
            for key, agent in (payload.get('agents') or {}).items():
                mounts = [str(item) for item in list((agent or {}).get('knowledge_mounts') or []) if str(item or '').strip() and str(item) != kb_id]
                if mounts != list((agent or {}).get('knowledge_mounts') or []):
                    agent['knowledge_mounts'] = mounts
                    agent['updated_at'] = time.time()
            self._sync_legacy_knowledge_base(payload)
            return True

        return bool(self.store.update(mutator))

    def add_knowledge_entry(self, content: str, kb_id: str | None = None) -> dict | None:
        kb_id = str(kb_id or self.DEFAULT_KNOWLEDGE_BASE_ID).strip()
        content = str(content or '').strip()
        if not kb_id or not content:
            return None

        def mutator(payload: dict):
            knowledge_bases = payload.setdefault('knowledge_bases', {})
            kb = knowledge_bases.get(kb_id)
            if not kb:
                return None
            entries = kb.setdefault('entries', [])
            now = time.time()
            item = self._normalize_knowledge_entry(
                {
                    'entry_id': uuid.uuid4().hex[:12],
                    'content': content,
                    'created_at': now,
                    'updated_at': now,
                }
            )
            entries.append(item)
            kb['updated_at'] = now
            knowledge_bases[kb_id] = self._normalize_knowledge_base(kb)
            self._sync_legacy_knowledge_base(payload)
            return dict(item)

        return self.store.update(mutator)

    def update_knowledge_entry(self, entry_id: str, content: str, kb_id: str | None = None) -> dict | None:
        kb_id = str(kb_id or self.DEFAULT_KNOWLEDGE_BASE_ID).strip()
        entry_id = str(entry_id or '').strip()
        content = str(content or '').strip()
        if not kb_id or not entry_id or not content:
            return None

        def mutator(payload: dict):
            kb = (payload.setdefault('knowledge_bases', {})).get(kb_id)
            if not kb:
                return None
            for item in kb.setdefault('entries', []):
                if str(item.get('entry_id') or '') != entry_id:
                    continue
                item['content'] = content
                item['updated_at'] = time.time()
                kb['updated_at'] = item['updated_at']
                self._sync_legacy_knowledge_base(payload)
                return dict(item)
            return None

        return self.store.update(mutator)

    def delete_knowledge_entry(self, entry_id: str, kb_id: str | None = None) -> bool:
        kb_id = str(kb_id or self.DEFAULT_KNOWLEDGE_BASE_ID).strip()
        entry_id = str(entry_id or '').strip()
        if not kb_id or not entry_id:
            return False

        def mutator(payload: dict):
            kb = (payload.setdefault('knowledge_bases', {})).get(kb_id)
            if not kb:
                return False
            items = kb.setdefault('entries', [])
            before = len(items)
            items[:] = [item for item in items if str(item.get('entry_id') or '') != entry_id]
            changed = len(items) != before
            if changed:
                kb['updated_at'] = time.time()
                self._sync_legacy_knowledge_base(payload)
            return changed

        return bool(self.store.update(mutator))

    def get_scope_knowledge_mounts(self, scope_type: str, scope_id: str) -> list[str]:
        profile = self.get_or_create_agent(scope_type, scope_id, role='master' if scope_type == 'master' else 'child')
        mounts = [str(item) for item in list(profile.knowledge_mounts or []) if str(item or '').strip()]
        return mounts

    def set_scope_knowledge_mounts(self, scope_type: str, scope_id: str, kb_ids: list[str]) -> list[str]:
        key = self._agent_key(scope_type, scope_id)
        normalized_ids = []
        seen = set()
        known_ids = {item['kb_id'] for item in self.list_knowledge_bases()}
        for kb_id in kb_ids or []:
            clean = str(kb_id or '').strip()
            if not clean or clean in seen or clean not in known_ids:
                continue
            normalized_ids.append(clean)
            seen.add(clean)

        def mutator(payload: dict):
            agents = payload['agents']
            data = agents.get(key)
            if not data:
                role = 'master' if scope_type == 'master' else 'child'
                data = self._build_agent_profile_dict(scope_type, scope_id, role)
                agents[key] = data
            data['knowledge_mounts'] = list(normalized_ids)
            data['updated_at'] = time.time()
            return list(normalized_ids)

        return list(self.store.update(mutator) or [])

    @staticmethod
    def _normalize_memory(memory: dict):
        memory.setdefault('messages', [])
        memory.setdefault('notes', [])
        memory.setdefault('tool_logs', [])
        memory.setdefault('turn_logs', [])
        memory.setdefault('diary_window', [])
        memory.setdefault('diary_summaries', [])
        memory.setdefault('diary_pending', [])
        memory.setdefault('diary_next_index', 0)
        for item in memory['notes']:
            item.setdefault('note_id', uuid.uuid4().hex[:12])
            item.setdefault('created_at', time.time())
            item.setdefault('updated_at', item.get('created_at'))
        for item in memory['tool_logs']:
            item.setdefault('log_id', uuid.uuid4().hex[:12])
            item.setdefault('created_at', time.time())
        for item in memory['turn_logs']:
            item.setdefault('turn_id', uuid.uuid4().hex[:12])
            item.setdefault('created_at', time.time())

    def _ensure_memory_entry(self, memory: dict) -> dict:
        self._normalize_memory(memory)
        return memory

    def _agent_key(self, scope_type: str, scope_id: str) -> str:
        return f'{scope_type}:{scope_id}'

    def _memory_key(self, scope_type: str, scope_id: str) -> str:
        return f'{scope_type}:{scope_id}'

    @staticmethod
    def _empty_user_profile(user_id: str) -> dict:
        now = time.time()
        return {
            'user_id': user_id,
            'aliases': [],
            'scopes': [],
            'facts': [],
            'province': '',        # 省份/地域
            'impression': '',      # 主AI 对该用户的印象（自由文本）
            'attributes': {},      # 任意键值情报: {key: {'value':.., 'source_scope':.., 'updated_at':..}}
            'created_at': now,
            'updated_at': now,
        }

    @staticmethod
    def _add_unique_text(items: list[str], value: str, limit: int = 20):
        value = str(value or '').strip()
        if not value:
            return
        if value in items:
            items.remove(value)
        items.append(value)
        del items[:-limit]

    @staticmethod
    def _upsert_scope(scopes: list[dict], scope_type: str, scope_id: str, last_seen: float):
        scope_type = str(scope_type or '').strip()
        scope_id = str(scope_id or '').strip()
        if not scope_type or not scope_id:
            return
        for item in scopes:
            if str(item.get('scope_type') or '') == scope_type and str(item.get('scope_id') or '') == scope_id:
                item['last_seen'] = last_seen
                return
        scopes.append({'scope_type': scope_type, 'scope_id': scope_id, 'last_seen': last_seen})
        scopes.sort(key=lambda entry: float(entry.get('last_seen') or 0.0))
        del scopes[:-20]

    def touch_user_identity(self, user_id: str, nickname: str, scope_type: str, scope_id: str):
        user_id = str(user_id or '').strip()
        if not user_id:
            return

        def mutator(payload: dict):
            users = payload['relations'].setdefault('users', {})
            profile = users.setdefault(user_id, self._empty_user_profile(user_id))
            self._add_unique_text(profile.setdefault('aliases', []), nickname)
            if scope_type == 'private' and str(scope_id or '').strip() == user_id:
                self._upsert_scope(profile.setdefault('scopes', []), scope_type, scope_id, time.time())
            profile['updated_at'] = time.time()

        self.store.update(mutator)

    def add_user_fact(
        self,
        user_id: str,
        fact: str,
        source_scope_type: str = '',
        source_scope_id: str = '',
        source_agent: str = '',
    ):
        user_id = str(user_id or '').strip()
        fact = str(fact or '').strip()
        if not user_id or not fact:
            return

        def mutator(payload: dict):
            users = payload['relations'].setdefault('users', {})
            profile = users.setdefault(user_id, self._empty_user_profile(user_id))
            facts = profile.setdefault('facts', [])
            for item in reversed(facts):
                if str(item.get('content') or '').strip() == fact:
                    item['updated_at'] = time.time()
                    return
            facts.append(
                {
                    'content': fact,
                    'source_scope_type': str(source_scope_type or ''),
                    'source_scope_id': str(source_scope_id or ''),
                    'source_agent': str(source_agent or ''),
                    'created_at': time.time(),
                    'updated_at': time.time(),
                }
            )
            facts.sort(key=lambda item: float(item.get('updated_at') or 0.0))
            del facts[:-30]
            profile['updated_at'] = time.time()

        self.store.update(mutator)

    def update_user_intel(
        self,
        user_id: str,
        province: str | None = None,
        impression: str | None = None,
        attributes: dict | None = None,
        source_scope_type: str = '',
        source_scope_id: str = '',
    ) -> dict | None:
        """结构化情报写入口：只更新传入的非 None 字段。
        province/impression 直接覆盖；attributes 做 merge，每个 key 记 value+来源+时间。
        单 key 名与 value 做限长，attributes 总条数限 40，防档案膨胀。"""
        user_id = str(user_id or '').strip()
        if not user_id:
            return None

        def mutator(payload: dict):
            users = payload.setdefault('relations', {}).setdefault('users', {})
            profile = users.setdefault(user_id, self._empty_user_profile(user_id))
            if province is not None:
                profile['province'] = str(province).strip()[:60]
            if impression is not None:
                profile['impression'] = str(impression).strip()[:500]
            if attributes:
                attrs = profile.setdefault('attributes', {})
                now = time.time()
                for raw_key, raw_val in dict(attributes).items():
                    key = str(raw_key or '').strip()[:40]
                    val = str(raw_val if raw_val is not None else '').strip()[:200]
                    if not key or not val:
                        continue
                    attrs[key] = {
                        'value': val,
                        'source_scope': f"{source_scope_type}:{source_scope_id}".strip(':'),
                        'updated_at': now,
                    }
                if len(attrs) > 40:
                    # 按更新时间保留最近 40 个
                    keep = sorted(
                        attrs.items(),
                        key=lambda kv: float((kv[1] or {}).get('updated_at') or 0.0),
                        reverse=True,
                    )[:40]
                    profile['attributes'] = dict(keep)
            profile['updated_at'] = time.time()
            return dict(profile)

        return self.store.update(mutator)

    def _combined_user_profiles(self, payload: dict) -> dict[str, dict]:
        result: dict[str, dict] = {}
        relations = ((payload.get('relations') or {}).get('users') or {})
        for user_id, raw in relations.items():
            normalized = self._empty_user_profile(str(user_id))
            normalized.update(dict(raw or {}))
            normalized['user_id'] = str(user_id)
            normalized['aliases'] = list(normalized.get('aliases') or [])
            normalized['scopes'] = list(normalized.get('scopes') or [])
            normalized['facts'] = list(normalized.get('facts') or [])
            normalized['province'] = str(normalized.get('province') or '')
            normalized['impression'] = str(normalized.get('impression') or '')
            normalized['attributes'] = dict(normalized.get('attributes') or {})
            result[str(user_id)] = normalized

        memories = payload.get('memories') or {}
        for memory_key, memory in memories.items():
            try:
                scope_type, scope_id = memory_key.split(':', 1)
            except ValueError:
                continue
            for item in memory.get('messages') or []:
                user_id = str(item.get('user_id') or '').strip()
                if not user_id:
                    continue
                profile = result.setdefault(user_id, self._empty_user_profile(user_id))
                self._add_unique_text(profile.setdefault('aliases', []), str(item.get('nickname') or ''))
                if scope_type == 'private' and scope_id == user_id:
                    self._upsert_scope(profile.setdefault('scopes', []), scope_type, scope_id, float(item.get('timestamp') or time.time()))
                profile['updated_at'] = max(
                    float(profile.get('updated_at') or 0.0),
                    float(item.get('timestamp') or 0.0),
                )
        return result

    def get_user_profile(self, user_id: str) -> dict | None:
        payload = self.store.load()
        return self._combined_user_profiles(payload).get(str(user_id or '').strip())

    def resolve_user_candidates(self, query: str, limit: int = 5) -> list[dict]:
        query = str(query or '').strip()
        if not query:
            return []
        payload = self.store.load()
        profiles = self._combined_user_profiles(payload)
        scored: list[tuple[int, dict]] = []
        lowered = query.lower()
        for user_id, profile in profiles.items():
            score = 0
            if user_id == query:
                score += 100
            elif query.isdigit() and query in user_id:
                score += 60
            aliases = [str(item or '').strip() for item in profile.get('aliases') or [] if str(item or '').strip()]
            for alias in aliases:
                alias_lower = alias.lower()
                if alias == query:
                    score = max(score, 90 + min(len(alias), 9))
                elif len(alias) >= 2 and alias_lower in lowered:
                    score = max(score, 50 + min(len(alias), 9))
                elif lowered in alias_lower:
                    score = max(score, 45 + min(len(alias), 9))
            if not score:
                continue
            enriched = dict(profile)
            enriched['aliases'] = aliases
            scored.append((score, enriched))
        scored.sort(key=lambda pair: (pair[0], float(pair[1].get('updated_at') or 0.0)), reverse=True)
        return [item for _, item in scored[:limit]]

    def resolve_scope_by_query(self, query: str, preferred_scope_type: str = 'private') -> dict | None:
        for profile in self.resolve_user_candidates(query, limit=8):
            scopes = list(profile.get('scopes') or [])
            scopes.sort(key=lambda item: float(item.get('last_seen') or 0.0), reverse=True)
            for scope in scopes:
                if str(scope.get('scope_type') or '') == preferred_scope_type:
                    return {
                        'user_id': profile.get('user_id'),
                        'aliases': profile.get('aliases') or [],
                        'facts': profile.get('facts') or [],
                        'scope_type': scope.get('scope_type'),
                        'scope_id': str(scope.get('scope_id') or ''),
                    }
            if scopes:
                scope = scopes[0]
                return {
                    'user_id': profile.get('user_id'),
                    'aliases': profile.get('aliases') or [],
                    'facts': profile.get('facts') or [],
                    'scope_type': scope.get('scope_type'),
                    'scope_id': str(scope.get('scope_id') or ''),
                }
        return None

    def find_users_mentioned_in_text(self, text: str, exclude_user_id: str = '', limit: int = 3) -> list[dict]:
        text = str(text or '').strip()
        exclude_user_id = str(exclude_user_id or '').strip()
        if not text:
            return []
        payload = self.store.load()
        profiles = self._combined_user_profiles(payload)
        matches: list[tuple[int, dict]] = []
        lowered = text.lower()
        for user_id, profile in profiles.items():
            if exclude_user_id and user_id == exclude_user_id:
                continue
            score = 0
            if user_id and user_id in text:
                score = max(score, 100)
            for alias in profile.get('aliases') or []:
                alias = str(alias or '').strip()
                if len(alias) < 2:
                    continue
                alias_lower = alias.lower()
                if alias_lower in lowered:
                    score = max(score, 40 + min(len(alias), 12))
            if not score:
                continue
            matches.append((score, profile))
        matches.sort(key=lambda pair: (pair[0], float(pair[1].get('updated_at') or 0.0)), reverse=True)
        return [dict(item) for _, item in matches[:limit]]

    def get_or_create_master(self) -> AgentProfile:
        return self.get_or_create_agent('master', 'global', role='master')

    def get_or_create_agent(self, scope_type: str, scope_id: str, role: str = 'child') -> AgentProfile:
        key = self._agent_key(scope_type, scope_id)

        # 快路径：agent 已存在时只读返回，不触发全量写盘（避免每条消息都重写整份状态文件）。
        existing = (self.store.load().get('agents') or {}).get(key)
        if existing:
            return AgentProfile(**existing)

        def mutator(payload: dict):
            agents = payload['agents']
            data = agents.get(key)
            if not data:
                data = self._build_agent_profile_dict(scope_type, scope_id, role)
                agents[key] = data
            data['updated_at'] = time.time()
            return AgentProfile(**data)

        return self.store.update(mutator)

    def append_message(self, scope_type: str, scope_id: str, message: dict, limit: int, diary_size: int = 0) -> bool:
        key = self._memory_key(scope_type, scope_id)
        has_pending = False

        def mem_mutator(memory: dict):
            nonlocal has_pending
            self._normalize_memory(memory)
            if diary_size > 0:
                self._maybe_migrate_to_diary(memory, diary_size)
            messages = memory['messages']
            messages.append(message)
            if diary_size > 0:
                if len(messages) >= diary_size:
                    self._seal_diary(memory)
                has_pending = bool(memory.get('diary_pending'))
            else:
                del messages[:-limit]

        self.memory_store.update(key, mem_mutator)
        self._bump_agent_message_count(scope_type, scope_id)
        return has_pending

    def _bump_agent_message_count(self, scope_type: str, scope_id: str) -> None:
        agent_key = self._agent_key(scope_type, scope_id)

        def agent_mutator(payload: dict):
            agents = payload['agents']
            data = agents.get(agent_key)
            if not data:
                role = 'master' if scope_type == 'master' else 'child'
                data = self._build_agent_profile_dict(scope_type, scope_id, role)
                agents[agent_key] = data
            data['updated_at'] = time.time()
            data['message_count'] = int(data.get('message_count') or 0) + 1

        self.store.update(agent_mutator)

    def attach_tool_context_to_message(
        self,
        scope_type: str,
        scope_id: str,
        message_id,
        message_ref,
        tool_context_messages: list[dict],
    ) -> bool:
        key = self._memory_key(scope_type, scope_id)
        attached = False

        def mutator(memory: dict):
            nonlocal attached
            self._normalize_memory(memory)
            messages = memory.get('messages', [])
            for item in reversed(messages):
                if not isinstance(item, dict):
                    continue
                if message_ref and str(item.get('message_ref') or '') == str(message_ref):
                    item['tool_context_messages'] = list(tool_context_messages or [])
                    attached = True
                    return
                if message_id is not None and str(item.get('message_id') or '') == str(message_id):
                    item['tool_context_messages'] = list(tool_context_messages or [])
                    attached = True
                    return

        self.memory_store.update(key, mutator)
        return attached

    def upsert_tool_context_checkpoint(
        self,
        scope_type: str,
        scope_id: str,
        checkpoint_id: str,
        message: dict,
        limit: int,
        diary_size: int = 0,
    ) -> tuple[dict, bool]:
        """Create or update one persistent assistant checkpoint for a live turn.

        Live tool loops may call several read-only tools before they finally send a
        message. We want crash-safe persistence, but we must not append multiple
        empty assistant entries because history reconstruction would later observe a
        longer persisted trail than the runtime actually replays. A stable
        checkpoint_id lets the runtime mutate the same logical checkpoint in place.
        """
        key = self._memory_key(scope_type, scope_id)
        checkpoint_id = str(checkpoint_id or '').strip()
        if not checkpoint_id:
            raise ValueError('checkpoint_id must be non-empty')
        stored_message = copy.deepcopy(dict(message or {}))
        stored_message['tool_checkpoint_id'] = checkpoint_id
        created = False
        has_pending = False

        def mem_mutator(memory: dict):
            nonlocal created, has_pending
            self._normalize_memory(memory)
            if diary_size > 0:
                self._maybe_migrate_to_diary(memory, diary_size)
            messages = memory['messages']
            for item in reversed(messages):
                if not isinstance(item, dict):
                    continue
                if str(item.get('tool_checkpoint_id') or '') != checkpoint_id:
                    continue
                item.clear()
                item.update(copy.deepcopy(stored_message))
                return
            created = True
            messages.append(copy.deepcopy(stored_message))
            if diary_size > 0:
                if len(messages) >= diary_size:
                    self._seal_diary(memory)
                has_pending = bool(memory.get('diary_pending'))
            else:
                del messages[:-limit]

        self.memory_store.update(key, mem_mutator)
        if created:
            self._bump_agent_message_count(scope_type, scope_id)
        return copy.deepcopy(stored_message), has_pending

    @staticmethod
    def _seal_diary(memory: dict):
        messages = memory.get('messages', [])
        if not messages:
            return
        next_idx = int(memory.get('diary_next_index') or 0)
        diary_window = memory.setdefault('diary_window', [])
        diary_pending = memory.setdefault('diary_pending', [])
        diary_window.append({'index': next_idx, 'messages': list(messages), 'sealed_at': int(time.time())})
        memory['diary_next_index'] = next_idx + 1
        memory['messages'] = []
        while len(diary_window) > 2:
            diary_pending.append(diary_window.pop(0))

    @staticmethod
    def _maybe_migrate_to_diary(memory: dict, diary_size: int):
        if memory.get('diary_next_index') or memory.get('diary_window'):
            return
        messages = memory.get('messages', [])
        if len(messages) <= diary_size:
            return
        chunks, i = [], 0
        while i + diary_size <= len(messages):
            chunks.append(messages[i:i + diary_size])
            i += diary_size
        remainder = messages[i:]
        diary_window, diary_pending, next_idx = [], [], 0
        for chunk in chunks:
            diary_window.append({'index': next_idx, 'messages': chunk, 'sealed_at': int(time.time())})
            next_idx += 1
        while len(diary_window) > 2:
            diary_pending.append(diary_window.pop(0))
        memory['diary_window'] = diary_window
        memory['diary_pending'] = diary_pending
        memory['diary_next_index'] = next_idx
        memory['messages'] = remainder

    def get_diary_context(self, scope_type: str, scope_id: str) -> dict:
        key = self._memory_key(scope_type, scope_id)
        memory = self.memory_store.load(key) or {}
        self._normalize_memory(memory)
        return {
            'summaries': list(memory.get('diary_summaries') or []),
            'window': [dict(d) for d in (memory.get('diary_window') or [])],
            'pending': [dict(d) for d in (memory.get('diary_pending') or [])],
            'current': list(memory.get('messages') or []),
            'has_pending': bool(memory.get('diary_pending')),
        }

    def get_pending_diary(self, scope_type: str, scope_id: str) -> dict | None:
        key = self._memory_key(scope_type, scope_id)
        memory = self.memory_store.load(key) or {}
        self._normalize_memory(memory)
        pending = memory.get('diary_pending') or []
        if not pending:
            return None
        return dict(min(pending, key=lambda d: int(d.get('index') or 0)))

    # 日记摘要上限：超过此数量时触发元总结（将最旧 50 条摘要合并为一条）
    MAX_DIARY_ENTRIES = 100
    DIARY_META_BATCH = 50

    def store_diary_summary(self, scope_type: str, scope_id: str, diary_index: int, text: str) -> bool:
        """存储一条日记摘要。返回 True 表示需要进行元总结（摘要总数超过上限）。"""
        key = self._memory_key(scope_type, scope_id)
        needs_meta = False

        def mutator(memory: dict):
            nonlocal needs_meta
            self._normalize_memory(memory)
            memory['diary_pending'] = [
                d for d in (memory.get('diary_pending') or [])
                if int(d.get('index') or 0) != diary_index
            ]
            summaries = memory.setdefault('diary_summaries', [])
            summaries.append({'index': diary_index, 'text': text, 'summarized_at': int(time.time())})
            summaries.sort(key=lambda x: int(x.get('index') or 0))
            # 检查是否需要进行元总结
            if len(summaries) > self.MAX_DIARY_ENTRIES and not memory.get('meta_summary_pending'):
                needs_meta = True
                memory['meta_summary_pending'] = True

        self.memory_store.update(key, mutator)
        return needs_meta

    def get_meta_summary_candidates(self, scope_type: str, scope_id: str) -> list[dict] | None:
        """取最旧的 DIARY_META_BATCH 条日记摘要用于元总结。不足则返回 None。"""
        key = self._memory_key(scope_type, scope_id)
        memory = self.memory_store.load(key) or {}
        self._normalize_memory(memory)
        summaries = list(memory.get('diary_summaries') or [])
        if len(summaries) <= self.MAX_DIARY_ENTRIES:
            return None
        return summaries[:self.DIARY_META_BATCH]

    def store_meta_summary(self, scope_type: str, scope_id: str, text: str):
        """用一条元总结替换最旧的 DIARY_META_BATCH 条日记摘要。"""
        key = self._memory_key(scope_type, scope_id)

        def mutator(memory: dict):
            self._normalize_memory(memory)
            summaries = memory.setdefault('diary_summaries', [])
            # 用一条元总结替换前 DIARY_META_BATCH 条
            meta_entry = {
                'index': summaries[0]['index'] if summaries else 0,
                'text': text,
                'summarized_at': int(time.time()),
                'is_meta': True,
            }
            memory['diary_summaries'] = [meta_entry] + summaries[self.DIARY_META_BATCH:]
            memory['meta_summary_pending'] = False

        self.memory_store.update(key, mutator)

    def list_messages(self, scope_type: str, scope_id: str) -> list[dict]:
        key = self._memory_key(scope_type, scope_id)
        memory = self.memory_store.load(key)
        return list((memory or {}).get('messages', []))

    def clear_messages(self, scope_type: str, scope_id: str):
        key = self._memory_key(scope_type, scope_id)

        def mutator(memory: dict):
            self._normalize_memory(memory)
            memory['messages'] = []
            # 只清除仍以原文保留的当前对话上下文；历史摘要、备注和审计日志保留。
            memory['diary_window'] = []
            memory['diary_pending'] = []

        self.memory_store.update(key, mutator)

    def clear_notes(self, scope_type: str, scope_id: str):
        key = self._memory_key(scope_type, scope_id)

        def mutator(memory: dict):
            self._normalize_memory(memory)
            memory['notes'] = []

        self.memory_store.update(key, mutator)

    def clear_memory(self, scope_type: str, scope_id: str):
        key = self._memory_key(scope_type, scope_id)

        def mutator(memory: dict):
            memory.clear()
            memory.update({'messages': [], 'notes': [], 'tool_logs': [], 'turn_logs': []})

        self.memory_store.update(key, mutator)

    def add_note(self, scope_type: str, scope_id: str, note: str) -> dict | None:
        key = self._memory_key(scope_type, scope_id)
        note = str(note or '').strip()
        if not note:
            return None

        def mutator(memory: dict):
            self._normalize_memory(memory)
            now = time.time()
            item = {
                'note_id': uuid.uuid4().hex[:12],
                'content': note,
                'created_at': now,
                'updated_at': now,
            }
            memory['notes'].append(item)
            del memory['notes'][:-200]
            return dict(item)

        return self.memory_store.update(key, mutator)

    def list_notes(self, scope_type: str, scope_id: str) -> list[dict]:
        key = self._memory_key(scope_type, scope_id)
        memory = dict(self.memory_store.load(key) or {})
        self._normalize_memory(memory)
        return list(memory.get('notes', []))

    def get_note(self, scope_type: str, scope_id: str, note_id: str) -> dict | None:
        note_id = str(note_id or '').strip()
        if not note_id:
            return None
        for item in self.list_notes(scope_type, scope_id):
            if str(item.get('note_id') or '') == note_id:
                return dict(item)
        return None

    def update_note(self, scope_type: str, scope_id: str, note_id: str, content: str) -> dict | None:
        key = self._memory_key(scope_type, scope_id)
        note_id = str(note_id or '').strip()
        content = str(content or '').strip()
        if not note_id or not content:
            return None

        def mutator(memory: dict):
            self._normalize_memory(memory)
            for item in memory['notes']:
                if str(item.get('note_id') or '') != note_id:
                    continue
                item['content'] = content
                item['updated_at'] = time.time()
                return dict(item)
            return None

        return self.memory_store.update(key, mutator)

    def add_tool_log(
        self,
        scope_type: str,
        scope_id: str,
        agent_id: str,
        tool_name: str,
        tool_input: str,
        tool_result: str,
        limit: int = 500,
    ) -> dict:
        key = self._memory_key(scope_type, scope_id)

        def mutator(memory: dict):
            self._normalize_memory(memory)
            item = {
                'log_id': uuid.uuid4().hex[:12],
                'agent_id': str(agent_id or ''),
                'tool_name': str(tool_name or '').strip(),
                'tool_input': str(tool_input or ''),
                'tool_result': str(tool_result or ''),
                'created_at': time.time(),
            }
            memory['tool_logs'].append(item)
            del memory['tool_logs'][:-max(1, int(limit or 500))]
            return dict(item)

        return self.memory_store.update(key, mutator)

    def list_tool_logs(self, scope_type: str, scope_id: str) -> list[dict]:
        key = self._memory_key(scope_type, scope_id)
        memory = dict(self.memory_store.load(key) or {})
        self._normalize_memory(memory)
        return list(memory.get('tool_logs', []))

    def add_turn_log(self, scope_type: str, scope_id: str, log: dict, limit: int = TURN_LOG_LIMIT) -> dict:
        key = self._memory_key(scope_type, scope_id)

        def mutator(memory: dict):
            self._normalize_memory(memory)
            item = slim_turn_log(dict(log or {}))
            item.setdefault('turn_id', uuid.uuid4().hex[:12])
            item.setdefault('created_at', time.time())
            memory['turn_logs'].append(item)
            del memory['turn_logs'][:-max(1, int(limit or TURN_LOG_LIMIT))]
            return dict(item)

        return self.memory_store.update(key, mutator)

    def list_turn_logs(self, scope_type: str, scope_id: str) -> list[dict]:
        key = self._memory_key(scope_type, scope_id)
        memory = dict(self.memory_store.load(key) or {})
        self._normalize_memory(memory)
        return list(memory.get('turn_logs', []))

    def get_turn_log(self, scope_type: str, scope_id: str, turn_id: str) -> dict | None:
        turn_id = str(turn_id or '').strip()
        if not turn_id:
            return None
        for item in self.list_turn_logs(scope_type, scope_id):
            if str(item.get('turn_id') or '') == turn_id:
                return dict(item)
        return None

    def update_agent_impression(self, scope_type: str, scope_id: str, impression: str):
        key = self._agent_key(scope_type, scope_id)

        def mutator(payload: dict):
            agents = payload['agents']
            data = agents.get(key)
            if not data:
                role = 'master' if scope_type == 'master' else 'child'
                data = self._build_agent_profile_dict(scope_type, scope_id, role)
                agents[key] = data
            data['impression'] = impression
            data['impression_updated_at'] = time.time()
            data['updated_at'] = time.time()
            return AgentProfile(**data)

        return self.store.update(mutator)

    def update_agent_display_name(self, scope_type: str, scope_id: str, display_name: str):
        key = self._agent_key(scope_type, scope_id)

        def mutator(payload: dict):
            agents = payload['agents']
            data = agents.get(key)
            if not data:
                role = 'master' if scope_type == 'master' else 'child'
                data = self._build_agent_profile_dict(scope_type, scope_id, role)
                agents[key] = data
            data['display_name'] = display_name
            data['updated_at'] = time.time()
            return AgentProfile(**data)

        return self.store.update(mutator)

    def update_agent_trigger_rate(self, scope_type: str, scope_id: str, trigger_rate: float):
        key = self._agent_key(scope_type, scope_id)

        def mutator(payload: dict):
            agents = payload['agents']
            data = agents.get(key)
            if not data:
                role = 'master' if scope_type == 'master' else 'child'
                data = self._build_agent_profile_dict(scope_type, scope_id, role)
                agents[key] = data
            data['trigger_rate'] = float(trigger_rate)
            data['updated_at'] = time.time()
            return AgentProfile(**data)

        return self.store.update(mutator)

    def update_all_agent_trigger_rates(self, trigger_rate: float) -> int:
        trigger_rate = float(trigger_rate)

        def mutator(payload: dict):
            updated = 0
            for data in (payload.get('agents') or {}).values():
                if str(data.get('scope_type') or '') == 'master':
                    continue
                if float(data.get('trigger_rate', self.default_trigger_rate) or 0.0) == trigger_rate:
                    continue
                data['trigger_rate'] = trigger_rate
                data['updated_at'] = time.time()
                updated += 1
            return updated

        return int(self.store.update(mutator) or 0)

    def create_task(self, source_agent: str, kind: str, payload: dict) -> PendingTask:
        task = PendingTask(task_id=uuid.uuid4().hex[:12], source_agent=source_agent, kind=kind, payload=payload)

        def mutator(state: dict):
            state['tasks'][task.task_id] = task.to_dict()

        self.store.update(mutator)
        return task

    def create_unique_task(
        self,
        source_agent: str,
        kind: str,
        payload: dict,
        unique_payload_keys: tuple[str, ...],
    ) -> tuple[PendingTask | dict, bool]:
        """原子创建任务；同 kind 和关键 payload 的活动任务已存在时返回旧任务。"""
        task = PendingTask(task_id=uuid.uuid4().hex[:12], source_agent=source_agent, kind=kind, payload=payload)

        def mutator(state: dict):
            for existing in (state.get('tasks') or {}).values():
                if existing.get('kind') != kind or existing.get('status') not in {'queued', 'running'}:
                    continue
                existing_payload = existing.get('payload') or {}
                if all(str(existing_payload.get(key)) == str(payload.get(key)) for key in unique_payload_keys):
                    return existing, False
            state['tasks'][task.task_id] = task.to_dict()
            return task, True

        return self.store.update(mutator)

    def update_task(self, task_id: str, status: str, result: str | None = None):
        def mutator(payload: dict):
            task = payload['tasks'].get(task_id)
            if not task:
                return None
            task['status'] = status
            task['updated_at'] = time.time()
            if result is not None:
                task['result'] = result
            return task

        return self.store.update(mutator)

    def get_task(self, task_id: str) -> dict | None:
        payload = self.store.load()
        return (payload.get('tasks') or {}).get(task_id)

    def list_tasks(self, statuses: list[str] | None = None, kinds: list[str] | None = None) -> list[dict]:
        payload = self.store.load()
        tasks = list((payload.get('tasks') or {}).values())
        if statuses is not None:
            tasks = [task for task in tasks if task.get('status') in statuses]
        if kinds is not None:
            tasks = [task for task in tasks if task.get('kind') in kinds]
        tasks.sort(key=lambda task: task.get('created_at', 0))
        return tasks

    def load_state(self) -> dict:
        return self.store.load()

    def reset_all(self):
        def mutator(payload: dict):
            payload.clear()
            self._ensure_shape(payload)

        self.store.update(mutator)
        self.memory_store.reset_all()

    def count_memory_scopes(self) -> int:
        return len(self.memory_store.list_scopes())

    def list_agents(self) -> list[dict]:
        payload = self.store.load()
        agents = list((payload.get('agents') or {}).values())
        agents.sort(key=lambda item: item.get('updated_at', 0), reverse=True)
        return agents

    def get_agent(self, scope_type: str, scope_id: str) -> dict | None:
        payload = self.store.load()
        return (payload.get('agents') or {}).get(self._agent_key(scope_type, scope_id))

    def get_memory(self, scope_type: str, scope_id: str) -> dict:
        key = self._memory_key(scope_type, scope_id)
        memory = dict(self.memory_store.load(key) or {})
        self._normalize_memory(memory)
        return memory

    def get_scope_relation(self, scope_type: str, scope_id: str) -> dict | None:
        """获取一个scope（群聊/私聊）的关系数据：好感度、关联度、备注"""
        payload = self.store.load()
        scope_key = self._agent_key(scope_type, scope_id)
        return (payload.get('relations', {}).get('scopes', {}).get(scope_key))

    def update_scope_relation(
        self,
        scope_type: str,
        scope_id: str,
        affinity: float | None = None,
        relevance: float | None = None,
        admin_note: str | None = None,
    ) -> dict:
        """更新scope关系数据"""
        scope_key = self._agent_key(scope_type, scope_id)

        def mutator(payload: dict):
            scopes = payload.setdefault('relations', {}).setdefault('scopes', {})
            entry = scopes.setdefault(scope_key, {
                'scope_type': scope_type,
                'scope_id': str(scope_id),
                'affinity': 0.0,
                'relevance': 0.0,
                'admin_note': '',
                'updated_at': time.time(),
            })
            if affinity is not None:
                entry['affinity'] = float(affinity)
            if relevance is not None:
                entry['relevance'] = float(relevance)
            if admin_note is not None:
                entry['admin_note'] = str(admin_note)
            entry['updated_at'] = time.time()
            return dict(entry)

        return self.store.update(mutator)

    def update_user_relation(
        self,
        user_id: str,
        affinity: float | None = None,
        admin_note: str | None = None,
    ) -> dict:
        """更新用户关系数据"""
        user_id = str(user_id or '').strip()
        if not user_id:
            return {}

        def mutator(payload: dict):
            users = payload.setdefault('relations', {}).setdefault('users', {})
            profile = users.setdefault(user_id, self._empty_user_profile(user_id))
            if affinity is not None:
                profile['affinity'] = float(affinity)
            if admin_note is not None:
                profile['admin_note'] = str(admin_note)
            profile['updated_at'] = time.time()
            return dict(profile)

        return self.store.update(mutator)

    def list_scope_relations(self) -> list[dict]:
        """列出所有scope关系"""
        payload = self.store.load()
        scopes = (payload.get('relations', {}).get('scopes', {}) or {})
        agents = payload.get('agents', {}) or {}

        result = []
        for scope_key, relation in scopes.items():
            agent = agents.get(scope_key, {})
            result.append({
                'scope_key': scope_key,
                'scope_type': relation.get('scope_type', ''),
                'scope_id': str(relation.get('scope_id', '')),
                'affinity': float(relation.get('affinity', 0.0)),
                'relevance': float(relation.get('relevance', 0.0)),
                'admin_note': str(relation.get('admin_note', '')),
                'updated_at': float(relation.get('updated_at', 0)),
                'message_count': int(agent.get('message_count', 0)),
                'impression': str(agent.get('impression', '')),
            })

        result.sort(key=lambda x: x['updated_at'], reverse=True)
        return result

    def list_user_relations(self) -> list[dict]:
        """列出所有用户关系（合并profiles）"""
        payload = self.store.load()
        profiles = self._combined_user_profiles(payload)

        result = []
        for user_id, profile in profiles.items():
            result.append({
                'user_id': user_id,
                'aliases': profile.get('aliases', []),
                'affinity': float(profile.get('affinity', 0.0)),
                'admin_note': str(profile.get('admin_note', '')),
                'facts': profile.get('facts', []),
                'scopes': profile.get('scopes', []),
                'updated_at': float(profile.get('updated_at', 0)),
            })

        result.sort(key=lambda x: x['updated_at'], reverse=True)
        return result
