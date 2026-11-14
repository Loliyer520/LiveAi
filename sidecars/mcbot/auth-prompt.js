/**
 * AuthMe 类登录插件的提示识别。
 *
 * 单独成模块是为了可测试：server.js 一 import 就会起 HTTP 服务，没法单测。
 * 只做纯文本分类，不碰连接状态。
 *
 * 顺序有意义：成功 > 密码错 > 已注册 > 验证码 > 注册 > 登录。
 * 「已注册」必须排在「注册」前，否则 "该用户名已注册" 会被当成注册提示。
 */
const AUTH_PATTERNS = [
  ['success', /(登录|登陆)成功|成功(登录|登陆)|已(经)?登录(成功)?|successfully logged in|logged in successfully|welcome back|you are now logged in|身份验证成功/i],
  ['wrong-password', /密码(错误|不正确|不对|无效)|错误的密码|wrong password|incorrect password|invalid password|password (is )?incorrect|bad password/i],
  ['already-registered', /(已|已经|已被)注册|already registered|username.{0,20}already|该?(用户名|名字|名称).{0,6}(已被|已经|被).{0,4}注册|name.{0,10}already (in use|taken)/i],
  ['captcha', /验证码|captcha|邮箱验证|email (verification|verify)/i],
  ['register', /请先注册|请注册|尚未注册|还没有注册|未注册|需要注册|please register|register (with|using|at)|use \/(reg|register)\b|输入.{0,8}\/(reg|register)\b/i],
  ['login', /请先登录|请登录|尚未登录|未登录|需要登录|please log ?in|use \/login\b|输入.{0,8}\/login\b|log ?in (with|using)|\/login\s*</i],
  ['already-logged-in', /已经登录|已登录|already logged in/i],
]

/** 返回 success / wrong-password / already-registered / captcha / register / login / already-logged-in / ''。 */
export function classifyAuthPrompt(raw) {
  const text = String(raw ?? '').replace(/§./g, '')
  for (const [kind, pattern] of AUTH_PATTERNS) {
    if (pattern.test(text)) return kind
  }
  return ''
}

export const AUTH_PROMPT_KINDS = AUTH_PATTERNS.map(([kind]) => kind)
