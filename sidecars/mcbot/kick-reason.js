/**
 * 服务器踢人原因分类：决定「自动重连要不要继续」。
 *
 * - premium  正版验证 / 会话失效：离线登录无解，重试没用
 * - version  版本不匹配：重试没用，应改 version
 * - terminal 封禁 / 白名单 / 黑名单：重试没用
 * - transient 满员 / 重启 / 重复登录：服务器恢复后就能进，应重置退避
 * - unknown  按普通退避处理
 */
const PATTERNS = [
  ['premium', /(failed to verify username|verify username|invalid session|bad login|正版验证|premium account)/i],
  ['version', /(outdated (server|client)|incompatible version|版本不(兼容|匹配)|请(使用|更新到).{0,12}版本)/i],
  ['terminal', /(banned|ban\b|封禁|封号|永封|whitelist|白名单|not allowed|不允许|kicked by (an )?operator|管理员.{0,6}踢|ip.{0,8}ban|blacklist|黑名单|多账号|共享账号)/i],
  ['transient', /(server (is )?full|服务器.{0,6}满|满员|too many players|already logged in|已经登录|duplicate login|重复登录|restart|重启|timed out|连接超时)/i],
]

export function classifyKick(raw) {
  const text = String(raw ?? '')
  for (const [kind, pattern] of PATTERNS) {
    if (pattern.test(text)) return kind
  }
  return 'unknown'
}

export function isTerminalKick(kind) {
  return kind === 'premium' || kind === 'version' || kind === 'terminal'
}
