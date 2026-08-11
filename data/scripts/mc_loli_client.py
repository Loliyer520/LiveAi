#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mc_loli_client.py — 以离线模式（offline mode）用玩家名 'loli' 登录 Minecraft Java 版服务器。

功能概览
========
1. status ping：先探测目标服务器的协议版本号（protocol version），无需任何依赖。
2. handshake：把连接状态从初始切换到 login（next state = 2）。
3. login start：发送携带用户名的登录起始包，进入登录流程。
4. 处理服务器响应：
   - Set Compression（设置压缩阈值，后续所有包按 zlib 压缩帧解析）
   - Login Success（登录成功，拿到 UUID/用户名）
   - Disconnect（被踢，常见于开启了正版验证 online-mode=true）
   - Encryption Request（服务器要求加密握手 = 开启了正版验证，离线登录无法通过）
5. 进入 play / configuration 状态后做最基本的 Keep-Alive 保活：
   收到服务器的 Keep-Alive 请求就原样回发，维持在线。

设计说明
========
- 本脚本刻意 **不依赖任何第三方库**（quarry 需要 Twisted、pyCraft 已停止维护），
  只用 Python 标准库手动实现 Minecraft 的 VarInt / 数据包封装，便于在受限环境直接运行。
- 协议版本（PROTOCOL_VERSION）可通过命令行 --protocol 配置；默认会先用 status ping
  自动探测服务器的协议号并采用它，探测失败时回退到内置默认值。
- Minecraft 各版本的数据包 ID 会变化，本脚本对「登录阶段」的关键包做了兼容处理，
  Keep-Alive 的包 ID 通过内置映射表 + 命令行参数覆盖来适配不同版本。

命令行用法见文件底部 main() 或 README 说明。
"""

import argparse
import hashlib
import json
import socket
import struct
import sys
import time
import uuid
import zlib

# ─────────────────────────────────────────────────────────────────────────────
# 默认配置
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_HOST = "mc.posc.net"
DEFAULT_PORT = 25565
DEFAULT_USERNAME = "loli"
# 探测失败时使用的回退协议号。765 = 1.20.4，一个较通用的默认值。
FALLBACK_PROTOCOL = 765

# 常见协议版本 -> (clientbound keep-alive ID, serverbound keep-alive ID) 映射。
# play 状态下服务器会周期性发送 Keep-Alive，客户端需原样回发相同的 long 值。
# 不同 MC 版本包 ID 不同，这里给出若干常见版本；未命中时用 DEFAULT_KEEPALIVE。
# 可用 --keepalive-clientbound / --keepalive-serverbound 手动覆盖。
KEEPALIVE_IDS = {
    # protocol: (clientbound_play_keepalive, serverbound_play_keepalive)
    340: (0x21, 0x0B),  # 1.12.2
    404: (0x21, 0x0E),  # 1.13.2
    498: (0x21, 0x0F),  # 1.14.4
    578: (0x21, 0x0F),  # 1.15.2
    754: (0x1F, 0x10),  # 1.16.5
    758: (0x21, 0x0F),  # 1.18.2
    759: (0x21, 0x11),  # 1.19
    760: (0x20, 0x12),  # 1.19.2
    761: (0x1F, 0x11),  # 1.19.3
    762: (0x23, 0x12),  # 1.19.4
    763: (0x24, 0x12),  # 1.20.1
    765: (0x24, 0x15),  # 1.20.4
    775: (0x26, 0x1B),  # 1.21.8（据 minecraft-data proto.yml 校验）
}
DEFAULT_KEEPALIVE = (0x24, 0x15)

# configuration 阶段关键包 ID（disconnect, finish_configuration, keep_alive），
# clientbound / serverbound 在这些包上通常是同一个 ID。1.20.2+ 才有 configuration 阶段。
# 不同版本 configuration 包 ID 会随新增的 cookie/report 等包前移而变化，这里按需补充。
CONFIG_PACKET_IDS = {
    764: (0x01, 0x02, 0x03),  # 1.20.2
    765: (0x01, 0x02, 0x03),  # 1.20.4
    775: (0x02, 0x03, 0x04),  # 1.21.8（据 minecraft-data proto.yml 校验）
}
DEFAULT_CONFIG_PACKET_IDS = (0x02, 0x03, 0x04)


# ─────────────────────────────────────────────────────────────────────────────
# 底层协议原语：VarInt / 字符串 / 数据包读写
# ─────────────────────────────────────────────────────────────────────────────
def encode_varint(value: int) -> bytes:
    """把一个（无符号）整数编码为 Minecraft VarInt。"""
    out = bytearray()
    # 处理成 32 位无符号，兼容负数（协议里 VarInt 是 32 位有符号补码）
    value &= 0xFFFFFFFF
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def read_varint_from_socket(sock: socket.socket) -> int:
    """从 socket 逐字节读取一个 VarInt。"""
    num = 0
    for i in range(5):  # VarInt 最长 5 字节
        byte = _recv_exact(sock, 1)[0]
        num |= (byte & 0x7F) << (7 * i)
        if not (byte & 0x80):
            break
    else:
        raise IOError("VarInt 过长（超过 5 字节）")
    # 转成 32 位有符号
    if num & 0x80000000:
        num -= 0x100000000
    return num


def read_varint_from_bytes(data: bytes, offset: int) -> tuple[int, int]:
    """从 bytes 的 offset 处解析一个 VarInt，返回 (值, 新offset)。"""
    num = 0
    for i in range(5):
        byte = data[offset]
        offset += 1
        num |= (byte & 0x7F) << (7 * i)
        if not (byte & 0x80):
            break
    else:
        raise IOError("VarInt 过长（超过 5 字节）")
    if num & 0x80000000:
        num -= 0x100000000
    return num, offset


def encode_string(text: str) -> bytes:
    """UTF-8 字符串：VarInt 长度前缀 + 字节内容。"""
    raw = text.encode("utf-8")
    return encode_varint(len(raw)) + raw


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """精确读取 n 字节，读不满就报错（连接被关闭等）。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被服务器关闭（recv 返回空）")
        buf.extend(chunk)
    return bytes(buf)


