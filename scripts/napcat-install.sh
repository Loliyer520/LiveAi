#!/usr/bin/env bash
# DeepLive 本机 NapCat 一键部署（Linux x86_64 / arm64，无桌面环境）
#
# 由 bot 面板调用，也可手动执行：
#   NAPCAT_WS_URL=ws://127.0.0.1:7636 NAPCAT_WS_TOKEN=xxx bash scripts/napcat-install.sh
#
# 做的事：
#   1. 安装 Xvfb/unzip/jq/g++ 等依赖（缺什么装什么）
#   2. 安装官方 Linux QQ（3.2.34-53644，已装且版本够则跳过）
#   3. 下载 NapCat.Shell 最新版并解压到 $NAPCAT_HOME/napcat（保留 config/plugins）
#   4. 编译官方非侵入式 launcher，写 launcher.sh
#   5. 写 deeplive-napcat.service 并开机自启
#
# 部署结果写到 $NAPCAT_HOME/.install-state.json，面板据此显示成功/失败。

set -euo pipefail
umask 022
export DEBIAN_FRONTEND=noninteractive

NAPCAT_HOME="${NAPCAT_HOME:-/my/run/napcat}"
NAPCAT_SERVICE="${NAPCAT_SERVICE:-deeplive-napcat}"
NAPCAT_QQ_DIR="${NAPCAT_QQ_DIR:-/opt/QQ}"
NAPCAT_WS_URL="${NAPCAT_WS_URL:-ws://127.0.0.1:7636}"
NAPCAT_WS_TOKEN="${NAPCAT_WS_TOKEN:-}"
NAPCAT_QUICK_LOGIN="${NAPCAT_QUICK_LOGIN:-}"
STATE_FILE="$NAPCAT_HOME/.install-state.json"
STATE_MSG=""
QQ_VERSION="3.2.34-53644"
QQ_BASE_URL="https://qqdl.gtimg.cn/qqfile/QQNT/9.9.36/beta/9ee04bef"
GITHUB_PROXIES=("https://ghfast.top" "https://ghproxy.net" "https://gh-proxy.com" "https://github.dpik.top")

mkdir -p "$NAPCAT_HOME"

log() { printf '[napcat-install] %s\n' "$*"; }
step() { printf '[napcat-install] == %s ==\n' "$*"; }

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n\r\t' '   '
}

finish() {
  local code=$?
  trap - EXIT
  local status="ok"
  if [ "$code" -ne 0 ]; then status="failed"; fi
  printf '{"status":"%s","exitCode":%d,"finishedAt":%s,"message":"%s"}\n' \
    "$status" "$code" "$(date +%s%3N)" "$(json_escape "$STATE_MSG")" > "$STATE_FILE" 2>/dev/null || true
  exit "$code"
}
trap finish EXIT

fail() {
  STATE_MSG="$1"
  log "ERROR: $1"
  exit 1
}

fetch_github() {
  local url="$1" dest="$2" candidate
  for candidate in "$url" "${GITHUB_PROXIES[@]/%//$url}"; do
    if curl -fL --connect-timeout 20 --max-time 1800 --retry 2 \
      --proto '=http,https' --proto-redir '=http,https' "$candidate" -o "$dest"; then
      return 0
    fi
    rm -f -- "$dest"
  done
  return 1
}

# NapCat.Shell.zip 里的 package.json 版本号是 0.0.1（打包产物），不能用来展示。
# 优先问 GitHub API 要 release tag；拿不到再从 napcat.mjs 里抠版本常量。
release_tag() {
  local api="https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest" candidate body tag
  for candidate in "$api" "${GITHUB_PROXIES[@]/%//$api}"; do
    body="$(curl -fsL --connect-timeout 15 --max-time 60 \
      --proto '=http,https' --proto-redir '=http,https' "$candidate" 2>/dev/null)" || continue
    tag="$(printf '%s' "$body" | jq -r '.tag_name // empty' 2>/dev/null || true)"
    if [ -n "$tag" ]; then printf '%s\n' "$tag"; return 0; fi
  done
  return 1
}

qq_installed() {
  [ -x "$NAPCAT_QQ_DIR/qq" ] || return 1
  local pkg="$NAPCAT_QQ_DIR/resources/app/package.json"
  [ -f "$pkg" ] || return 1
  jq -e '((.buildVersion // 0) | tonumber) >= 53644' "$pkg" >/dev/null 2>&1
}

