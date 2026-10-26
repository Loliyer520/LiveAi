/**
 * 管理面板单页应用：左侧导航（首页/知识/管理 + 每插件一个导航 + 底部设置/检查更新）。
 * 纯静态无依赖，hash 路由；token 走 ?token= 或 localStorage。
 * 注意：本文件是字符串模板，内嵌 JS 不能用反引号与 ${}（会吃掉外层模板），一律字符串拼接。
 */

export const WEBUI_PAGE = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f7fd">
<title>DeepLive 控制台</title>
<style>
:root {
  color-scheme: light;
  --bg: #f3f7fd; --panel: rgba(255, 255, 255, 0.78); --panel-solid: #ffffff;
  --line: rgba(56, 96, 153, 0.13); --line-strong: rgba(42, 93, 172, 0.28);
  --text: #172d4e; --sub: #6b809e; --accent: #2b63bd; --accent-strong: #214f9c;
  --accent-soft: rgba(55, 113, 204, 0.09); --cyan: #168f91;
  --ok: #16875f; --bad: #d64e65; --warn: #ad751c;
  --mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  --shadow: 0 14px 40px rgba(36, 76, 135, 0.075);
  --radius: 22px;
}
* { box-sizing: border-box; }
html { min-width: 320px; scroll-behavior: smooth; }
body {
  margin: 0; min-height: 100vh; min-height: 100dvh; overflow-x: hidden; color: var(--text);
  font: 14px/1.65 Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg); -webkit-font-smoothing: antialiased;
}
body::before {
  content: ""; position: fixed; inset: 0; z-index: -2; pointer-events: none;
  background:
    linear-gradient(90deg, rgba(248, 251, 255, 0.91) 0%, rgba(243, 248, 255, 0.76) 44%, rgba(237, 244, 252, 0.5) 100%),
    linear-gradient(180deg, rgba(242, 248, 255, 0.22), rgba(237, 244, 252, 0.38)),
    url("/assets/console-bg.jpg") center 42% / cover no-repeat fixed;
  background-color: var(--bg);
}
body::after {
  content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none; opacity: 0.5;
  background-image: linear-gradient(rgba(66, 108, 165, 0.055) 1px, transparent 1px), linear-gradient(90deg, rgba(66, 108, 165, 0.055) 1px, transparent 1px);
  background-size: 56px 56px;
  -webkit-mask-image: linear-gradient(to bottom, black, transparent 82%);
  mask-image: linear-gradient(to bottom, black, transparent 82%);
}
::selection { color: #15345f; background: rgba(128, 174, 238, 0.28); }
button, input, textarea, select { font: inherit; }
button, a { -webkit-tap-highlight-color: transparent; }
button { cursor: pointer; }
a { color: inherit; }
button:focus-visible, a:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 3px;
}
::-webkit-scrollbar { width: 7px; height: 7px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(70, 108, 160, 0.2); border-radius: 20px; }
::-webkit-scrollbar-thumb:hover { background: rgba(70, 108, 160, 0.34); }

#app { display: flex; min-height: 100vh; min-height: 100dvh; }
.icon { width: 19px; height: 19px; flex: 0 0 auto; vertical-align: -0.2em; }

