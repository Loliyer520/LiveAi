"""模型配置管理器 - 读取 models_config.json 并提供统一的模型/渠道管理接口"""

import json
from pathlib import Path
from typing import Optional

from pack.console_logger import error, info, warn


class ModelManager:
    def __init__(self, config_path: str = 'data/models_config.json'):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.available_models: list[dict] = []
        self.current_model_index = 0
        self._select_initial_model()

    @staticmethod
    def _default_config() -> dict:
        return {'channels': [], 'vision': {}, 'main': {}, 'tiered': {}}

    def _load_config(self) -> dict:
        """加载模型配置文件"""
        if not self.config_path.exists():
            warn(f'[ModelManager] 配置文件不存在，使用空配置: {self.config_path}')
            return self._default_config()

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            info(f'[ModelManager] 已加载配置 channels={len(config.get("channels", []))}')
            for key, default_value in self._default_config().items():
                config.setdefault(key, default_value.copy() if isinstance(default_value, dict) else list(default_value))
            return config
        except Exception as exc:
            error(f'[ModelManager] 配置加载失败: {exc}')
            return self._default_config()

    def _save_config(self) -> tuple[bool, str]:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            return True, f'已保存到 {self.config_path}'
        except Exception as exc:
            error(f'[ModelManager] 配置保存失败: {exc}')
            return False, f'保存配置失败: {exc}'

    def reload_config(self):
        """重新加载配置文件（用于热更新）"""
        current = self.get_current_model()
        current_display = current['display_name'] if current else ''
        self.config = self._load_config()
        self._select_initial_model(preferred_display=current_display)

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = str(value or '')
        if not value:
            return '(空)'
        if len(value) <= 8:
            return '*' * len(value)
        return f'{value[:4]}{"*" * (len(value) - 8)}{value[-4:]}'

    @staticmethod
    def _normalize_model_items(models) -> list[dict]:
        result: list[dict] = []
        if isinstance(models, str):
            raw_items = [item.strip() for item in models.split(',') if item.strip()]
            for item in raw_items:
                if ':' in item:
                    name, model_id = item.split(':', 1)
                    name = name.strip()
                    model_id = model_id.strip()
                else:
                    name = item
                    model_id = item
                if model_id:
                    result.append({'name': name or model_id, 'model_id': model_id})
            return result

        for item in models or []:
            if isinstance(item, str):
                model_id = item.strip()
                if model_id:
                    result.append({'name': model_id, 'model_id': model_id})
                continue
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or item.get('model_name') or item.get('model_id') or '').strip()
            model_id = str(item.get('model_id') or item.get('model_name') or name).strip()
            if model_id:
                result.append({'name': name or model_id, 'model_id': model_id})
        return result

    def _resolve_channel_index(self, target: str) -> Optional[int]:
        target = str(target or '').strip()
        if not target:
            return None
        channels = self.config.get('channels') or []
        if target.isdigit():
            idx = int(target)
            if 0 <= idx < len(channels):
                return idx
        for idx, channel in enumerate(channels):
            if target in {
                str(channel.get('name') or '').strip(),
                f'渠道{idx + 1}',
                f'channel{idx}',
                f'channel{idx + 1}',
            }:
                return idx
        return None

    def _build_model_entry(self, channel_idx: int, channel_name: str, model_name: str, model_id: str, channel: dict) -> dict:
        return {
            'channel_index': channel_idx,
            'channel_name': channel_name,
            'model_name': model_name,
            'model_id': model_id,
            'base_url': str(channel.get('base_url') or '').strip().rstrip('/'),
            'api_key': str(channel.get('api_key') or '').strip(),
            'messages_path': str(channel.get('messages_path') or '/v1/messages').strip() or '/v1/messages',
            'display_name': f'{channel_name}/{model_name}',
        }

    def _rebuild_available_models(self):
        self.available_models = []
        channels = self.config.get('channels') or []
        main_models = (self.config.get('main') or {}).get('models') or []
        seen: set[tuple[int, str]] = set()

        for item in main_models:
            try:
                channel_idx = int(item.get('channel'))
            except (TypeError, ValueError):
                continue
            if channel_idx < 0 or channel_idx >= len(channels):
                continue
            channel = channels[channel_idx]
            model_id = str(item.get('model_name') or item.get('model_id') or item.get('name') or '').strip()
            if not model_id:
                continue
            channel_name = str(channel.get('name') or f'渠道{channel_idx + 1}').strip()
            display_model = str(item.get('name') or model_id).strip()
            key = (channel_idx, model_id)
            if key in seen:
                continue
            seen.add(key)
            self.available_models.append(self._build_model_entry(channel_idx, channel_name, display_model, model_id, channel))

        for channel_idx, channel in enumerate(channels):
            channel_name = str(channel.get('name') or f'渠道{channel_idx + 1}').strip()
            for model in channel.get('models') or []:
                model_name = str(model.get('name') or model.get('model_id') or '').strip()
                model_id = str(model.get('model_id') or model_name).strip()
                if not model_id:
                    continue
                key = (channel_idx, model_id)
                if key in seen:
                    continue
                seen.add(key)
                self.available_models.append(self._build_model_entry(channel_idx, channel_name, model_name or model_id, model_id, channel))

    def _select_initial_model(self, preferred_display: str = ''):
        """根据 main.default_channel/default_model 或当前偏好选择初始模型"""
        self._rebuild_available_models()
        if not self.available_models:
            warn('[ModelManager] 无可用模型')
            return

        if preferred_display:
            for idx, item in enumerate(self.available_models):
                if item['display_name'] == preferred_display:
                    self.current_model_index = idx
                    current = self.available_models[self.current_model_index]
                    info(f'[ModelManager] 当前模型 {current["display_name"]} id={current["model_id"]}')
                    return

        self.current_model_index = min(self.current_model_index, len(self.available_models) - 1)
        main_config = self.config.get('main') or {}
        default_channel = str(main_config.get('default_channel') or '').strip()
        default_model = str(main_config.get('default_model') or '').strip()

        if default_channel or default_model:
            for idx, item in enumerate(self.available_models):
                channel_ok = not default_channel or item['channel_name'] == default_channel
                model_ok = not default_model or item['model_name'] == default_model or item['model_id'] == default_model
                if channel_ok and model_ok:
                    self.current_model_index = idx
                    break

        current = self.available_models[self.current_model_index]
        info(f'[ModelManager] 当前模型 {current["display_name"]} id={current["model_id"]}')

    def _persist_current_selection(self) -> tuple[bool, str]:
        current = self.get_current_model_entry()
        if not current:
            return False, '当前没有可持久化的模型'
        main = self.config.setdefault('main', {})
        main['default_channel'] = current['channel_name']
        main['default_model'] = current['model_name']
        ok, msg = self._save_config()
        if not ok:
            return False, msg
        return True, f'已持久化当前模型 {current["display_name"]}'

    def get_current_model_entry(self) -> Optional[dict]:
        if not self.available_models or self.current_model_index >= len(self.available_models):
            return None
        return dict(self.available_models[self.current_model_index])

    def get_current_model(self) -> Optional[dict]:
        """获取当前选中的模型配置"""
        model = self.get_current_model_entry()
        if not model:
            return None
        return {
            'base_url': model['base_url'],
            'api_key': model['api_key'],
            'model_name': model['model_id'],
            'messages_path': model['messages_path'],
            'display_name': model['display_name'],
        }

    def get_role_model(self, role: str) -> Optional[dict]:
        """获取指定角色的独立模型配置，未配置时回退到 get_current_model()"""
        roles = self.config.get('roles') or {}
        role_config = roles.get(role)
        if not role_config:
            return self.get_current_model()
        channel_idx = self._resolve_channel_index(str(role_config.get('channel') or ''))
        if channel_idx is None:
            return self.get_current_model()
        channels = self.config.get('channels') or []
        if channel_idx >= len(channels):
            return self.get_current_model()
        channel = channels[channel_idx]
        model_name = str(role_config.get('model_name') or '').strip()
        if not model_name:
            return self.get_current_model()
        return {
            'base_url': str(channel.get('base_url') or '').strip().rstrip('/'),
            'api_key': str(channel.get('api_key') or '').strip(),
            'model_name': model_name,
            'messages_path': str(channel.get('messages_path') or '/v1/messages').strip() or '/v1/messages',
            'display_name': f'{str(channel.get("name") or f"渠道{channel_idx+1}").strip()}/{model_name}',
        }

    def get_vision_model(self) -> Optional[dict]:
        """获取视觉模型配置"""
        vision = self.config.get('vision', {})
        if vision.get('base_url') and vision.get('model_id'):
            return {
                'base_url': vision['base_url'].rstrip('/'),
                'api_key': vision.get('api_key', ''),
                'model_name': vision['model_id'],
            }

        channels = self.config.get('channels') or []
        models = vision.get('models') or []
        if not models:
            return None
        item = models[0]
        try:
            channel_idx = int(item.get('channel'))
        except (TypeError, ValueError):
            return None
        if channel_idx < 0 or channel_idx >= len(channels):
            return None
        model_id = str(item.get('model_name') or '').strip()
        if not model_id:
            return None
        channel = channels[channel_idx]
        return {
            'base_url': str(channel.get('base_url') or '').strip().rstrip('/'),
            'api_key': str(channel.get('api_key') or '').strip(),
            'model_name': model_id,
        }

    def get_summary_text(self) -> str:
        current = self.get_current_model_entry()
        if not current:
            return '当前没有可用模型。'
        channels = self.config.get('channels') or []
        return (
            f'当前模型: {current["display_name"]}\n'
            f'base_url: {current["base_url"]}\n'
            f'messages_path: {current["messages_path"]}\n'
            f'渠道数: {len(channels)} | 可用模型数: {len(self.available_models)}'
        )

    def switch_model(self, target: str, persist: bool = False) -> tuple[bool, str]:
        """切换模型，支持 channel/model、model 或列表序号"""
        target = str(target or '').strip()
        if not self.available_models:
            return False, '无可用模型'
        if not target:
            return False, '缺少模型名称'

        if target.isdigit():
            idx = int(target)
            if 0 <= idx < len(self.available_models):
                self.current_model_index = idx
                message = f'已切换到 {self.available_models[idx]["display_name"]}'
                if persist:
                    ok, persist_msg = self._persist_current_selection()
                    if not ok:
                        return False, persist_msg
                    message = f'{message}\n{persist_msg}'
                return True, message

        for idx, item in enumerate(self.available_models):
            if target in {item['display_name'], item['model_name'], item['model_id']}:
                self.current_model_index = idx
                message = f'已切换到 {item["display_name"]}'
                if persist:
                    ok, persist_msg = self._persist_current_selection()
                    if not ok:
                        return False, persist_msg
                    message = f'{message}\n{persist_msg}'
                return True, message

        return False, f'模型不存在: {target}'

    def switch_next_model(self) -> Optional[dict]:
        """切换到下一个模型（已禁用轮询 failover）"""
        return None

    def list_models(self) -> str:
        """列出所有可用模型"""
        if not self.available_models:
            return '无可用模型'

        lines = [self.get_summary_text(), '', '可用模型:']
        for idx, item in enumerate(self.available_models):
            marker = ' [当前]' if idx == self.current_model_index else ''
            lines.append(
                f'  {idx}. {item["display_name"]} ({item["model_id"]}) | '
                f'channel={item["channel_index"]} | path={item["messages_path"]}{marker}'
            )
        return '\n'.join(lines)

    def list_channels(self) -> str:
        channels = self.config.get('channels') or []
        if not channels:
            return '当前没有配置任何渠道。'
        lines = ['渠道列表:']
        for idx, channel in enumerate(channels):
            models = self._normalize_model_items(channel.get('models'))
            model_text = ', '.join(f'{m["name"]}:{m["model_id"]}' for m in models) or '(无模型)'
            lines.append(
                f'  {idx}. {channel.get("name") or f"渠道{idx + 1}"} | '
                f'base_url={str(channel.get("base_url") or "").strip()} | '
                f'messages_path={str(channel.get("messages_path") or "/v1/messages").strip() or "/v1/messages"} | '
                f'api_key={self._mask_secret(channel.get("api_key") or "")} | '
                f'models={model_text}'
            )
        return '\n'.join(lines)

    def add_channel(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        messages_path: str = '/v1/messages',
        models=None,
    ) -> tuple[bool, str]:
        name = str(name or '').strip()
        base_url = str(base_url or '').strip().rstrip('/')
        api_key = str(api_key or '').strip()
        messages_path = str(messages_path or '/v1/messages').strip() or '/v1/messages'
        if not name or not base_url:
            return False, '新增渠道至少需要 name 和 base_url。'
        channels = self.config.setdefault('channels', [])
        if any(str(item.get('name') or '').strip() == name for item in channels):
            return False, f'渠道名称已存在: {name}'
        channels.append(
            {
                'name': name,
                'base_url': base_url,
                'api_key': api_key,
                'messages_path': messages_path,
                'models': self._normalize_model_items(models),
            }
        )
        ok, msg = self._save_config()
        if not ok:
            channels.pop()
            return False, msg
        self.reload_config()
        return True, f'已新增渠道 {name}。\n{msg}'

    def update_channel(self, target: str, **fields) -> tuple[bool, str]:
        idx = self._resolve_channel_index(target)
        if idx is None:
            return False, f'未找到渠道: {target}'
        channels = self.config.setdefault('channels', [])
        channel = channels[idx]
        old_name = str(channel.get('name') or '').strip()
        if 'name' in fields and fields['name'] is not None:
            new_name = str(fields['name']).strip()
            if not new_name:
                return False, 'name 不能为空。'
            for other_idx, item in enumerate(channels):
                if other_idx != idx and str(item.get('name') or '').strip() == new_name:
                    return False, f'渠道名称已存在: {new_name}'
            channel['name'] = new_name
        if 'base_url' in fields and fields['base_url'] is not None:
            channel['base_url'] = str(fields['base_url']).strip().rstrip('/')
        if 'api_key' in fields and fields['api_key'] is not None:
            channel['api_key'] = str(fields['api_key']).strip()
        if 'messages_path' in fields and fields['messages_path'] is not None:
            channel['messages_path'] = str(fields['messages_path']).strip() or '/v1/messages'
        if 'models' in fields and fields['models'] is not None:
            channel['models'] = self._normalize_model_items(fields['models'])
        main = self.config.setdefault('main', {})
        if old_name and str(main.get('default_channel') or '').strip() == old_name:
            main['default_channel'] = str(channel.get('name') or old_name).strip()
        ok, msg = self._save_config()
        if not ok:
            return False, msg
        self.reload_config()
        return True, f'已更新渠道 {target}。\n{msg}'

    def remove_channel(self, target: str) -> tuple[bool, str]:
        idx = self._resolve_channel_index(target)
        if idx is None:
            return False, f'未找到渠道: {target}'
        channels = self.config.setdefault('channels', [])
        removed = channels.pop(idx)
        main = self.config.setdefault('main', {})
        new_main_models = []
        for item in (main.get('models') or []):
            try:
                channel_idx = int(item.get('channel'))
            except (TypeError, ValueError):
                continue
            if channel_idx == idx:
                continue
            new_item = dict(item)
            if channel_idx > idx:
                new_item['channel'] = channel_idx - 1
            new_main_models.append(new_item)
        main['models'] = new_main_models
        if str(main.get('default_channel') or '').strip() == str(removed.get('name') or '').strip():
            main['default_channel'] = ''
            main['default_model'] = ''
        vision = self.config.setdefault('vision', {})
        new_vision_models = []
        for item in (vision.get('models') or []):
            try:
                channel_idx = int(item.get('channel'))
            except (TypeError, ValueError):
                continue
            if channel_idx == idx:
                continue
            new_item = dict(item)
            if channel_idx > idx:
                new_item['channel'] = channel_idx - 1
            new_vision_models.append(new_item)
        if 'models' in vision:
            vision['models'] = new_vision_models
        ok, msg = self._save_config()
        if not ok:
            channels.insert(idx, removed)
            return False, msg
        self.reload_config()
        return True, f'已删除渠道 {removed.get("name") or target}。\n{msg}'
