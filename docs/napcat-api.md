# NapCat 低频接口与 QQ 工具

DeepLive 的 QQ 能力分两层：

- **专用工具**：参数、权限和安全检查固定在代码里，优先使用。
- **`napcat_call`**：主 AI / Onebot 中枢把低频接口的 JSON 请求体直接交给 NapCat。
  调用 `napcat_call(action="help", params={})` 可让 AI 取出同一份内置速查文档；
  文档源为 `src/chat/napcat-api-guide.ts`。

## 专用工具

| 工具 | 可用范围 | 功能 |
|---|---|---|
| `group_mute` | 群子 AI、主 AI、Onebot | 单人禁言/解除（已有） |
| `group_manage` | 群子 AI（仅当前群）、主 AI、Onebot（可指定群） | 踢人/批量踢人、全员禁言、设撤管理员、群名片、专属头衔、群公告、改群名、群打卡 |
| `qq_interact` | QQ 会话、主 AI、Onebot | 戳一戳、资料卡点赞 |
| `qq_account` | 主 AI、Onebot | 好友/入群申请列表与批复、接受入群邀请、删除好友、退群、好友/群列表 |
| `qzone` | 主 AI、Onebot | 发文字/多图说说、可见范围、按 tid 删除说说 |
| `napcat_call` | 主 AI、Onebot | 低频 OneBot / NapCat JSON 接口 |

OneBot 没有“主动加陌生好友 / 主动申请加入任意群”的标准接口。`qq_account`
处理的是**收到的好友申请、别人申请入群、别人邀请机器人入群**；接受邀请即完成加群。

## 通用 JSON 调用

```json
{
  "action": "get_group_member_info",
  "params": {
    "group_id": 123456,
    "user_id": 987654,
    "no_cache": true
  }
}
```

`action="help"` 返回群成员、公告、精华消息、群文件、OCR、QQ 空间公开动态等
低频接口的参数说明。完整接口以实际运行的 NapCat 版本为准：
[NapCat OneBot API](https://napneko.github.io/onebot/api)。

## 安全边界

- 好友与入群申请只进入主 AI，不进普通用户会话；事件缓存 24 小时。
- 普通群子 AI 不能跨群，管理动作检查机器人和目标成员的群主/管理员层级。
- 删除号主好友、踢出或禁言号主被硬拒；退群始终 `is_dismiss=false`，raw 也禁止解散群。
- raw 禁止 `send_packet`、进程控制、修改机器人资料/头像等高风险接口。
- raw 和说说/公告图片字段禁止本地绝对路径与 `file://`，防止 NapCat 读取并外发任意文件。
- raw 请求体限制 64KB，默认超时 30 秒，最大 180 秒。

测试：`tests/qq-tools.test.ts`（schema 暴露、权限矩阵、申请消费、空间、raw 防护）。
