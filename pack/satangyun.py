from pack.napcat import NapcatBot
from core.events import ChatMessage, GroupIncreaseEvent
from pack.chat_model import OpenAICompatibleChatModel
from pack.encoding import file_to_base64_uri
from pack.image_generation import NormalDrawingService
from pack.satangyun_api import SatangyunAPI


class SatangyunModule:
    def __init__(
        self,
        bot: NapcatBot,
        group_id: int,
        api: SatangyunAPI,
        draw_service: NormalDrawingService,
        welcome_model: OpenAICompatibleChatModel | None = None,
        notice_image_url: str | None = None,
        welcome_model_name: str | None = None,
    ):
        self.bot = bot
        self.group_id = group_id
        self.api = api
        self.draw_service = draw_service
        self.welcome_model = welcome_model
        self.notice_image_url = notice_image_url
        self.welcome_model_name = welcome_model_name

    def register(self):
        self.bot.on_group_increase(self.handle_group_increase)
        self.bot.on_group_message(self.handle_group_message)

    def _in_scope(self, message: ChatMessage) -> bool:
        return message.chat_type == 'group' and message.chat_id == self.group_id

    def handle_group_increase(self, event: GroupIncreaseEvent):
        if event.group_id != self.group_id:
            return
        if not self.notice_image_url:
            return
        self.bot.send_image(
            'group',
            self.group_id,
            self.notice_image_url,
            text='欢迎！入群请读~\n（为保证社区安全，请勿谈论无关内容）\n（新版词典笔没有固定密码！请不要到处问密码！）',
        )

    def handle_group_message(self, message: ChatMessage):
        if not self._in_scope(message):
            return

        text = message.text.strip()
        if text == '#':
            self.bot.send_group_text(self.group_id, self.api.get_user_summary(message.user_id))
            return

        if text.startswith('#bind:'):
            self._handle_bind(message, text.replace('#bind:', '', 1).strip())
            return

        if text.startswith('hua '):
            self._handle_draw(message, text[4:].strip())

    def _handle_bind(self, message: ChatMessage, code: str):
        result = self.api.bind_account(message.user_id, code)
        if result == -1:
            self.bot.send_group_text(self.group_id, '绑定失败惹~ 要不再试试？')
            return
        if result == 0:
            self.bot.send_group_text(self.group_id, '绑定失败啦~ 是不是时间太久啦？')
            return

        self.bot.send_group_text(self.group_id, '绑定成功~ 欢迎加入砂糖云！\n绑定完成后即可直接用QQ号+密码登录哦！')
        welcome = self._build_welcome_text(message)
        self.bot.send_group_text(self.group_id, f'{self.bot.at(message.user_id)} {welcome}')

    def _build_welcome_text(self, message: ChatMessage) -> str:
        if not self.welcome_model:
            return '欢迎来到砂糖云喵~'

        nickname = message.nickname
        prompt = (
            '你是砂糖云群欢迎语生成器。'
            '只输出一条欢迎语，不要解释。'
            '20字以内，语气可爱、自然、轻微日系，但别太做作。'
            '尽量根据昵称做亲切称呼，避免不合适的直呼。'
            f'当前用户昵称：{nickname}'
        )
        try:
            reply = self.welcome_model.complete(
                [
                    {'role': 'system', 'content': '你擅长生成短欢迎语。'},
                    {'role': 'user', 'content': prompt},
                ],
                model_name=self.welcome_model_name,
                temperature=0.9,
            )
            return reply or '欢迎来到砂糖云喵~'
        except Exception:
            return '欢迎来到砂糖云喵~'

    def _handle_draw(self, message: ChatMessage, prompt: str):
        if not prompt:
            self.bot.send_group_text(self.group_id, '要告诉我画什么呀~')
            return

        self.bot.send_group_text(self.group_id, '正在为主人画画喵~')
        try:
            image_path = self.draw_service.generate(prompt)
            self.bot.send_image('group', self.group_id, file_to_base64_uri(str(image_path)))
        except Exception as exc:
            self.bot.send_group_text(self.group_id, f'呜啊坏掉惹~ {exc}')
