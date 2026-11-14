import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'

const root = path.resolve(import.meta.dirname, '..')
const configPath = process.env.MCBOT_CONFIG ?? path.join(root, 'config.json')
const config = existsSync(configPath) ? JSON.parse(readFileSync(configPath, 'utf8')) : {}
const checks = []
function check(name, ok) { checks.push({ name, ok }); console.log(`${ok ? 'OK' : 'MISSING'} ${name}`) }
check('Node >=22.16', Number(process.versions.node.split('.')[0]) > 22 || (Number(process.versions.node.split('.')[0]) === 22 && Number(process.versions.node.split('.')[1]) >= 16))
check('Mineflayer installed', existsSync(path.join(root, 'node_modules', 'mineflayer', 'package.json')))
if (process.argv.includes('--rust')) {
  check('Rust mc-native executable', existsSync(config.rustBinary ?? path.join(root, 'bin', process.platform === 'win32' ? 'mc-native.exe' : 'mc-native')))
  check('26.2 official assets', existsSync(path.join(config.rustAssets ?? path.join(root, 'runtime/minecraft-26.2-client'), 'versions', '26.2', '26.2.jar')))
  const facts = config.rustRuntimeData ?? path.join(root, 'runtime/runtime-data-26.2')
  const manifestPath = path.join(facts, 'manifest.json')
  let valid = false
  try {
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
    valid = manifest.format === 1 && manifest.version === '26.2' && manifest.protocol === 776
      && Object.keys(manifest.files).length === 36
      && Object.entries(manifest.files).every(([name, entry]) => {
        if (path.basename(name) !== name) return false
        const bytes = readFileSync(path.join(facts, name))
        return bytes.length === entry.size && createHash('sha1').update(bytes).digest('hex') === entry.sha1
      })
  } catch { /* Missing/invalid runtime facts must never look ready. */ }
  check('26.2 runtime manifest and all 36 file hashes', valid)
}
console.log(`Configuration: ${configPath} (credentials not displayed)`)
if (checks.some((entry) => !entry.ok)) process.exitCode = 1
