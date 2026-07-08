from pack.napcat import NapcatBot


class ContactTool:
    def __init__(self, bot: NapcatBot):
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

    def send_chat_message(self, chat_type: str, target_id: int, content: str):
        return self.bot.send_text(chat_type, target_id, content)
