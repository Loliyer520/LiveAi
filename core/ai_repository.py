import time
import uuid

from core.ai_types import AgentProfile, PendingTask
from pack.json_store import JsonStore


class AIRepository:
    def __init__(self, store: JsonStore):
        self.store = store
        self.store.update(self._ensure_shape)

    @staticmethod
    def _ensure_shape(payload: dict):
        payload.setdefault('agents', {})
        payload.setdefault('memories', {})
        payload.setdefault('tasks', {})
        payload.setdefault('relations', {'users': {}, 'scopes': {}})
        payload.setdefault('settings', {})
        payload.setdefault('knowledge_base', [])
        for memory in (payload.get('memories') or {}).values():
            AIRepository._normalize_memory(memory)

    def get_setting(self, key: str, default=None):
        payload = self.store.load()
        return (payload.get('settings') or {}).get(key, default)

    def set_setting(self, key: str, value):
        def mutator(payload: dict):
            payload.setdefault('settings', {})[key] = value

        self.store.update(mutator)

    def get_knowledge_base(self) -> list[dict]:
        payload = self.store.load()
        return list(payload.get('knowledge_base') or [])

    def add_knowledge_entry(self, content: str) -> dict | None:
        content = str(content or '').strip()
        if not content:
            return None

        def mutator(payload: dict):
            now = time.time()
            item = {
                'entry_id': uuid.uuid4().hex[:12],
                'content': content,
                'created_at': now,
                'updated_at': now,
            }
            payload.setdefault('knowledge_base', []).append(item)
            return dict(item)

        return self.store.update(mutator)

    def update_knowledge_entry(self, entry_id: str, content: str) -> dict | None:
        entry_id = str(entry_id or '').strip()
        content = str(content or '').strip()
        if not entry_id or not content:
            return None

        def mutator(payload: dict):
            for item in payload.setdefault('knowledge_base', []):
                if str(item.get('entry_id') or '') != entry_id:
                    continue
                item['content'] = content
                item['updated_at'] = time.time()
                return dict(item)
            return None

        return self.store.update(mutator)

    def delete_knowledge_entry(self, entry_id: str) -> bool:
        entry_id = str(entry_id or '').strip()
        if not entry_id:
            return False

        def mutator(payload: dict):
            items = payload.setdefault('knowledge_base', [])
            before = len(items)
            items[:] = [item for item in items if str(item.get('entry_id') or '') != entry_id]
            return len(items) != before

        return bool(self.store.update(mutator))

    @staticmethod
    def _normalize_memory(memory: dict):
        memory.setdefault('messages', [])
        memory.setdefault('notes', [])
        memory.setdefault('tool_logs', [])
        memory.setdefault('turn_logs', [])
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

    def _ensure_memory_entry(self, payload: dict, key: str) -> dict:
        memory = payload['memories'].setdefault(key, {})
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

        def mutator(payload: dict):
            agents = payload['agents']
            data = agents.get(key)
            if not data:
                data = AgentProfile(agent_id=key, scope_type=scope_type, scope_id=str(scope_id), role=role).to_dict()
                agents[key] = data
            data['updated_at'] = time.time()
            return AgentProfile(**data)

        return self.store.update(mutator)

    def append_message(self, scope_type: str, scope_id: str, message: dict, limit: int):
        key = self._memory_key(scope_type, scope_id)
        agent_key = self._agent_key(scope_type, scope_id)

        def mutator(payload: dict):
            messages = self._ensure_memory_entry(payload, key)['messages']
            messages.append(message)
            del messages[:-limit]
            agents = payload['agents']
            data = agents.get(agent_key)
            if not data:
                role = 'master' if scope_type == 'master' else 'child'
                data = AgentProfile(agent_id=agent_key, scope_type=scope_type, scope_id=str(scope_id), role=role).to_dict()
                agents[agent_key] = data
            data['updated_at'] = time.time()
            data['message_count'] = int(data.get('message_count') or 0) + 1

        self.store.update(mutator)

    def list_messages(self, scope_type: str, scope_id: str) -> list[dict]:
        payload = self.store.load()
        key = self._memory_key(scope_type, scope_id)
        return list((payload.get('memories', {}).get(key) or {}).get('messages', []))

    def clear_messages(self, scope_type: str, scope_id: str):
        key = self._memory_key(scope_type, scope_id)

        def mutator(payload: dict):
            memory = self._ensure_memory_entry(payload, key)
            memory['messages'] = []

        self.store.update(mutator)

    def clear_notes(self, scope_type: str, scope_id: str):
        key = self._memory_key(scope_type, scope_id)

        def mutator(payload: dict):
            memory = self._ensure_memory_entry(payload, key)
            memory['notes'] = []

        self.store.update(mutator)

    def clear_memory(self, scope_type: str, scope_id: str):
        key = self._memory_key(scope_type, scope_id)

        def mutator(payload: dict):
            payload['memories'][key] = {'messages': [], 'notes': [], 'tool_logs': [], 'turn_logs': []}

        self.store.update(mutator)

    def add_note(self, scope_type: str, scope_id: str, note: str) -> dict | None:
        key = self._memory_key(scope_type, scope_id)
        note = str(note or '').strip()
        if not note:
            return None

        def mutator(payload: dict):
            now = time.time()
            memory = self._ensure_memory_entry(payload, key)
            item = {
                'note_id': uuid.uuid4().hex[:12],
                'content': note,
                'created_at': now,
                'updated_at': now,
            }
            memory['notes'].append(item)
            del memory['notes'][:-200]
            return dict(item)

        return self.store.update(mutator)

    def list_notes(self, scope_type: str, scope_id: str) -> list[dict]:
        payload = self.store.load()
        key = self._memory_key(scope_type, scope_id)
        memory = dict((payload.get('memories', {}).get(key) or {}))
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

        def mutator(payload: dict):
            memory = self._ensure_memory_entry(payload, key)
            for item in memory['notes']:
                if str(item.get('note_id') or '') != note_id:
                    continue
                item['content'] = content
                item['updated_at'] = time.time()
                return dict(item)
            return None

        return self.store.update(mutator)

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

        def mutator(payload: dict):
            memory = self._ensure_memory_entry(payload, key)
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

        return self.store.update(mutator)

    def list_tool_logs(self, scope_type: str, scope_id: str) -> list[dict]:
        payload = self.store.load()
        key = self._memory_key(scope_type, scope_id)
        memory = dict((payload.get('memories', {}).get(key) or {}))
        self._normalize_memory(memory)
        return list(memory.get('tool_logs', []))

    def add_turn_log(self, scope_type: str, scope_id: str, log: dict, limit: int = 500) -> dict:
        key = self._memory_key(scope_type, scope_id)

        def mutator(payload: dict):
            memory = self._ensure_memory_entry(payload, key)
            item = dict(log or {})
            item.setdefault('turn_id', uuid.uuid4().hex[:12])
            item.setdefault('created_at', time.time())
            memory['turn_logs'].append(item)
            del memory['turn_logs'][:-max(1, int(limit or 500))]
            return dict(item)

        return self.store.update(mutator)

    def list_turn_logs(self, scope_type: str, scope_id: str) -> list[dict]:
        payload = self.store.load()
        key = self._memory_key(scope_type, scope_id)
        memory = dict((payload.get('memories', {}).get(key) or {}))
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
                data = AgentProfile(agent_id=key, scope_type=scope_type, scope_id=str(scope_id), role=role).to_dict()
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
                data = AgentProfile(agent_id=key, scope_type=scope_type, scope_id=str(scope_id), role=role).to_dict()
                agents[key] = data
            data['display_name'] = display_name
            data['updated_at'] = time.time()
            return AgentProfile(**data)

        return self.store.update(mutator)

    def create_task(self, source_agent: str, kind: str, payload: dict) -> PendingTask:
        task = PendingTask(task_id=uuid.uuid4().hex[:12], source_agent=source_agent, kind=kind, payload=payload)

        def mutator(state: dict):
            state['tasks'][task.task_id] = task.to_dict()

        self.store.update(mutator)
        return task

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

    def list_agents(self) -> list[dict]:
        payload = self.store.load()
        agents = list((payload.get('agents') or {}).values())
        agents.sort(key=lambda item: item.get('updated_at', 0), reverse=True)
        return agents

    def get_agent(self, scope_type: str, scope_id: str) -> dict | None:
        payload = self.store.load()
        return (payload.get('agents') or {}).get(self._agent_key(scope_type, scope_id))

    def get_memory(self, scope_type: str, scope_id: str) -> dict:
        payload = self.store.load()
        key = self._memory_key(scope_type, scope_id)
        memory = dict((payload.get('memories', {}).get(key) or {}))
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
