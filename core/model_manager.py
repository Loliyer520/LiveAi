"""模型配置管理器 - 读取 models_config.json 并提供统一的模型选择接口"""

import json
from pathlib import Path
from typing import Optional
from pack.console_logger import info, warn, error


class ModelManager:
    def __init__(self, config_path: str = 'data/models_config.json'):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.current_channel_index = 0
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

    def _select_initial_model(self):
        """根据 main.default_channel 和 default_model 选择初始模型"""
        channels = self.config.get('channels', [])
        if not channels:
            warn('[ModelManager] 无可用渠道')
            return

        main_config = self.config.get('main', {})
        default_channel = main_config.get('default_channel')
        default_model = main_config.get('default_model')

        # 查找默认渠道
        if default_channel:
            for idx, ch in enumerate(channels):
                if ch.get('name') == default_channel:
                    self.current_channel_index = idx
                    break

        # 查找默认模型
        current_channel = channels[self.current_channel_index]
        models = current_channel.get('models', [])
        if default_model and models:
            for idx, m in enumerate(models):
                if m.get('name') == default_model:
                    self.current_model_index = idx
                    break

        info(
            f'[ModelManager] 当前模型 '
            f'channel={current_channel.get("name")} '
            f'model={models[self.current_model_index].get("name") if models else "none"}'
        )

    def get_current_model(self) -> Optional[dict]:
        """获取当前选中的模型配置"""
        channels = self.config.get('channels', [])
        if not channels or self.current_channel_index >= len(channels):
            return None

        channel = channels[self.current_channel_index]
        models = channel.get('models', [])
        if not models or self.current_model_index >= len(models):
            return None

        model = models[self.current_model_index]
        return {
            'base_url': channel['base_url'].rstrip('/'),
            'api_key': channel['api_key'],
            'model_name': model['model_id'],
            'messages_path': '/v1/messages',  # Anthropic 兼容接口固定路径
            'display_name': f"{channel['name']}/{model['name']}",
        }

    def get_vision_model(self) -> Optional[dict]:
        """获取视觉模型配置"""
        vision = self.config.get('vision', {})
        if not vision.get('base_url') or not vision.get('model_id'):
            return None

        return {
            'base_url': vision['base_url'].rstrip('/'),
            'api_key': vision.get('api_key', ''),
            'model_name': vision['model_id'],
        }

    def switch_model(self, target: str) -> tuple[bool, str]:
        """切换模型

        Args:
            target: 格式 "channel/model" 或 "model"（当前渠道）

        Returns:
            (成功, 消息)
        """
        channels = self.config.get('channels', [])
        if not channels:
            return False, '无可用渠道'

        parts = target.split('/', 1)
        if len(parts) == 2:
            channel_name, model_name = parts
            # 查找渠道
            channel_idx = None
            for idx, ch in enumerate(channels):
                if ch.get('name') == channel_name:
                    channel_idx = idx
                    break
            if channel_idx is None:
                return False, f'渠道不存在: {channel_name}'

            # 查找模型
            models = channels[channel_idx].get('models', [])
            model_idx = None
            for idx, m in enumerate(models):
                if m.get('name') == model_name:
                    model_idx = idx
                    break
            if model_idx is None:
                return False, f'模型不存在: {model_name}'

            self.current_channel_index = channel_idx
            self.current_model_index = model_idx
            return True, f'已切换到 {channel_name}/{model_name}'

        else:
            model_name = parts[0]
            current_channel = channels[self.current_channel_index]
            models = current_channel.get('models', [])
            model_idx = None
            for idx, m in enumerate(models):
                if m.get('name') == model_name:
                    model_idx = idx
                    break
            if model_idx is None:
                return False, f'当前渠道无此模型: {model_name}'

            self.current_model_index = model_idx
            return True, f'已切换到 {current_channel.get("name")}/{model_name}'

    def list_models(self) -> str:
        """列出所有可用模型"""
        channels = self.config.get('channels', [])
        if not channels:
            return '无可用渠道'

        lines = ['可用模型:']
        current = self.get_current_model()
        current_display = current['display_name'] if current else ''

        for ch in channels:
            lines.append(f"\n渠道: {ch['name']}")
            for m in ch.get('models', []):
                display = f"{ch['name']}/{m['name']}"
                marker = ' [当前]' if display == current_display else ''
                lines.append(f"  · {m['name']}{marker}")

        return '\n'.join(lines)
