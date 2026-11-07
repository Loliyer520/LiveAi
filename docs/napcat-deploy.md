# 本机 NapCat 自动部署

面板「NapCat 配置」页新增了 **本机 NapCat 运行环境** 卡片：一键在这台服务器上安装并运行
NapCat，不再依赖号主自己的电脑常开。

## 一键部署做了什么

`scripts/napcat-install.sh`（以 root 运行，由 bot 面板拉起）：

1. 安装系统依赖：`xvfb`、`xauth`、`jq`、`unzip`、`g++` 等（已装则跳过）。
2. 安装官方 Linux QQ `3.2.34-53644`（`buildVersion ≥ 53644`，已装且版本够则跳过）。
3. 下载 `NapCat.Shell.zip` 最新版，解压到 `/my/run/napcat/napcat`。
   解压时排除 `config/`、`plugins/`，升级不会丢配置和插件。
4. 编译官方非侵入式 launcher（`libnapcat_launcher.so`），写 `launcher.sh`。
5. 写 `deeplive-napcat.service`：开机自启、崩溃自动拉起、日志写
   `/my/run/napcat/napcat.log`。
6. 启动服务，等待 QQ 扫码登录。

面板会预先写入反向 WebSocket 配置：

```
/my/run/napcat/napcat/config/onebot11.json
  network.websocketClients[name=deeplive]
    url   = ws://127.0.0.1:<bot 反向 WS 端口>
    token = bot 的 napcat.access_token
```

所以 NapCat 登录后会自动连回 DeepLive。`webui.json` 首次部署时生成随机 token，
监听 `127.0.0.1:6099`，之后重新部署不会覆盖。

## 扫码登录

部署完成后 NapCat 会生成 `/my/run/napcat/napcat/cache/qrcode.png`。在面板卡片里点
**查看登录二维码** 即可显示（二维码约 2 分钟过期，过期后再点一次刷新）。
用手机 QQ 扫码并在手机上确认；登录成功后卡片会变成 **已连接 QQ**。

首次登录可能触发腾讯的新设备验证；按手机 QQ 提示操作即可。

## 文件与命令

| 内容 | 路径 |
| --- | --- |
| NapCat 数据 / 配置 / 二维码 | `/my/run/napcat/napcat/` |
| 部署日志 | `/my/run/napcat/install.log` |
| 运行日志 | `/my/run/napcat/napcat.log` |
| systemd 单元 | `/etc/systemd/system/deeplive-napcat.service` |
| QQ | `/opt/QQ/` |

手动执行（面板不可用时）：

```bash
NAPCAT_WS_URL=ws://127.0.0.1:7636 \
NAPCAT_WS_TOKEN=<bot 的 access_token> \
bash /my/run/bot/scripts/napcat-install.sh

systemctl status deeplive-napcat
journalctl -u deeplive-napcat -n 100 --no-pager
```

## 注意

- 仅支持 Linux amd64 / arm64；需要 root（bot 以 root 运行）。
- 首次部署约占用 1GB 磁盘；安装前会检查至少 1.5GB 可用空间。
- 自动部署只支持 bot 的 **server 模式**（NapCat 反向连入 bot）。client 模式请在
  「连接参数」里切换后保存并重启 bot。
- 本机 NapCat 和号主电脑上的 NapCat 不要同时登录同一个 QQ，否则会互相挤下线。
- 部署脚本会执行 `apt-get install` 和写 systemd 单元，属于系统级变更；面板已有 token
  鉴权和跨站请求拦截。
