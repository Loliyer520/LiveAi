# Minecraft sidecar

这里包含 DeepLive 实际使用的通用 MC 边车源码：Node HTTP 桥、Mineflayer、
行为/寻路引擎、自动登录与重连，以及 26.2 Rust headless 适配源码。
主程序通过 `data/plugins/mc/plugin.json` 的 HTTP 工具和 `src/chat/mc-chat.ts` 调用它。
边车独立安装依赖，不改变框架主进程的依赖树。

## 安装和启动

需要 Node.js **22.16+**（`import.meta.dirname` 和测试工具需要现代 Node）。
在仓库根目录执行：

```sh
npm --prefix sidecars/mcbot ci
npm --prefix sidecars/mcbot test
npm --prefix sidecars/mcbot run doctor
npm --prefix sidecars/mcbot start
```

首次启动会在边车目录创建私有 `config.json`，生成随机 token，默认监听
`127.0.0.1:7660`。不要公开该控制端口。把 `config.json` 中的 token **私下**填入
框架 `data/plugins/secrets.json` 的 `MCBOT_TOKEN` 字段，两边保持一致。
游戏默认名是 `DeepLive`，进服仍需调用 `mc_join`，启动不会自动加入历史服务器。

`MCBOT_CONFIG` 可指定其他配置文件；默认账号档案与该配置文件同目录。
`MCBOT_PROFILES` 可单独指定账号档案。`MCBOT_TOKEN` 可覆盖 HTTP token。
自定义地址用框架环境变量 `LIVEAI_MCBOT_URL`，并同步插件清单的接口地址。
Linux 常驻服务样例见 `deploy/deeplive-mcbot.service`，安装前修改路径。

首次安装即可使用 Mineflayer 支持的版本。**26.2 / protocol 776 还必须准备下述 Rust
客户端、官方资源和运行资料**，仅 `npm ci` 不够；不要把缺少它们误报成已可进入 26.2。

## 26.2 Rust 源码和构建

`rust/bot.rs` 是完整的 NDJSON headless 入口；`rust/client.patch` 包含 LiveSession、
主入口和交互层适配。`rust/upstream.json` 固定上游仓库及完整提交 SHA，构建脚本
从该版本取源码、校验并应用补丁，避免依赖某台机器 `/tmp` 里的改动。

安装 Git、Rust stable（支持 edition 2024；依赖以锁文件为准）、Linux 原生编译工具后：

```sh
# 获取固定上游并应用仓库内的全部适配源码
npm --prefix sidecars/mcbot run build:rust -- --prepare-only
# Cargo 检查（可选）
npm --prefix sidecars/mcbot run build:rust -- --check
# release 构建并安装到 sidecars/mcbot/bin/mc-native
npm --prefix sidecars/mcbot run build:rust
```

源码放入被 Git 忽略的 `.cache/rust-client/`。若升级补丁后缓存不匹配，脚本会明确
报错而不覆盖文件；移走旧缓存后重试。`CARGO`、`CARGO_TARGET_DIR` 支持自定义工具和
构建缓存。Windows 的二进制默认路径需在 `config.json.rustBinary` 设置为 `.exe`。
上游来源与声明许可见 `rust/upstream.json`；上游通用客户端仍由其维护者维护。

### 官方资源和固定运行资料

原版 JAR、纹理、运行事实、JDK、服务器世界和编译二进制不入 Git。
可由你已有的合法安装准备，或按固定上游的 `download_client.py` 与 README 中的
Java oracle 导出步骤生成；Java 导出阶段需要 JDK 25，正常游玩不用启动 Java。

```sh
cd sidecars/mcbot/.cache/rust-client
python3 download_client.py
# 按该固定上游 README 的 Java oracle 步骤生成 build/java-oracle 中的运行事实
# 注意不是只运行 prepare_runtime_data.py：它仅打包已有导出结果。
python3 tools/prepare_runtime_data.py --output runtime-data/26.2
```

资源下载器会校验官方文件；若它报告 SHA-1 不匹配，需处理该错误，不能视为下载成功。
在边车的私有 `config.json` 配置实际绝对路径：

```json
{
  "host": "127.0.0.1",
  "port": 7660,
  "token": "YOUR_LOCAL_SECRET",
  "rustAssets": "/absolute/path/minecraft-26.2-client",
  "rustRuntimeData": "/absolute/path/runtime-data/26.2"
}
```

配置完运行 `npm --prefix sidecars/mcbot run doctor -- --rust`，检查二进制、
官方客户端 JAR、运行资料清单和 36 项文件 SHA-1。此检查通过不是完整实服验收。
26.2 当前只支持离线认证，尚未实现微软正版会话。

## 可选 RCON 与私有适配

控制自己的服务器时，可显式设置 `MC_SERVER_PROPERTIES=/path/to/server.properties`。
边车会内部读取 RCON 端口/密码并连 `127.0.0.1`；未设置时禁用 RCON 辅助功能，
不会读取开发者服务器文件。部分实体查询、辅助合成和行为依赖它；一般进服和聊天不依赖。

私有服的一次性进服码适配应放在仓库外，通过 `config.json.joinHook` 指定；
接口示例 `join-hook.example.mjs`。真实账号、目标服名单、皮肤、白名单、玩家档案、
部署私有 hook 和服务端密码均不随源码发布。

## 测试

`npm test` 用模拟客户端和隔离文件测试调度、行为、协议封装、登录、档案、重连分类及
进服 hook，不碰当前在线角色。`npm run test:live` 则需要你另行提供官方测试服务器
JAR、JDK25 和上述 Rust 运行依赖；使用独立端口和临时世界。
可通过 `MCBOT_JAVA`、`MCBOT_26_2_SERVER`、`MCBOT_TEST_PORT` 设置，勿使用生产世界。
