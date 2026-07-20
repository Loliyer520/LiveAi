import requests


class SatangyunAPI:
    def __init__(self, api_base: str, admin_token: str):
        self.api_base = api_base.rstrip('/')
        self.admin_token = admin_token

    def get_user_summary(self, user_id: int) -> str:
        try:
            response = requests.post(
                f'{self.api_base}/user/get_user',
                json={'id': user_id, 'adminToken': self.admin_token},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return f'账户查询失败: {exc}'

        if payload.get('code') == 0:
            return '账户不存在哦~'

        data = payload.get('data') or {}
        money = data.get('money', 0)
        point = data.get('point', 0)
        return f'砂糖 {money} 积分 {point}'

    def bind_account(self, qq: int, code: str) -> int:
        try:
            response = requests.post(
                f'{self.api_base}/user/bindCode',
                json={'qq': str(qq), 'code': code, 'adminToken': self.admin_token},
                timeout=30,
            )
            response.raise_for_status()
            return int(response.json().get('code', -1))
        except Exception:
            return -1
