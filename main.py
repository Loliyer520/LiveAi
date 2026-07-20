import importlib.util
from pathlib import Path

from pack.napcat import NapcatBot
from core.config import AppConfig
from core.ai_repository import AIRepository
from core.ai_runtime import AIOrchestrator
from core.model_manager import ModelManager
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


def _auto_migrate_memories(state_path: str) -> None:
    """启动时自动迁移（方案B）：把单文件 ai_state.json 的 memories 按 scope 拆分。

    只在检测到旧结构（有 memories 键 / 无 _schema_version=2）时执行一次。
    迁移脚本内部会自动备份 + 完整性校验，校验不过则不替换真实文件并返回非0，
    此时中止启动（抛异常），避免用未迁移/半迁移状态继续跑。

    在 AIRepository 初始化之前、进程尚未把旧 memories 载入内存之前调用，
    确保不会与运行中的写盘抢占同一文件。
    """
    script_path = Path(__file__).resolve().parent / 'scripts' / 'migrate_split_memories.py'
    if not script_path.exists():
        return
    spec = importlib.util.spec_from_file_location('migrate_split_memories', script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    code = module.migrate(Path(state_path), dry_run=False)
    if code not in (0,):
        raise RuntimeError(
            f'ai_state.json 记忆拆分迁移失败 (code={code})，已中止启动。'
            f'原文件未被替换，请检查迁移日志后重试。'
        )


def build_app() -> NapcatBot:
    config = AppConfig()
    bot = NapcatBot(
        ws_url=config.napcat.ws_url,
        http_url=config.napcat.http_url,
        self_id=config.napcat.self_id,
        http_access_token=config.napcat.http_access_token,
    )

    model_manager = ModelManager(config.ai.models_config_path)
    current = model_manager.get_current_model() or {}
    _vision = model_manager.get_role_model('vision') or model_manager.get_vision_model() or {}

    shared_model = OpenAICompatibleChatModel(
        base_url=current.get('base_url', ''),
        api_key=current.get('api_key', ''),
        model_name=current.get('model_name', ''),
    )
    anthropic_model = AnthropicChatModel(
        base_url=current.get('base_url', ''),
        api_key=current.get('api_key', ''),
        model_name=current.get('model_name', ''),
        messages_path=current.get('messages_path', '/v1/messages'),
    )
    vision_model = OpenAICompatibleVisionModel(
        base_url=_vision.get('base_url', ''),
        api_key=_vision.get('api_key', ''),
        model_name=_vision.get('model_name', ''),
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

    ai_storage_path = _prepare_ai_storage_path(config.ai.storage_path)
    _auto_migrate_memories(ai_storage_path)
    ai_repo = AIRepository(JsonStore(ai_storage_path))
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
    _mm = ModelManager(config.ai.models_config_path)
    _cur = _mm.get_current_model() or {}
    model_name = _cur.get('display_name') or _cur.get('model_name', '未配置')
    model_url = _cur.get('base_url', '')

    ws_host = config.napcat.ws_url.split('?')[0].replace('ws://', '').replace('wss://', '')
    qq_str = str(config.napcat.self_id) if config.napcat.self_id else '未设置'

    panel_lines = [
        f"模型  {model_name}  ·  {_s(model_url, 'gray')}",
        f"QQ    {qq_str}",
        '',
        f"WebSocket  {_s(ws_host, 'gray')}",
        f"WebUI      {_s(f'http://{config.webui.host}:{config.webui.port}', 'gray')}",
    ]
    if version_line:
        panel_lines.insert(1, version_line)  # 插在模型行之后

    banner(panel_lines)

    build_app().start()
