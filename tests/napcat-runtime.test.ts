/** 本机 NapCat 运行环境：配置合并、systemd 模板、tail、部署前置校验。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import {
  NapcatRuntime,
  buildReverseWsClient,
  mergeOnebotConfig,
  renderSystemdUnit,
  tailLines,
  versionFromMarker,
} from '../src/napcat/runtime.js';

test('buildReverseWsClient: 固定一条指向 bot 的反向 WS 客户端', () => {
  const client = buildReverseWsClient('ws://127.0.0.1:7636', 'tok');
  assert.equal(client.name, 'deeplive');
  assert.equal(client.enable, true);
  assert.equal(client.url, 'ws://127.0.0.1:7636');
  assert.equal(client.token, 'tok');
  assert.equal(client.messagePostFormat, 'array');
  assert.equal(client.reconnectInterval, 5000);
});

test('mergeOnebotConfig: 保留用户配置，只替换同名客户端', () => {
  const existing = {
    network: {
      httpServers: [{ name: 'keep-me', enable: true, port: 3000 }],
      websocketClients: [
        { name: 'someone-else', url: 'ws://x', enable: true },
        { name: 'deeplive', url: 'ws://old', enable: false, token: 'old' },
      ],
    },
    musicSignUrl: 'https://sign.test',
  };
  const merged = mergeOnebotConfig(existing, 'ws://127.0.0.1:7636', 'new-token');
  const network = merged.network as Record<string, unknown>;
  const clients = network.websocketClients as Array<Record<string, unknown>>;
  assert.equal(clients.length, 2, '同名客户端被替换而不是追加');
  assert.equal(clients[0]!.name, 'someone-else');
  assert.equal(clients[1]!.url, 'ws://127.0.0.1:7636');
  assert.equal(clients[1]!.token, 'new-token');
  assert.equal(clients[1]!.enable, true);
  assert.equal((network.httpServers as unknown[]).length, 1, 'HTTP 服务端配置保留');
  assert.equal(merged.musicSignUrl, 'https://sign.test');
  assert.deepEqual(network.websocketServers, []);
});

test('mergeOnebotConfig: 空/畸形输入也能生成合法骨架', () => {
  const merged = mergeOnebotConfig(null, 'ws://127.0.0.1:7636', '');
  const network = merged.network as Record<string, unknown>;
  const clients = network.websocketClients as Array<Record<string, unknown>>;
  assert.equal(clients.length, 1);
  assert.equal(clients[0]!.name, 'deeplive');
  assert.equal(clients[0]!.token, '');
  assert.deepEqual(network.httpServers, []);
  assert.deepEqual(network.httpClients, []);
});

test('tailLines: 取末尾 N 行并忽略结尾空行', () => {
  assert.deepEqual(tailLines('a\nb\nc\n', 2), ['b', 'c']);
  assert.deepEqual(tailLines('', 3), []);
  assert.deepEqual(tailLines('one', 3), ['one']);
});

test('renderSystemdUnit: ExecStart 指向 launcher，输出落 napcat.log', () => {
  const unit = renderSystemdUnit('/my/run/napcat', 'deeplive-napcat');
  assert.ok(unit.includes('WorkingDirectory=/my/run/napcat'));
  assert.ok(unit.includes('Environment=HOME=/my/run/napcat'), 'HOME 未设置时 QQ 会 SIGILL');
  assert.ok(unit.includes('ExecStart=/my/run/napcat/launcher.sh'));
  assert.ok(unit.includes('Restart=always'));
  assert.ok(unit.includes('StandardOutput=append:/my/run/napcat/napcat.log'));
  assert.ok(unit.includes('WantedBy=multi-user.target'));
});

test('ensureConfig: 写入反向 WS 配置，webui token 只生成一次', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'napcat-runtime-'));
  try {
    const runtime = new NapcatRuntime({
      home: dir,
      serviceName: 'deeplive-napcat',
      projectRoot: '/my/run/bot',
      botPort: 7636,
      mode: 'server',
      accessToken: 'ws-token',
      isConnected: () => false,
      selfId: () => 0,
    });
    await runtime.ensureConfig();
    const configDir = join(dir, 'napcat', 'config');
    const onebot = JSON.parse(await readFile(join(configDir, 'onebot11.json'), 'utf-8')) as {
      network: { websocketClients: Array<Record<string, unknown>> };
    };
    const client = onebot.network.websocketClients.find((entry) => entry.name === 'deeplive');
    assert.ok(client, '包含 deeplive 客户端');
    assert.equal(client!.url, 'ws://127.0.0.1:7636');
    assert.equal(client!.token, 'ws-token');

    const webuiPath = join(configDir, 'webui.json');
    const webui = JSON.parse(await readFile(webuiPath, 'utf-8')) as { token: string; host: string };
    assert.equal(webui.host, '127.0.0.1');
    assert.ok(webui.token.length >= 16);

    // 再次部署不应重置用户改过的 WebUI 密码
    await writeFile(webuiPath, JSON.stringify({ host: '127.0.0.1', port: 6099, token: 'user-set-token', loginRate: 3 }), 'utf-8');
    await runtime.ensureConfig();
    const again = JSON.parse(await readFile(webuiPath, 'utf-8')) as { token: string };
    assert.equal(again.token, 'user-set-token');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('install: client 模式直接拒绝，不起安装进程', async () => {
  const runtime = new NapcatRuntime({
    home: '/tmp/does-not-matter',
    serviceName: 'deeplive-napcat',
    projectRoot: '/my/run/bot',
    botPort: 0,
    mode: 'client',
    accessToken: '',
    isConnected: () => false,
    selfId: () => 0,
  });
  const result = await runtime.install();
  assert.equal(result.ok, false);
  assert.ok(result.error?.includes('server 模式'));
});

test('versionFromMarker: 只接受版本号，拒绝日志/空内容', () => {
  assert.equal(versionFromMarker('4.18.41\n'), '4.18.41');
  assert.equal(versionFromMarker(' v4.18.41-beta.1 '), 'v4.18.41-beta.1');
  assert.equal(versionFromMarker(''), null);
  assert.equal(versionFromMarker('loading…'), null);
  assert.equal(versionFromMarker('4.18.41 部署完成'), null);
});

test('安装脚本通过 bash 语法检查', () => {
  const result = spawnSync('bash', ['-n', join(process.cwd(), 'scripts', 'napcat-install.sh')], { encoding: 'utf-8' });
  assert.equal(result.status, 0, result.stderr);
});
