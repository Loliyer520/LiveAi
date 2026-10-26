/** MC2D 联机客户端：世界生成确定性、PNG 编码、配置解析。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { inflateSync } from 'node:zlib';
import { encodePng, generateWorld, mulberry32 } from '../src/chat/mc2d.js';
import { loadConfig } from '../src/config/config.js';

const WORLD_W = 400;
const WORLD_H = 80;

test('mulberry32 确定且在 [0,1) 内', () => {
  const a = mulberry32(42);
  const b = mulberry32(42);
  for (let i = 0; i < 100; i++) {
    const value = a();
    assert.equal(value, b());
    assert.ok(value >= 0 && value < 1);
  }
  assert.notEqual(mulberry32(42)(), mulberry32(43)());
});

test('generateWorld 与服务端同算法：确定性、地形骨架、矿石与树', () => {
  const world = generateWorld(1600302395);
  assert.equal(world.length, WORLD_W * WORLD_H);
  assert.deepEqual(world, generateWorld(1600302395));
  assert.notDeepEqual(world, generateWorld(1600302396));

  // 每一列都有地表，且高度落在地形公式的合理区间
  for (const x of [0, 25, 100, 200, 399]) {
    let surface = -1;
    for (let y = 0; y < WORLD_H; y++) {
      if (world[y * WORLD_W + x] !== 0) {
        surface = y;
        break;
      }
    }
    assert.ok(surface > 15 && surface < 60, `列 ${x} 地表=${surface}`);
    assert.equal(world[surface * WORLD_W + x] === 1 || world[surface * WORLD_W + x] === 6, true);
  }

  // 树木（原木 4）、煤 9、钻石 12 都存在（世界生成的三个特征层）
  assert.ok(world.includes(4), '缺原木（树）');
  assert.ok(world.includes(9), '缺煤矿');
  assert.ok(world.includes(12), '缺钻石矿');
});

test('encodePng 输出合法 RGBA PNG（签名 / IHDR / IDAT 可解压）', () => {
  const width = 4;
  const height = 3;
  const rgba = new Uint8Array(width * height * 4);
  rgba.fill(128);
  const png = encodePng(width, height, rgba);

  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  // PNG 签名 8B + IHDR 长度 4B + 类型 4B → 数据从 16 开始
  assert.equal(png.readUInt32BE(16), width);
  assert.equal(png.readUInt32BE(20), height);
  assert.equal(png[24], 8); // bit depth
  assert.equal(png[25], 6); // color type RGBA

  let offset = 8;
  let idat = Buffer.alloc(0);
  while (offset < png.length) {
    const length = png.readUInt32BE(offset);
    const type = png.subarray(offset + 4, offset + 8).toString('latin1');
    if (type === 'IDAT') idat = Buffer.concat([idat, png.subarray(offset + 8, offset + 8 + length)]);
    offset += 12 + length;
  }
  const raw = inflateSync(idat);
  assert.equal(raw.length, (width * 4 + 1) * height);
  for (let y = 0; y < height; y++) assert.equal(raw[y * (width * 4 + 1)], 0); // filter=None
});

test('loadConfig 解析 mc2d 段（默认值 + env 覆盖 + uid 清洗）', () => {
  const defaults = loadConfig({ LIVEAI_CONFIG_PATH: 'data/defaults-do-not-exist.json' });
  assert.equal(defaults.mc2d.enabled, true);
  assert.equal(defaults.mc2d.wsUrl, 'ws://127.0.0.1:6601');
  assert.equal(defaults.mc2d.name, '冰糖');
  assert.equal(defaults.mc2d.uid, 'liveai-bot');
  assert.equal(defaults.mc2d.autoJoin, false);
  assert.equal(Object.isFrozen(defaults.mc2d), true);

  const custom = loadConfig({
    LIVEAI_CONFIG_PATH: 'data/defaults-do-not-exist.json',
    LIVEAI_MC2D_ENABLED: 'false',
    LIVEAI_MC2D_AUTO_JOIN: '1',
    LIVEAI_MC2D_WS_URL: 'ws://example.test:9999/mc',
    LIVEAI_MC2D_NAME: '一二三四五六七八九十十一十二十三',
    LIVEAI_MC2D_UID: 'Live AI! bot',
  });
  assert.equal(custom.mc2d.enabled, false);
  assert.equal(custom.mc2d.autoJoin, true);
  assert.equal(custom.mc2d.wsUrl, 'ws://example.test:9999/mc');
  assert.equal(custom.mc2d.name.length, 12);
  assert.equal(custom.mc2d.uid, 'LiveAIbot');
});
