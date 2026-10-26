import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { createReadStream } from 'node:fs';
import { extname } from 'node:path';
import type { LiveAiConfig } from '../config/config.js';
import type { LiveAiModule } from '../host/types.js';
import { handleWebui, type WebuiDeps } from './webui.js';
import { resolveSpoolFile } from '../chat/media-spool.js';

export interface Readiness {
  isReady(): boolean;
}

export class HealthServer implements LiveAiModule {
  readonly name = 'health-server';
  private server: Server | undefined;

  constructor(
    private readonly config: Pick<LiveAiConfig, 'host' | 'port'>,
    private readonly readiness: Readiness,
    /** 给了就托管管理面板（/ 与 /api/*）；不给则只有 /health /ready。 */
    private readonly webui?: WebuiDeps,
  ) {}

  async start(): Promise<void> {
    if (this.server) {
      return;
    }

    const server = createServer((request, response) => this.handleRequest(request, response));
    this.server = server;

    try {
      await new Promise<void>((resolve, reject) => {
        const onError = (error: Error) => {
          server.off('listening', onListening);
          reject(error);
        };
        const onListening = () => {
          server.off('error', onError);
          resolve();
        };
        server.once('error', onError);
        server.once('listening', onListening);
        server.listen(this.config.port, this.config.host);
      });
    } catch (error) {
      this.server = undefined;
      server.close();
      throw error;
    }
  }

  async stop(): Promise<void> {
    const server = this.server;
    if (!server) {
      return;
    }
    this.server = undefined;

    await new Promise<void>((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  }

  address(): { host: string; port: number } | undefined {
    const address = this.server?.address();
    if (!address || typeof address === 'string') {
      return undefined;
    }
    return { host: address.address, port: address.port };
  }

  private async serveMedia(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (request.method !== 'GET') {
      response.writeHead(405, { Allow: 'GET' });
      response.end();
      return;
    }
    // /media/<id>/<name>
    const parts = (request.url ?? '').split('?')[0].split('/').filter(Boolean);
    if (parts.length !== 3) {
      response.writeHead(404);
      response.end();
      return;
    }
    const file = await resolveSpoolFile(parts[1], decodeURIComponent(parts[2]));
    if (file === null) {
      response.writeHead(404);
      response.end();
      return;
    }
    const types: Record<string, string> = { '.mp4': 'video/mp4', '.flv': 'video/x-flv', '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.bin': 'application/octet-stream' };
    response.writeHead(200, { 'content-type': types[extname(file).toLowerCase()] ?? 'application/octet-stream' });
    createReadStream(file).pipe(response);
  }

  private handleRequest(request: IncomingMessage, response: ServerResponse): void {
    const pathname = (request.url ?? '').split('?', 1)[0];
    if (pathname === '/health' || pathname === '/ready') {
      if (request.method !== 'GET') {
        response.writeHead(405, { Allow: 'GET' });
        response.end();
        return;
      }
    }
    // 媒体外吐池能力路由：/media/<id>/<name>，uuid 截断 id 当鉴权，不带 token。
    // 给远端 NapCat 反向拉视频/语音文件用（resolveSpoolFile 内部白名单防穿越）。
    if (pathname.startsWith('/media/')) {
      void this.serveMedia(request, response).catch(() => {
        if (!response.headersSent) response.writeHead(500);
        response.end();
      });
      return;
    }

    // 管理面板（含 POST 写接口）走自己的处理器；/health /ready 保持裸奔给探针。
    if (this.webui && (pathname === '/' || pathname === '/index.html' || pathname.startsWith('/api/') || pathname.startsWith('/assets/'))) {
      void handleWebui(request, response, this.webui).catch(() => {
        if (!response.headersSent) response.writeHead(500);
        response.end();
      });
      return;
    }

    if (request.method !== 'GET') {
      response.writeHead(405, { Allow: 'GET' });
      response.end();
      return;
    }

    if (pathname === '/health') {
      this.sendJson(response, 200, { status: 'ok' });
      return;
    }

    if (pathname === '/ready') {
      const ready = this.readiness.isReady();
      this.sendJson(response, ready ? 200 : 503, { status: ready ? 'ready' : 'starting' });
      return;
    }

    response.writeHead(404);
    response.end();
  }

  private sendJson(response: ServerResponse, statusCode: number, payload: Record<string, string>): void {
    response.writeHead(statusCode, { 'content-type': 'application/json; charset=utf-8' });
    response.end(JSON.stringify(payload));
  }
}
