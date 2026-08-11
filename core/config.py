from dataclasses import dataclass, field
import os
from pathlib import Path
import yaml

from pack.console_logger import warn


def _load_yaml_config() -> dict:
    """Load configuration from config.yaml if it exists"""
    config_path = Path(__file__).resolve().parent.parent / 'config.yaml'
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            warn(f'加载 config.yaml 失败: {e}')
            return {}
    return {}


_yaml_config = _load_yaml_config()


def _get(section: str, key: str, env_key: str, default: str) -> str:
    """Get config value: YAML > ENV > default"""
    yaml_value = _yaml_config.get(section, {}).get(key)
    if yaml_value is not None:
        return str(yaml_value)
    return os.getenv(env_key, default)


def _get_int(section: str, key: str, env_key: str, default: int) -> int:
    """Get integer config value: YAML > ENV > default"""
    yaml_value = _yaml_config.get(section, {}).get(key)
    if yaml_value is not None:
        return int(yaml_value)
    return int(os.getenv(env_key, str(default)))


def _get_float(section: str, key: str, env_key: str, default: float) -> float:
    """Get float config value: YAML > ENV > default"""
    yaml_value = _yaml_config.get(section, {}).get(key)
    if yaml_value is not None:
        return float(yaml_value)
    return float(os.getenv(env_key, str(default)))


def _get_bool(section: str, key: str, env_key: str, default: bool) -> bool:
    """Get boolean config value: YAML > ENV > default"""
    yaml_value = _yaml_config.get(section, {}).get(key)
    if yaml_value is not None:
        return bool(yaml_value)
    return os.getenv(env_key, '1' if default else '0') == '1'


def _get_section(section: str) -> dict:
    value = _yaml_config.get(section, {})
    return dict(value) if isinstance(value, dict) else {}




@dataclass
class NapcatConfig:
    ws_url: str = _get('napcat', 'ws_url', 'BOT_WS_URL', 'ws://localhost:3001/ws')
    http_url: str = _get('napcat', 'http_url', 'BOT_HTTP_URL', 'http://localhost:8080')
    http_access_token: str = _get('napcat', 'http_access_token', 'BOT_HTTP_ACCESS_TOKEN', 'your_token_here')
    self_id: int = _get_int('napcat', 'self_id', 'BOT_SELF_ID', 1234567890)


@dataclass
class SatangyunConfig:
    target_group_id: int = _get_int('satangyun', 'target_group_id', 'SATANGYUN_GROUP_ID', 0)
    auth_api_base: str = _get('satangyun', 'auth_api_base', 'SATANGYUN_AUTH_BASE', 'https://auth.example.com')
    admin_token: str = _get('satangyun', 'admin_token', 'SATANGYUN_ADMIN_TOKEN', 'your_admin_token')
    notice_image_url: str = _get('satangyun', 'notice_image_url', 'SATANGYUN_NOTICE_IMAGE', 'https://example.com/notice.jpg')
    normal_draw_url: str = _get('satangyun', 'normal_draw_url', 'SATANGYUN_DRAW_URL', 'https://example.com/generate')
    normal_draw_token: str = _get('satangyun', 'normal_draw_token', 'SATANGYUN_DRAW_TOKEN', 'your_draw_token')
    welcome_model: str = _get('satangyun', 'welcome_model', 'SATANGYUN_WELCOME_MODEL', 'gpt-4o-mini')


