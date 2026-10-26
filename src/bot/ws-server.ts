/**
 * 零依赖 WebSocket 服务端（RFC6455 最小实现），专为 OneBot 反向 WS 接入：
 * NapCat 在 NAT 后面连不出来，由它主动连到本服务端（bot 是服务端、NapCat
 * 是客户端），事件推送与动作调用共用这条双工连接。
 *
 * 只实现需要的部分：文本帧（含 continuation 分片）、ping→pong、close；
 * 服务端发送不 mask，客户端帧按协议必须 mask（校验并解 mask）。
 */

import { createServer, type IncomingMessage, type Server as HttpServer } from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import type { Duplex } from 'node:stream';

const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

export interface WsServerConnection {
  readonly id: string;
  /** 升级请求的 URL（含 query，如 ?access_token=…）。 */
  readonly url: string;
  /** 已解出的 access_token（query 参数或 Authorization header），未带为 null。 */
  readonly token: string | null;
  send: (text: string) => void;
  close: (code?: number) => void;
  /** 主动触发的心跳探活：等 pong，超时/失败都会以 close 收场。 */
  ping: () => void;
}

export interface WsServerHandlers {
  /** token 校验；false 拒绝升级（401）。 */
  authorize?: (token: string | null, requestUrl: string) => boolean;
  onConnection: (conn: WsServerConnection) => void;
  onText: (conn: WsServerConnection, data: string) => void;
  onClose: (conn: WsServerConnection) => void;
}

interface Frame {
  opcode: number;
  payload: Buffer;
  final: boolean;
}

export class WsServer {
  private http: HttpServer | null = null;
  private boundPort: number | null = null;
  private readonly conns = new Map<string, Conn>();
  private readonly handlers: WsServerHandlers;

  /** 实际监听端口（port 0 = OS 分配，listen 后可查）；未启动为 null。 */
  get port(): number | null {
    return this.boundPort;
  }

  constructor(handlers: WsServerHandlers) {
    this.handlers = handlers;
  }

  get connectionCount(): number {
    return this.conns.size;
  }

  start(host: string, port: number): Promise<void> {
    const http = createServer((_req, res) => {
      res.writeHead(426);
      res.end();
    });
    http.on('upgrade', (req, socket) => this.handleUpgrade(req, socket));
    this.http = http;
    return new Promise((resolve, reject) => {
      http.once('error', reject);
      http.listen(port, host, () => {
        const addr = http.address();
        this.boundPort = typeof addr === 'object' && addr !== null ? addr.port : port;
        resolve();
      });
    });
  }

  async stop(): Promise<void> {
    for (const conn of this.conns.values()) conn.close(1001);
    this.conns.clear();
    const http = this.http;
    this.http = null;
    this.boundPort = null;
    if (http === null) return;
    return new Promise((resolve) => http.close(() => resolve()));
  }

  // ── handshake ─────────────────────────────────────────────────────────────

  private handleUpgrade(req: IncomingMessage, socket: Duplex): void {
    const key = String(req.headers['sec-websocket-key'] ?? '');
    if (!key || String(req.headers.upgrade ?? '').toLowerCase() !== 'websocket') {
      socket.destroy();
      return;
    }
    const url = req.url ?? '/';
    const token = extractToken(url, req.headers.authorization);
    if (this.handlers.authorize && !this.handlers.authorize(token, url)) {
      socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
      socket.destroy();
      return;
    }
    const accept = createHash('sha1').update(key + WS_GUID).digest('base64');
    socket.write(
      'HTTP/1.1 101 Switching Protocols\r\n' +
        'Upgrade: websocket\r\n' +
        'Connection: Upgrade\r\n' +
        `Sec-WebSocket-Accept: ${accept}\r\n\r\n`,
    );

    const conn = new Conn(socket, this.handlers, this, url, token);
    this.conns.set(conn.id, conn);
    this.handlers.onConnection(conn);
  }

  internalRemove(id: string): void {
    this.conns.delete(id);
  }
}

function extractToken(url: string, authorization: string | string[] | undefined): string | null {
  const query = url.split('?')[1] ?? '';
  for (const part of query.split('&')) {
    const [k, v] = part.split('=');
    if (k === 'access_token' && v !== undefined) return decodeURIComponent(v);
  }
  const header = Array.isArray(authorization) ? authorization[0] : authorization;
  if (typeof header === 'string') {
    const bare = header.startsWith('Bearer ') ? header.slice(7) : header;
    if (bare) return bare;
  }
  return null;
}