free_kb() {
  df -Pk "$NAPCAT_HOME" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0
}

if [ "$(id -u)" -ne 0 ]; then
  fail "需要 root 权限运行（面板以 root 身份启动 bot）"
fi

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) fail "不支持的 CPU 架构：$(uname -m)，仅支持 amd64/arm64" ;;
esac

step "检查系统依赖"
need_deps=0
for cmd in xvfb-run xauth jq curl unzip g++ dpkg; do
  command -v "$cmd" >/dev/null 2>&1 || need_deps=1
done
if [ "$need_deps" -eq 1 ]; then
  log "安装 Xvfb、jq、g++ 等依赖…"
  if ! apt-get install -y -qq xvfb xauth jq curl unzip ca-certificates procps g++ dpkg; then
    log "apt 索引可能过期，更新后重试…"
    apt-get update -y -qq || fail "apt-get update 失败"
    apt-get install -y -qq xvfb xauth jq curl unzip ca-certificates procps g++ dpkg || fail "系统依赖安装失败"
  fi
else
  log "依赖已齐全，跳过"
fi

step "准备 Linux QQ"
if qq_installed; then
  log "已安装满足要求的 QQ（buildVersion ≥ 53644），跳过"
else
  if [ "$(free_kb)" -lt 1572864 ]; then
    fail "磁盘剩余不足 1.5GB，无法安装 QQ（可先清理 /var/cache/apt 或旧日志）"
  fi
  QQ_FILE="linuxqq_${QQ_VERSION}_${ARCH}.deb"
  QQ_URL="${NAPCAT_QQ_URL:-$QQ_BASE_URL/$QQ_FILE}"
  log "下载 QQ：$QQ_FILE"
  curl -fL --connect-timeout 20 --max-time 1800 --retry 2 \
    --proto '=http,https' --proto-redir '=http,https' "$QQ_URL" -o "$NAPCAT_HOME/QQ.deb" \
    || fail "QQ 下载失败：$QQ_URL"
  dpkg-deb --info "$NAPCAT_HOME/QQ.deb" >/dev/null 2>&1 || fail "QQ 安装包损坏"
  log "安装 QQ（依赖较多，可能需要几分钟）…"
  if ! apt-get install -y -qq "$NAPCAT_HOME/QQ.deb"; then
    log "依赖未就绪，尝试修复后重装…"
    apt-get install -y -qq -f || true
    apt-get install -y -qq "$NAPCAT_HOME/QQ.deb" || fail "QQ 安装失败（可查看：apt-get install -f）"
  fi
  apt-get install -y -qq libnss3 libgbm1 libgssapi-krb5-2 || fail "QQ 运行库安装失败"
  # Ubuntu 24.04 起 asound 包改名为 libasound2t64，两个名字都试一次。
  if ! apt-get install -y -qq libasound2t64; then
    apt-get install -y -qq libasound2 || fail "libasound2 安装失败"
  fi
  qq_installed || fail "QQ 安装后版本检查失败（需要 buildVersion ≥ 53644）"
  rm -f -- "$NAPCAT_HOME/QQ.deb"
fi

step "停止旧 NapCat 进程"
systemctl stop "$NAPCAT_SERVICE" >/dev/null 2>&1 || true

step "下载并解压 NapCat"
ZIP="$NAPCAT_HOME/NapCat.Shell.zip"
rm -f -- "$ZIP"
fetch_github "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip" "$ZIP" \
  || fail "NapCat 下载失败（GitHub 及镜像均不可用）"
unzip -t "$ZIP" >/dev/null 2>&1 || fail "NapCat 压缩包损坏"
mkdir -p "$NAPCAT_HOME/napcat"
# 先覆盖程序文件，再补全缺失文件；config/plugins 永不覆盖，升级不丢配置。
unzip -q -o -d "$NAPCAT_HOME/napcat" "$ZIP" -x 'config/*' 'plugins/*'
unzip -q -n -d "$NAPCAT_HOME/napcat" "$ZIP"
[ -f "$NAPCAT_HOME/napcat/napcat.mjs" ] || fail "NapCat 解压后缺少 napcat.mjs"
NAPCAT_TAG="$(release_tag || true)"
if [ -z "$NAPCAT_TAG" ]; then
  NAPCAT_TAG="$(grep -oE '"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?" *\|\| *"1\.0\.0-dev"' \
    "$NAPCAT_HOME/napcat/napcat.mjs" 2>/dev/null \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?' | head -n 1 || true)"