@dataclass
class AIConfig:
    enabled: bool = _get_bool('ai', 'enabled', 'AI_ENABLED', True)
    admin_qq: int = _get_int('ai', 'admin_qq', 'AI_ADMIN_QQ', 0)
    master_qq: int = _get_int('ai', 'master_qq', 'AI_MASTER_QQ', 0)
    global_trigger_rate: float = _get_float('ai', 'global_trigger_rate', 'AI_GLOBAL_TRIGGER_RATE', 0.0)
    models_config_path: str = _get('ai', 'models_config_path', 'AI_MODELS_CONFIG_PATH', 'data/models_config.json')
    storage_path: str = _get('ai', 'storage_path', 'AI_STORAGE_PATH', 'data/msgs/ai_state.json')
    main_prompt_path: str = _get('ai', 'main_prompt_path', 'AI_MAIN_PROMPT_PATH', 'data/prompt/main.txt')
    staff_prompt_path: str = _get('ai', 'staff_prompt_path', 'AI_STAFF_PROMPT_PATH', 'data/prompt/staff.txt')
    char_prompt_path: str = _get('ai', 'char_prompt_path', 'AI_CHAR_PROMPT_PATH', 'data/prompt/char.txt')
    worker_count: int = _get_int('ai', 'worker_count', 'AI_WORKER_COUNT', 2)
    chat_model_workers: int = _get_int(
        'ai', 'chat_model_workers', 'AI_CHAT_MODEL_WORKERS', max(4, worker_count)
    )
    background_workers: int = _get_int(
        'ai', 'background_workers', 'AI_BACKGROUND_WORKERS', max(1, min(4, worker_count))
    )
    history_limit: int = _get_int('ai', 'history_limit', 'AI_HISTORY_LIMIT', 500)
    diary_size: int = _get_int('ai', 'diary_size', 'AI_DIARY_SIZE', 50)
    search_api_key: str = _get('ai', 'search_api_key', 'AI_SEARCH_API_KEY', 'your_search_api_key')
    search_base_url: str = _get('ai', 'search_base_url', 'AI_SEARCH_BASE_URL', 'https://open.feedcoopapi.com/search_api/global_search')
    search_doc_count: int = _get_int('ai', 'search_doc_count', 'AI_SEARCH_DOC_COUNT', 5)
    tts_provider: str = _get('ai', 'tts_provider', 'AI_TTS_PROVIDER', 'fish_audio')
    tts_api_key: str = _get('ai', 'tts_api_key', 'AI_TTS_API_KEY', '')
    tts_reference_id: str = _get('ai', 'tts_reference_id', 'AI_TTS_REFERENCE_ID', '')
    tts_base_url: str = _get('ai', 'tts_base_url', 'AI_TTS_BASE_URL', '')
    tts_model: str = _get('ai', 'tts_model', 'AI_TTS_MODEL', 's2-pro')
    github_api_token: str = _get('ai', 'github_api_token', 'AI_GITHUB_API_TOKEN', 'ghp_your_github_token')
    update_repo_owner: str = _get('ai', 'update_repo_owner', 'AI_UPDATE_REPO_OWNER', 'Loliyer520')
    update_repo_name: str = _get('ai', 'update_repo_name', 'AI_UPDATE_REPO_NAME', 'LiveAi')
    auto_update_enabled: bool = _get_bool('ai', 'auto_update_enabled', 'AI_AUTO_UPDATE_ENABLED', True)
    auto_update_check_hour: int = _get_int('ai', 'auto_update_check_hour', 'AI_AUTO_UPDATE_CHECK_HOUR', 4)
    tasker_prompt_path: str = _get('ai', 'tasker_prompt_path', 'AI_TASKER_PROMPT_PATH', _get('ai', 'dev_agent_prompt_path', 'AI_DEV_AGENT_PROMPT_PATH', 'data/prompt/dev_agent.txt'))
    # Legacy attribute kept for old runtime/extensions.
    dev_agent_prompt_path: str = tasker_prompt_path
    agent_prompt_path: str = _get('ai', 'agent_prompt_path', 'AI_AGENT_PROMPT_PATH', 'data/prompt/agent.txt')
    ssh_profiles: list['SSHProfileConfig'] = field(default_factory=lambda: _load_ssh_profiles())


@dataclass(frozen=True)
class SSHProfileConfig:
    profile_id: str
    target: str
    root_dir: str = '~'
    port: int = 22
    identity_file: str = ''
    password: str = ''
    shell: str = 'bash'
    strict_host_key_checking: bool = True


def parse_ssh_profiles(raw_profiles, warn_prefix: str = 'ai.ssh_profiles') -> list[SSHProfileConfig]:
    profiles: list[SSHProfileConfig] = []
    if not isinstance(raw_profiles, list):
        warn(f'{warn_prefix} 配置格式无效，应为列表，已忽略。')
        return profiles
    for index, item in enumerate(raw_profiles, start=1):
        if not isinstance(item, dict):
            warn(f'{warn_prefix}[{index}] 不是对象，已忽略。')
            continue
        profile_id = str(item.get('id') or item.get('profile_id') or '').strip()
        target = str(item.get('target') or '').strip()
        if not profile_id or not target:
            warn(f'{warn_prefix}[{index}] 缺少 id/target，已忽略。')
            continue
        try:
            profiles.append(
                SSHProfileConfig(
                    profile_id=profile_id,
                    target=target,
                    root_dir=str(item.get('root_dir') or '~').strip() or '~',
                    port=int(item.get('port') or 22),
                    identity_file=str(item.get('identity_file') or '').strip(),
                    password=str(item.get('password') or '').strip(),
                    shell=str(item.get('shell') or 'bash').strip() or 'bash',
                    strict_host_key_checking=bool(item.get('strict_host_key_checking', True)),
                )
            )
        except Exception as exc:
            warn(f'{warn_prefix}[{index}] 解析失败，已忽略: {exc}')
    return profiles


def _load_ssh_profiles() -> list[SSHProfileConfig]:
    return parse_ssh_profiles(_get_section('ai').get('ssh_profiles') or [])

@dataclass
class WebUIConfig:
    port: int = _get_int('webui', 'port', 'WEBUI_PORT', 8765)

    enabled: bool = _get_bool('webui', 'enabled', 'WEBUI_ENABLED', True)
    host: str = _get('webui', 'host', 'WEBUI_HOST', '127.0.0.1')

@dataclass
class AppConfig:
    napcat: NapcatConfig = field(default_factory=NapcatConfig)
    satangyun: SatangyunConfig = field(default_factory=SatangyunConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    webui: WebUIConfig = field(default_factory=WebUIConfig)


def save_config_to_yaml(updates: dict):
    """Save configuration updates back to config.yaml"""
    config_path = Path(__file__).resolve().parent.parent / 'config.yaml'

    # Load existing config or start fresh
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            config = {}
    else:
        config = {}

    # Deep merge updates
    for section, values in updates.items():
        if section not in config:
            config[section] = {}
        if isinstance(values, dict):
            config[section].update(values)
        else:
            config[section] = values

    # Write back
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        warn(f'保存 config.yaml 失败: {e}')
        return False
