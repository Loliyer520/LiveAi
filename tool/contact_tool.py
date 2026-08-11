from core.transport import NapcatActionTransport


class ContactTool:
    def __init__(self, bot: NapcatActionTransport):
        self.bot = bot

    def get_group_list(self) -> list[dict]:
        try:
            return self.bot.get_group_list()
        except Exception:
            return []

    def get_friend_list(self) -> list[dict]:
        try:
            return self.bot.get_friend_list()
        except Exception:
            return []

    def send_private_message(self, user_id: int, content: str):
        return self.bot.send_private_text(user_id, content)

    # ── 好友与群请求管理 ──

    def request_friend_add(self, user_id: int, comment: str = '') -> dict:
        return self.bot.request_friend_add(user_id, comment)

    def get_friend_requests(self, count: int = 50) -> dict:
        return self.bot.get_friend_requests(count)

    def set_friend_add_request(self, flag: str, approve: bool, remark: str = '') -> dict:
        return self.bot.set_friend_add_request(flag, approve, remark)

    def request_group_join(self, group_id: int, comment: str = '') -> dict:
        return self.bot.request_group_join(group_id, comment)

    def get_group_requests(self, count: int = 50) -> dict:
        return self.bot.get_group_requests(count)

    def set_group_add_request(self, flag: str, sub_type: str, approve: bool, reason: str = '') -> dict:
        return self.bot.set_group_add_request(flag, sub_type, approve, reason)
        return self.bot.send_private_text(user_id, content)

    def send_chat_message(self, chat_type: str, target_id: int, content: str):
        return self.bot.send_text(chat_type, target_id, content)

    def send_chat_image(self, chat_type: str, target_id: int, file: str, text: str | None = None):
        return self.bot.send_image(chat_type, target_id, file, text)

    def send_chat_record(self, chat_type: str, target_id: int, file: str):
        return self.bot.send_record(chat_type, target_id, file)

    def send_chat_file(self, chat_type: str, target_id: int, file: str, name: str | None = None):
        return self.bot.send_file(chat_type, target_id, file, name)

    # ── 群管理 ──

    def get_member_role(self, group_id: int, user_id: int) -> str:
        """获取群成员角色：owner / admin / member / unknown。"""
        try:
            info = self.bot.get_group_member_info(group_id, user_id)
            return info.get('role', 'unknown')
        except Exception:
            return 'unknown'

    def set_group_ban(self, group_id: int, user_id: int, duration: int) -> dict:
        """禁言群成员。duration 单位秒，0=解除禁言。返回 API 响应。"""
        return self.bot.set_group_ban(group_id, user_id, duration)

    def set_group_whole_ban(self, group_id: int, enable: bool) -> dict:
        """全员禁言开关。"""
        return self.bot.set_group_whole_ban(group_id, enable)
