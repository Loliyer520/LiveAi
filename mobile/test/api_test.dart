import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:deeplive_mobile/api.dart';

void main() {
  test(
    'URL validation supports reverse proxy paths and refuses credential URLs',
    () {
      expect(
        const Connection(url: 'https://ai.example.com/bot/')
            .endpoint('/api/mobile/chat')
            .toString(),
        'https://ai.example.com/bot/api/mobile/chat',
      );
      for (final url in [
        'ftp://example.com',
        'http://name:secret@example.com',
        'http://example.com?token=secret',
        'localhost',
      ]) {
        expect(
          () => Connection(url: url).endpoint('/api/mobile/chat'),
          throwsFormatException,
        );
      }
    },
  );
  test(
    'API authenticates, preserves same-time messages and sends text only',
    () async {
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      final captured = <String>[];
      server.listen((request) async {
        expect(request.headers.value('authorization'), 'Bearer test-token');
        expect(request.uri.path, '/api/mobile/chat');
        request.response.headers.contentType = ContentType.json;
        if (request.method == 'GET') {
          request.response.write(
            jsonEncode({
              'messages': [
                {'role': 'me', 'name': '我', 'text': '你好', 'ts': 1000},
                {'role': 'ai', 'name': 'AI', 'text': '在呢', 'ts': 1000},
              ],
            }),
          );
        } else {
          captured.add(await utf8.decoder.bind(request).join());
          request.response.write('{"ok":true}');
        }
        await request.response.close();
      });
      final api = ChatApi(
        Connection(url: 'http://127.0.0.1:${server.port}', token: 'test-token'),
      );
      addTearDown(() async {
        api.close();
        await server.close(force: true);
      });
      final history = await api.history();
      expect(history, hasLength(2));
      expect(history.first.isMine, isTrue);
      expect(history.last.isAi, isTrue);
      expect(history.first.key, isNot(history.last.key));
      await api.send('  你好\n第二行  ');
      expect(jsonDecode(captured.single), {'text': '你好\n第二行'});
    },
  );
  test(
    'authentication errors are readable and do not expose response secrets',
    () async {
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      server.listen((request) async {
        request.response.statusCode = 403;
        request.response.write('sensitive-response');
        await request.response.close();
      });
      final api = ChatApi(Connection(url: 'http://127.0.0.1:${server.port}'));
      addTearDown(() async {
        api.close();
        await server.close(force: true);
      });
      await expectLater(
        api.history(),
        throwsA(
          isA<HttpException>().having(
            (e) => e.message,
            'message',
            contains('凭证无效'),
          ),
        ),
      );
    },
  );
  test('provision replaces admin token with a scoped session token', () async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((request) async {
      expect(request.uri.path, '/api/mobile/sessions');
      expect(request.method, 'POST');
      expect(request.headers.value('authorization'), 'Bearer bootstrap-admin');
      request.response.headers.contentType = ContentType.json;
      request.response.write(
        jsonEncode({
          'sessionId': 'separate-session',
          'token': 'dlapp_scoped-test-token',
        }),
      );
      await request.response.close();
    });
    final api = ChatApi(
      Connection(
        url: 'http://127.0.0.1:${server.port}',
        token: 'bootstrap-admin',
      ),
    );
    addTearDown(() async {
      api.close();
      await server.close(force: true);
    });
    final connected = await api.provision();
    expect(connected.token, 'dlapp_scoped-test-token');
    expect(jsonEncode(connected.toJson()), isNot(contains('bootstrap-admin')));
  });
}