/* ── 玻璃侧边栏 ── */
aside {
  width: 248px; flex: 0 0 248px; height: 100vh; height: 100dvh; position: sticky; top: 0; z-index: 20;
  display: flex; flex-direction: column; color: #627b9e;
  background: rgba(255, 255, 255, 0.7); border-right: 1px solid var(--line);
  -webkit-backdrop-filter: blur(28px) saturate(135%); backdrop-filter: blur(28px) saturate(135%);
}
aside::before {
  content: ""; position: absolute; top: 0; right: -1px; width: 1px; height: 180px;
  background: linear-gradient(to bottom, #6c9be0, transparent); opacity: 0.45;
}
aside .brand { display: flex; align-items: center; gap: 12px; padding: 28px 24px 22px; }
.brand-logo {
  width: 43px; height: 43px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 14px;
  color: #f5f9ff; border: 1px solid rgba(36, 82, 154, 0.16);
  background: linear-gradient(145deg, #3c74cb, #214d96);
  box-shadow: inset 0 1px rgba(255,255,255,0.24), 0 8px 22px rgba(40, 85, 163, 0.18);
}
.brand-logo .icon { width: 25px; height: 25px; }
.brand-name { color: #17345e; font-size: 20px; line-height: 1.2; font-weight: 720; letter-spacing: -0.4px; }
aside .brand small { display: block; margin-top: 5px; color: #8295b0; font-size: 9px; letter-spacing: 1.8px; }
.side-caption {
  display: flex; align-items: center; gap: 7px; margin: 0 22px 14px; padding: 8px 11px;
  color: #6f87a8; background: rgba(225, 237, 252, 0.58); border: 1px solid rgba(64, 107, 167, 0.09);
  border-radius: 10px; font-size: 9px; letter-spacing: 1.2px;
}
.status-orb { width: 6px; height: 6px; border-radius: 50%; background: #28aaa0; box-shadow: 0 0 9px rgba(40, 170, 160, 0.35); }
.side-caption b { margin-left: auto; color: #6c85a8; font-weight: 500; }
nav { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 0 0 12px; }
nav .group { padding: 17px 25px 7px; color: #91a2b9; font-size: 10px; font-weight: 600; letter-spacing: 1.7px; text-transform: uppercase; }
nav a, aside .foot a {
  position: relative; display: flex; align-items: center; gap: 11px; margin: 3px 13px; padding: 10px 13px;
  color: #647eA2; text-decoration: none; border: 1px solid transparent; border-radius: 13px;
  transition: color .22s ease, background .22s ease, border-color .22s ease, transform .22s ease;
}
nav a:hover, aside .foot a:hover { color: #214e91; background: rgba(224, 237, 253, 0.62); transform: translateX(2px); }
nav a.on, aside .foot a.on {
  color: #1f4f99; border-color: rgba(64, 112, 187, 0.14);
  background: linear-gradient(100deg, rgba(211, 228, 251, 0.92), rgba(232, 241, 253, 0.64));
  box-shadow: inset 0 1px rgba(255,255,255,0.7), 0 7px 20px rgba(56, 99, 163, 0.06);
}
nav a.on::before, aside .foot a.on::before {
  content: ""; position: absolute; left: -1px; width: 3px; height: 19px; border-radius: 0 5px 5px 0;
  background: #2e65b9; box-shadow: 0 0 12px rgba(46, 101, 185, 0.25);
}
.nav-icon { display: grid; place-items: center; width: 21px; color: #7c94b4; transition: color .22s ease, transform .22s ease; }
nav a:hover .nav-icon, aside .foot a:hover .nav-icon, nav a.on .nav-icon, aside .foot a.on .nav-icon { color: var(--accent); }
nav a.on .nav-icon { transform: scale(1.06); }
.nav-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
nav a .dot { margin-left: auto; width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: #b4c2d3; }
nav a .dot.ok { background: #28ad7f; box-shadow: 0 0 8px rgba(40, 173, 127, 0.25); }
nav a .dot.bad { background: #df6579; box-shadow: 0 0 8px rgba(223, 101, 121, 0.22); }
aside .foot { padding: 10px 0 16px; border-top: 1px solid var(--line); }

/* ── 抽屉菜单按钮与遮罩（仅小屏显示） ── */
#menuBtn { display: none; }
#scrim { display: none; }

/* ── 新手向导 ── */
#guide {
  position: fixed; inset: 0; z-index: 80; display: none; place-items: center; padding: 18px;
  background: rgba(35, 56, 86, .34); -webkit-backdrop-filter: blur(3px); backdrop-filter: blur(3px);
}
#guide.show { display: grid; }
.guide-card {
  width: min(94vw, 430px); padding: 26px 24px 20px; border-radius: 22px; overflow: auto;
  color: #3a587e; background: rgba(255, 255, 255, .96); border: 1px solid rgba(54, 96, 154, .16);
  box-shadow: 0 24px 70px rgba(36, 76, 135, .3); animation: contentIn .4s cubic-bezier(.2,.8,.2,1) both;
}
.guide-card .eyebrow { margin-bottom: 5px; }
.guide-card h2 { margin: 0 0 8px; font-size: 20px; color: #17345e; }
.guide-card p { margin: 0 0 14px; font-size: 13px; line-height: 1.65; color: #55719a; }
.guide-ok { color: #1d8a68; font-weight: 600; }
.guide-steps { display: flex; gap: 6px; justify-content: center; margin: 4px 0 16px; }
.guide-steps i { width: 22px; height: 5px; border-radius: 3px; background: #d7e3f2; transition: background .25s ease; }
.guide-steps i.on { background: #2e65b9; }
.guide-actions { display: flex; gap: 9px; justify-content: space-between; align-items: center; }
.guide-actions .btn.primary { margin-left: auto; }

/* ── 主区与页头 ── */
main { flex: 1; width: 100%; max-width: 1420px; min-width: 0; margin: 0 auto; padding: 28px 34px 48px; }
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.eyebrow { margin-bottom: 7px; color: #6c87ac; font-size: 10px; font-weight: 650; letter-spacing: 2px; text-transform: uppercase; }
h1 { margin: 0; color: #15325b; font-size: 28px; line-height: 1.2; font-weight: 700; letter-spacing: -0.7px; }
.page-sub { margin: 5px 0 0; color: var(--sub); font-size: 13px; }
.page-head-icon {
  width: 42px; height: 42px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 14px;
  color: var(--accent); border: 1px solid rgba(56, 103, 172, 0.12); background: rgba(255, 255, 255, 0.56);
  box-shadow: 0 8px 24px rgba(49, 90, 151, 0.06), inset 0 1px rgba(255,255,255,0.8);
}
.page-head-icon .icon { width: 20px; height: 20px; }
.live-badge { display: inline-flex; align-items: center; gap: 7px; margin-top: 10px; color: #6983a7; font-size: 10px; letter-spacing: 1.4px; }
.live-badge i { width: 6px; height: 6px; border-radius: 50%; background: var(--cyan); animation: breathe 2.4s ease-in-out infinite; }
h2 { margin: 18px 0 10px; color: #24456f; font-size: 16px; line-height: 1.4; font-weight: 620; letter-spacing: -0.15px; }
.card h2 { display: flex; align-items: center; gap: 8px; }
.card h2 .icon { width: 17px; height: 17px; color: #527cb6; }
.help { margin: -3px 0 12px; color: var(--sub); font-size: 12px; line-height: 1.6; }

/* ── 毛玻璃卡片 ── */
.card {
  position: relative; margin-bottom: 14px; padding: 18px; overflow: auto;
  background: linear-gradient(145deg, rgba(255, 255, 255, 0.86), rgba(248, 251, 255, 0.7));
  border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow), inset 0 1px rgba(255,255,255,0.8);
  -webkit-backdrop-filter: blur(22px) saturate(125%); backdrop-filter: blur(22px) saturate(125%);
  transition: border-color .28s ease, box-shadow .28s ease, transform .28s ease;
}
.card::before {
  content: ""; position: absolute; top: 0; left: 22px; right: 35%; height: 1px; pointer-events: none;
  background: linear-gradient(90deg, rgba(255, 255, 255, 0.95), transparent);
}
.card:hover { border-color: rgba(62, 108, 174, 0.22); }
.grid { display: grid; gap: 14px; margin-bottom: 14px; }
.grid.c2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid.c3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.grid > .card { margin-bottom: 0; }
.grid > .card:hover { transform: translateY(-2px); border-color: var(--line-strong); box-shadow: 0 16px 38px rgba(41, 82, 143, 0.1); }
.card > table { min-width: 420px; }
.hero { overflow: hidden; }

.hero {
  display: flex; align-items: center; justify-content: space-between; min-height: 150px; padding: 22px 26px;
  background:
    radial-gradient(circle at 82% 22%, rgba(115, 165, 237, 0.18), transparent 38%),
    linear-gradient(120deg, rgba(226, 238, 255, 0.96), rgba(248, 251, 255, 0.82));
}
.hero::after { content: ""; position: absolute; width: 250px; height: 250px; right: -70px; bottom: -150px; border: 1px solid rgba(68, 116, 188, 0.11); border-radius: 50%; box-shadow: 0 0 0 35px rgba(87,137,208,0.025), 0 0 0 70px rgba(87,137,208,0.018); }
.hero-copy { position: relative; z-index: 1; }
.hero h2 { margin: 0 0 7px; color: #183b6d; font-size: 23px; letter-spacing: -0.55px; }
.hero p { max-width: 580px; margin: 0; color: #637eA3; }
.hero-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.hero-chip { padding: 5px 11px; color: #52729f; background: rgba(255, 255, 255, 0.55); border: 1px solid rgba(65, 111, 179, 0.11); border-radius: 99px; font-size: 11px; }
.hero-visual { position: relative; width: 105px; height: 105px; display: grid; place-items: center; flex: 0 0 auto; margin-right: 18px; }
.core-mark { width: 66px; height: 66px; display: grid; place-items: center; color: #f3f7ff; border: 1px solid rgba(40, 87, 162, 0.18); border-radius: 22px; background: linear-gradient(145deg, #487dd0, #28559f); box-shadow: 0 12px 35px rgba(46, 93, 170, 0.22), inset 0 1px rgba(255,255,255,0.22); animation: coreFloat 5s ease-in-out infinite; }
.core-mark .icon { width: 34px; height: 34px; }
.orbit { position: absolute; inset: 4px; border: 1px solid rgba(61, 111, 184, 0.18); border-radius: 50%; animation: spin 18s linear infinite; }
.orbit::after { content: ""; position: absolute; top: 12px; left: 14px; width: 6px; height: 6px; border-radius: 50%; background: #2da9a3; box-shadow: 0 0 10px rgba(45, 169, 163, 0.35); }
.orbit.two { inset: 20px; border-style: dashed; animation-duration: 13s; animation-direction: reverse; }
.orbit.two::after { top: auto; left: auto; right: 3px; bottom: 17px; width: 4px; height: 4px; background: var(--accent); }
.metric-top { display: flex; align-items: center; justify-content: flex-end; margin-bottom: 12px; color: var(--sub); font-size: 12px; }
.metric-icon { width: 36px; height: 36px; display: grid; place-items: center; color: var(--accent); border: 1px solid rgba(54, 103, 178, 0.1); border-radius: 12px; background: var(--accent-soft); }
.metric-icon .icon { width: 18px; height: 18px; }
.stat { color: #193861; font-size: 30px; line-height: 1.25; font-weight: 680; letter-spacing: -0.7px; }
.stat .pill { vertical-align: 4px; }
.stat-label { margin-top: 7px; color: var(--sub); font-size: 12px; }

.pill { display: inline-flex; align-items: center; gap: 5px; width: fit-content; padding: 3px 10px; border: 1px solid transparent; border-radius: 99px; font-size: 11px; font-weight: 550; letter-spacing: .1px; white-space: nowrap; }
.pill::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.pill.ok { color: var(--ok); background: rgba(31, 158, 110, 0.08); border-color: rgba(22, 135, 95, 0.13); }
.pill.bad { color: var(--bad); background: rgba(214, 78, 101, 0.07); border-color: rgba(214, 78, 101, 0.13); }
.pill.warn { color: var(--warn); background: rgba(190, 134, 40, 0.08); border-color: rgba(173, 117, 28, 0.13); }
.pill.mute { color: #7287a4; background: rgba(105, 132, 168, 0.08); border-color: rgba(105, 132, 168, 0.11); }

/* ── 数据表格 ── */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 10px; text-align: left; vertical-align: top; border-bottom: 1px solid rgba(61, 101, 155, 0.09); }
th { color: #7b91af; font-size: 10px; font-weight: 600; letter-spacing: .65px; white-space: nowrap; text-transform: uppercase; }
td { color: #405d82; }
tr { transition: background .18s ease; }
tr:hover td { background: rgba(222, 235, 251, 0.36); }
tr:last-child td { border-bottom: none; }

/* ── 按钮与输入 ── */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 36px; padding: 7px 15px;
  color: #49678f; text-decoration: none; background: rgba(255, 255, 255, 0.58); border: 1px solid rgba(54, 99, 159, 0.16);
  border-radius: 11px; box-shadow: inset 0 1px rgba(255,255,255,0.7), 0 3px 10px rgba(43, 82, 139, 0.025);
  transition: transform .18s ease, color .18s ease, border-color .18s ease, background .18s ease, box-shadow .18s ease;
}
.btn .icon { width: 15px; height: 15px; }
.btn:hover { color: #1f4f99; border-color: rgba(43, 99, 189, 0.34); background: rgba(228, 238, 252, 0.86); transform: translateY(-1px); }
.btn:active { transform: translateY(0) scale(.97); }
.btn.primary { color: #f5f8ff; border-color: rgba(36, 79, 157, 0.22); background: linear-gradient(135deg, #3d75cb, #28569f); box-shadow: 0 8px 22px rgba(46, 90, 166, 0.2), inset 0 1px rgba(255,255,255,0.2); }
.btn.primary:hover { color: #fff; background: linear-gradient(135deg, #4c82d6, #3262ae); box-shadow: 0 10px 28px rgba(46, 90, 166, 0.27); }
.btn.danger { color: #cb4c64; }
.btn.danger:hover { color: #b8324d; border-color: rgba(202, 64, 89, 0.27); background: rgba(226, 79, 104, 0.07); }
.btn.small { min-height: 29px; padding: 4px 11px; border-radius: 9px; font-size: 11px; }
.btn:disabled { opacity: .4; cursor: default; transform: none; }

input[type=text], input[type=number], input[type=password], select, textarea {
  width: 100%; padding: 9px 12px; color: var(--text); caret-color: var(--accent);
  background: rgba(248, 251, 255, 0.85); border: 1px solid rgba(54, 96, 154, 0.16); border-radius: 11px;
  box-shadow: inset 0 1px 3px rgba(37, 74, 126, 0.035); transition: border-color .2s ease, background .2s ease, box-shadow .2s ease;
}
input::placeholder, textarea::placeholder { color: #94a6bd; }
input:hover, textarea:hover, select:hover { border-color: rgba(43, 99, 189, 0.3); }
input:focus, textarea:focus, select:focus { border-color: rgba(43, 99, 189, 0.52); background: #fff; box-shadow: 0 0 0 3px rgba(43, 99, 189, 0.09); outline: none; }
input[type=checkbox] { accent-color: var(--accent-strong); }
select { appearance: auto; }
select option { color: var(--text); background: var(--panel-solid); }
textarea { font-family: var(--mono); font-size: 12px; line-height: 1.65; resize: vertical; }
label.field { display: block; margin-bottom: 14px; color: #365479; font-size: 13px; }
label.field span { display: block; margin-bottom: 6px; color: var(--sub); font-size: 11px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 12px; }
.form-grid .field { margin-bottom: 12px; }
.form-grid .wide { grid-column: 1 / -1; }
.row { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.grow { flex: 1; min-width: 0; }
.mono { font-family: var(--mono); font-size: 11px; color: #607c9f; }
.dim { color: var(--sub); }
.clickable { color: var(--accent); cursor: pointer; }

/* ── 选项卡与聊天 ── */
.tabs { display: flex; gap: 5px; width: fit-content; max-width: 100%; margin: 0 0 14px; padding: 4px; overflow-x: auto; background: rgba(255, 255, 255, 0.64); border: 1px solid var(--line); border-radius: 15px; backdrop-filter: blur(16px); }
.tabs button { flex: 0 0 auto; padding: 7px 14px; color: #7187a5; background: transparent; border: 1px solid transparent; border-radius: 10px; transition: color .2s ease, background .2s ease, box-shadow .2s ease; }
.tabs button:hover { color: #214f9c; background: rgba(225, 237, 252, 0.5); }
.tabs button.on { color: #214f9c; background: linear-gradient(135deg, #e0ebfb, #edf4fd); border-color: rgba(43, 99, 189, 0.12); box-shadow: 0 4px 15px rgba(43, 86, 149, 0.06); }
.chat { display: flex; flex-direction: column; gap: 12px; min-height: 260px; max-height: 58vh; max-height: 58dvh; overflow-y: auto; padding: 7px 6px 10px; scroll-behavior: smooth; }
.manage-route { display: flex; flex-direction: column; height: 100vh; height: 100dvh; max-width: 1100px; padding-bottom: 24px; }
.chat-layout { display: flex; flex: 1; flex-direction: column; min-height: 0; }
.manage-route .chat { flex: 1; min-height: 0; max-height: none; padding: 2px 10px 20px 2px; }
.manage-route .chat::before { content: ""; margin-top: auto; }
.chat-composer { display: flex; align-items: center; gap: 10px; padding: 9px 10px 9px 17px; background: rgba(255,255,255,.8); border: 1px solid var(--line); border-radius: 18px; box-shadow: 0 12px 34px rgba(36,76,135,.09), inset 0 1px rgba(255,255,255,.8); backdrop-filter: blur(22px); }
.chat-composer input { min-height: 40px; padding: 8px 0; background: transparent; border: 0; box-shadow: none; }
.chat-composer input:focus { background: transparent; box-shadow: none; }
.chat-composer .btn { min-height: 40px; border-radius: 12px; }
.bubble { max-width: 76%; padding: 11px 15px; color: #3a587e; border: 1px solid var(--line); border-radius: 17px; white-space: pre-wrap; word-break: break-word; font-size: 13px; animation: bubbleIn .3s cubic-bezier(.2,.75,.25,1) both; }
.bubble.ai { align-self: flex-start; background: rgba(236, 243, 253, 0.8); border-bottom-left-radius: 5px; }
.bubble.me { align-self: flex-end; color: #f4f7ff; background: linear-gradient(135deg, #4079cf, #2d5da9); border-color: rgba(40, 85, 157, 0.15); border-bottom-right-radius: 5px; box-shadow: 0 8px 22px rgba(46, 90, 166, 0.16); }
.bubble.sys { align-self: center; padding: 5px 10px; color: var(--sub); background: transparent; border: none; font-size: 11px; }
.bubble.user { align-self: flex-start; background: var(--accent-soft); border-bottom-left-radius: 5px; }
.bubble .who { margin-bottom: 4px; color: #7b92af; font-size: 10px; }
.bubble.me .who { color: #d7e5fb; }

.tag { display: inline-block; margin: 2px 4px 2px 0; padding: 2px 8px; color: #3264aa; background: var(--accent-soft); border: 1px solid rgba(43, 99, 189, 0.09); border-radius: 7px; font-size: 10px; }
.switch { position: relative; display: inline-block; width: 43px; height: 24px; flex: 0 0 auto; vertical-align: middle; }
.switch input { position: absolute; opacity: 0; pointer-events: none; }
.switch i { position: absolute; inset: 0; background: #d7e1ed; border: 1px solid rgba(62, 101, 153, 0.11); border-radius: 99px; transition: background .24s ease, box-shadow .24s ease; }
.switch i::after { content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; background: #fff; border-radius: 50%; box-shadow: 0 2px 5px rgba(36, 68, 111, .2); transition: transform .24s cubic-bezier(.2,.8,.2,1), background .24s ease; }
.switch input:checked + i { background: linear-gradient(135deg, #4079cf, #2d5da9); box-shadow: 0 0 14px rgba(46, 90, 166, 0.16); }
.switch input:checked + i::after { background: #fff; transform: translateX(19px); }
.switch input:focus-visible + i { outline: 2px solid var(--accent); outline-offset: 3px; }

/* ── 登录、加载与反馈 ── */
#toast {
  position: fixed; left: 50%; bottom: calc(28px + env(safe-area-inset-bottom, 0px)); z-index: 99; max-width: min(90vw, 560px); padding: 11px 19px;
  color: #264a79; background: rgba(255, 255, 255, 0.9); border: 1px solid rgba(43, 99, 189, 0.18); border-radius: 14px;
  box-shadow: 0 15px 45px rgba(36, 76, 135, 0.15); backdrop-filter: blur(22px); font-size: 12px;
  opacity: 0; pointer-events: none; transform: translate(-50%, 18px) scale(.97); transition: opacity .25s ease, transform .25s ease;
}
#toast.show { opacity: 1; transform: translate(-50%, 0) scale(1); }
#toast.err { color: #b83e55; background: rgba(255, 246, 248, 0.94); border-color: rgba(202, 64, 89, 0.22); }
#login { width: calc(100% - 32px); max-width: 410px; margin: 14vh auto 0; margin-top: max(32px, 14dvh); padding: 34px; border-radius: 28px; animation: contentIn .55s cubic-bezier(.2,.8,.2,1) both; }
.login-logo { width: 58px; height: 58px; display: grid; place-items: center; margin-bottom: 22px; color: #f4f8ff; background: linear-gradient(145deg, #467ed1, #28569f); border: 1px solid rgba(36, 79, 157, .18); border-radius: 19px; box-shadow: 0 10px 30px rgba(46, 90, 166, .2); }
.login-logo .icon { width: 30px; height: 30px; }
#login h1 { font-size: 25px; }
#login input { margin: 17px 0 12px; min-height: 44px; }
#login .btn { min-height: 43px; }
.loading-state { min-height: 240px; display: grid; place-items: center; align-content: center; gap: 14px; color: var(--sub); }
.loader { width: 29px; height: 29px; border: 2px solid rgba(43, 99, 189, .12); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; }
.loading-state p { margin: 0; font-size: 12px; letter-spacing: .4px; }

#main > * { animation: contentIn .46s cubic-bezier(.2,.75,.25,1) both; }
#main > *:nth-child(2) { animation-delay: .045s; }
#main > *:nth-child(3) { animation-delay: .08s; }
#main > *:nth-child(4) { animation-delay: .11s; }
#main > *:nth-child(n+5) { animation-delay: .13s; }
@keyframes contentIn { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
@keyframes bubbleIn { from { opacity: 0; transform: translateY(7px) scale(.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes breathe { 0%,100% { opacity: .45; box-shadow: 0 0 0 0 rgba(101,228,221,.2); } 50% { opacity: 1; box-shadow: 0 0 0 5px rgba(101,228,221,0); } }
@keyframes coreFloat { 0%,100% { transform: translateY(0) rotate(-2deg); } 50% { transform: translateY(-7px) rotate(2deg); } }

@media (max-width: 1100px) {
  aside { width: 84px; flex-basis: 84px; }
  aside .brand { justify-content: center; padding: 24px 12px 18px; }
  .brand-copy, .side-caption, .nav-label { display: none; }
  nav .group { padding: 16px 0 7px; text-align: center; font-size: 8px; letter-spacing: 1px; }
  nav a, aside .foot a { justify-content: center; margin: 4px 11px; padding: 12px 8px; }
  nav a .dot { position: absolute; top: 7px; right: 8px; }
  nav a:hover, aside .foot a:hover { transform: none; }
  main { padding: 30px 28px 64px; }
}
@media (max-width: 900px) {
  .grid.c2, .grid.c3 { grid-template-columns: 1fr; }
  .hero { min-height: 150px; }
  .hero-visual { margin-right: 5px; }
}
@media (max-width: 640px) {
  body::before { background-position: center, center, 64% 42%; }
  #menuBtn {
    position: fixed; top: calc(10px + min(env(safe-area-inset-top, 0px), 22px)); left: 12px; z-index: 70;
    display: grid; place-items: center; width: 46px; height: 46px; padding: 0; color: #2b5aa3;
    border: 1px solid rgba(54, 96, 154, .16); border-radius: 15px;
    background: rgba(255, 255, 255, .88); box-shadow: 0 10px 30px rgba(36, 76, 135, .16), inset 0 1px rgba(255,255,255,.8);
    -webkit-backdrop-filter: blur(22px) saturate(140%); backdrop-filter: blur(22px) saturate(140%);
    transition: transform .32s cubic-bezier(.3,.8,.3,1), background .2s ease;
  }
  #menuBtn .icon { width: 21px; height: 21px; }
  #menuBtn:active { background: rgba(228, 238, 252, .94); }
  body.drawer-open #menuBtn { transform: translateX(294px); }
  #scrim {
    position: fixed; inset: 0; z-index: 55; display: block;
    background: rgba(35, 56, 86, .3); -webkit-backdrop-filter: blur(2px); backdrop-filter: blur(2px);
    opacity: 0; pointer-events: none; transition: opacity .3s ease;
  }
  body.drawer-open #scrim { opacity: 1; pointer-events: auto; }
  aside {
    position: fixed; top: 0; bottom: 0; left: 0; width: 282px; flex-basis: 282px; height: auto; z-index: 60;
    transform: translateX(-103%); transition: transform .32s cubic-bezier(.3,.8,.3,1);
    border-right: 1px solid rgba(54, 96, 154, .14);
    background: rgba(255, 255, 255, .94); box-shadow: none;
  }
  body.drawer-open aside { transform: translateX(0); box-shadow: 0 22px 70px rgba(36, 76, 135, .28); }
  aside::before { display: block; }
  aside .brand { display: flex; justify-content: flex-start; padding: 28px 24px 22px; }
  .brand-copy, .side-caption, .nav-label { display: block; }
  nav { display: block; padding: 0 0 12px; overflow-y: auto; overflow-x: hidden; }
  nav .group { display: block; padding: 17px 25px 7px; text-align: left; font-size: 10px; letter-spacing: 1.7px; }
  nav a, aside .foot a { min-width: 0; min-height: 0; flex-direction: row; justify-content: flex-start; gap: 11px; margin: 3px 13px; padding: 10px 13px; border-radius: 13px; font-size: inherit; }
  nav a .nav-label { display: block; max-width: none; }
  nav a .dot { position: static; top: auto; right: auto; margin-left: auto; }
  nav a.on::before, aside .foot a.on::before { top: auto; left: -1px; width: 3px; height: 19px; transform: none; border-radius: 0 5px 5px 0; }
  .nav-icon { width: 21px; }
  .nav-icon .icon { width: 19px; height: 19px; }
  aside .foot { display: block; flex: 0 0 auto; padding: 10px 0 16px; border: 0; border-top: 1px solid var(--line); }
  aside .foot a { min-width: 0; }
  main { padding: calc(66px + min(env(safe-area-inset-top, 0px), 22px)) 14px 30px; }
  .manage-route { padding: calc(66px + min(env(safe-area-inset-top, 0px), 22px)) 14px 24px; }
  .manage-route .page-head { margin-bottom: 12px; }
  .chat-composer { gap: 7px; padding: 7px 7px 7px 13px; border-radius: 16px; }
  .chat-composer .btn { padding: 7px 12px; }
  .page-head { margin-bottom: 13px; }
  h1 { font-size: 26px; }
  .page-sub { margin-top: 5px; font-size: 12px; }
  .page-head-icon { width: 45px; height: 45px; border-radius: 15px; }
  .card { padding: 16px; border-radius: 18px; }
  .form-grid { grid-template-columns: 1fr; }
  .form-grid .wide { grid-column: auto; }
  .hero { min-height: 0; padding: 20px; }
  .hero h2 { margin-bottom: 7px; font-size: 21px; }
  .hero p { font-size: 12px; }
  .hero-chips { gap: 6px; margin-top: 12px; }
  .hero-chip { padding: 3px 8px; font-size: 10px; }
  .hero-visual { width: 82px; height: 82px; }
  .core-mark { width: 49px; height: 49px; border-radius: 16px; }
  .core-mark .icon { width: 26px; height: 26px; }
  .grid.metrics { grid-template-columns: 1fr; gap: 10px; }
  .grid.metrics .metric-card { display: grid; grid-template-columns: 34px auto minmax(0, 1fr); align-items: center; gap: 8px 10px; padding: 12px 14px; }
  .grid.metrics .metric-top { margin: 0; }
  .grid.metrics .metric-icon { width: 32px; height: 32px; }
  .grid.metrics .metric-card .stat-label { margin: 0; white-space: normal; }
  .grid.metrics .metric-card .btn { grid-column: 1 / -1; justify-self: start; min-height: 32px; margin-top: 2px !important; padding: 5px 11px; }
  .metric-top { margin-bottom: 10px; }
  .metric-top > span:first-child { display: none; }
  .metric-icon { width: 30px; height: 30px; border-radius: 10px; }
  .stat { font-size: 23px; letter-spacing: -0.5px; }
  .stat .pill { padding: 2px 6px; font-size: 9px; }
  .stat-label { margin-top: 4px; font-size: 10px; }
  .metric-card .stat-label { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bubble { max-width: 90%; }
  .chat { min-height: 220px; max-height: 55dvh; }
  .tabs { width: 100%; margin-bottom: 12px; }
  th, td { padding: 8px; }
  #toast { bottom: calc(24px + env(safe-area-inset-bottom, 0px)); }
  #login { margin-top: max(24px, 7dvh); padding: 27px; }
}
@media (max-width: 430px) {
  .hero-visual { display: none; }
  .page-head-icon { display: none; }
}
@media (min-width: 641px) and (max-height: 500px) and (orientation: landscape) {
  main { padding-top: 16px; }
  .page-head { margin-bottom: 14px; }
  .page-sub { margin-top: 4px; }
  .hero { min-height: 0; padding-top: 18px; padding-bottom: 18px; }
  .hero-visual { width: 80px; height: 80px; }
  .chat { min-height: 150px; max-height: 52dvh; }
  #login { margin-top: 20px; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
}
</style>
</head>
<body>
<div id="toast" role="status" aria-live="polite"></div>
<div id="guide"><div class="guide-card" id="guideCard"></div></div>
<div id="login" class="card" style="display:none">
  <div class="login-logo" data-icon="spark"></div>
  <div class="eyebrow">安全管理面板</div>
  <h1>欢迎回来</h1>
  <p class="dim" style="font-size:13px;margin:9px 0 0">输入面板访问密码，进入 DeepLive 控制台。</p>
  <input type="password" id="loginToken" placeholder="面板访问 Token" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
  <button class="btn primary" style="width:100%" onclick="doLogin()">进入控制台 <span data-icon="arrow"></span></button>
  <p class="dim" style="font-size:10px;margin:14px 0 0">Token 即 config.json 中的 webui.token</p>
</div>

<div id="app" style="display:none">
<button id="menuBtn" type="button" aria-label="打开菜单" onclick="toggleDrawer()"><span data-icon="menu"></span></button>
<div id="scrim" onclick="closeDrawer()"></div>
<aside>
  <div class="brand">
    <div class="brand-logo" data-icon="spark"></div>
    <div class="brand-copy"><div class="brand-name">DeepLive</div><small>智能助手控制台</small></div>
  </div>
  <div class="side-caption"><span class="status-orb"></span> 控制台</div>
  <nav id="nav" aria-label="主导航"></nav>
  <div class="foot">
    <a href="#/settings" data-route="settings" title="设置"><span class="nav-icon" data-icon="settings"></span><span class="nav-label">设置</span></a>
    <a href="#/update" data-route="update" title="检查更新"><span class="nav-icon" data-icon="update"></span><span class="nav-label">检查更新</span></a>
  </div>
</aside>
<main id="main"></main>
</div>

<script>
"use strict";
// ─── 基础设施 ────────────────────────────────────────────────────────────────
var TOKEN = localStorage.getItem("dl_token") || new URLSearchParams(location.search).get("token") || "";
var state = { route: "home", plugins: [], pluginsLoaded: false };

function h(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }

// 内嵌 Lucide 风格 SVG：无图标字体、无外部请求，颜色统一跟随 currentColor。
var ICONS = {
  spark: '<path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z"/><path d="m19 16 .8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16Z"/>',
  home: '<path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10M9 20v-6h6v6"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V4H6.5A2.5 2.5 0 0 0 4 6.5v13Z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5M8 7h8M8 11h6"/>',
  message: '<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4v8Z"/><path d="M8 9h8M8 13h5"/>',
  bot: '<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M8 12h.01M16 12h.01M8 16c2.5 1.3 5.5 1.3 8 0M2 12v4M22 12v4"/>',
  game: '<path d="M8 6h8a6 6 0 0 1 5.7 7.8l-1.4 4.3a2.5 2.5 0 0 1-4.1.9L14 17h-4l-2.2 2a2.5 2.5 0 0 1-4.1-.9l-1.4-4.3A6 6 0 0 1 8 6Z"/><path d="M7 10v4M5 12h4M16 11h.01M18 13h.01"/>',
  tv: '<rect x="3" y="5" width="18" height="13" rx="3"/><path d="m8 2 4 3 4-3M8 22h8M10 9l4 2.5-4 2.5V9Z"/>',
  cloud: '<path d="M17.5 18.5a4 4 0 0 0 .4-7.98 5.5 5.5 0 0 0-10.72 1.4A3.5 3.5 0 0 0 7.5 18.5h10Z"/>',
  puzzle: '<path d="M19 13h-2a2 2 0 1 0 0 4h2v4h-4v-2a2 2 0 1 0-4 0v2H7v-4H5a2 2 0 1 1 0-4h2V9H3V5h4V3a2 2 0 1 1 4 0v2h4v4h2a2 2 0 1 1 0 4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
  update: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 8a7 7 0 0 1 11.4-1L20 12M4 12l2.5 5a7 7 0 0 0 11.4-1"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  send: '<path d="m22 2-7 20-4-9-9-4 20-7Z"/><path d="M22 2 11 13"/>',
  activity: '<path d="M3 12h4l2.5-7 5 14 2.5-7h4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  refresh: '<path d="M20 6v6h-6M4 18v-6h6"/><path d="M18.5 9A7 7 0 0 0 6 6.5L4 12M20 12l-2 5.5A7 7 0 0 1 6.5 15"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  x: '<path d="m6 6 12 12M18 6 6 18"/>',
};
function icon(name, className) {
  return '<svg class="icon ' + h(className || "") + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || ICONS.puzzle) + "</svg>";
}
function hydrateIcons() {
  var nodes = document.querySelectorAll("[data-icon]");
  for (var i = 0; i < nodes.length; i++) {
    nodes[i].innerHTML = icon(nodes[i].getAttribute("data-icon"));
    nodes[i].removeAttribute("data-icon");
  }
}

function api(path, method, body) {
  var sep = path.indexOf("?") === -1 ? "?" : "&";
  return fetch(path + sep + "token=" + encodeURIComponent(TOKEN), {
    method: method || "GET",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  }).then(function (r) {
    if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
    return r.json().then(function (data) {
      if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
      return data;
    });
  });
}

function toast(msg, isErr) {
  var el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "show" + (isErr ? " err" : "");
  setTimeout(function () { el.className = ""; }, 2600);
}
function fmtTime(sec) {
  if (!sec) return "—";
  var d = new Date(sec * 1000);
  var p = function (n) { return String(n).padStart(2, "0"); };
  return (d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}
function fmtBytes(n) {
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}
function fmtUptime(sec) {
  if (sec >= 86400) return Math.floor(sec / 86400) + " 天 " + Math.floor(sec % 86400 / 3600) + " 时";
  if (sec >= 3600) return Math.floor(sec / 3600) + " 时 " + Math.floor(sec % 3600 / 60) + " 分";
  return Math.floor(sec / 60) + " 分";
}
function el(html) { var d = document.createElement("div"); d.innerHTML = html; return d.firstElementChild; }

function doLogin() {
  var v = document.getElementById("loginToken").value.trim();
  if (!v) return;
  localStorage.setItem("dl_token", v);
  TOKEN = v;
  document.getElementById("login").style.display = "none";
  boot();
}
function showLogin() {
  document.getElementById("app").style.display = "none";
  document.getElementById("login").style.display = "block";
  hydrateIcons();
}

// ─── 路由 ────────────────────────────────────────────────────────────────────
function renderToolPluginPage(main, name) { renderToolPlugin(main, name); }
var PLUGIN_PAGES = {
  onebot: { title: "QQ 机器人", icon: "bot", render: renderOnebot },
  mc: { title: "Minecraft 陪玩", icon: "game", render: renderMc },
  bili: { title: "哔哩哔哩", icon: "tv", render: renderBili },
  novel: { title: "小说工具", icon: "book", render: function (main) { renderToolPlugin(main, "novel"); } },
  weather: { title: "天气工具", icon: "cloud", render: function (main) { renderToolPlugin(main, "weather"); } },
};
var PLUGIN_TITLES = { novel: "小说工具", weather: "天气工具", bili: "B 站视频工具", mc: "Minecraft 游戏工具" };
var FIXED_NAV = [
  { route: "home", title: "首页", icon: "home", render: renderHome },
  { route: "kb", title: "知识库", icon: "book", render: renderKb },
  { route: "manage", title: "主 AI 对话", icon: "message", render: renderManage },
  { route: "napcat", title: "NapCat 配置", icon: "database", render: renderNapcat },
  { route: "probes", title: "性能", icon: "activity", render: renderProbes },
];

function toggleDrawer() {
  var open = !document.body.classList.contains("drawer-open");
  document.body.classList.toggle("drawer-open", open);
  var btn = document.getElementById("menuBtn");
  btn.innerHTML = icon(open ? "x" : "menu");
  btn.setAttribute("aria-label", open ? "关闭菜单" : "打开菜单");
}
function closeDrawer() {
  if (!document.body.classList.contains("drawer-open")) return;
  document.body.classList.remove("drawer-open");
  var btn = document.getElementById("menuBtn");
  btn.innerHTML = icon("menu");
  btn.setAttribute("aria-label", "打开菜单");
}

function currentRoute() {
  var hash = location.hash.replace(/^#\\/?/, "");
  return hash || "home";
}
function buildNav() {
  var nav = document.getElementById("nav");
  var html = '<div class="group">常用功能</div>';
  for (var i = 0; i < FIXED_NAV.length; i++) {
    var item = FIXED_NAV[i];
    html += '<a href="#/' + item.route + '" data-route="' + item.route + '" title="' + item.title + '">' +
      '<span class="nav-icon">' + icon(item.icon) + '</span><span class="nav-label">' + item.title + "</span></a>";
  }
  html += '<div class="group">扩展功能</div>';
  for (var j = 0; j < state.plugins.length; j++) {
    var p = state.plugins[j];
    var page = PLUGIN_PAGES[p.name];
    var dot = p.error ? "bad" : p.enabled ? "ok" : "";
    if (page) {
      html += '<a href="#/plugin/' + p.name + '" data-route="plugin/' + p.name + '" title="' + page.title + '">' +
        '<span class="nav-icon">' + icon(page.icon) + '</span><span class="nav-label">' + page.title + '</span><span class="dot ' + dot + '"></span></a>';
    } else {
      // 无专属页面的工具插件走通用插件页（renderToolPlugin）。
      var pluginTitle = PLUGIN_TITLES[p.name] || p.name;
      html += '<a href="#/plugin/' + p.name + '" data-route="plugin/' + p.name + '" title="' + h(pluginTitle) + '">' +
        '<span class="nav-icon">' + icon("puzzle") + '</span><span class="nav-label">' + h(pluginTitle) + '</span><span class="dot ' + dot + '"></span></a>';
    }
  }
  nav.innerHTML = html;
}
function markNav(route) {
  var links = document.querySelectorAll("a[data-route]");
  for (var i = 0; i < links.length; i++) {
    links[i].classList.toggle("on", links[i].getAttribute("data-route") === route);
  }
}

function route() {
  closeDrawer();
  var r = currentRoute();
  state.route = r;
  markNav(r);
  var main = document.getElementById("main");
  main.className = r === "manage" ? "manage-route" : "";
  main.innerHTML = '<div class="loading-state"><span class="loader"></span><p>正在同步数据…</p></div>';
  var fixed = null;
  for (var i = 0; i < FIXED_NAV.length; i++) if (FIXED_NAV[i].route === r) fixed = FIXED_NAV[i];
  if (fixed) { fixed.render(main); return; }
  if (r.indexOf("plugin/") === 0) {
    var name = r.slice(7);
    var page = PLUGIN_PAGES[name];
    if (page) { page.render(main); return; }
    if (/^[\w-]+$/.test(name)) { renderToolPlugin(main, name); return; }
  }
  if (r === "settings") { renderSettings(main); return; }
  if (r === "update") { renderUpdate(main); return; }
  renderHome(main);
}

function pageHead(title, sub) {
  return '<div class="page-head"><div><h1>' + h(title) + '</h1>' +
    '<p class="page-sub">' + h(sub || "") + '</p></div><div class="page-head-icon">' + icon("spark") + "</div></div>";
}
function cardTitle(name, title) {
  return '<h2 style="margin-top:0">' + icon(name) + h(title) + "</h2>";
}

// ─── 首页 ────────────────────────────────────────────────────────────────────
function renderHome(main) {
  main.innerHTML = pageHead("首页", "机器人、会话和模型的运行概况");
  api("/api/status").then(function (s) {
    var usage = s.usage || {};
    var total = usage.total || {};
    var rolesHtml = "";
    var roleNames = { main: "主对话", vision: "图片理解", summary: "后台总结" };
    for (var i = 0; i < s.roles.length; i++) {
      var role = s.roles[i];
      rolesHtml += "<tr><td>" + h(roleNames[role.role] || role.role) + "</td><td>" + h(role.channel) + "</td><td class=mono>" + h(role.model) + "</td></tr>";
    }
    main.innerHTML = pageHead("首页", "机器人、会话和模型的运行概况") +
      '<div class="card hero"><div class="hero-copy"><div class="eyebrow">运行概况</div><h2>DeepLive 控制台</h2>' +
      '<p>绿色表示运行正常；如果 QQ 显示离线，请先检查 NapCat 是否已连接。</p>' +
      '<div class="hero-chips"><span class="hero-chip">' + (s.bot.connected ? "QQ 已连接" : "QQ 未连接") + '</span>' +
      '<span class="hero-chip">' + s.scopes.enabled + ' 个启用会话</span><span class="hero-chip">' + (total.requests || 0) + ' 次模型请求</span></div></div>' +
      '<div class="hero-visual"><span class="orbit"></span><span class="orbit two"></span><div class="core-mark">' + icon("spark") + "</div></div></div>" +
      (s.bot.connected ? "" : '<div class="card" style="border-left:3px solid #e8a33d"><div class="row" style="justify-content:space-between"><span>QQ 目前未连接。先确认 NapCat 已登录，再检查下面的连接参数。</span>' +
        '<a class="btn primary" href="#/napcat">' + icon("database") + 'NapCat 连接配置</a></div></div>') +
      (s.bot.connected && s.scopes.enabled === 0 ? '<div class="card" style="border-left:3px solid #e8a33d"><div class="row" style="justify-content:space-between"><span>QQ 已连接，但还没有会话开启自动回复，AI 收到消息不会回应。</span>' +
        '<a class="btn primary" href="#/settings">' + icon("activity") + '去设置开启</a></div></div>' : "") +
      (s.bot.connected && s.scopes.enabled > 0 && !(total.requests || 0) ? '<div class="card" style="border-left:3px solid #6aa5e8"><div class="row" style="justify-content:space-between"><span>一切就绪。去 QQ 给 bot 发条消息试试，或让朋友 @它。</span>' +
        '<a class="btn" href="#/manage">' + icon("message") + '先在面板和它聊聊</a></div></div>' : "") +
      '<div class="grid c3 metrics">' +
      '<div class="card metric-card"><div class="metric-top"><span>QQ 状态</span><span class="metric-icon">' + icon("bot") + '</span></div>' +
      '<div class="stat">' + (s.bot.connected ? '<span class="pill ok">在线</span>' : '<span class="pill bad">离线</span>') + '</div>' +
      '<div class="stat-label">' + h(s.bot.nickname) + " · " + h(s.bot.selfId || "未知") + '</div>' +
      '<a class="btn" href="#/napcat" style="margin-top:10px">' + icon("database") + 'NapCat 设置</a></div>' +
      '<div class="card metric-card"><div class="metric-top"><span>运行时间</span><span class="metric-icon">' + icon("clock") + '</span></div>' +
      '<div class="stat">' + fmtUptime(s.uptimeSec) + '</div><div class="stat-label">本次连续运行时间</div></div>' +
      '<div class="card metric-card"><div class="metric-top"><span>自动回复</span><span class="metric-icon">' + icon("users") + '</span></div>' +
      '<div class="stat">' + s.scopes.enabled + ' <span style="color:#607a9f;font-size:20px;font-weight:500">/ ' + s.scopes.total + '</span></div>' +
      '<div class="stat-label">已启用 / 全部会话</div></div></div>' +
      '<div class="grid c2">' +
      '<div class="card">' + cardTitle("activity", "模型用量（本次启动）") + '<p class=help>统计 AI 调用次数和 Token 消耗，重启服务后重新计数。</p><table>' +
      "<tr><th>调用次数</th><td>" + (total.requests || 0) + "</td></tr>" +
      "<tr><th>失败次数</th><td>" + (total.failures || 0) + "</td></tr>" +
      "<tr><th>输入 Token</th><td>" + (total.inputTokens || 0).toLocaleString() + "</td></tr>" +
      "<tr><th>输出 Token</th><td>" + (total.outputTokens || 0).toLocaleString() + "</td></tr>" +
      (usage.lastError ? '<tr><th>最近错误</th><td class=mono style="color:var(--bad)">' + h(usage.lastError.message || usage.lastError) + "</td></tr>" : "") +
      "</table></div>" +
      '<div class="card">' + cardTitle("cpu", "各功能使用的模型") + '<p class=help>主对话、图片理解和后台总结可以分别使用不同模型。</p>' +
      (rolesHtml ? "<table><tr><th>用途</th><th>渠道</th><th>模型</th></tr>" + rolesHtml + "</table>" : '<p class=dim>还没有配置模型</p>') +
      "</div></div>";
  }).catch(function (e) { main.innerHTML = pageHead("首页", "") + '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
}

// ─── 知识 ────────────────────────────────────────────────────────────────────
var kbView = { entries: [], pending: [], mounts: [], q: "", editing: null };

function renderKb(main) {
  main.innerHTML = pageHead("知识库", "管理 AI 可以查阅和使用的知识");
  Promise.all([
    api("/api/kb/entries?limit=300"),
    api("/api/kb/pending"),
    api("/api/kb/mounts"),
  ]).then(function (res) {
    kbView.entries = res[0].entries;
    kbView.pending = res[1].pending;
    kbView.mounts = res[2].mounts;
    paintKb(main);
  }).catch(function (e) { main.innerHTML = pageHead("知识库", "") + '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
}

function paintKb(main) {
  var html = pageHead("知识库", kbView.entries.length + " 条知识 · " + kbView.pending.length + " 条待确认 · " + kbView.mounts.length + " 个使用范围");
  html += '<div class="card"><div class="row">' +
    '<input type=text id=kbQ class=grow placeholder="搜索主题、内容或标签…" value="' + h(kbView.q) + '" onkeydown="if(event.key===\\'Enter\\')kbSearch()">' +
    '<button class="btn" onclick="kbSearch()">' + icon("search") + '搜索</button>' +
    '<button class="btn primary" onclick="kbNew()">' + icon("plus") + '添加知识</button></div></div>';

  if (kbView.pending.length > 0) {
    html += '<div class="card"><h2 style="margin-top:0;color:var(--warn)">待确认的知识（' + kbView.pending.length + "）</h2>" +
      '<p class=help>这些知识由 AI 自动整理，确认内容准确后再正式加入知识库。</p><table><tr><th>主题</th><th>内容</th><th>提交者</th><th>时间</th><th></th></tr>';
    for (var i = 0; i < kbView.pending.length; i++) {
      var p = kbView.pending[i];
      html += "<tr><td>" + h(p.entry.subject) + "</td><td>" + h(p.entry.content.slice(0, 80)) + "</td><td class=mono>" + h(p.proposedBy) + "</td><td class=mono>" + fmtTime(p.proposedAt) + "</td>" +
        '<td style="white-space:nowrap"><button class="btn small" onclick="kbReview(\\'' + h(p.id) + '\\',\\'approve\\')">通过</button> ' +
        '<button class="btn small danger" onclick="kbReview(\\'' + h(p.id) + '\\',\\'reject\\')">拒绝</button></td></tr>';
    }
    html += "</table></div>";
  }

  html += '<div class="card">' + cardTitle("database", "知识条目") + '<p class=help>每条知识由主题、正文和标签组成，标签方便分类和限制使用范围。</p>';
  if (kbView.entries.length === 0) html += '<p class=dim>没有找到知识，试试其他关键词或点击「添加知识」。</p>';
  else {
    html += "<table><tr><th style=width:22%>主题</th><th>内容</th><th style=width:16%>标签</th><th style=width:9%>置信</th><th style=width:12%></th></tr>";
    for (var j = 0; j < kbView.entries.length; j++) {
      var e = kbView.entries[j];
      html += "<tr><td>" + h(e.subject) + '</td><td style="white-space:pre-wrap">' + h(e.content.slice(0, 200)) +
        (e.content.length > 200 ? "…" : "") + "</td><td>" + e.tags.map(function (t) { return '<span class=tag>' + h(t) + "</span>"; }).join("") +
        "</td><td>" + (e.confidence != null ? e.confidence.toFixed(2) : "—") + "</td>" +
        '<td style="white-space:nowrap"><button class="btn small" onclick="kbEdit(\\'' + h(e.id) + '\\')">编辑</button> ' +
        '<button class="btn small danger" onclick="kbDel(\\'' + h(e.id) + '\\')">删</button></td></tr>';
    }
    html += "</table>";
  }
  html += "</div>";

  html += '<div class="card"><div class="row" style="margin-bottom:6px"><h2 style="margin:0" class=grow>谁可以使用这些知识</h2>' +
    '<button class="btn small primary" onclick="kbMountNew()">' + icon("plus") + '添加使用范围</button></div>' +
    '<p class=help>指定群聊、私聊或其他 AI 可以查阅哪些知识；标签留空表示全部知识。</p>';
  if (kbView.mounts.length === 0) html += '<p class=dim>还没有设置使用范围，其他 AI 暂时看不到这些知识。</p>';
  else {
    html += "<table><tr><th>使用对象</th><th>权限</th><th>知识标签</th><th>添加时间</th><th></th></tr>";
    for (var k = 0; k < kbView.mounts.length; k++) {
      var m = kbView.mounts[k];
      html += "<tr><td class=mono>" + h(m.target) + "</td><td>" + h(m.permission) + "</td><td>" +
        (m.tags && m.tags.length ? m.tags.map(function (t) { return '<span class=tag>' + h(t) + "</span>"; }).join("") : '<span class=dim>全部</span>') +
        "</td><td class=mono>" + fmtTime(m.grantedAt) + '</td>' +
        '<td><button class="btn small danger" onclick="kbUnmount(\\'' + h(m.target) + "\\','" + h((m.tags || []).join(",")) + '\\')">撤销</button></td></tr>';
    }
    html += "</table>";
  }
  html += "</div>";

  if (kbView.editing !== null) html += kbEditForm();
  main.innerHTML = html;
}

function kbEditForm() {
  var e = kbView.editing === "new" ? { id: "", subject: "", content: "", tags: [], confidence: 0.9 } : null;
  if (e === null) {
    for (var i = 0; i < kbView.entries.length; i++) if (kbView.entries[i].id === kbView.editing) e = kbView.entries[i];
  }
  if (e === null) return "";
  return '<div class="card" style="border-color:var(--accent)"><h2 style="margin-top:0">' + (e.id ? "编辑知识" : "添加知识") + '</h2>' +
    '<p class=help>用简短主题说明这条知识是什么，正文填写 AI 需要记住的内容。</p>' +
    '<label class=field><span>主题</span><input type=text id=kbFSubject value="' + h(e.subject) + '"></label>' +
    '<label class=field><span>内容</span><textarea id=kbFContent rows=4>' + h(e.content) + "</textarea></label>" +
    '<label class=field><span>标签（逗号分隔）</span><input type=text id=kbFTags value="' + h(e.tags.join(", ")) + '"></label>' +
    '<div class="row"><button class="btn primary" onclick="kbSave(\\'' + h(e.id) + '\\')">保存</button>' +
    '<button class="btn" onclick="kbCancel()">取消</button></div></div>';
}
function kbSearch() { kbView.q = document.getElementById("kbQ").value.trim(); var main = document.getElementById("main"); var q = kbView.q ? "&q=" + encodeURIComponent(kbView.q) : "";
  api("/api/kb/entries?limit=300" + q).then(function (r) { kbView.entries = r.entries; paintKb(main); }); }
function kbNew() { kbView.editing = "new"; paintKb(document.getElementById("main")); }
function kbEdit(id) { kbView.editing = id; paintKb(document.getElementById("main")); }
function kbCancel() { kbView.editing = null; paintKb(document.getElementById("main")); }
function kbSave(id) {
  var body = {
    subject: document.getElementById("kbFSubject").value.trim(),
    content: document.getElementById("kbFContent").value.trim(),
    tags: document.getElementById("kbFTags").value.split(/[,，]/).map(function (t) { return t.trim(); }).filter(Boolean),
  };
  var call = id ? api("/api/kb/entry/update", "POST", Object.assign({ id: id }, body)) : api("/api/kb/entry", "POST", body);
  call.then(function () { toast("已保存"); kbView.editing = null; renderKb(document.getElementById("main")); })
    .catch(function (e) { toast(e.message, true); });
}
function kbDel(id) { if (!confirm("删除该条目？")) return; api("/api/kb/entry/delete", "POST", { id: id }).then(function () { toast("已删除"); renderKb(document.getElementById("main")); }).catch(function (e) { toast(e.message, true); }); }
function kbReview(id, action) { api("/api/kb/review", "POST", { id: id, action: action }).then(function () { toast(action === "approve" ? "已通过入库" : "已拒绝"); renderKb(document.getElementById("main")); }).catch(function (e) { toast(e.message, true); }); }
function kbMountNew() {
  var target = prompt("谁可以使用知识？填写使用对象：\\n群聊 group:群号（如 group:123456）\\n私聊 private:QQ号（如 private:789）\\n全部对象填 all；游戏 AI 可填 domain:mc。");
  if (!target) return;
  var perm = prompt("允许的操作：read=只读，append=可补充，write=可修改。\\n一般保持 read 即可：", "read");
  if (!perm) return;
  var tags = prompt("只允许使用哪些标签的知识？多个标签用逗号分隔，留空表示全部知识：", "");
  api("/api/kb/mount", "POST", { target: target.trim(), permission: perm.trim(), tags: tags && tags.trim() ? tags.split(/[,，]/).map(function (t) { return t.trim(); }).filter(Boolean) : undefined })
    .then(function () { toast("已授权"); renderKb(document.getElementById("main")); }).catch(function (e) { toast(e.message, true); });
}
function kbUnmount(target, tags) {
  if (!confirm("取消 " + target + " 的知识使用权限？")) return;
  api("/api/kb/unmount", "POST", { target: target, tags: tags ? tags.split(",").filter(Boolean) : undefined })
    .then(function () { toast("已撤销"); renderKb(document.getElementById("main")); }).catch(function (e) { toast(e.message, true); });
}

// ─── 管理（与主AI对话） ───────────────────────────────────────────────────────
var chatLastTs = 0;
var chatTimer = null;

function renderManage(main) {
  if (chatTimer) { clearInterval(chatTimer); chatTimer = null; }
  chatLastTs = 0;
  main.innerHTML = pageHead("主 AI 对话", "直接与主 AI 交谈；这里与主人 QQ 私聊共用对话记录") +
    '<div class="chat-layout"><div class="chat" id="chatBox"></div>' +
    '<div class="chat-composer"><input type=text id=chatInput class=grow placeholder="输入消息，按回车发送…" aria-label="消息内容" onkeydown="if(event.key===\\'Enter\\')chatSend()">' +
    '<button class="btn primary" onclick="chatSend()">' + icon("send") + '发送</button></div></div>';
  chatPoll();
  chatTimer = setInterval(chatPoll, 2500);
}
function chatPoll() {
  if (state.route !== "manage") { if (chatTimer) { clearInterval(chatTimer); chatTimer = null; } return; }
  api("/api/chat?after=" + chatLastTs).then(function (r) {
    if (chatLastTs === 0 && r.messages.length === 0) {
      var box = document.getElementById("chatBox");
      if (box) box.innerHTML = '<div class="bubble sys">还没有消息，在下方输入内容即可开始对话。</div>';
    }
    var box2 = document.getElementById("chatBox");
    if (!box2) return;
    for (var i = 0; i < r.messages.length; i++) {
      var m = r.messages[i];
      chatLastTs = Math.max(chatLastTs, m.ts);
      var div = el('<div class="bubble ' + h(m.role) + '"><div class=who>' + h(m.name) + " · " + fmtTime(m.ts) + '</div>' + h(m.text) + "</div>");
      box2.appendChild(div);
    }
    box2.scrollTop = box2.scrollHeight;
  }).catch(function () { /* 轮询失败静默 */ });
}
function chatSend() {
  var input = document.getElementById("chatInput");
  var text = input.value.trim();
  if (!text) return;
  input.value = "";
  api("/api/chat", "POST", { text: text }).then(function () { setTimeout(chatPoll, 400); }).catch(function (e) { toast(e.message, true); });
}

// ─── Onebot 桥 ────────────────────────────────────────────────────────────────
var obTab = "account";
var obScopes = [];
var obRelations = null;
var obPrompts = null;
var obPromptSel = "";
var obPromptContent = "";

function napcatFormHtml(n) {
  return '<div class=form-grid>' +
    '<label class=field><span>连接方式</span><select id=napcatMode>' +
    '<option value=server' + (n.mode === "server" ? " selected" : "") + '>等待 NapCat 连接（server）</option>' +
    '<option value=client' + (n.mode === "client" ? " selected" : "") + '>主动连接 NapCat（client）</option></select></label>' +
    '<label class=field><span>机器人 QQ（未知可填 0）</span><input type=number min=0 id=napcatSelf value="' + h(n.selfId || 0) + '"></label>' +
    '<label class="field wide"><span>WebSocket 地址</span><input type=text id=napcatWs value="' + h(n.wsUrl) + '" placeholder="ws://127.0.0.1:7631/openclaw-bind"></label>' +
    '<label class="field wide"><span>HTTP 地址</span><input type=text id=napcatHttp value="' + h(n.httpUrl) + '" placeholder="http://127.0.0.1:7632"></label>' +
    '<label class=field><span>监听端口（等待连接时填写）</span><input type=number min=0 max=65535 id=napcatPort value="' + h(n.reverseWsPort || 0) + '"></label>' +
    '<label class=field><span>访问 Token</span><input type=password id=napcatToken value="" autocomplete=off placeholder="' + (n.accessToken ? "已配置，留空保持不变" : "未设置，可按需填写") + '"></label>' +
    '<label class="field wide"><span>媒体访问地址（可选）</span><input type=text id=napcatPublic value="' + h(n.publicBaseUrl) + '" placeholder="http://服务器地址:7650"></label></div>' +
    '<div class=row><button class="btn primary" onclick="napcatSave()">' + icon("database") + '保存连接配置</button>' +
    '<span class=dim style="font-size:12px">Token 留空不会改变；保存后重启 deeplive-bot 生效</span></div>';
}

function renderNapcat(main) {
  main.innerHTML = pageHead("NapCat 连接配置", "设置 DeepLive 如何连接 NapCat，并查看当前 QQ 状态") +
    '<div id="napcatBody"><p class=dim>加载中…</p></div>';
  Promise.all([api("/api/status"), api("/api/napcat/config")]).then(function (results) {
    var s = results[0];
    var n = results[1];
    document.getElementById("napcatBody").innerHTML =
      '<div class="card"><div class="row" style="justify-content:space-between">' +
      '<div><div class=stat style="font-size:20px">' + (s.bot.connected ? '<span class="pill ok">QQ 已连接</span>' : '<span class="pill bad">QQ 未连接</span>') + '</div>' +
      '<p class=dim style="margin:6px 0 0">' + h(s.bot.nickname || "还没有获取到昵称") + " · " + h(s.bot.selfId || "未知 QQ") + '</p></div>' +
      '<div style="text-align:right"><p class=dim style="margin:0;font-size:12px">当前连接方式</p>' +
      '<p style="margin:4px 0 0;font-weight:600;color:#21406a">' + (s.bot.mode === "server" ? "NapCat 主动连入（server 模式）" : "主动连接 NapCat（client 模式）") + '</p></div></div></div>' +
      '<div class="card"><h2 style="margin-top:0">连接参数</h2><p class=help>按 NapCat 面板里的设置填写。保存前会自动备份，保存后需要重启 deeplive-bot 才会生效。</p>' +
      napcatFormHtml(n) + '</div>' +
      '<div class="card"><h2 style="margin-top:0">连接不上怎么办？</h2><p class=help style="margin-top:0">1. 确认 NapCat 已经启动并登录 QQ。<br>2. 确认 NapCat 的连接方式和这里一致，地址、端口也相同。<br>3. QQ 登录失效时，到 NapCat 面板重新扫码。</p>' +
      '<p class=dim style="font-size:12px;margin:0">改完参数记得先保存，再重启 deeplive-bot。</p></div>';
  }).catch(function (e) {
    document.getElementById("napcatBody").innerHTML = '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>";
  });
}
function renderOnebot(main) {
  main.innerHTML = pageHead("QQ 机器人", "查看连接状态、联系人、自动回复和 AI 人设") +
    '<div class="tabs">' +
    tabBtn("account", "连接状态") + tabBtn("relations", "联系人") + tabBtn("scopes", "自动回复") + tabBtn("persona", "AI 人设") +
    '</div><div id="obBody"><p class=dim>加载中…</p></div>';
  paintObTab(main);
}
function tabBtn(id, label) {
  return '<button class="' + (obTab === id ? "on" : "") + '" onclick="obGo(\\'' + id + '\\')">' + label + "</button>";
}
function obGo(tab) { obTab = tab; renderOnebot(document.getElementById("main")); }

function paintObTab(main) {
  var body = function () { return document.getElementById("obBody"); };
  if (obTab === "account") {
    api("/api/status").then(function (s) {
      body().innerHTML = '<div class="card"><div class="row" style="justify-content:space-between">' +
        '<div><div class=stat style="font-size:20px">' + (s.bot.connected ? '<span class="pill ok">QQ 已连接</span>' : '<span class="pill bad">QQ 未连接</span>') + '</div>' +
        '<p class=dim style="margin:6px 0 0">' + h(s.bot.nickname || "还没有获取到昵称") + " · " + h(s.bot.selfId || "未知 QQ") + '</p></div>' +
        '<div style="text-align:right"><p class=dim style="margin:0;font-size:12px">当前连接方式</p>' +
        '<p style="margin:4px 0 0;font-weight:600;color:#21406a">' + (s.bot.mode === "server" ? "NapCat 主动连入（server 模式）" : "主动连接 NapCat（client 模式）") + '</p></div></div>' +
        '<div class=row style="margin-top:16px"><a class="btn primary" href="#/napcat">' + icon("database") + '打开 NapCat 连接配置</a>' +
        '<span class=dim style="font-size:12px">修改地址、端口、Token 都在这里</span></div></div>' +
        '<div class="grid c2">' +
        '<div class="card"><h2 style="margin-top:0">机器人账号</h2><p class=help>NapCat 负责 QQ 登录，DeepLive 通过它收发消息。</p><table>' +
        "<tr><th>QQ</th><td class=mono>" + h(s.bot.selfId || "等待连接后获取") + "</td></tr>" +
        "<tr><th>昵称</th><td>" + h(s.bot.nickname) + "</td></tr>" +
        "<tr><th>运行时长</th><td>" + fmtUptime(s.uptimeSec) + "</td></tr></table></div>" +
        '<div class="card"><h2 style="margin-top:0">连接不上怎么办？</h2><p class=help style="margin-top:0">1. 确认 NapCat 已启动并登录 QQ。<br>2. 确认 NapCat 的连接方式与这里一致，地址和端口也相同。<br>3. QQ 登录失效时，到 NapCat 面板重新扫码。</p></div></div>';
    }).catch(function (e) { body().innerHTML = '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
    return;
  }
  if (obTab === "relations") {
    api("/api/relations").then(function (r) {
      obRelations = r;
      var html = '<div class="grid c3">' +
        '<div class="card"><div class="stat">' + r.stats.people + '</div><div class="stat-label">已识别人物</div></div>' +
        '<div class="card"><div class="stat">' + r.stats.facts + '</div><div class="stat-label">人物和话题记录</div></div>' +
        '<div class="card"><div class="stat">' + r.stats.scopes + '</div><div class="stat-label">会话档案</div></div></div>';
      html += '<div class="card"><h2 style="margin-top:0">认识的人</h2><p class=help>AI 从不同群聊和私聊中自动整理的人物印象。</p>';
      if (r.people.length === 0) html += '<p class=dim>还没有人物记录，开启「自动整理人物和话题」后会随对话积累。</p>';
      else {
        html += "<table><tr><th>人物</th><th>QQ</th><th>出现于</th><th>印象</th><th>记录</th><th>最近</th></tr>";
        for (var i = 0; i < Math.min(r.people.length, 100); i++) {
          var p = r.people[i];
          html += "<tr><td>" + h(p.canonicalName) + "</td><td class=mono>" + h(p.userId) + "</td>" +
            "<td>" + p.scopes.length + " 会话</td><td>" + h(p.impression.slice(0, 40)) + "</td><td>" + p.factCount + " 条</td>" +
            "<td class=mono>" + fmtTime(p.lastSeenAt) + "</td></tr>";
        }
        html += "</table>";
      }
      html += '</div><div class="card"><h2 style="margin-top:0">会话档案</h2>';
      if (r.scopes.length === 0) html += '<p class=dim>暂无</p>';
      else {
        html += "<table><tr><th>会话</th><th>成员</th><th>话题</th><th>印象</th><th>最近活跃</th></tr>";
        for (var j = 0; j < Math.min(r.scopes.length, 100); j++) {
          var sc = r.scopes[j];
          html += "<tr><td>" + h(sc.displayName || sc.scopeKey) + ' <span class=dim mono>' + h(sc.scopeKey) + "</span></td>" +
            "<td>" + sc.memberCount + "</td><td>" + sc.topicTags.slice(0, 4).map(function (t) { return '<span class=tag>' + h(t) + "</span>"; }).join("") + "</td>" +
            "<td>" + h(sc.impression.slice(0, 40)) + "</td><td class=mono>" + fmtTime(sc.lastActiveAt) + "</td></tr>";
        }
        html += "</table>";
      }
      html += "</div>";
      body().innerHTML = html;
    }).catch(function (e) { body().innerHTML = '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
    return;
  }
  if (obTab === "scopes") {
    api("/api/scopes").then(function (list) {
      obScopes = list;
      var html = '<div class="card"><h2 style="margin-top:0">自动回复（' + list.length + "）</h2>" +
        '<p class=help>开关决定 AI 是否回复该会话；回复概率 1 表示每条都处理，0 表示不主动处理。</p>';
      html += "<table><tr><th>群聊或私聊</th><th>自动回复</th><th>回复概率</th><th>触发词</th><th>专属人设</th></tr>";
      for (var i = 0; i < list.length; i++) {
        var a = list[i];
        html += "<tr><td>" + h(a.displayName || a.scopeId) + ' <span class=dim mono>' + h(a.scopeType) + ":" + h(a.scopeId) + "</span>" +
          (a.isMaster ? ' <span class="pill ok">主 AI</span>' : "") + "</td>" +
          '<td><label class=switch><input type=checkbox ' + (a.enabled ? "checked" : "") + (a.isMaster ? " disabled" : "") +
          ' onchange="obToggleScope(\\'' + h(a.scopeType) + "\\','" + h(a.scopeId) + '\\',this.checked)"><i></i></label></td>' +
          '<td><input type=number min=0 max=1 step=0.05 value="' + a.triggerRate + '" style="width:70px" onchange="obRate(\\'' + h(a.scopeType) + "\\','" + h(a.scopeId) + '\\',this.value)"></td>' +
          '<td class=mono style="max-width:220px">' + h((a.triggerWords || []).join("、")) + "</td>" +
          "<td>" + (a.hasPersona ? "✓" : '<span class=dim>—</span>') + "</td></tr>";
      }
      html += "</table></div>";
      body().innerHTML = html;
    }).catch(function (e) { body().innerHTML = '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
    return;
  }
  // persona
  api("/api/prompts").then(function (list) {
    obPrompts = list;
    if (!obPromptSel && list.length > 0) obPromptSel = list[0].path;
    var opts = "";
    for (var i = 0; i < list.length; i++) {
      opts += '<option value="' + h(list[i].path) + '"' + (list[i].path === obPromptSel ? " selected" : "") + ">" + h(list[i].path) + "（" + fmtBytes(list[i].size) + "）</option>";
    }
    body().innerHTML = '<div class="card"><div class="row" style="margin-bottom:10px">' +
      '<select id=obPromptPick class=grow onchange="obPickPrompt(this.value)">' + opts + "</select>" +
      '<button class="btn primary" onclick="obSavePrompt()">' + icon("database") + '保存</button></div>' +
      '<textarea id=obPromptText rows=18></textarea>' +
      '<p class=help style="margin:8px 0 0">人设决定 AI 的说话风格和身份，保存后立即生效。master.txt 是主 AI，staff 文件夹是专用 AI，domain 文件夹是各功能的主 AI。</p></div>';
    if (obPromptSel) obPickPrompt(obPromptSel);
  }).catch(function (e) { body().innerHTML = '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
}
function napcatSave() {
  api("/api/napcat/config", "POST", {
    mode: document.getElementById("napcatMode").value,
    wsUrl: document.getElementById("napcatWs").value.trim(),
    httpUrl: document.getElementById("napcatHttp").value.trim(),
    reverseWsPort: Number(document.getElementById("napcatPort").value),
    publicBaseUrl: document.getElementById("napcatPublic").value.trim(),
    selfId: Number(document.getElementById("napcatSelf").value),
    accessToken: document.getElementById("napcatToken").value,
  }).then(function () { toast("连接配置已保存，重启 deeplive-bot 后生效"); })
    .catch(function (e) { toast(e.message, true); });
}
function obToggleScope(type, id, enabled) {
  api("/api/scope", "POST", { scopeType: type, scopeId: id, enabled: enabled })
    .then(function () { toast(enabled ? "已启用" : "已停用"); })
    .catch(function (e) { toast(e.message, true); paintObTab(document.getElementById("main")); });
}
function obRate(type, id, value) {
  api("/api/scope", "POST", { scopeType: type, scopeId: id, triggerRate: Number(value) })
    .then(function () { toast("回复概率已更新"); })
    .catch(function (e) { toast(e.message, true); });
}
function obPickPrompt(path) {
  obPromptSel = path;
  api("/api/prompt?path=" + encodeURIComponent(path)).then(function (r) {
    var ta = document.getElementById("obPromptText");
    if (ta) { ta.value = r.content; obPromptContent = r.content; }
  });
}
function obSavePrompt() {
  var ta = document.getElementById("obPromptText");
  if (!ta || !obPromptSel) return;
  api("/api/prompt", "POST", { path: obPromptSel, content: ta.value })
    .then(function () { toast("已保存并生效"); })
    .catch(function (e) { toast(e.message, true); });
}

// ─── Minecraft 桥 ─────────────────────────────────────────────────────────────
function renderMc(main) {
  main.innerHTML = pageHead("Minecraft 陪玩", "查看游戏服务、登录账号和游戏 AI");
  Promise.all([api("/api/mc/status"), api("/api/mc/subagents")]).then(function (res) {
    var st = res[0];
    var subs = res[1].subagents;
    var sidecar = st.sidecar || {};
    var acc = st.account || {};
    var html = '<div class="grid c2">' +
      '<div class="card"><h2 style="margin-top:0">游戏服务状态</h2><p class=help>游戏服务负责进入服务器、移动和执行游戏操作。</p>';
    if (sidecar.ok) {
      html += "<table>";
      var statusNames = { message: "提示", username: "游戏名", host: "服务器地址", port: "端口", online: "在线状态", version: "游戏版本", health: "生命值", food: "饥饿值", position: "位置", dimension: "所在维度", players: "在线玩家" };
      for (var key in sidecar) {
        if (key === "ok" || key === "status") continue;
        html += "<tr><th>" + h(statusNames[key.toLowerCase()] || key) + "</th><td class=mono>" + h(typeof sidecar[key] === "object" ? JSON.stringify(sidecar[key]) : sidecar[key]) + "</td></tr>";
      }
      html += "</table>";
    } else {
      html += '<p style="color:var(--bad)">游戏服务未连接' + (sidecar.error ? "：" + h(sidecar.error) : "") + '</p><p class=dim style="font-size:13px">请检查 deeplive-mcbot 服务是否已启动。未连接时，AI 暂时无法进入或操作游戏。</p>';
    }
    html += "</div>";

    html += '<div class="card"><h2 style="margin-top:0">游戏账号</h2>' +
      '<label class=field><span>账号类型</span><select id=mcAuth>' +
      '<option value=offline' + (acc.auth !== "microsoft" ? " selected" : "") + '>离线（盗版服）</option>' +
      '<option value=microsoft' + (acc.auth === "microsoft" ? " selected" : "") + '>微软正版</option></select></label>' +
      '<label class=field><span>用户名</span><input type=text id=mcUser value="' + h(acc.username || "") + '"></label>' +
      '<label class=field><span>密码 / 凭据' + (acc.password ? "（已配置，留空 = 不改动）" : "") + '</span><input type=password id=mcPass value="" placeholder="' + h(acc.password || "") + '"></label>' +
      '<button class="btn primary" onclick="mcSaveAccount()">' + icon("database") + '保存账号</button>' +
      '<p class=dim style="font-size:12px;margin:8px 0 0">保存后写入游戏账号配置，<b>重启 deeplive-mcbot 后生效</b>。</p></div></div>';

    html += '<div class="card"><h2 style="margin-top:0">游戏 AI（' + subs.length + "）</h2>" +
      '<p class=help>每个游戏 AI 有独立的对话记录和人设。</p>';
    if (subs.length === 0) html += '<p class=dim>还没有游戏 AI，由主 AI 分配游戏角色后会显示在这里。</p>';
    else {
      html += "<table><tr><th>对话标识</th><th>名称</th><th>状态</th><th>人设摘要</th><th>最近总结</th></tr>";
      for (var i = 0; i < subs.length; i++) {
        var sub = subs[i];
        html += "<tr><td class=mono>" + h(sub.scopeKey) + "</td><td>" + h(sub.displayName) + "</td>" +
          "<td>" + (sub.enabled ? '<span class="pill ok">开</span>' : '<span class="pill mute">关</span>') + "</td>" +
          "<td>" + h(sub.persona.slice(0, 60)) + "</td><td class=mono>" + fmtTime(sub.summaryBuiltAt) + "</td></tr>";
      }
      html += "</table>";
    }
    html += "</div>";
    main.innerHTML = pageHead("Minecraft 陪玩", "查看游戏服务、登录账号和游戏 AI") + html;
  }).catch(function (e) { main.innerHTML = pageHead("Minecraft 陪玩", "") + '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
}
function mcSaveAccount() {
  var body = {
    auth: document.getElementById("mcAuth").value,
    username: document.getElementById("mcUser").value.trim(),
    password: document.getElementById("mcPass").value,
  };
  if (!body.password) { toast("密码留空会覆盖成空——请输入完整密码再保存", true); return; }
  api("/api/mc/account", "POST", body)
    .then(function () { toast("游戏账号已保存，重启 deeplive-mcbot 后生效"); })
    .catch(function (e) { toast(e.message, true); });
}

// ─── Bili 桥 ──────────────────────────────────────────────────────────────────
function renderBili(main) {
  main.innerHTML = pageHead("哔哩哔哩", "查看 B 站 AI 和各视频的处理状态");
  api("/api/bili/stats").then(function (r) {
    var html = '<div class="grid c3">' +
      '<div class="card"><div class="stat">' + (r.hubEnabled ? '<span class="pill ok">运行中</span>' : '<span class="pill mute">已停用</span>') + '</div><div class="stat-label">B 站主 AI</div></div>' +
      '<div class="card"><div class="stat">' + r.videoScopeCount + '</div><div class="stat-label">视频 AI</div></div>' +
      '<div class="card"><div class="stat">' + (r.usage.total.requests || 0) + '</div><div class="stat-label">模型请求（本次启动）</div></div></div>';
    html += '<div class="card"><h2 style="margin-top:0">视频 AI</h2><p class=help>每个视频使用独立对话记录，方便分别讨论不同视频。</p>';
    if (r.videoScopes.length === 0) html += '<p class=dim>还没有视频 AI，由 B 站主 AI 分配视频处理角色后会显示在这里。</p>';
    else {
      html += "<table><tr><th>BV 号</th><th>名称</th><th>启用</th><th>总结于</th></tr>";
      for (var i = 0; i < r.videoScopes.length; i++) {
        var v = r.videoScopes[i];
        html += "<tr><td class=mono>" + h(v.bvid) + "</td><td>" + h(v.displayName || "—") + "</td>" +
          "<td>" + (v.enabled ? '<span class="pill ok">开</span>' : '<span class="pill mute">关</span>') + "</td>" +
          "<td class=mono>" + fmtTime(v.summaryBuiltAt) + "</td></tr>";
      }
      html += "</table>";
    }
    html += "</div>";
    main.innerHTML = pageHead("哔哩哔哩", "查看 B 站 AI 和各视频的处理状态") + html;
  }).catch(function (e) { main.innerHTML = pageHead("哔哩哔哩", "") + '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
}

// ─── 通用工具插件页（小说/天气等无专属页面的插件） ────────────────────────────
var tryState = { plugin: "", tools: [] };

function renderToolPlugin(main, name) {
  var title = (PLUGIN_PAGES[name] && PLUGIN_PAGES[name].title) || PLUGIN_TITLES[name] || name;
  main.innerHTML = pageHead(title, "工具插件详情、服务状态和试玩");
  api("/api/plugins/" + name + "/detail").then(function (d) {
    tryState = { plugin: name, tools: d.tools || [] };
    var html = '<div class="card"><div class="row" style="justify-content:space-between">' +
      '<div><div class=stat style="font-size:20px">' + (d.info.enabled ? '<span class="pill ok">已启用</span>' : '<span class="pill mute">已停用</span>') +
      (d.version ? ' <span class="pill mute">v' + h(d.version) + '</span>' : '') + '</div>' +
      '<p class=dim style="margin:6px 0 0">' + h(d.info.description || "这个插件没有描述") + '</p></div>' +
      '<label class=switch title="启用/停用"><input type=checkbox ' + (d.info.enabled ? "checked" : "") + ' onchange="pluginToggle(\\'' + h(name) + '\\',this.checked)"><i></i></label></div>' +
      '<p class=dim style="font-size:12px;margin:10px 0 0">停用后 AI 不再看到这个插件的工具，已有对话数据不受影响。</p></div>';

    var configSpec = d.config && d.config.spec ? d.config.spec : {};
    var configValues = d.config && d.config.values ? d.config.values : {};
    var configKeys = Object.keys(configSpec);
    if (configKeys.length > 0) {
      html += '<div class="card"><h2 style="margin-top:0">插件配置</h2>' +
        '<p class=help>manifest 声明的设置项，请求模板里用 {{config.键}} 引用。保存后立即生效。</p><div class=form-grid>';
      for (var ci = 0; ci < configKeys.length; ci++) {
        var ck = configKeys[ci];
        var cf = configSpec[ck];
        var cv = configValues[ck];
        if (cf.type === "boolean") {
          html += '<label class=field><span>' + h(cf.title || ck) + '</span><select data-config-key="' + h(ck) + '">' +
            '<option value="1"' + (cv === true ? " selected" : "") + '>开启</option>' +
            '<option value="0"' + (cv !== true ? " selected" : "") + '>关闭</option></select></label>';
        } else {
          html += '<label class=field><span>' + h(cf.title || ck) + (cf.description ? ' <span class=dim style="font-size:11px">' + h(cf.description) + '</span>' : '') + '</span>' +
            '<input type=text data-config-key="' + h(ck) + '" value="' + h(cv === undefined ? (cf.default !== undefined ? String(cf.default) : "") : String(cv)) + '"></label>';
        }
      }
      html += '</div><div class=row style="margin-top:10px"><button class="btn primary" onclick="pluginConfigSave(\\'' + h(name) + '\\')">' + icon("database") + '保存配置</button></div></div>';
    }

    if (d.service) {
      html += '<div class="card"><h2 style="margin-top:0">服务状态</h2><table>' +
        '<tr><th>服务地址</th><td class=mono>' + h(d.service.url) + '</td></tr>' +
        '<tr><th>可达性</th><td>' + (d.service.ok
          ? '<span class="pill ok">在线</span> <span class=dim style="font-size:12px">HTTP ' + h(d.service.status) + ' · ' + d.service.ms + 'ms</span>'
          : '<span class="pill bad">不可达</span> <span class=dim style="font-size:12px">' + h(d.service.error || "") + '</span>') + '</td></tr></table></div>';
    }

    if (d.extras && d.extras.commandScopes) {
      html += '<div class="card"><h2 style="margin-top:0">指令模式会话</h2>' +
        '<p class=help>这些会话不经过模型，搜书/看章指令直接应答。独立小说 bot（deeplive-novel）接管后这里可以留空。</p>' +
        (d.extras.commandScopes.length === 0
          ? '<p class=dim>没有配置指令模式会话' + (d.extras.commandBaseUrl ? '，指令服务：' + h(d.extras.commandBaseUrl) : "") + '。</p>'
          : '<table><tr><th>会话标识</th></tr>' + d.extras.commandScopes.map(function (s) { return '<tr><td class=mono>' + h(s) + '</td></tr>'; }).join("") + '</table>') +
        '</div>';
    }

    html += '<div class="card"><h2 style="margin-top:0">工具清单（' + d.tools.length + '）</h2>' +
      '<p class=help>启用时 AI 在对话中可以调用这些工具。</p><table><tr><th>工具</th><th>说明</th><th>参数</th></tr>';
    for (var i = 0; i < d.tools.length; i++) {
      var t = d.tools[i];
      var params = t.params.map(function (p) { return p.key + (p.required ? "*" : ""); }).join("、");
      html += '<tr><td class=mono style="font-size:12px">' + h(t.name) + '</td>' +
        '<td style="font-size:13px">' + h(t.description) + '</td>' +
        '<td class=dim style="font-size:12px">' + h(params || "无") + '</td></tr>';
    }
    html += '</table><p class=dim style="font-size:12px;margin:8px 0 0">* = 必填参数</p></div>';

    var tryable = d.tools.filter(function (t) { return t.tryable; });
    if (tryable.length > 0) {
      html += '<div class="card"><h2 style="margin-top:0">试玩</h2>' +
        '<p class=help>以真实请求直接调用工具，返回原始响应。带密钥或会写文件的工具不开放试玩。</p>' +
        '<label class=field><span>选择工具</span><select id=tryToolSel onchange="tryPaintParams()">';
      for (var k = 0; k < tryable.length; k++) html += '<option value="' + h(tryable[k].name) + '">' + h(tryable[k].name) + '</option>';
      html += '</select></label><div id=tryParams class=form-grid></div>' +
        '<div class=row style="margin-top:10px"><button class="btn primary" onclick="tryRun()">' + icon("send") + '运行</button></div>' +
        '<pre id=tryResult class=mono style="display:none;white-space:pre-wrap;word-break:break-all;max-height:320px;overflow:auto;background:rgba(20,40,70,.06);border-radius:10px;padding:12px;font-size:12px;margin:12px 0 0"></pre></div>';
    }

    main.innerHTML = pageHead(title, "工具插件详情、服务状态和试玩") + html;
    tryPaintParams();
  }).catch(function (e) { main.innerHTML = pageHead(title, "") + '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
}

function tryPaintParams() {
  var sel = document.getElementById("tryToolSel");
  var box = document.getElementById("tryParams");
  if (!sel || !box) return;
  var tool = null;
  for (var i = 0; i < tryState.tools.length; i++) if (tryState.tools[i].name === sel.value) tool = tryState.tools[i];
  if (!tool) { box.innerHTML = ""; return; }
  if (tool.params.length === 0) { box.innerHTML = '<p class=dim style="font-size:13px">这个工具没有参数。</p>'; return; }
  var html = "";
  for (var j = 0; j < tool.params.length; j++) {
    var p = tool.params[j];
    html += '<label class=field><span>' + h(p.key) + (p.required ? "（必填）" : "（可选）") + '</span>' +
      '<input type=text data-try-param="' + h(p.key) + '" placeholder="' + h(p.description || p.type) + '"></label>';
  }
  box.innerHTML = html;
}

function pluginConfigSave(name) {
  var values = {};
  var inputs = document.querySelectorAll("[data-config-key]");
  for (var i = 0; i < inputs.length; i++) values[inputs[i].getAttribute("data-config-key")] = inputs[i].value;
  api("/api/plugins/" + name + "/config", "POST", { values: values })
    .then(function () { toast("配置已保存并生效"); })
    .catch(function (e) { toast(e.message, true); });
}

function tryRun() {
  var sel = document.getElementById("tryToolSel");
  var out = document.getElementById("tryResult");
  if (!sel || !out) return;
  var args = {};
  var inputs = document.querySelectorAll("[data-try-param]");
  for (var i = 0; i < inputs.length; i++) {
    var v = inputs[i].value.trim();
    if (v) args[inputs[i].getAttribute("data-try-param")] = v;
  }
  out.style.display = "block";
  out.textContent = "请求中…";
  api("/api/plugins/" + tryState.plugin + "/try", "POST", { tool: sel.value, args: args }).then(function (r) {
    out.textContent = "HTTP " + (r.status || "-") + " · " + (r.ms || 0) + "ms\\n\\n" + (r.body || r.error || "");
  }).catch(function (e) {
    out.textContent = "调用失败：" + e.message;
  });
}

// ─── 性能探针页（统一计时打点：慢调用 TOP 榜 + 最近事件） ────────────────────
function renderProbes(main) {
  main.innerHTML = pageHead("性能", "框架探针：慢调用聚合与事件现场");
  api("/api/probes").then(function (p) {
    var agg = p.aggregates || [];
    var html = '<div class="card"><h2 style="margin-top:0">慢调用 TOP（按最差一次排序）</h2>' +
      '<p class=help>计时点由框架自动埋在关键路径（对话轮次、插件工具调用）。' + p.slowThresholdMs + 'ms 以上记为慢事件。</p>';
    if (agg.length === 0) html += '<p class=dim>还没有打点数据——产生一次对话或插件调用后回来刷新。</p>';
    else {
      html += '<table><tr><th>计时点</th><th>次数</th><th>平均</th><th>最差</th><th>累计</th><th>最近</th></tr>';
      for (var i = 0; i < agg.length; i++) {
        var a = agg[i];
        html += '<tr><td class=mono style="font-size:12px">' + h(a.name) + '</td><td>' + a.count + '</td>' +
          '<td class=mono>' + a.avgMs + 'ms</td><td class=mono>' + (a.maxMs >= p.slowThresholdMs ? '<span style="color:var(--bad)">' + a.maxMs + 'ms</span>' : a.maxMs + 'ms') + '</td>' +
          '<td class=mono>' + (a.totalMs / 1000).toFixed(1) + 's</td><td class=dim style="font-size:12px">' + fmtTime(Math.floor(a.lastAt / 1000)) + '</td></tr>';
      }
      html += '</table>';
    }
    html += '</div>';

    html += '<div class="card"><h2 style="margin-top:0">慢事件现场（最近 ' + (p.slow || []).length + ' 条）</h2>';
    if (!(p.slow || []).length) html += '<p class=dim>暂无慢事件。</p>';
    else {
      html += '<table><tr><th>时间</th><th>计时点</th><th>耗时</th><th>现场</th></tr>';
      for (var j = 0; j < p.slow.length; j++) {
        var s = p.slow[j];
        var meta = "";
        for (var k in (s.meta || {})) meta += h(k) + "=" + h(String(s.meta[k])) + " ";
        html += '<tr><td class=dim style="font-size:12px">' + fmtTime(Math.floor(s.at / 1000)) + '</td><td class=mono style="font-size:12px">' + h(s.name) + '</td>' +
          '<td class=mono>' + s.ms + 'ms</td><td class=dim style="font-size:12px">' + meta + '</td></tr>';
      }
      html += '</table>';
    }
    html += '</div><div class=row><button class="btn" onclick="probesReset()">' + icon("refresh") + '清空重计</button></div>';
    main.innerHTML = pageHead("性能", "框架探针：慢调用聚合与事件现场") + html;
  }).catch(function (e) { main.innerHTML = pageHead("性能", "") + '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
}
function probesReset() {
  api("/api/probes/reset", "POST", {}).then(function () { renderProbes(document.getElementById("main")); toast("已清空"); });
}

// ─── 设置 ────────────────────────────────────────────────────────────────────
var settingsData = null;

function renderSettings(main) {
  main.innerHTML = pageHead("设置", "管理自动回复、工具插件、模型和本地数据");
  api("/api/settings").then(function (s) {
    settingsData = s;
    var llm = s.llm;
    var allOn = llm.scopesTotal > 1 && llm.scopesEnabled >= llm.scopesTotal - 1;
    var html = '<div class="card"><h2 style="margin-top:0">AI 总开关</h2><p class=help>关闭某项后只会停止对应功能，不会删除已有数据。</p>' +
      '<div class="row" style="margin-bottom:8px"><span class=grow>所有会话自动回复（已启用 ' + llm.scopesEnabled + " / " + llm.scopesTotal + '）</span>' +
      '<label class=switch><input type=checkbox id=swLlm ' + (allOn ? "checked" : "") + "><i></i></label></div>" +
      '<div class="row" style="margin-bottom:8px"><span class=grow>后台整理对话' +
      (llm.summaryEnabled !== llm.persisted.summaryEnabled ? ' <span class="pill warn">尚未永久保存</span>' : "") + "</span>" +
      '<label class=switch><input type=checkbox id=swSummary ' + (llm.summaryEnabled ? "checked" : "") + "><i></i></label></div>" +
      '<div class="row" style="margin-bottom:10px"><span class=grow>自动整理人物和话题' +
      (llm.intelEnabled !== llm.persisted.intelEnabled ? ' <span class="pill warn">尚未永久保存</span>' : "") + "</span>" +
      '<label class=switch><input type=checkbox id=swIntel ' + (llm.intelEnabled ? "checked" : "") + "><i></i></label></div>" +
      '<div class="row"><label style="font-size:13px"><input type=checkbox id=swPersist> 永久保存（重启后仍保留）</label>' +
      '<button class="btn primary" onclick="settingsApply()">' + icon("activity") + '应用设置</button></div>' +
      '<p class=dim style="font-size:12px;margin:8px 0 0">主人的 QQ 私聊始终保持启用，避免无法联系主 AI。</p>' +
      '<div class="row" style="margin-top:10px;border-top:1px solid var(--line);padding-top:12px"><button class="btn" onclick="guideReplay()">' + icon("spark") + '重看新手引导</button>' +
      '<span class=dim style="font-size:12px">三步检查：QQ 连接、模型渠道、自动回复范围</span></div></div>';

    html += '<div class="card"><h2 style="margin-top:0">工具插件</h2><p class=help>插件为 AI 提供查天气、搜小说等额外能力，停用后不影响其他功能。</p>';
    if (s.plugins.length === 0) html += '<p class=dim>还没有安装工具插件。</p>';
    else {
      html += "<table><tr><th>插件</th><th>工具</th><th>启用</th><th>状态</th></tr>";
      for (var i = 0; i < s.plugins.length; i++) {
        var p = s.plugins[i];
        var pluginNames = { bili: "B 站视频工具", mc: "Minecraft 游戏工具", novel: "小说工具", weather: "天气工具" };
        var pluginHelp = {
          bili: "搜索视频、查看视频信息，并把视频发给用户。",
          mc: "让 AI 进入 Minecraft 服务器，移动、聊天、挖掘和放置方块。",
          novel: "搜索番茄小说、查看目录并阅读章节。",
          weather: "查询指定城市的天气，无需申请密钥。",
        };
        html += "<tr><td>" + h(pluginNames[p.name] || p.name) + '<div class=dim style="font-size:12px">' + h(pluginHelp[p.name] || p.description) + "</div></td>" +
          '<td class=mono style="font-size:12px;white-space:nowrap">' + p.tools.length + " 个</td>" +
          '<td><label class=switch><input type=checkbox ' + (p.enabled ? "checked" : "") + ' onchange="pluginToggle(\\'' + h(p.name) + '\\',this.checked)"><i></i></label></td>' +
          "<td>" + (p.error ? '<span class="pill bad">' + h(p.error) + "</span>" : p.enabled ? '<span class="pill ok">正常</span>' : '<span class="pill mute">已停用</span>') + "</td></tr>";
      }
      html += "</table>";
    }
    html += "</div>";

    html += '<div class="card"><h2 style="margin-top:0">模型设置（高级）</h2><p class=help>用于更换模型渠道、地址和密钥；不确定时请勿修改。</p>' +
      '<button class="btn" onclick="modelsEdit()">' + icon("cpu") + '编辑模型配置</button> <span class=dim style="font-size:12px">保存后立即生效，密钥会隐藏显示</span>' +
      '<div id=modelsEditor style="display:none;margin-top:10px"><textarea id=modelsText rows=16></textarea>' +
      '<div class="row" style="margin-top:8px"><button class="btn primary" onclick="modelsSave()">保存并重载</button>' +
      '<button class="btn" onclick="document.getElementById(\\'modelsEditor\\').style.display=\\'none\\'">收起</button></div></div></div>';

    html += '<div class="card"><div class="row" style="margin-bottom:6px"><h2 style="margin:0" class=grow>数据清理（谨慎操作）</h2>' +
      '<button class="btn small" onclick="loadDataDirs()">' + icon("refresh") + '刷新</button></div>' +
      '<p class=help>这里显示缓存和本地数据占用。清空后无法恢复，操作前请确认内容不再需要。</p><div id=dataDirs><p class=dim>扫描中…</p></div></div>';

    main.innerHTML = pageHead("设置", "管理自动回复、工具插件、模型和本地数据") + html;
    loadDataDirs();
  }).catch(function (e) { main.innerHTML = pageHead("设置", "") + '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
}

function settingsApply() {
  var body = {
    scopesEnabled: document.getElementById("swLlm").checked,
    summaryEnabled: document.getElementById("swSummary").checked,
    intelEnabled: document.getElementById("swIntel").checked,
    persist: document.getElementById("swPersist").checked,
  };
  api("/api/settings/llm", "POST", body)
    .then(function (r) {
      toast("已应用（会话开关变更 " + r.changedScopes + " 个）" + (body.persist ? "，并已落盘 config.json" : ""));
      renderSettings(document.getElementById("main"));
    })
    .catch(function (e) { toast(e.message, true); });
}
function pluginToggle(name, enabled) {
  api("/api/plugins/toggle", "POST", { name: name, enabled: enabled })
    .then(function () { toast(name + (enabled ? " 已启用" : " 已停用")); loadPlugins().then(route); })
    .catch(function (e) { toast(e.message, true); route(); });
}
function modelsEdit() {
  api("/api/models/config").then(function (r) {
    document.getElementById("modelsText").value = r.content;
    document.getElementById("modelsEditor").style.display = "block";
  }).catch(function (e) { toast(e.message, true); });
}
function modelsSave() {
  api("/api/models/config", "POST", { content: document.getElementById("modelsText").value })
    .then(function () { toast("已保存并热重载"); })
    .catch(function (e) { toast(e.message, true); });
}
function loadDataDirs() {
  var box = document.getElementById("dataDirs");
  if (!box) return;
  api("/api/data").then(function (r) {
    var html = "<table><tr><th>目录</th><th>大小</th><th>文件数</th><th></th></tr>";
    for (var i = 0; i < r.dirs.length; i++) {
      var d = r.dirs[i];
      html += "<tr><td>" + h(d.name) + ' <span class=dim mono style="font-size:11px">' + h(d.path) + "</span></td>" +
        "<td>" + fmtBytes(d.bytes) + "</td><td>" + d.files + "</td><td>" +
        (d.clearable ? '<button class="btn small danger" onclick="dataClear(\\'' + h(d.path) + '\\')">清空</button>' : '<span class=dim style="font-size:12px">不可清</span>') +
        "</td></tr>";
    }
    box.innerHTML = html + "</table>";
  }).catch(function (e) { box.innerHTML = '<p style="color:var(--bad)">' + h(e.message) + "</p>"; });
}
function dataClear(path) {
  var name = path.indexOf("workspace") !== -1 && path.indexOf("workspace-tts") === -1 ? "workspace"
    : path.slice(path.replace(/\\/$/, "").lastIndexOf("/") + 1);
  if (!confirm("清空 " + path + " 的全部内容？此操作不可恢复。")) return;
  api("/api/data/clear", "POST", { target: name })
    .then(function (r) { toast("已清空（" + r.removed + " 项）"); loadDataDirs(); })
    .catch(function (e) { toast(e.message, true); });
}

// ─── 检查更新 ────────────────────────────────────────────────────────────────
function renderUpdate(main) {
  main.innerHTML = pageHead("检查更新", "查看当前版本以及 GitHub 上是否有新版本");
  api("/api/update").then(function (u) {
    var html = '<div class="card"><p class=help>版本号使用 Git 提交标识；如果获取最新版本失败，可先检查服务器网络。</p><table>' +
      "<tr><th>面板版本</th><td class=mono>v" + h(u.version) + "</td></tr>" +
      "<tr><th>当前版本</th><td class=mono>" + h(u.local.commit) + "（" + h(u.local.branch) + "）" + (u.local.summary ? " — " + h(u.local.summary) : "") + "</td></tr>";
    if (u.error) {
      html += '<tr><th>最新版本</th><td style="color:var(--warn)">' + h(u.error) + "</td></tr>";
    } else if (u.remote) {
      html += "<tr><th>最新版本</th><td class=mono>" + h(u.remote.commit) + "</td></tr>" +
        "<tr><th>检查结果</th><td>" + (u.upToDate ? '<span class="pill ok">已是最新</span>'
          : '<span class="pill warn">有 ' + (u.behind == null ? "若干" : u.behind) + " 个新提交</span>") + "</td></tr>";
    }
    html += '</table><div class="row" style="margin-top:12px">' +
      '<button class="btn" onclick="route()">' + icon("refresh") + '重新检查</button></div>';
    if (u.upToDate === false) {
      html += '<p class=dim style="font-size:12px">高级操作：执行 git fetch && git merge（或 cherry-pick），再执行 npm run build && systemctl restart deeplive-bot。操作前建议先备份。</p>';
    }
    html += "</div>";
    main.innerHTML = pageHead("检查更新", "查看当前版本以及 GitHub 上是否有新版本 · 检查于 " + fmtTime(u.checkedAt / 1000)) + html;
  }).catch(function (e) { main.innerHTML = pageHead("检查更新", "") + '<div class="card" style="color:var(--bad)">' + h(e.message) + "</div>"; });
}

// ─── 启动 ────────────────────────────────────────────────────────────────────
function loadPlugins() {
  return api("/api/plugins").then(function (r) {
    state.plugins = r.plugins;
    state.pluginsLoaded = true;
    buildNav();
    markNav(state.route);
  }).catch(function () { /* 插件列表失败不阻塞导航 */ });
}

// ─── 新手向导 ────────────────────────────────────────────────────────────────
var guideStep = 0;

function guideStart() { guideStep = 0; document.getElementById("guide").classList.add("show"); guidePaint(); }
function guideSkip() { document.getElementById("guide").classList.remove("show"); }
function guideFinish() { localStorage.setItem("dl_guide_done", "1"); guideSkip(); toast("随时可以在「设置」里重看新手引导"); }
function guideReplay() { localStorage.removeItem("dl_guide_done"); guideStart(); }
function guideGo(route) { guideSkip(); location.hash = "#/" + route; }

function guidePaint() {
  var card = document.getElementById("guideCard");
  var dots = '<div class="guide-steps">';
  for (var d = 0; d < 3; d++) dots += "<i class=\\"" + (d === guideStep ? "on" : "") + "\\"></i>";
  dots += "</div>";
  var foot = '<div class="guide-actions"><button class="btn" onclick="guideFinish()">跳过向导</button>' +
    '<button class="btn primary" id="guideNextBtn">' + (guideStep === 2 ? "完成" : "下一步") + " " + icon("arrow") + "</button></div>";
  card.innerHTML = dots;
  if (guideStep === 0) {
    card.innerHTML = dots + '<div class="eyebrow">第 1 步 · 共 3 步</div><h2>连接 QQ</h2>' +
      '<p>DeepLive 通过 NapCat 收发 QQ 消息。先在电脑或手机上启动 NapCat 并登录 QQ，再把它连接到本面板。</p><p id=guideInfo>正在检查连接状态…</p>' + foot;
    api("/api/status").then(function (s) {
      var el = document.getElementById("guideInfo");
      if (!el) return;
      if (s.bot.connected) {
        el.innerHTML = '<span class="guide-ok">✓ QQ 已连接（' + h(s.bot.nickname || s.bot.selfId) + "）</span>，可以直接进入下一步。";
      } else {
        el.innerHTML = 'QQ 目前未连接。点击下方「去 NapCat 配置」查看连接地址，填进 NapCat 后再回来继续。';
        var next = document.getElementById("guideNextBtn");
        next.innerHTML = '去 NapCat 配置';
        next.onclick = function () { guideGo("napcat"); };
      }
    }).catch(function () { var el = document.getElementById("guideInfo"); if (el) el.textContent = "状态获取失败，可先跳过，稍后在首页查看。"; });
  } else if (guideStep === 1) {
    card.innerHTML = dots + '<div class="eyebrow">第 2 步 · 共 3 步</div><h2>检查模型渠道</h2>' +
      '<p>AI 回复依赖模型渠道。下方是当前配置，若为空或报错，请到「设置」页编辑模型配置。</p><p id=guideInfo>正在读取模型配置…</p>' + foot;
    api("/api/models").then(function (m) {
      var el = document.getElementById("guideInfo");
      if (!el) return;
      var firstLine = (m.text || "").split("\\n")[0] || "";
      el.innerHTML = firstLine ? '当前渠道：<span class="guide-ok">' + h(firstLine) + "</span>" : '还没有可用渠道，请到「设置」页配置模型。';
    }).catch(function () { var el = document.getElementById("guideInfo"); if (el) el.textContent = "模型配置读取失败，请到「设置」页检查。"; });
  } else {
    card.innerHTML = dots + '<div class="eyebrow">第 3 步 · 共 3 步</div><h2>选择自动回复范围</h2>' +
      '<p id=guideInfo>正在统计会话…</p>' + foot;
    api("/api/status").then(function (s) {
      var el = document.getElementById("guideInfo");
      if (!el) return;
      if (s.scopes.enabled > 0) {
        el.innerHTML = '已有 <span class="guide-ok">' + s.scopes.enabled + " 个会话</span>开启自动回复。可在「设置」或「QQ 机器人 → 自动回复」里随时调整。";
      } else {
        el.innerHTML = '目前没有会话开启自动回复。点击下方「去设置」批量开启，或在「QQ 机器人 → 自动回复」逐个选择。';
        var next = document.getElementById("guideNextBtn");
        next.innerHTML = '去设置';
        next.onclick = function () { guideGo("settings"); };
      }
    }).catch(function () { var el = document.getElementById("guideInfo"); if (el) el.textContent = "会话统计获取失败，可稍后在设置页调整。"; });
  }
  var next = document.getElementById("guideNextBtn");
  if (next && next.innerHTML.indexOf("去") !== 0) next.onclick = function () { if (guideStep >= 2) guideFinish(); else { guideStep += 1; guidePaint(); } };
}

function boot() {
  document.getElementById("app").style.display = "flex";
  hydrateIcons();
  buildNav();
  document.querySelector("aside").addEventListener("click", function (e) {
    if (e.target.closest("a")) closeDrawer();
  });
  loadPlugins().then(route);
  if (!localStorage.getItem("dl_guide_done")) setTimeout(guideStart, 600);
}

window.addEventListener("hashchange", route);
if (!TOKEN) showLogin();
else boot();
</script>
</body>
</html>
`;
