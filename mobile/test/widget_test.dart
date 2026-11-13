import 'package:deeplive_mobile/api.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:deeplive_mobile/main.dart';
import 'package:deeplive_mobile/native_composer.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(nativeChannel, (call) async => null);
  });
  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(nativeChannel, null);
  });
  testWidgets('inbox opens connection settings on small phone without server', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360, 720);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(const DeepLiveApp());
    await tester.pumpAndSettle();
    expect(find.text('点击开始和 AI 聊天'), findsOneWidget);
    await tester.tap(find.text('点击开始和 AI 聊天'));
    await tester.pumpAndSettle();
    expect(find.text('连接 DeepLive'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
  testWidgets('composer keeps rejected message and clears accepted message', (
    tester,
  ) async {
    var accept = false;
    String? sent;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Align(
            alignment: Alignment.bottomCenter,
            child: NativeComposer(
              onSend: (text) async {
                sent = text;
                return accept;
              },
            ),
          ),
        ),
      ),
    );
    await tester.enterText(find.byType(TextField), '你好\n第二行');
    await tester.pump();
    await tester.tap(find.text('发送'));
    await tester.pump();
    expect(sent, '你好\n第二行');
    expect(find.text('你好\n第二行'), findsOneWidget);
    accept = true;
    await tester.tap(find.text('发送'));
    await tester.pump();
    expect(find.text('你好\n第二行'), findsNothing);
    expect(tester.takeException(), isNull);
  });
  testWidgets('chat keeps composer above keyboard and shows user as outgoing', (
    tester,
  ) async {
    final state = ChatState();
    state.messages = const [
      ChatMessage(role: 'me', name: '我', text: '你好', ts: 1700000000),
      ChatMessage(role: 'ai', name: 'DeepLive', text: '在呢', ts: 1700000000),
    ];
    tester.view.physicalSize = const Size(360, 720);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(MaterialApp(home: ConversationPage(state: state)));
    await tester.pumpAndSettle();
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    addTearDown(tester.view.resetViewInsets);
    await tester.pumpAndSettle();
    expect(find.text('你好'), findsOneWidget);
    expect(find.text('在呢'), findsOneWidget);
    expect(
      tester.getBottomLeft(find.byType(NativeComposer)).dy,
      lessThanOrEqualTo(420),
    );
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    state.dispose();
  });
}
