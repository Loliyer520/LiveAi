import 'dart:async';
import 'dart:convert';
import 'dart:io';

class Connection {
  const Connection({
    required this.url,
    this.token = '',
    this.name = 'DeepLive',
  });
  final String url;
  final String token;
  final String name;
  static Connection fromJson(Map<String, dynamic> json) => Connection(
    url: json['url'] as String? ?? '',
    token: json['token'] as String? ?? '',
    name: json['name'] as String? ?? 'DeepLive',
  );
  Map<String, String> toJson() => {'url': url, 'token': token, 'name': name};
  Uri endpoint(String path) {
    final base = Uri.tryParse(url.trim());
    if (base == null ||
        !['http', 'https'].contains(base.scheme) ||
        base.host.isEmpty ||
        base.userInfo.isNotEmpty ||
        base.hasQuery ||
        base.hasFragment) {
      throw const FormatException(
        '请输入完整服务器地址，例如 https://ai.example.com，不要在地址中填写 token',
      );
    }
    final prefix = base.path.replaceFirst(RegExp(r'/+$'), '');
    return base.replace(path: '$prefix$path');
  }
}

class ChatMessage {
  const ChatMessage({
    required this.role,
    required this.name,
    required this.text,
    required this.ts,
  });
  final String role, name, text;
  final double ts;
  bool get isMine => role == 'me';
  bool get isAi => role == 'ai';
  String get key => jsonEncode([ts, role, name, text]);
  factory ChatMessage.fromJson(Map<String, dynamic> json) => ChatMessage(
    role: json['role'] as String? ?? 'sys',
    name: json['name'] as String? ?? '',
    text: json['text'] as String? ?? '',
    ts: (json['ts'] as num).toDouble(),
  );
}

class ChatApi {
  ChatApi(this.connection) {
    _http.connectionTimeout = const Duration(seconds: 10);
  }
  final Connection connection;
  final HttpClient _http = HttpClient();
  Future<Map<String, dynamic>> request(
    String method,
    String path, [
    Object? body,
  ]) async {
    // All requests are bounded; never include server response bodies or credentials in errors.
    try {
      return await (() async {
        final req = await _http.openUrl(method, connection.endpoint(path));
        req.followRedirects =
            false; // Never forward administrator credentials to redirects.
        req.headers.set(HttpHeaders.acceptHeader, 'application/json');
        if (connection.token.isNotEmpty) {
          req.headers.set(
            HttpHeaders.authorizationHeader,
            'Bearer ${connection.token}',
          );
        }
        if (body != null) {
          req.headers.contentType = ContentType.json;
          req.write(jsonEncode(body));
        }
        final res = await req.close();
        if (res.statusCode != 200) {
          await res.drain<void>();
          throw HttpException(switch (res.statusCode) {
            401 || 403 => '会话凭证无效或权限不足，请重新配置连接',
            400 => '请求被拒绝，请检查消息长度或服务器设置',
            >= 300 && < 400 => '服务器返回重定向，请填写最终地址',
            _ => '服务器请求失败（${res.statusCode}）',
          });
        }
        final text = await utf8.decoder.bind(res).join();
        final data = jsonDecode(text);
        if (data is! Map<String, dynamic>) {
          throw const FormatException('服务器未返回有效接口数据');
        }
        return data;
      })().timeout(const Duration(seconds: 15));
    } on SocketException {
      throw const HttpException('无法连接服务器，请检查地址、网络和服务监听设置');
    } on TimeoutException {
      throw const HttpException('连接超时，请检查服务器网络；发送超时请先查看历史，避免重复发送');
    }
  }

  Future<Connection> provision() async {
    final data = await request('POST', '/api/mobile/sessions');
    final value = data['token'];
    if (value is! String || !value.startsWith('dlapp_')) {
      throw const FormatException('服务器未返回独立会话凭证，请升级服务端');
    }
    return Connection(url: connection.url, token: value, name: connection.name);
  }

  Future<List<ChatMessage>> history() async {
    final data = await request('GET', '/api/mobile/chat');
    final list = data['messages'];
    if (list is! List) throw const FormatException('服务器不支持 DeepLive 聊天接口');
    return list
        .map((m) => ChatMessage.fromJson(Map<String, dynamic>.from(m as Map)))
        .toList();
  }

  Future<void> send(String text) async {
    if (text.trim().isEmpty || text.length > 4000) {
      throw const FormatException('请输入 1～4000 字消息');
    }
    await request('POST', '/api/mobile/chat', {'text': text.trim()});
  }

  void close() => _http.close(force: true);
}

String friendlyError(Object error) {
  if (error is HttpException) return error.message;
  if (error is FormatException) return error.message;
  return '连接出现问题，请检查设置后重试';
}
