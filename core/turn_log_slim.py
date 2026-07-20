"""turn_log 精简（瘦身）逻辑，迁移脚本与运行时 add_turn_log 共用。

号主定案（方案B）：
- 精简存储：不再保留完整 model_messages（220KB级的大头），只留一个 preview 摘要。
- tool_iterations 保留结构但对超长字符串字段截断，避免膨胀又保留调试可读性。
- turn_logs 每个 scope 最多保留 20 条。
"""

# 单个字符串字段截断上限（用于 tool_iterations 内部文本）
_STR_LIMIT = 2000
# preview 摘要长度（对应 webui 列表页 preview）
PREVIEW_LIMIT = 240
# turn_logs 每个 scope 保留条数
TURN_LOG_LIMIT = 20


def _truncate_str(value: str) -> str:
    if len(value) <= _STR_LIMIT:
        return value
    return value[:_STR_LIMIT] + f'…(截断,原{len(value)}字)'


def _truncate_deep(obj):
    """递归遍历 dict/list，对超长字符串截断，保留整体结构。
    不依赖具体字段名，适配 tool_iterations 任意内部结构。"""
    if isinstance(obj, str):
        return _truncate_str(obj)
    if isinstance(obj, dict):
        return {k: _truncate_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_deep(v) for v in obj]
    return obj


def slim_turn_log(log: dict) -> dict:
    """把单条 turn_log 精简：清空完整 model_messages、加 preview、截断 tool_iterations。
    保留其余所有小字段（agent_id/temperature/turn_meta/raw_reply/final_reply/
    generation_ms/note/turn_id/created_at）。"""
    if not isinstance(log, dict):
        return log
    mm = log.get('model_messages') or []
    preview = ''
    if len(mm) > 1 and isinstance(mm[1], dict):
        preview = str(mm[1].get('content') or '')[:PREVIEW_LIMIT]
    slim = dict(log)
    slim['model_messages'] = []
    slim['model_messages_dropped'] = len(mm)
    slim['preview'] = preview
    slim['tool_iterations'] = _truncate_deep(log.get('tool_iterations') or [])
    return slim