fi
printf '%s\n' "$NAPCAT_TAG" > "$NAPCAT_HOME/napcat/VERSION"
log "NapCat 版本：${NAPCAT_TAG:-unknown}"
rm -f -- "$ZIP"

step "准备 launcher"
LAUNCHER_SO="$NAPCAT_HOME/libnapcat_launcher.so"
if [ ! -x "$LAUNCHER_SO" ]; then
  fetch_github "https://raw.githubusercontent.com/NapNeko/napcat-linux-launcher/refs/heads/main/launcher.cpp" "$NAPCAT_HOME/launcher.cpp" \
    || fail "launcher.cpp 下载失败"
  g++ -shared -fPIC "$NAPCAT_HOME/launcher.cpp" -o "$LAUNCHER_SO.new" -ldl || fail "launcher 编译失败"
  mv -- "$LAUNCHER_SO.new" "$LAUNCHER_SO"
  log "launcher 编译完成"
fi

cat > "$NAPCAT_HOME/launcher.sh" <<EOF
#!/bin/bash
cd -- "\$(dirname -- "\${BASH_SOURCE[0]}")" || exit 1
export HOME="\${HOME:-$NAPCAT_HOME}"
trap "" SIGPIPE
exec xvfb-run -a env LD_PRELOAD=./libnapcat_launcher.so $NAPCAT_QQ_DIR/qq --no-sandbox${NAPCAT_QUICK_LOGIN:+ -q $NAPCAT_QUICK_LOGIN} "\$@"
EOF
chmod +x "$NAPCAT_HOME/launcher.sh"

ONEBOT_CONFIG="$NAPCAT_HOME/napcat/config/onebot11.json"
if [ ! -f "$ONEBOT_CONFIG" ] && [ -n "$NAPCAT_WS_TOKEN" ]; then
  mkdir -p "$NAPCAT_HOME/napcat/config"
  jq -n --arg url "$NAPCAT_WS_URL" --arg token "$NAPCAT_WS_TOKEN" '{
    network: {
      httpServers: [], httpSseServers: [], httpClients: [],
      websocketServers: [],
      websocketClients: [{
        name: "deeplive", enable: true, url: $url,
        messagePostFormat: "array", reportSelfMessage: false,
        reconnectInterval: 5000, token: $token, debug: false, heartInterval: 30000
      }],
      plugins: []
    },
    musicSignUrl: "", enableLocalFile2Url: false, parseMultMsg: false
  }' > "$ONEBOT_CONFIG"
fi
if [ ! -f "$ONEBOT_CONFIG" ]; then
  log "警告：未找到 onebot11.json，NapCat 启动后不会自动连回 bot（面板「一键部署」会自动生成）"
else
  log "反向 WS 目标：$NAPCAT_WS_URL"
fi

step "写入 systemd 单元"
cat > "/etc/systemd/system/${NAPCAT_SERVICE}.service" <<EOF
[Unit]
Description=DeepLive NapCat ($NAPCAT_SERVICE)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
# HOME 未设置时 QQ 启动即 SIGILL（ud2 断言），必须显式给。
Environment=HOME=$NAPCAT_HOME
WorkingDirectory=$NAPCAT_HOME
ExecStart=$NAPCAT_HOME/launcher.sh
Restart=always
RestartSec=5
TimeoutStopSec=20
KillMode=mixed
LimitCORE=0
StandardOutput=append:$NAPCAT_HOME/napcat.log
StandardError=append:$NAPCAT_HOME/napcat.log

[Install]
WantedBy=multi-user.target
EOF

step "启动 NapCat"
systemctl daemon-reload
systemctl enable "$NAPCAT_SERVICE" >/dev/null 2>&1 || true
systemctl restart "$NAPCAT_SERVICE" || fail "systemctl restart 失败"
sleep 3
if ! systemctl is-active --quiet "$NAPCAT_SERVICE"; then
  journalctl -u "$NAPCAT_SERVICE" -n 30 --no-pager >&2 2>/dev/null || true
  tail -n 30 "$NAPCAT_HOME/napcat.log" >&2 2>/dev/null || true
  fail "NapCat 启动失败，请查看日志"
fi

# 安装完成，清理占空间的缓存
apt-get clean >/dev/null 2>&1 || true
log "部署完成：NapCat 已启动，等待 QQ 扫码登录"
