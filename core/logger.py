"""结构化日志系统：环形缓冲区 + 优先级过滤 + 分类查询。

日志级别（用于 entry.level 字段）：
  info   - 常规信息
  warn   - 警告
  error  - 错误/异常

日志分类（用于 entry.category 字段）：
  agent  - 后台 agent（create_agent / run_agent_loop / destroy_agent）
  task   - 后台任务（set_alarm / notify_master / dev_agent 等）
  api    - API 调用（请求/响应/状态码/重试）
  chat   - 聊天 AI（触发判定/排队合并/生成）

优先级过滤规则（query_logs 的 priority 参数）：
  0 — 所有日志，不过滤
  1 — 忽略 API 的 info，只留 API 的 warn/error；其他分类全部保留
  2 — 忽略所有 info，只留 warn/error（即只看异常和告警）
  3 — 只看 agent 分类的完整日志
  4 — 只看 chat 分类的完整日志
  5 — 同 4（完整 AI 日志）
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field


# 日志级别
INFO = 'info'
WARN = 'warn'
ERROR = 'error'

# 日志分类
CAT_AGENT = 'agent'
CAT_TASK = 'task'
CAT_API = 'api'
CAT_CHAT = 'chat'

ALL_CATEGORIES = {CAT_AGENT, CAT_TASK, CAT_API, CAT_CHAT}


@dataclass
class LogEntry:
    """单条日志记录。保留所有字段以便后续按需扩展查询维度。"""
    timestamp: float = field(default_factory=time.time)
    level: str = INFO         # info / warn / error
    category: str = CAT_CHAT  # agent / task / api / chat
    scope_key: str = ''       # 会话标识，如 'group:123'、'private:456'、'master:0'、''（全局）
    message: str = ''         # 正文

    def to_dict(self) -> dict:
        return {
            'ts': self.timestamp,
            'level': self.level,
            'category': self.category,
            'scope_key': self.scope_key,
            'message': self.message,
        }


class BotLogger:
    """全局唯一的结构化日志实例。

    特性：
    - 环形缓冲区，max_entries 条上限，超出自动丢弃最早记录
    - 线程安全（threading.Lock）
    - 分级查询：按 priority（0-5）+ scope_key 过滤 + count 截断
    - 返回最近 N 条，时间倒序（最新在前）
    """

    def __init__(self, max_entries: int = 10000):
        self._max_entries = max(100, int(max_entries or 10000))
        self._buffer: deque[LogEntry] = deque(maxlen=self._max_entries)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def log(self, level: str, category: str, scope_key: str, message: str) -> None:
        """写入一条日志。"""
        level = str(level or INFO).strip().lower()
        if level not in (INFO, WARN, ERROR):
            level = INFO
        category = str(category or CAT_CHAT).strip().lower()
        if category not in ALL_CATEGORIES:
            category = CAT_CHAT
        scope_key = str(scope_key or '').strip()
        message = str(message or '')[:2000]  # 单条上限 2000 字符

        entry = LogEntry(
            timestamp=time.time(),
            level=level,
            category=category,
            scope_key=scope_key,
            message=message,
        )
        with self._lock:
            self._buffer.append(entry)

    def info(self, category: str, scope_key: str, message: str) -> None:
        self.log(INFO, category, scope_key, message)

    def warn(self, category: str, scope_key: str, message: str) -> None:
        self.log(WARN, category, scope_key, message)

    def error(self, category: str, scope_key: str, message: str) -> None:
        self.log(ERROR, category, scope_key, message)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query(
        self,
        count: int = 20,
        priority: int = 0,
        scope_key: str = '',
    ) -> list[dict]:
        """按优先级和会话过滤，返回最近 count 条日志（时间倒序，最新在前）。

        count:  返回条数，1-200
        priority: 0-5，过滤规则见模块 docstring
        scope_key: 会话标识，空字符串表示不过滤
        """
        count = max(1, min(200, int(count or 20)))
        try:
            priority = int(priority or 0)
        except (TypeError, ValueError):
            priority = 0
        priority = max(0, min(5, priority))
        scope_key = str(scope_key or '').strip()

        with self._lock:
            # 从最新往旧遍历
            entries = list(self._buffer)

        result: list[dict] = []
        for entry in reversed(entries):
            if not self._match_priority(entry, priority):
                continue
            if scope_key and entry.scope_key and entry.scope_key != scope_key:
                # scope_key 精确匹配：查询方传群号/QQ号时可精确命中
                # 空 scope_key 的日志（全局）在指定 scope_key 时也返回，因为它们是全局性的
                if entry.scope_key:
                    continue
            result.append(entry.to_dict())
            if len(result) >= count:
                break

        return result

    def query_text(
        self,
        count: int = 20,
        priority: int = 0,
        scope_key: str = '',
    ) -> str:
        """同 query，但返回格式化的可读文本，供 AI 工具直接展示。"""
        entries = self.query(count=count, priority=priority, scope_key=scope_key)
        if not entries:
            scope_info = f'（scope={scope_key}）' if scope_key else ''
            return f'暂无匹配日志记录{scope_info}。'

        from datetime import datetime

        lines: list[str] = []
        for i, e in enumerate(entries, 1):
            ts = datetime.fromtimestamp(e['ts']).strftime('%m-%d %H:%M:%S')
            cat = e['category'].upper()
            level = e['level'].upper()
            scope = f' [{e["scope_key"]}]' if e['scope_key'] else ''
            lines.append(f'#{i} {ts} {cat}/{level}{scope}  {e["message"]}')

        lines.insert(0, f'共 {len(entries)} 条日志（优先级={priority}）：')
        return '\n'.join(lines)

    def stats(self) -> dict:
        """返回日志缓冲区的简要统计。"""
        with self._lock:
            total = len(self._buffer)
        return {
            'total_entries': total,
            'max_entries': self._max_entries,
            'categories': list(ALL_CATEGORIES),
        }

    # ------------------------------------------------------------------
    # 内部：优先级匹配
    # ------------------------------------------------------------------
    @staticmethod
    def _match_priority(entry: LogEntry, priority: int) -> bool:
        """判断一条日志是否通过优先级过滤。"""
        if priority == 0:
            # 0：所有日志
            return True

        if priority == 1:
            # 1：忽略 API 的 info，API 的 warn/error 保留；其他分类全部保留
            if entry.category == CAT_API and entry.level == INFO:
                return False
            return True

        if priority == 2:
            # 2：忽略所有 info，只留 warn/error
            return entry.level in (WARN, ERROR)

        if priority == 3:
            # 3：只看 agent 分类的完整日志
            return entry.category == CAT_AGENT

        # priority 4 / 5：完整 AI（chat）日志
        return entry.category == CAT_CHAT


# 全局单例：供全项目直接 import 使用
_bot_logger: BotLogger | None = None
_logger_lock = threading.Lock()


def get_bot_logger() -> BotLogger:
    """获取全局唯一的 BotLogger 实例（惰性初始化，线程安全）。"""
    global _bot_logger
    if _bot_logger is None:
        with _logger_lock:
            if _bot_logger is None:
                _bot_logger = BotLogger(max_entries=10000)
    return _bot_logger


# 便捷函数：直接使用全局实例写入
def log_info(category: str, scope_key: str, message: str) -> None:
    get_bot_logger().info(category, scope_key, message)


def log_warn(category: str, scope_key: str, message: str) -> None:
    get_bot_logger().warn(category, scope_key, message)


def log_error(category: str, scope_key: str, message: str) -> None:
    get_bot_logger().error(category, scope_key, message)


def query_logs(count: int = 20, priority: int = 0, scope_key: str = '') -> list[dict]:
    return get_bot_logger().query(count=count, priority=priority, scope_key=scope_key)


def query_logs_text(count: int = 20, priority: int = 0, scope_key: str = '') -> str:
    return get_bot_logger().query_text(count=count, priority=priority, scope_key=scope_key)
