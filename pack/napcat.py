import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import requests
import websocket

from core.events import ChatMessage, GroupIncreaseEvent
from pack.console_logger import ok, warn, error as log_error


class NapcatBot:
    def __init__(self, ws_url: str, http_url: str, self_id: int, http_access_token: str = ''):
        self.ws_url = ws_url
        self.http_url = http_url.rstrip('/')
        self.self_id = self_id
        self.http_access_token = http_access_token
        self.ws = None
        self._event_handlers = {
            'group_message': [],
            'private_message': [],
            'group_increase': [],
            'self_message': [],
        }
        # 精确表：message_id（已归一化） -> 记录时间，用于 HTTP 响应拿到 message_id 后的精确去重
        self._recent_self_sent_ids: dict[int | str, float] = {}
        # 占位表：(chat_type, target_id, content_digest) -> 记录时间，
        # 在 self.post() 发出请求之前先占位，堵住 WS 回显先于 HTTP 响应到达的竞态窗口
        self._pending_self_sent: dict[tuple, float] = {}
        self._recent_self_sent_lock = threading.Lock()
        self._self_sent_ttl = 120.0
        # 占位表的兜底过期时间，避免请求异常/无 message_id 时占位残留过久误吞其他设备消息
        self._pending_self_sent_ttl = 30.0
        # 单线程分发执行器：所有 WS 消息的 handler 调用串行化，彻底杜绝并发竞态
        self._dispatch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='napcat-dispatch')

    def on_group_message(self, func: Callable[[ChatMessage], None]):
        self._event_handlers['group_message'].append(func)
        return func

    def on_private_message(self, func: Callable[[ChatMessage], None]):
        self._event_handlers['private_message'].append(func)
        return func

    def on_group_increase(self, func: Callable[[GroupIncreaseEvent], None]):
        self._event_handlers['group_increase'].append(func)
        return func

    def on_self_message(self, func: Callable[[ChatMessage], None]):
        self._event_handlers['self_message'].append(func)
        return func

    @staticmethod
    def _normalize_message_id(message_id):
        """把 message_id 归一化成统一类型用于去重比对：优先转 int，失败则兜底存 str。"""
        if message_id is None:
            return None
        try:
            return int(message_id)
        except (TypeError, ValueError):
            return str(message_id)

    @staticmethod
    def _canonical_content_key(message) -> str:
        """把消息内容（str 或 segment 列表）归一化成占位去重用的稳定摘要输入，
        只保留内容强相关、发送前后不会变化的字段，忽略回显时可能补充的额外字段
        （如 image 段的 url/file_size 等）。"""
        if isinstance(message, str):
            return message
        if isinstance(message, list):
            parts = []
            for segment in message:
                if not isinstance(segment, dict):
                    parts.append(str(segment))
                    continue
                seg_type = str(segment.get('type') or '')
                data = segment.get('data') or {}
                if seg_type == 'text':
                    parts.append(f"text:{data.get('text', '')}")
                elif seg_type == 'image':
                    parts.append(f"image:{data.get('file', '')}")
                elif seg_type == 'reply':
                    parts.append(f"reply:{data.get('id', '')}")
                elif seg_type == 'at':
                    parts.append(f"at:{data.get('qq', '')}")
                else:
                    parts.append(f'{seg_type}:{json.dumps(data, sort_keys=True, ensure_ascii=False)}')
            return '|'.join(parts)
        return str(message) if message is not None else ''

    def _pending_key(self, chat_type: str, target_id, message) -> tuple:
        digest = hashlib.sha1(self._canonical_content_key(message).encode('utf-8', 'ignore')).hexdigest()
        return (chat_type, target_id, digest)

    def _mark_pending_self_sent(self, chat_type: str, target_id, message) -> tuple:
        """在调用 self.post() 之前占位，标记该 scope+内容即将由本进程发出，
        用于堵住 WS message_sent 回显先于 HTTP 响应到达的竞态窗口。
        返回占位 key，供发送结束（无论成功/失败）后清理。"""
        key = self._pending_key(chat_type, target_id, message)
        with self._recent_self_sent_lock:
            now = time.time()
            self._pending_self_sent[key] = now
            expired = [k for k, ts in self._pending_self_sent.items() if now - ts > self._pending_self_sent_ttl]
            for k in expired:
                del self._pending_self_sent[k]
        return key

    def _clear_pending_self_sent(self, key: tuple) -> None:
        with self._recent_self_sent_lock:
            self._pending_self_sent.pop(key, None)

    def _is_pending_self_sent(self, chat_type: str, target_id, message) -> bool:
        key = self._pending_key(chat_type, target_id, message)
        with self._recent_self_sent_lock:
            ts = self._pending_self_sent.get(key)
            if ts is None:
                return False
            return time.time() - ts <= self._pending_self_sent_ttl

    def _remember_self_sent(self, message_id) -> None:
        normalized = self._normalize_message_id(message_id)
        if normalized is None:
            return
        with self._recent_self_sent_lock:
            now = time.time()
            self._recent_self_sent_ids[normalized] = now
            expired = [mid for mid, ts in self._recent_self_sent_ids.items() if now - ts > self._self_sent_ttl]
            for mid in expired:
                del self._recent_self_sent_ids[mid]

    def _is_recent_self_sent(self, message_id) -> bool:
        normalized = self._normalize_message_id(message_id)
        if normalized is None:
            return False
        with self._recent_self_sent_lock:
            if normalized in self._recent_self_sent_ids:
                return True
            if isinstance(normalized, int):
                return str(normalized) in self._recent_self_sent_ids
            try:
                return int(normalized) in self._recent_self_sent_ids
            except (TypeError, ValueError):
                return False

    def _on_error(self, ws, err):
        log_error(f'Napcat WebSocket 错误: {err}')

    def _on_close(self, ws, close_status_code, close_msg):
        warn(f'Napcat 连接已关闭 (code={close_status_code})')

    def _on_open(self, ws):
        ok('Napcat WebSocket 连接已建立')

    def _dispatch(self, handlers, payload):
        for handler in handlers:
            self._dispatch_executor.submit(handler, payload)

    def _event_self_ids(self, data: dict) -> set[str]:
        ids = {str(self.self_id)}
        event_self_id = data.get('self_id')
        if event_self_id not in {None, ''}:
            ids.add(str(event_self_id))
        return {item for item in ids if item}

    def _message_mentions_self(self, data: dict) -> bool:
        self_ids = self._event_self_ids(data)
        raw_message = str(data.get('raw_message') or '')
        for qq in re.findall(r'\[CQ:at,qq=(\d+)(?:,[^\]]*)?\]', raw_message):
            if qq in self_ids:
                return True
        segments = data.get('message') or []
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                if str(segment.get('type') or '') != 'at':
                    continue
                qq = str((segment.get('data') or {}).get('qq') or '')
                if qq in self_ids:
                    return True
        return False

    def _build_message(self, data: dict, chat_type: str) -> ChatMessage:
        raw_message = data.get('raw_message', '')
        chat_id = data.get('group_id') if chat_type == 'group' else data.get('user_id')
        return ChatMessage(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=data.get('user_id'),
            text=raw_message,
            raw_message=raw_message,
            sender=data.get('sender') or {},
            message_id=data.get('message_id'),
            mentions_self=self._message_mentions_self(data),
            raw_data=data,
        )

    def _build_self_message(self, data: dict, chat_type: str) -> ChatMessage:
        raw_message = data.get('raw_message', '')
        if chat_type == 'group':
            chat_id = data.get('group_id')
        else:
            chat_id = data.get('target_id') or data.get('user_id')
        return ChatMessage(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=data.get('user_id'),
            text=raw_message,
            raw_message=raw_message,
            sender=data.get('sender') or {},
            message_id=data.get('message_id'),
            mentions_self=False,
            raw_data=data,
        )

    def _on_message(self, ws, message: str):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        if data.get('meta_event_type') in {'heartbeat', 'lifecycle'}:
            return

        post_type = data.get('post_type')
        if post_type == 'message':
            message_type = data.get('message_type')
            if message_type == 'group':
                self._dispatch(self._event_handlers['group_message'], self._build_message(data, 'group'))
            elif message_type == 'private':
                self._dispatch(self._event_handlers['private_message'], self._build_message(data, 'private'))
            return

        if post_type == 'message_sent':
            message_type = data.get('message_type')
            if message_type == 'group':
                target_id = data.get('group_id')
            elif message_type == 'private':
                target_id = data.get('target_id') or data.get('user_id')
            else:
                target_id = None
            content = data.get('message')
            if content is None:
                content = data.get('raw_message', '')
            raw_content = data.get('raw_message', '')
            if self._is_recent_self_sent(data.get('message_id')) or (
                message_type in {'group', 'private'} and (
                    self._is_pending_self_sent(message_type, target_id, content) or
                    (raw_content and self._is_pending_self_sent(message_type, target_id, raw_content))
                )
            ):
                return

            if message_type == 'group':
                self._dispatch(self._event_handlers['self_message'], self._build_self_message(data, 'group'))
            elif message_type == 'private':
                self._dispatch(self._event_handlers['self_message'], self._build_self_message(data, 'private'))
            return

        if post_type == 'notice' and data.get('notice_type') == 'group_increase':
            event = GroupIncreaseEvent(
                group_id=data.get('group_id'),
                user_id=data.get('user_id'),
                sub_type=data.get('sub_type'),
                raw_data=data,
            )
            self._dispatch(self._event_handlers['group_increase'], event)

    def post(self, action: str, params: dict) -> dict:
        headers = {'Content-Type': 'application/json'}
        if self.http_access_token:
            headers['Authorization'] = f'Bearer {self.http_access_token}'
        response = requests.post(
            f'{self.http_url}/{action}',
            json=params,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def send_text(self, chat_type: str, target_id: int, message: str):
        action = 'send_group_msg' if chat_type == 'group' else 'send_private_msg'
        key = 'group_id' if chat_type == 'group' else 'user_id'
        pending_key = self._mark_pending_self_sent(chat_type, target_id, message)
        try:
            response = self.post(action, {key: target_id, 'message': message})
            mid = (response.get('data') or {}).get('message_id') if isinstance(response, dict) else None
            self._remember_self_sent(mid)
        finally:
            # 无论成功/失败都清理占位，避免残留误吞真实他设备消息
            self._clear_pending_self_sent(pending_key)
        return response

    def send_group_text(self, group_id: int, message: str):
        return self.send_text('group', group_id, message)

    def send_private_text(self, user_id: int, message: str):
        return self.send_text('private', user_id, message)

    def send_image(self, chat_type: str, target_id: int, file: str, text: str | None = None):
        segments = [{'type': 'image', 'data': {'summary': '[图片]', 'file': file}}]
        if text:
            segments.append({'type': 'text', 'data': {'text': text}})
        action = 'send_group_msg' if chat_type == 'group' else 'send_private_msg'
        key = 'group_id' if chat_type == 'group' else 'user_id'
        pending_key = self._mark_pending_self_sent(chat_type, target_id, segments)
        try:
            response = self.post(action, {key: target_id, 'message': segments})
            mid = (response.get('data') or {}).get('message_id') if isinstance(response, dict) else None
            self._remember_self_sent(mid)
        finally:
            self._clear_pending_self_sent(pending_key)
        return response

    def send_reply_text(self, message: ChatMessage, content: str):
        reply_code = f'[CQ:reply,id={message.message_id}]'
        return self.send_text(message.chat_type, message.chat_id, f'{reply_code}{content}')

    def recall_message(self, message_id) -> dict:
        return self.post('delete_msg', {'message_id': message_id})

    def get_file(self, file_id: str) -> dict:
        """获取文件信息。返回 {file: '/本地路径', url: '...', name: '...', size: int}"""
        response = self.post('get_file', {'file_id': file_id})
        return response.get('data') or {}

    def fetch_custom_face(self, count: int = 48) -> list[str]:
        """获取账号收藏的表情（QQ 收藏表情），返回 URL 字符串列表。

        NapCat 扩展动作 fetch_custom_face，data 可能是 URL 字符串数组，
        也可能是对象数组（含 url 字段），两种都做兼容。
        """
        response = self.post('fetch_custom_face', {'count': count})
        data = response.get('data') or []
        urls: list[str] = []
        for item in data:
            if isinstance(item, str):
                url = item.strip()
            elif isinstance(item, dict):
                url = str(item.get('url') or item.get('emoji_id') or '').strip()
            else:
                url = ''
            if url:
                urls.append(url)
        return urls

    def get_group_list(self) -> list[dict]:
        response = self.post('get_group_list', {})
        return response.get('data') or []

    def get_friend_list(self) -> list[dict]:
        response = self.post('get_friend_list', {})
        return response.get('data') or []

    def get_stranger_info(self, user_id: int) -> dict:
        response = self.post('get_stranger_info', {'user_id': user_id})
        return response.get('data') or {}

    def get_group_info(self, group_id: int) -> dict:
        response = self.post('get_group_info', {'group_id': group_id})
        return response.get('data') or {}

    def get_group_member_list(self, group_id: int) -> list[dict]:
        response = self.post('get_group_member_list', {'group_id': group_id})
        return response.get('data') or []

    def send_file(self, chat_type: str, target_id: int, file: str, name: str | None = None) -> dict:
        """上传并发送本地文件到私聊或群聊。
        chat_type: 'private' | 'group'
        file: 服务器上的绝对路径
        name: 可选，对方看到的文件名；不传则取路径末尾文件名
        """
        import os as _os
        display_name = name or _os.path.basename(file)
        if chat_type == 'group':
            return self.post('upload_group_file', {
                'group_id': target_id,
                'file': file,
                'name': display_name,
            })
        else:
            return self.post('upload_private_file', {
                'user_id': target_id,
                'file': file,
                'name': display_name,
            })


    def get_group_member_info(self, group_id: int, user_id: int, no_cache: bool = False) -> dict:
        """获取群成员信息（含角色：owner/admin/member）。"""
        response = self.post('get_group_member_info', {
            'group_id': group_id,
            'user_id': user_id,
            'no_cache': no_cache,
        })
        return response.get('data') or {}

    def set_group_ban(self, group_id: int, user_id: int, duration: int) -> dict:
        """禁言群成员。duration 单位秒，0 表示解除禁言。"""
        response = self.post('set_group_ban', {
            'group_id': group_id,
            'user_id': user_id,
            'duration': duration,
        })
        return response.get('data') or {}

    def set_group_whole_ban(self, group_id: int, enable: bool) -> dict:
        """全员禁言开关。"""
        response = self.post('set_group_whole_ban', {
            'group_id': group_id,
            'enable': enable,
        })
        return response.get('data') or {}
    @staticmethod
    def at(user_id: int) -> str:
        return f'[CQ:at,qq={user_id}]'

    def start(self):
        ws_url = self.ws_url
        if len(sys.argv) > 1:
            ws_url = sys.argv[1]

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self.ws.run_forever()