class PacketBuffer:
    """帮助按顺序构造一个数据包的负载（payload）。"""

    def __init__(self):
        self._data = bytearray()

    def write_varint(self, value: int) -> "PacketBuffer":
        self._data += encode_varint(value)
        return self

    def write_string(self, text: str) -> "PacketBuffer":
        self._data += encode_string(text)
        return self

    def write_ushort(self, value: int) -> "PacketBuffer":
        self._data += struct.pack(">H", value)
        return self

    def write_long(self, value: int) -> "PacketBuffer":
        self._data += struct.pack(">q", value)
        return self

    def write_bytes(self, raw: bytes) -> "PacketBuffer":
        self._data += raw
        return self

    def write_uuid(self, u: uuid.UUID) -> "PacketBuffer":
        self._data += u.bytes
        return self

    def getvalue(self) -> bytes:
        return bytes(self._data)


# ─────────────────────────────────────────────────────────────────────────────
# 数据包发送 / 接收（含可选压缩）
# ─────────────────────────────────────────────────────────────────────────────
def send_packet(sock: socket.socket, packet_id: int, payload: bytes, threshold: int) -> None:
    """
    发送一个数据包。
    - threshold < 0：未启用压缩，帧格式 = VarInt(总长) + VarInt(id) + payload
    - threshold >= 0：启用压缩帧，格式 = VarInt(总长) + VarInt(dataLength) + [zlib?]数据
      dataLength = 0 表示未压缩（数据小于阈值），否则为解压后长度。
    """
    body = encode_varint(packet_id) + payload
    if threshold < 0:
        frame = encode_varint(len(body)) + body
    else:
        if len(body) >= threshold:
            compressed = zlib.compress(body)
            data = encode_varint(len(body)) + compressed
        else:
            data = encode_varint(0) + body
        frame = encode_varint(len(data)) + data
    sock.sendall(frame)


def read_packet(sock: socket.socket, threshold: int) -> tuple[int, bytes]:
    """
    读取一个数据包，返回 (packet_id, payload)。
    自动处理压缩帧。
    """
    length = read_varint_from_socket(sock)  # 帧总长
    raw = _recv_exact(sock, length)
    if threshold < 0:
        pid, off = read_varint_from_bytes(raw, 0)
        return pid, raw[off:]
    # 压缩帧：先读 dataLength
    data_length, off = read_varint_from_bytes(raw, 0)
    body = raw[off:]
    if data_length == 0:
        # 未压缩
        pid, off2 = read_varint_from_bytes(body, 0)
        return pid, body[off2:]
    # 已压缩，需解压
    decompressed = zlib.decompress(body)
    pid, off2 = read_varint_from_bytes(decompressed, 0)
    return pid, decompressed[off2:]


