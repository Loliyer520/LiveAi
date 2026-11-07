/**
 * napcat_call(action="help") returns this guide. Keep it concise: it is model
 * context, not a mirror of NapCat's entire generated API reference.
 */
export const NAPCAT_API_GUIDE = `NapCat 低频接口速查（napcat_call）

调用：napcat_call {action: "接口名", params: {…}}；action="help" 返回本文。
本工具只给主AI与 Onebot 中枢。QQ 号/群号可用数字或数字字符串；布尔值用 true/false。
返回 data 会序列化为 JSON；空 data 通常表示成功。失败会带 NapCat 原始错误。

已有专用工具（优先用，不要绕道 raw）：
- group_manage：踢人、全员禁言、设/撤管理员、群名片、专属头衔、公告、改名、打卡
- group_mute：单人禁言/解禁；recall_message：撤回
- qq_interact：戳一戳、资料卡点赞
- qq_account：好友/入群申请、删好友、退群、好友/群列表
- qzone：发说说、删说说

常用低频接口：
1. get_group_member_info {group_id, user_id, no_cache?: boolean} — 群成员资料
2. get_group_info {group_id, no_cache?: boolean} — 群资料
3. get_stranger_info {user_id} — 陌生人资料
4. get_friend_with_category {} — 带分组的好友列表
5. set_friend_remark {user_id, remark} — 修改好友备注
6. get_group_notice {group_id} — 群公告列表
7. del_group_notice {group_id, notice_id} — 删除公告
8. set_essence_msg {message_id} — 设精华消息
9. del_essence_msg {message_id} — 取消精华消息
10. get_group_essence {group_id} — 精华消息列表
11. get_group_shut_list {group_id} — 禁言列表
12. get_group_honor_info {group_id, type} — 群荣誉；type=all/talkative/performer/legend/strong_newbie/emotion
13. get_group_detail_info {group_id} — 群详细资料
14. get_group_info_ex {group_id} — 群扩展资料
15. get_group_share_link {group_id} — 群分享链接
16. set_msg_emoji_like {message_id, emoji_id} — 给消息贴表情
17. get_emoji_likes {message_id, emoji_type} — 查询消息表情回应
18. get_group_at_all_remain {group_id} — 剩余 @全体成员 次数
19. get_group_file_system_info {group_id} — 群文件空间信息
20. get_group_root_files {group_id} — 群根目录文件
21. get_group_files_by_folder {group_id, folder_id} — 指定目录文件
22. get_group_file_url {group_id, file_id, busid} — 群文件下载链接
23. get_file {file_id} — 收到的文件转下载链接
24. get_image {file} — 收到的图片转本地/下载信息
25. get_record {file, out_format} — 语音转码；out_format=mp3/wav/wma/spx/ogg/amr
26. ocr_image {image} — 图片 OCR；image 用收到的 file_id
27. fetch_custom_face {} — 收藏表情 URL 列表
28. get_login_info {} — 当前机器人账号
29. get_status {} — NapCat 在线状态
30. get_version_info {} — NapCat 版本

QQ 空间公开动态扩展（与本人发说说的 qzone 工具不同）：
- get_qzone_public_feeds {cursor?} — 公开动态列表，cursor 用上页 next_cursor
- get_qzone_public_feed_detail {feed} — 动态详情；feed 用列表返回的完整 feed 对象
- get_qzone_public_comments {feed, cursor} — 评论列表；cursor 用详情/上页 attach_info
- comment_qzone_public_feed {feed, content} — 评论
- reply_qzone_public_feed {feed, comment, content, reply?} — 回复评论
- set_qzone_public_like {feed, enable} — 点赞/取消
- forward_qzone_public_feed {feed, reason?} — 转发
这些接口要求回传列表里的结构化 feed/comment 对象，不要自己编 feed_id。
字段会随 NapCat 版本调整；返回“接口不存在/参数错误”时不要反复重试。

安全边界：
- 不允许 send_packet 等底层包、进程控制、改机器人资料/头像的高风险 raw 动作。
- file/path/image/avatar 等文件字段禁止本地绝对路径和 file://，防把任意文件发走；
  可用 http(s)://、base64:// 或 NapCat 返回的文件 ID。工作区文件应先走既有媒体工具。
- 不用 raw 解散群、删除号主好友、踢/禁言号主。
- 主动加陌生人为好友、主动申请加入任意群不是 OneBot 标准能力；qq_account 处理的是
  收到的好友申请、入群申请和“邀请我入群”。让对方向机器人发申请/邀请即可。

完整接口以运行中的 NapCat 版本为准：https://napneko.github.io/onebot/api
`;
