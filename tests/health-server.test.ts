import test from 'node:test';
import assert from 'node:assert/strict';
import { request } from 'node:http';
import { HealthServer } from '../src/api/health-server.js';

function get(port: number, path: string): Promise<{ statusCode: number; body: string }> {
  return new Promise((resolve, reject) => {
    const req = request({ hostname: '127.0.0.1', port, path, method: 'GET' }, (response) => {
      const chunks: Buffer[] = [];
      response.on('data', (chunk: Buffer) => chunks.push(chunk));
      response.on('end', () => resolve({
        statusCode: response.statusCode ?? 0,
        body: Buffer.concat(chunks).toString('utf8'),
      }));
    });
    req.on('error', reject);
    req.end();
  });
}

test('HealthServer exposes health, readiness and not-found endpoints', async () => {
  let ready = false;
  const server = new HealthServer({ host: '127.0.0.1', port: 0 }, { isReady: () => ready });
  await server.start();
  const address = server.address();
  assert.ok(address);

  const health = await get(address.port, '/health');
  assert.equal(health.statusCode, 200);
  assert.deepEqual(JSON.parse(health.body), { status: 'ok' });

  const starting = await get(address.port, '/ready');
  assert.equal(starting.statusCode, 503);
  assert.deepEqual(JSON.parse(starting.body), { status: 'starting' });

  ready = true;
  const readiness = await get(address.port, '/ready');
  assert.equal(readiness.statusCode, 200);
  assert.deepEqual(JSON.parse(readiness.body), { status: 'ready' });

  const missing = await get(address.port, '/missing');
  assert.equal(missing.statusCode, 404);

  await server.stop();
  await server.stop();
});
