# DeepLive 手机客户端

Android 客户端，基于 CWS 手机端 Android 工程及其原生输入框兼容方案改造。
独立包名 `cn.loliyc.deeplive_mobile`，不会覆盖 CWS 或已有 LiveAi App。

## 首版功能

- QQ 式消息列表、联系人页、个人页和左右聊天气泡；长按可选择文字
- 对接当前框架 `GET /api/mobile/chat`、`POST /api/mobile/chat`，服务端继续负责模型、记忆和工具执行
- 前台每 2 秒同步最近 100 条消息，返回列表时显示未读数；后台暂停轮询，回到前台恢复
- 服务器地址、连接凭证和 AI 显示名称可配置，连接前验证接口
- 设置使用 Android Keystore / AES-GCM 加密存储；关闭 Android 备份，不硬编码凭据
- 保留发送失败的输入内容；发送超时需先查历史，避免手动重试造成重复发送

每次开通创建独立的 `app:<UUID>` 用户会话，有自己的聊天上下文和会话 AI，与 `master:core`、QQ 私聊和其他 App 会话分开。
后台主 AI 只参与内部协调，不直接展示给手机用户。接口只返回用户消息与会话 AI 经 `send_message` 发出的回复；内部任务、委派、汇报、主 AI 回传和主 AI 直接发言均不显示。

首次填写管理 token，经 `POST /api/mobile/sessions` 开通后，App 只加密保存独立会话 token，不保存管理 token。
也可由管理员预先开通，再把 `dlapp_...` 会话 token 交给用户填写。该凭证仅能访问自己的手机会话，不能访问主 AI 管理对话或其他管理 API。
服务端只持久化会话 token 的哈希，重启后保留绑定；不能通过请求里的 `sessionId` 切到其他用户。
本版提供会话凭证隔离，尚未提供账号注册、找回、凭证撤销或跨设备会话选择界面。
尚不支持独立多人账号、群聊客户端、图片/语音发送、后台推送通知、离线消息缓存及历史翻页。

## 输入兼容性来源

来源：`/tmp/code/cws-client`，迁移日期 2026-11-12。

- `android/` 构建模板沿用 CWS 手机端
- `MainActivity.kt` 的 `ensureNativeEdit` / `applyNativeEdit` / `moveNativeEdit` /
  `hideNativeEdit` 和 `nativeEdit*` 方法分支从 CWS 提取
- `lib/native_composer.dart` 沿用矩形探针、物理像素定位和可取消的逐帧跟随
- Flutter 只画壳和只读镜像；Android 原生 EditText 是聊天输入文本的事实源
- 多行输入，软键盘回车换行，右侧按钮或硬件 Ctrl+Enter 发送
- 发送前读原生真实文本，避免 IME 回声延迟丢字
- 不改回普通可编辑 Flutter TextField，也不改成 PlatformView

移除了与本应用无关的 CWS RPC、登录、自更新、录音、闹钟和常驻服务。
设置页面是标准表单；本次继承的原生覆盖兼容方案用于聊天输入栏。

## 连接

在 App 的「我 → 服务器连接」填写服务器的 WebUI 根地址及连接凭证（首次管理 token，或已开通的会话 token）。
地址示例 `https://ai.example.com`，支持反向代理路径前缀如 `https://example.com/bot`。
不要把 token 放在地址或 query 中；用独立的 token 字段。

手机的 `127.0.0.1` 指手机本身，不能直接用服务器的回环地址。
建议通过现有 HTTPS 反向代理访问；HTTP 仅用于可信内网。
服务器需要运行当前 DeepLive WebUI/API，无需更改现有聊天接口。
连接时只创建/验证会话，不会向 AI 发送任何消息。
旧版控制台配置不再读取管理聊天历史；重新保存连接可开通独立会话。

## 构建及检查

需要与 CWS 一致的 Flutter/Dart SDK（当前模板 Dart ^3.13.4）、Android SDK 和 JDK。
在 `mobile/` 内执行：

```sh
flutter pub get
flutter analyze
flutter test
flutter build apk --debug --target-platform android-arm64
```

默认使用调试签名，仅供安装测试，不能直接视为商店发布包。
正式发布前需要配置自己的 release 签名、增加版本号，并在实际手机上验证：
中文拼音候选/组合输入、长文本多行、回车换行、Ctrl+Enter、键盘收起/弹出、
切后台与返回、消息失败重试以及原 CWS 曾遇到兼容问题的 ROM。
