import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import type { LiveAiConfig } from '../config/config.js';
import type { LiveAiModule } from '../host/types.js';

export interface Readiness {
  isReady(): boolean;
}

export class HealthServer implements LiveAiModule {
  readonly name = 'health-server';
  private server: Server | undefined;

  constructor(
    private readonly config: Pick<LiveAiConfig, 'host' | 'port'>,
    private readonly readiness: Readiness,
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

  private handleRequest(request: IncomingMessage, response: ServerResponse): void {
    if (request.method !== 'GET') {
      response.writeHead(405, { Allow: 'GET' });
      response.end();
      return;
    }

    if (request.url === '/health') {
      this.sendJson(response, 200, { status: 'ok' });
      return;
    }

    if (request.url === '/ready') {
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