# ─────────────────────────────────────────────────────────────────────────────
# 离线 UUID 计算（与官方 offline mode 一致）
# ─────────────────────────────────────────────────────────────────────────────
def offline_uuid(username: str) -> uuid.UUID:
    """
    离线模式 UUID = 基于 'OfflinePlayer:<name>' 的 MD5，并置为版本 3 UUID。
    与 Minecraft 服务端 UUIDUtil.getOfflinePlayerUUID 行为一致。
    """
    digest = hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest()
    b = bytearray(digest)
    b[6] = (b[6] & 0x0F) | 0x30  # version 3
    b[8] = (b[8] & 0x3F) | 0x80  # variant
    return uuid.UUID(bytes=bytes(b))


# ─────────────────────────────────────────────────────────────────────────────
# handshake
# ─────────────────────────────────────────────────────────────────────────────
def send_handshake(sock: socket.socket, protocol: int, host: str, port: int, next_state: int) -> None:
    """
    Handshake 包（packet id 0x00，握手阶段未压缩）：
      protocol version (VarInt) + server address (String) + server port (UShort) + next state (VarInt)
    next_state：1 = status，2 = login
    """
    payload = (
        PacketBuffer()
        .write_varint(protocol)
        .write_string(host)
        .write_ushort(port)
        .write_varint(next_state)
        .getvalue()
    )
    send_packet(sock, 0x00, payload, threshold=-1)


