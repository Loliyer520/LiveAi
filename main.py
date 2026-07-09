from pathlib import Path

from pack.napcat import NapcatBot
from core.config import AppConfig
from core.ai_repository import AIRepository
from core.ai_runtime import AIOrchestrator
from pack.satangyun import SatangyunModule
from pack.webui_server import WebUIService
from pack.anthropic_chat_model import AnthropicChatModel
from pack.chat_model import OpenAICompatibleChatModel
from pack.image_generation import NormalDrawingService
from pack.satangyun_api import SatangyunAPI
from pack.vision_model import OpenAICompatibleVisionModel
from pack.json_store import JsonStore


def _prepare_ai_storage_path(storage_path: str) -> str:
    target = Path(storage_path)
    legacy = Path('storage/ai_state.json')
    if target.exists() or not legacy.exists():
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy.replace(target)
    return str(target)


def build_app() -> NapcatBot:
    config = AppConfig()
    bot = NapcatBot(
        ws_url=config.napcat.ws_url,
        http_url=config.napcat.http_url,
        self_id=config.napcat.self_id,
        http_access_token=config.napcat.http_access_token,
    )

    shared_model = OpenAICompatibleChatModel(
        base_url=config.ai.model_base_url,
        api_key=config.ai.api_key,
        model_name=config.ai.model_name,
    )
    anthropic_model = AnthropicChatModel(
        base_url=config.ai.model_base_url,
        api_key=config.ai.api_key,
        model_name=config.ai.model_name,
        messages_path=config.ai.model_messages_path,
    )
    vision_model = OpenAICompatibleVisionModel(
        base_url=config.ai.vision_base_url,
        api_key=config.ai.vision_api_key,
        model_name=config.ai.vision_model_name,
    )

    satangyun_module = SatangyunModule(
        bot=bot,
        group_id=config.satangyun.target_group_id,
        api=SatangyunAPI(config.satangyun.auth_api_base, config.satangyun.admin_token),
        draw_service=NormalDrawingService(
            config.satangyun.normal_draw_url,
            config.satangyun.normal_draw_token,
        ),
        welcome_model=shared_model,
        notice_image_url=config.satangyun.notice_image_url,
        welcome_model_name=config.satangyun.welcome_model,
    )
    satangyun_module.register()

    ai_repo = AIRepository(JsonStore(_prepare_ai_storage_path(config.ai.storage_path)))
    ai_orchestrator = AIOrchestrator(config.ai, bot, ai_repo, anthropic_model, vision_model)
    ai_orchestrator.start()
    ai_orchestrator.register()

    if config.webui.enabled:
        webui = WebUIService(
            host=config.webui.host,
            port=config.webui.port,
            repo=ai_repo,
            orchestrator=ai_orchestrator,
        )
        webui.start()

    return bot


if __name__ == "__main__":
    config = AppConfig()
    from pack.console_logger import banner, _s

    # ── 启动版本检查 ──
    version_line = ''
    try:
        from pack.update_service import UpdateService
        us = UpdateService(
            github_token=config.ai.github_api_token,
            repo_owner=config.ai.update_repo_owner,
            repo_name=config.ai.update_repo_name,
        )
        v = us.check_now_sync()
        cur = v.get('current_version', '?')
        if v.get('has_update'):
            latest = v.get('latest_version', '?')
            version_line = f"版本  {cur}  ·  {_s('GitHub 有新版本 !', 'yellow')}  →  {latest}"
        elif v.get('latest_version') == '?':
            version_line = f"版本  {cur}  ·  {_s('GitHub: 无法检查', 'gray')}"
        else:
            version_line = f"版本  {cur}  ·  {_s('GitHub 已是最新', 'gray')}"
    except Exception:
        pass

    # ── 构建面板 ──
    model_label = config.ai.default_chat_profile
    _profile_map = {
        'claude': (config.ai.claude_model_name, config.ai.claude_model_base_url),
        'opus': (config.ai.claude_opus_model_name, config.ai.claude_opus_model_base_url),
        'pro': (config.ai.pro_model_name, config.ai.pro_model_base_url),
    }
    model_name, model_url = _profile_map.get(model_label, (config.ai.model_name, config.ai.model_base_url))

    ws_host = config.napcat.ws_url.split('?')[0].replace('ws://', '').replace('wss://', '')
    qq_str = str(config.napcat.self_id) if config.napcat.self_id else '未设置'

    panel_lines = [
        f"模型  {model_name}  ·  {_s(model_label, 'cyan')}  ·  {_s(model_url, 'gray')}",
        f"QQ    {qq_str}",
        '',
        f"WebSocket  {_s(ws_host, 'gray')}",
        f"WebUI      {_s(f'http://{config.webui.host}:{config.webui.port}', 'gray')}",
    ]
    if version_line:
        panel_lines.insert(1, version_line)  # 插在模型行之后

    banner(panel_lines)

    build_app().start()