/** 单条接入连接：帧读取循环 + 发送。 */
class Conn implements WsServerConnection {
  readonly id = randomUUID();
  readonly url: string;
  readonly token: string | null;
  private socket: Duplex | null;
  private buffer = Buffer.alloc(0);
  private fragment: { opcode: number; chunks: Buffer[] } | null = null;
  private disposed = false;
  private awaitingPong = false;

  constructor(
    socket: Duplex,
    private readonly handlers: WsServerHandlers,
    private readonly server: { internalRemove(id: string): void },
    url: string,
    token: string | null,
  ) {
    this.socket = socket;
    this.url = url;
    this.token = token;
    socket.on('data', (chunk: Buffer) => this.onData(chunk));
    socket.on('close', () => this.dispose());
    socket.on('error', () => this.dispose());
  }

  send(text: string): void {
    this.writeFrame(0x1, Buffer.from(text, 'utf8'));
  }

  close(code = 1000): void {
    const payload = Buffer.alloc(2);
    payload.writeUInt16BE(code);
    this.writeFrame(0x8, payload);
    this.dispose();
  }

  ping(): void {
    this.writeFrame(0x9, Buffer.alloc(0));
  }

  private dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.socket?.destroy();
    this.socket = null;
    this.server.internalRemove(this.id);
    this.handlers.onClose(this);
  }

  private writeFrame(opcode: number, payload: Buffer): void {
    if (this.disposed) return;
    const socket = this.socket;
    if (socket === null) return;
    const len = payload.length;
    let header: Buffer;
    if (len < 126) {
      header = Buffer.from([0x80 | opcode, len]);
    } else if (len < 65536) {
      header = Buffer.alloc(4);
      header[0] = 0x80 | opcode;
      header[1] = 126;
      header.writeUInt16BE(len, 2);
    } else {
      header = Buffer.alloc(10);
      header[0] = 0x80 | opcode;
      header[1] = 127;
      header.writeBigUInt64BE(BigInt(len), 2);
    }
    socket.write(Buffer.concat([header, payload]));
  }

  private onData(chunk: Buffer): void {
    this.buffer = this.buffer.length === 0 ? Buffer.from(chunk) : Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const frame = this.tryParseFrame();
      if (frame === null) return;
      this.handleFrame(frame);
      if (this.disposed) return;
    }
  }

  private tryParseFrame(): Frame | null {
    const buf = this.buffer;
    if (buf.length < 2) return null;
    const final = (buf[0] & 0x80) !== 0;
    const opcode = buf[0] & 0x0f;
    const masked = (buf[1] & 0x80) !== 0;
    let len = buf[1] & 0x7f;
    let offset = 2;
    if (len === 126) {
      if (buf.length < offset + 2) return null;
      len = buf.readUInt16BE(offset);
      offset += 2;
    } else if (len === 127) {
      if (buf.length < offset + 8) return null;
      const big = buf.readBigUInt64BE(offset);
      if (big > BigInt(Number.MAX_SAFE_INTEGER)) {
        this.close(1009);
        return null;
      }
      len = Number(big);
      offset += 8;
    }
    let mask: Buffer | null = null;
    if (masked) {
      if (buf.length < offset + 4) return null;
      mask = buf.subarray(offset, offset + 4);
      offset += 4;
    }
    if (buf.length < offset + len) return null;
    let payload = buf.subarray(offset, offset + len);
    if (mask !== null) {
      payload = Buffer.from(payload); // 可写副本
      for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
    }
    this.buffer = buf.subarray(offset + len);
    return { opcode, payload, final };
  }

  private handleFrame(frame: Frame): void {
    switch (frame.opcode) {
      case 0x1: // text
      case 0x2: // binary
        if (!frame.final) {
          this.fragment = { opcode: frame.opcode, chunks: [frame.payload] };
          return;
        }
        this.deliverText(frame.payload);
        return;
      case 0x0: { // continuation
        if (this.fragment === null) return;
        this.fragment.chunks.push(frame.payload);
        if (!frame.final) return;
        const assembled = Buffer.concat(this.fragment.chunks);
        this.fragment = null;
        this.deliverText(assembled);
        return;
      }
      case 0x8: // close
        this.close(1000);
        return;
      case 0x9: // ping → pong
        this.writeFrame(0xa, frame.payload);
        return;
      case 0xa: // pong
        this.awaitingPong = false;
        return;
      default:
        return;
    }
  }

  private deliverText(payload: Buffer): void {
    const text = payload.toString('utf8');
    this.handlers.onText(this, text);
  }
}
