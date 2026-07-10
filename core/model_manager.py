"""模型配置管理器 - 读取 models_config.json 并提供统一的模型选择接口"""

import json
from pathlib import Path
from typing import Optional
from pack.console_logger import info, warn, error


class ModelManager:
    def __init__(self, config_path: str = 'data/models_config.json'):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.available_models: list[dict] = []
        self.current_model_index = 0
        self._select_initial_model()

    def _load_config(self) -> dict:
        """加载模型配置文件"""
        if not self.config_path.exists():
            warn(f'[ModelManager] 配置文件不存在，使用空配置: {self.config_path}')
            return {'channels': [], 'vision': {}, 'main': {}, 'tiered': {}}

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            info(f'[ModelManager] 已加载配置 channels={len(config.get("channels", []))}')
            return config
        except Exception as e:
            error(f'[ModelManager] 配置加载失败: {e}')
            return {'channels': [], 'vision': {}, 'main': {}, 'tiered': {}}

    def reload_config(self):
        """重新加载配置文件（用于热更新）"""
        self.config = self._load_config()
        self._select_initial_model()

    def _rebuild_available_models(self):
        self.available_models = []
        channels = self.config.get('channels') or []
        main_models = (self.config.get('main') or {}).get('models') or []

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
            self.available_models.append(self._build_model_entry(channel_idx, channel_name, display_model, model_id, channel))

        if self.available_models:
            return

        for channel_idx, channel in enumerate(channels):
            channel_name = str(channel.get('name') or f'渠道{channel_idx + 1}').strip()
            for model in channel.get('models') or []:
                model_name = str(model.get('name') or model.get('model_id') or '').strip()
                model_id = str(model.get('model_id') or model_name).strip()
                if not model_id:
                    continue
                self.available_models.append(self._build_model_entry(channel_idx, channel_name, model_name or model_id, model_id, channel))

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

    def _select_initial_model(self):
        """根据 main.default_channel/default_model 或 main.models 选择初始模型"""
        self._rebuild_available_models()
        if not self.available_models:
            warn('[ModelManager] 无可用模型')
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

    def get_current_model(self) -> Optional[dict]:
        """获取当前选中的模型配置"""
        if not self.available_models or self.current_model_index >= len(self.available_models):
            return None
        model = self.available_models[self.current_model_index]
        return {
            'base_url': model['base_url'],
            'api_key': model['api_key'],
            'model_name': model['model_id'],
            'messages_path': model['messages_path'],
            'display_name': model['display_name'],
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

    def switch_model(self, target: str) -> tuple[bool, str]:
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
                return True, f'已切换到 {self.available_models[idx]["display_name"]}'

        for idx, item in enumerate(self.available_models):
            if target in {item['display_name'], item['model_name'], item['model_id']}:
                self.current_model_index = idx
                return True, f'已切换到 {item["display_name"]}'

        return False, f'模型不存在: {target}'

    def switch_next_model(self) -> Optional[dict]:
        """切换到下一个模型，用于失败自动轮询"""
        if len(self.available_models) <= 1:
            return None
        self.current_model_index = (self.current_model_index + 1) % len(self.available_models)
        current = self.get_current_model()
        if current:
            warn(f'[ModelManager] 自动切换到 {current["display_name"]}')
        return current

    def list_models(self) -> str:
        """列出所有可用模型"""
        if not self.available_models:
            return '无可用模型'

        lines = ['可用模型:']
        for idx, item in enumerate(self.available_models):
            marker = ' [当前]' if idx == self.current_model_index else ''
            lines.append(f"  {idx}. {item['display_name']} ({item['model_id']}){marker}")
        return '\n'.join(lines)