# ─────────────────────────────────────────────────────────────────────────────
# status ping：探测协议版本
# ─────────────────────────────────────────────────────────────────────────────
def query_status(host: str, port: int, handshake_protocol: int = FALLBACK_PROTOCOL, timeout: float = 8.0) -> dict:
    """
    连接服务器执行一次 status ping，返回解析后的 status JSON。
    其中 data['version']['protocol'] 即服务器期望的协议号。
    """
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        # 1) handshake -> status(1)
        send_handshake(sock, handshake_protocol, host, port, next_state=1)
        # 2) status request（空负载）
        send_packet(sock, 0x00, b"", threshold=-1)
        # 3) 读取 status response
        pid, payload = read_packet(sock, threshold=-1)
        if pid != 0x00:
            raise IOError(f"status 响应包 ID 异常：0x{pid:02X}")
        json_len, off = read_varint_from_bytes(payload, 0)
        json_bytes = payload[off:off + json_len]
        return json.loads(json_bytes.decode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# login start
# ─────────────────────────────────────────────────────────────────────────────
def build_login_start(username: str, protocol: int) -> bytes:
    """
    构造 Login Start 负载。不同协议版本字段不同：
      - protocol < 759 (1.18.2 及更早)：仅 name(String)
      - 759 (1.19) / 760 (1.19.1-2)：name + hasSigData(Bool=false)[ + hasUUID(Bool) + UUID]
      - 761 (1.19.3) / 762 (1.19.4) / 763 (1.20.1)：name + hasUUID(Bool) + [UUID]
      - 764+ (1.20.2+)：name + UUID(必带)
    离线模式下我们不提供签名数据（hasSigData=false），UUID 用离线算法生成。
    """
    buf = PacketBuffer().write_string(username)
    u = offline_uuid(username)

    if protocol < 759:
        # 老版本：只要用户名
        return buf.getvalue()
    if protocol < 761:
        # 1.19 ~ 1.19.2：先是 hasSigData 布尔（离线无签名 -> false）
        buf.write_bytes(b"\x00")  # has signature data = false
        if protocol >= 760:
            # 1.19.1+：可选 UUID
            buf.write_bytes(b"\x01")  # has UUID = true
            buf.write_uuid(u)
        return buf.getvalue()
    if protocol < 764:
        # 1.19.3 ~ 1.20.1：name + hasUUID(Bool) + UUID
        buf.write_bytes(b"\x01")  # has UUID = true
        buf.write_uuid(u)
        return buf.getvalue()
    # 1.20.2+：name + UUID（强制）
    buf.write_uuid(u)
    return buf.getvalue()


def parse_chat_text(payload_json) -> str:
    """尽量从 chat/JSON 组件里提取可读文本（用于 Disconnect 原因显示）。"""
    try:
        if isinstance(payload_json, str):
            try:
                payload_json = json.loads(payload_json)
            except Exception:
                return payload_json
        if isinstance(payload_json, dict):
            parts = [payload_json.get("text", "")]
            for extra in payload_json.get("extra", []) or []:
                parts.append(parse_chat_text(extra))
            if payload_json.get("translate"):
                parts.append(str(payload_json.get("translate")))
            return "".join(parts)
        return str(payload_json)
    except Exception:
        return str(payload_json)


# ─────────────────────────────────────────────────────────────────────────────
# 登录流程主逻辑
# ─────────────────────────────────────────────────────────────────────────────
def login_and_keepalive(host: str, port: int, username: str, protocol: int,
                        ka_clientbound: int, ka_serverbound: int, timeout: float = 30.0,
                        max_keepalives: int = 2,
                        config_packet_ids: tuple = None) -> None:
    """执行 handshake -> login start -> 处理登录响应 -> play 阶段 keep-alive 保活。
    max_keepalives：处理多少次 play 阶段 keep-alive 后主动断开（0=不限制，一直保活）。
    config_packet_ids：configuration 阶段 (disconnect, finish_configuration, keep_alive) 包 ID，
    默认按 protocol 从 CONFIG_PACKET_IDS 映射表取值。
    """
    if config_packet_ids is None:
        config_packet_ids = CONFIG_PACKET_IDS.get(protocol, DEFAULT_CONFIG_PACKET_IDS)
    cfg_disconnect, cfg_finish, cfg_keepalive = config_packet_ids
    print(f"[*] 连接 {host}:{port}，协议版本 {protocol}，用户名 '{username}'（离线模式）")
    print(f"[*] Keep-Alive ID：play=(cb=0x{ka_clientbound:02X}, sb=0x{ka_serverbound:02X})，"
          f"configuration=(disconnect=0x{cfg_disconnect:02X}, finish=0x{cfg_finish:02X}, keepalive=0x{cfg_keepalive:02X})")
    keepalive_count = 0
    threshold = -1  # 未启用压缩

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        # 1) handshake -> login(2)
        send_handshake(sock, protocol, host, port, next_state=2)
        # 2) login start
        payload = build_login_start(username, protocol)
        send_packet(sock, 0x00, payload, threshold)
        print(f"[*] 已发送 Login Start（离线 UUID = {offline_uuid(username)}）")

        logged_in = False
        state = "login"  # login -> (可能 configuration) -> play

        while True:
            try:
                pid, data = read_packet(sock, threshold)
            except socket.timeout:
                # 读超时：在 play 阶段属正常（没有包），继续等
                continue

            if state == "login":
                # 登录阶段包 ID（各版本基本稳定）：
                # 0x00 Disconnect(login) / 0x01 Encryption Request /
                # 0x02 Login Success / 0x03 Set Compression /
                # 0x04 Login Plugin Request
                if pid == 0x03:
                    threshold, _ = read_varint_from_bytes(data, 0)
                    print(f"[*] 服务器启用压缩，阈值 = {threshold}")
                    continue
                if pid == 0x01:
                    print("[!] 服务器发送了 Encryption Request：说明该服务器开启了正版验证"
                          "（online-mode=true）。离线模式无法通过验证，登录终止。")
                    return
                if pid == 0x00:
                    off = 0
                    reason_len, off = read_varint_from_bytes(data, 0)
                    reason = data[off:off + reason_len].decode("utf-8", "replace")
                    try:
                        reason = parse_chat_text(json.loads(reason))
                    except Exception:
                        pass
                    print(f"[!] 登录被拒绝（Disconnect）：{reason}")
                    print("    常见原因：服务器开启了正版验证 online-mode=true，或用户名/白名单限制。")
                    return
                if pid == 0x04:
                    # Login Plugin Request：需回一个「未理解」的响应（message id + success=false）
                    msg_id, _ = read_varint_from_bytes(data, 0)
                    resp = PacketBuffer().write_varint(msg_id).write_bytes(b"\x00").getvalue()
                    send_packet(sock, 0x02, resp, threshold)
                    continue
                if pid == 0x02:
                    logged_in = True
                    print("[+] Login Success！登录成功。")
                    if protocol >= 764:
                        # 1.20.2+：登录成功后需发送 Login Acknowledged(0x03) 进入 configuration 状态
                        send_packet(sock, 0x03, b"", threshold)
                        state = "configuration"
                        print("[*] 已发送 Login Acknowledged，进入 configuration 阶段。")
                    else:
                        state = "play"
                        print("[*] 进入 play 阶段，开始 keep-alive 保活。")
                    continue
                # 其它未知登录包，忽略
                continue

            if state == "configuration":
                # 1.20.2+ configuration 阶段：
                # 服务器发送若干配置包，最后发 Finish Configuration，客户端回 Finish Configuration 进入 play。
                # 包 ID 按 protocol 从 CONFIG_PACKET_IDS 映射取得（1.21.8 已与旧版本不同）。
                if pid == cfg_finish:
                    send_packet(sock, cfg_finish, b"", threshold)  # Acknowledge Finish Configuration
                    state = "play"
                    print("[*] configuration 完成，进入 play 阶段，开始 keep-alive 保活。")
                    continue
                if pid == cfg_keepalive:
                    # configuration 阶段的 keep-alive，原样回发
                    send_packet(sock, cfg_keepalive, data, threshold)
                    continue
                if pid == cfg_disconnect:
                    # Disconnect(configuration)
                    reason = _try_decode_reason(data)
                    print(f"[!] 在 configuration 阶段被断开：{reason}")
                    return
                # 其它配置包忽略
                continue

            if state == "play":
                if pid == ka_clientbound:
                    # Keep-Alive（clientbound）：负载是一个 8 字节 long，原样回发
                    send_packet(sock, ka_serverbound, data[:8], threshold)
                    keepalive_count += 1
                    print(f"[*] 收到 Keep-Alive，已回发保活（id=0x{pid:02X}，第 {keepalive_count} 次），"
                          f"{time.strftime('%H:%M:%S')}")
                    if max_keepalives and keepalive_count >= max_keepalives:
                        print(f"[*] 已处理 {keepalive_count} 次 keep-alive，连接稳定，主动断开退出。")
                        return
                    continue
                # 其它 play 包（区块、实体等）忽略，仅维持连接
                continue


    except (ConnectionError, OSError) as e:
        if logged_in:
            print(f"[!] 连接中断：{e}")
        else:
            print(f"[!] 网络/连接错误：{e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _try_decode_reason(data: bytes) -> str:
    try:
        rlen, off = read_varint_from_bytes(data, 0)
        raw = data[off:off + rlen].decode("utf-8", "replace")
        try:
            return parse_chat_text(json.loads(raw))
        except Exception:
            return raw
    except Exception:
        return "<无法解析原因>"


# ─────────────────────────────────────────────────────────────────────────────
# 命令行入口
# ─────────────────────────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="以离线模式登录 Minecraft Java 版服务器并做基础 keep-alive 保活。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="服务器地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="服务器端口")
    parser.add_argument("--username", default=DEFAULT_USERNAME, help="离线模式用户名")
    parser.add_argument("--protocol", type=int, default=None,
                        help="协议版本号；不指定则用 status ping 自动探测")
    parser.add_argument("--status-only", action="store_true",
                        help="只做 status ping 探测协议版本，不登录")
    parser.add_argument("--keepalive-clientbound", type=lambda x: int(x, 0), default=None,
                        help="覆盖 play 阶段 clientbound keep-alive 包 ID（如 0x24）")
    parser.add_argument("--keepalive-serverbound", type=lambda x: int(x, 0), default=None,
                        help="覆盖 play 阶段 serverbound keep-alive 包 ID（如 0x15）")
    parser.add_argument("--timeout", type=float, default=30.0, help="socket 超时（秒）")
    args = parser.parse_args(argv)

    # 1) 探测协议版本（除非用户明确指定了 --protocol）
    detected_protocol = None
    try:
        print(f"[*] status ping 探测 {args.host}:{args.port} ...")
        status = query_status(args.host, args.port, timeout=args.timeout)
        version_info = status.get("version", {})
        detected_protocol = version_info.get("protocol")
        players = status.get("players", {})
        print(f"[+] 服务器版本名：{version_info.get('name', '?')}，协议号：{detected_protocol}")
        print(f"[+] 在线人数：{players.get('online', '?')}/{players.get('max', '?')}")
    except Exception as e:
        print(f"[!] status ping 失败：{e}")

    if args.status_only:
        return 0

    protocol = args.protocol or detected_protocol or FALLBACK_PROTOCOL
    if args.protocol:
        print(f"[*] 使用命令行指定的协议号：{protocol}")
    elif detected_protocol:
        print(f"[*] 使用探测到的协议号：{protocol}")
    else:
        print(f"[*] 未能探测协议号，回退到默认：{protocol}")

    # 2) 确定 keep-alive 包 ID
    default_ka = KEEPALIVE_IDS.get(protocol, DEFAULT_KEEPALIVE)
    ka_cb = args.keepalive_clientbound if args.keepalive_clientbound is not None else default_ka[0]
    ka_sb = args.keepalive_serverbound if args.keepalive_serverbound is not None else default_ka[1]
    if protocol not in KEEPALIVE_IDS and args.keepalive_clientbound is None:
        print(f"[!] 协议 {protocol} 无内置 keep-alive 映射，使用默认 0x{ka_cb:02X}/0x{ka_sb:02X}；"
              "如保活异常可用 --keepalive-clientbound/--keepalive-serverbound 覆盖。")

    # 3) 登录并保活
    try:
        login_and_keepalive(args.host, args.port, args.username, protocol, ka_cb, ka_sb, args.timeout)
    except KeyboardInterrupt:
        print("\n[*] 用户中断，退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
