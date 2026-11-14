/**
 * 进服适配器示例（开源模板，不含任何真实服务器/账号）。
 *
 * 用法：
 *   1. 把本文件复制到仓库之外（例如 /path/to/private/your-server.mjs）；
 *   2. 实现 prepareJoin，只在目标服务器上返回覆盖字段；
 *   3. 在 config.json 写 "joinHook": "/path/to/private/your-server.mjs"，
 *      或设环境变量 MCBOT_JOIN_HOOK=/path/to/your-server.mjs。
 *
 * 每次首次连接和自动重连都会调用一次；返回值只用于本次连接，不会写进档案。
 * 常见用途：一次性进服码、动态 token、内网跳板、按连接轮换身份。
 */

export default async function prepareJoin(ctx) {
  const { host, port, phase, profile, log } = ctx

  // 不匹配的服务器必须返回 null/{}，让核心走普通进服路径。
  if (host !== 'your.private.server' || Number(port) !== 25565) return null

  log(`${phase}：这里可以请求你们自己的认证服务`)

  // 示例：从自己的服务取本次连接要用的身份。
  // const res = await fetch('https://auth.example.com/join-code', { ... })
  // const { username, password } = await res.json()
  return {
    // username: 'abc12345',
    // password: 'optional',
    // version: '26.2',
    // backend: 'rust', // auto | rust | mineflayer
    note: '自定义进服适配示例（当前未启用具体逻辑）',
  }
}
