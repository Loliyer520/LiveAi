import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, chmodSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const root = path.resolve(import.meta.dirname, '..')
const rust = path.join(root, 'rust')
const upstream = JSON.parse(readFileSync(path.join(rust, 'upstream.json'), 'utf8'))
const cache = path.join(root, '.cache')
const source = path.join(cache, 'rust-client')
const fingerprint = createHash('sha256').update(JSON.stringify(upstream))
  .update(readFileSync(path.join(rust, 'client.patch'))).update(readFileSync(path.join(rust, 'bot.rs'))).digest('hex')
const marker = path.join(source, '.deeplive-source')

function run(command, args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, stdio: 'inherit' })
    child.on('error', reject)
    child.on('close', (code) => code === 0 ? resolve() : reject(new Error(`${command} exited ${code}`)))
  })
}

try {
  mkdirSync(cache, { recursive: true })
  if (existsSync(source)) {
    if (!existsSync(marker) || readFileSync(marker, 'utf8').trim() !== fingerprint) {
      throw new Error(`Source cache does not match this revision. Move ${source} aside and retry; no files overwritten.`)
    }
  } else {
    const staging = path.join(cache, `rust-client-${process.pid}`)
    mkdirSync(staging)
    await run('git', ['init', '--quiet'], staging)
    await run('git', ['remote', 'add', 'origin', upstream.repository], staging)
    await run('git', ['fetch', '--depth=1', 'origin', upstream.revision], staging)
    await run('git', ['checkout', '--detach', 'FETCH_HEAD'], staging)
    await run('git', ['apply', '--check', path.join(rust, 'client.patch')], staging)
    await run('git', ['apply', path.join(rust, 'client.patch')], staging)
    for (const [file, target] of Object.entries(upstream.localFiles)) copyFileSync(path.join(rust, file), path.join(staging, target))
    writeFileSync(path.join(staging, '.deeplive-source'), `${fingerprint}\n`)
    renameSync(staging, source)
  }
  console.log(`Rust source prepared at ${source} (upstream ${upstream.revision})`)
  if (!process.argv.includes('--prepare-only')) {
    const check = process.argv.includes('--check')
    await run(process.env.CARGO ?? 'cargo', [check ? 'check' : 'build', '--locked', '--release', '-p', 'mc-client', '--bin', 'mc-native'], source)
    if (!check) {
      const target = process.env.CARGO_TARGET_DIR ? path.resolve(process.env.CARGO_TARGET_DIR) : path.join(source, 'target')
      const binary = process.platform === 'win32' ? 'mc-native.exe' : 'mc-native'
      const output = path.join(root, 'bin', binary)
      mkdirSync(path.dirname(output), { recursive: true })
      copyFileSync(path.join(target, 'release', binary), output)
      if (process.platform !== 'win32') chmodSync(output, 0o755)
      console.log(`Installed ${output}; assets and runtime facts are separate (see README).`)
    }
  }
} catch (error) {
  console.error(error.message)
  process.exitCode = 1
}
