import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'native_composer.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarColor: Colors.transparent,
      statusBarIconBrightness: Brightness.dark,
    ),
  );
  runApp(const DeepLiveApp());
}

const blue = Color(0xFF1687FF);
const ink = Color(0xFF202939);
const muted = Color(0xFF8993A5);

class DeepLiveApp extends StatelessWidget {
  const DeepLiveApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'DeepLive',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      useMaterial3: true,
      scaffoldBackgroundColor: const Color(0xFFF5F7FB),
      colorScheme: ColorScheme.fromSeed(seedColor: blue, primary: blue),
      appBarTheme: const AppBarTheme(
        backgroundColor: Colors.white,
        foregroundColor: ink,
        scrolledUnderElevation: 0,
      ),
      fontFamilyFallback: const ['Noto Sans CJK SC', 'sans-serif'],
    ),
    home: const HomePage(),
  );
}

class ChatState extends ChangeNotifier with WidgetsBindingObserver {
  Connection? connection;
  ChatApi? _api;
  Timer? _timer;
  List<ChatMessage> messages = [];
  String? error;
  bool loading = false, online = false, chatOpen = false;
  int unread = 0, _generation = 0;
  bool _disposed = false;
  Future<void> init() async {
    WidgetsBinding.instance.addObserver(this);
    try {
      final raw = await nativeChannel.invokeMethod<String>('configLoad');
      if (_disposed) return;
      if (raw != null) {
        connect(Connection.fromJson(jsonDecode(raw) as Map<String, dynamic>));
      }
    } catch (_) {
      error = '连接设置读取失败，请重新配置';
      notifyListeners();
    }
  }

  void connect(Connection value) {
    _generation++;
    _timer?.cancel();
    _api?.close();
    connection = value;
    _api = ChatApi(value);
    messages = [];
    unread = 0;
    loading = false;
    online = false;
    error = null;
    notifyListeners();
    unawaited(refresh());
    _timer = Timer.periodic(
      const Duration(seconds: 2),
      (_) => unawaited(refresh()),
    );
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _timer?.cancel();
    if (state == AppLifecycleState.resumed && _api != null) {
      unawaited(refresh());
      _timer = Timer.periodic(
        const Duration(seconds: 2),
        (_) => unawaited(refresh()),
      );
    }
  }

  Future<void> refresh() async {
    if (_api == null || loading || _disposed) return;
    final generation = _generation;
    loading = true;
    notifyListeners();
    try {
      // Fetch a bounded full window: timestamps alone can drop simultaneous messages.
      final fresh = await _api!.history();
      if (_disposed || generation != _generation) return;
      if (!chatOpen && messages.isNotEmpty) {
        final previous = messages.map((m) => m.key).toSet();
        final latest = messages.last.ts;
        unread += fresh
            .where((m) => m.isAi && m.ts >= latest && !previous.contains(m.key))
            .length;
      }
      messages = fresh;
      online = true;
      error = null;
    } catch (e) {
      if (_disposed || generation != _generation) return;
      online = false;
      error = friendlyError(e);
    } finally {
      if (!_disposed && generation == _generation) {
        loading = false;
        notifyListeners();
      }
    }
  }

  Future<bool> send(String text) async {
    if (_api == null) return false;
    final generation = _generation;
    try {
      await _api!.send(text);
      if (_disposed || generation != _generation) return false;
      error = null;
      unawaited(refresh());
      return true;
    } catch (e) {
      if (!_disposed && generation == _generation) {
        error = friendlyError(e);
        notifyListeners();
      }
      return false;
    }
  }

  void markRead() {
    unread = 0;
  }

  @override
  void dispose() {
    _disposed = true;
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    _api?.close();
    super.dispose();
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final state = ChatState();
  int tab = 0;
  @override
  void initState() {
    super.initState();
    state.init();
  }

  @override
  void dispose() {
    state.dispose();
    super.dispose();
  }

  Future<void> settings() async {
    final result = await Navigator.push<Connection>(
      context,
      MaterialPageRoute(
        builder: (_) => ConnectionPage(initial: state.connection),
      ),
    );
    if (result != null && mounted) state.connect(result);
  }

  Future<void> openChat() async {
    if (state.connection == null) {
      await settings();
      return;
    }
    state.chatOpen = true;
    state.markRead();
    await Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => ConversationPage(state: state)),
    );
    state.chatOpen = false;
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: state,
    builder: (context, _) => Scaffold(
      appBar: AppBar(
        title: Text(
          ['消息', '联系人', '我'][tab],
          style: const TextStyle(fontWeight: FontWeight.w700),
        ),
        leading: const Padding(
          padding: EdgeInsets.all(10),
          child: Avatar(size: 36),
        ),
        actions: [
          IconButton(
            onPressed: settings,
            tooltip: '连接设置',
            icon: const Icon(Icons.add_circle_outline),
          ),
        ],
      ),
      body: tab == 0
          ? _inbox()
          : tab == 1
          ? _contacts()
          : _profile(),
      bottomNavigationBar: NavigationBar(
        selectedIndex: tab,
        backgroundColor: Colors.white,
        onDestinationSelected: (value) => setState(() => tab = value),
        destinations: [
          NavigationDestination(
            icon: Badge(
              isLabelVisible: state.unread > 0,
              label: Text('${state.unread}'),
              child: const Icon(Icons.chat_bubble_outline_rounded),
            ),
            selectedIcon: const Icon(Icons.chat_bubble_rounded),
            label: '消息',
          ),
          const NavigationDestination(
            icon: Icon(Icons.people_outline_rounded),
            selectedIcon: Icon(Icons.people_rounded),
            label: '联系人',
          ),
          const NavigationDestination(
            icon: Icon(Icons.person_outline_rounded),
            selectedIcon: Icon(Icons.person_rounded),
            label: '我',
          ),
        ],
      ),
    ),
  );
  Widget _inbox() => RefreshIndicator(
    onRefresh: state.refresh,
    child: ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      children: [
        Padding(
          padding: const EdgeInsets.all(16),
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            decoration: BoxDecoration(
              color: Colors.white,
              borderRadius: BorderRadius.circular(12),
            ),
            child: Row(
              children: [
                Icon(
                  state.online
                      ? Icons.cloud_done_outlined
                      : Icons.cloud_outlined,
                  size: 18,
                  color: blue,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    state.connection == null
                        ? '连接你的 DeepLive，开始聊天'
                        : state.online
                        ? '已连接 · 与服务器同步对话'
                        : '正在连接 · 可在设置中检查地址',
                    style: const TextStyle(color: muted, fontSize: 13),
                  ),
                ),
              ],
            ),
          ),
        ),
        if (state.error != null) _error(state.error!, state.refresh),
        Material(
          color: Colors.white,
          child: ListTile(
            contentPadding: const EdgeInsets.symmetric(
              horizontal: 20,
              vertical: 10,
            ),
            onTap: openChat,
            leading: const Avatar(),
            title: Row(
              children: [
                Expanded(
                  child: Text(
                    state.connection?.name ?? 'DeepLive',
                    style: const TextStyle(fontWeight: FontWeight.w600),
                  ),
                ),
                Text(
                  state.messages.isEmpty
                      ? ''
                      : timeLabel(state.messages.last.ts),
                  style: const TextStyle(fontSize: 12, color: muted),
                ),
              ],
            ),
            subtitle: Text(
              state.messages.isEmpty
                  ? '点击开始和 AI 聊天'
                  : state.messages.last.text.replaceAll('\n', ' '),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: muted),
            ),
            trailing: state.unread > 0
                ? Badge(label: Text('${state.unread}'))
                : null,
          ),
        ),
        const Padding(
          padding: EdgeInsets.all(24),
          child: Text(
            '手机端独立会话\nAI 的记忆、工具和自主决策继续由服务器运行',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 12, color: muted, height: 1.8),
          ),
        ),
      ],
    ),
  );
  Widget _contacts() => ListView(
    children: [
      const Padding(
        padding: EdgeInsets.all(20),
        child: Text('我的 AI', style: TextStyle(color: muted)),
      ),
      Material(
        color: Colors.white,
        child: ListTile(
          leading: const Avatar(),
          title: Text(state.connection?.name ?? 'DeepLive'),
          subtitle: const Text('会话 AI · 长期记忆 / 工具'),
          trailing: const Icon(Icons.chevron_right),
          onTap: openChat,
        ),
      ),
    ],
  );
  Widget _profile() => ListView(
    padding: const EdgeInsets.all(20),
    children: [
      const SizedBox(height: 20),
      const Center(child: Avatar(size: 76)),
      const SizedBox(height: 14),
      const Text(
        'DeepLive',
        textAlign: TextAlign.center,
        style: TextStyle(fontSize: 24, fontWeight: FontWeight.w700),
      ),
      const Text(
        '让 AI 陪在身边',
        textAlign: TextAlign.center,
        style: TextStyle(color: muted),
      ),
      const SizedBox(height: 32),
      Card(
        child: Column(
          children: [
            ListTile(
              leading: const Icon(Icons.dns_outlined),
              title: const Text('服务器连接'),
              subtitle: Text(state.connection?.url ?? '尚未配置'),
              trailing: const Icon(Icons.chevron_right),
              onTap: settings,
            ),
            const Divider(height: 1),
            ListTile(
              leading: const Icon(Icons.shield_outlined),
              title: const Text('输入兼容'),
              subtitle: const Text('CWS 原生输入方案 · 回车换行 · 按钮发送'),
            ),
          ],
        ),
      ),
      const SizedBox(height: 20),
      const Text(
        '0.1.1 · 独立会话版\n当前支持文字对话及前台未读提醒\n后台通知、图片和语音尚未接入',
        textAlign: TextAlign.center,
        style: TextStyle(color: muted, fontSize: 12, height: 1.8),
      ),
    ],
  );
}

class Avatar extends StatelessWidget {
  const Avatar({super.key, this.size = 48, this.mine = false});
  final double size;
  final bool mine;
  @override
  Widget build(BuildContext context) => Container(
    width: size,
    height: size,
    decoration: BoxDecoration(
      borderRadius: BorderRadius.circular(size * .32),
      gradient: LinearGradient(
        begin: Alignment.topLeft,
        end: Alignment.bottomRight,
        colors: mine
            ? [const Color(0xFF8FAAFF), const Color(0xFF697BEA)]
            : [const Color(0xFF56CCFF), blue],
      ),
    ),
    child: Icon(
      mine ? Icons.person_rounded : Icons.auto_awesome_rounded,
      color: Colors.white,
      size: size * .53,
    ),
  );
}

class ConversationPage extends StatefulWidget {
  const ConversationPage({super.key, required this.state});
  final ChatState state;
  @override
  State<ConversationPage> createState() => _ConversationPageState();
}

class _ConversationPageState extends State<ConversationPage> {
  final scroll = ScrollController();
  String? lastKey;
  @override
  void initState() {
    super.initState();
    widget.state.addListener(changed);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && scroll.hasClients) {
        scroll.jumpTo(scroll.position.maxScrollExtent);
      }
    });
  }

  void changed() {
    final messages = widget.state.messages;
    if (messages.isEmpty || messages.last.key == lastKey) return;
    lastKey = messages.last.key;
    final nearBottom = !scroll.hasClients || scroll.position.extentAfter < 120;
    if (nearBottom) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && scroll.hasClients) {
          scroll.animateTo(
            scroll.position.maxScrollExtent,
            duration: const Duration(milliseconds: 200),
            curve: Curves.easeOut,
          );
        }
      });
    }
  }

  @override
  void dispose() {
    widget.state.removeListener(changed);
    scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: widget.state,
    builder: (context, _) {
      final state = widget.state;
      return Scaffold(
        resizeToAvoidBottomInset: true,
        appBar: AppBar(
          title: Column(
            children: [
              Text(
                state.connection?.name ?? 'DeepLive',
                style: const TextStyle(
                  fontSize: 17,
                  fontWeight: FontWeight.w600,
                ),
              ),
              Text(
                state.online ? '已连接' : '连接中断',
                style: TextStyle(
                  fontSize: 11,
                  color: state.online ? const Color(0xFF3AB888) : muted,
                ),
              ),
            ],
          ),
          centerTitle: true,
          actions: [
            IconButton(
              onPressed: state.refresh,
              tooltip: '刷新消息',
              icon: const Icon(Icons.refresh_rounded),
            ),
          ],
        ),
        body: Column(
          children: [
            if (state.error != null) _error(state.error!, state.refresh),
            Expanded(
              child: state.messages.isEmpty
                  ? const Center(
                      child: Text('发一句话，开始聊天', style: TextStyle(color: muted)),
                    )
                  : ListView.builder(
                      controller: scroll,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 14,
                        vertical: 16,
                      ),
                      itemCount: state.messages.length,
                      itemBuilder: (context, index) {
                        final m = state.messages[index];
                        return Column(
                          children: [
                            if (index == 0 ||
                                m.ts - state.messages[index - 1].ts > 300)
                              Padding(
                                padding: const EdgeInsets.only(
                                  top: 8,
                                  bottom: 20,
                                ),
                                child: Text(
                                  timeLabel(m.ts, full: true),
                                  style: const TextStyle(
                                    color: muted,
                                    fontSize: 11,
                                  ),
                                ),
                              ),
                            _bubble(m),
                          ],
                        );
                      },
                    ),
            ),
            NativeComposer(
              onSend: (text) async {
                final sent = await state.send(text);
                if (context.mounted && !sent) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(content: Text(state.error ?? '发送失败，消息已保留')),
                  );
                }
                return sent;
              },
            ),
          ],
        ),
      );
    },
  );
  Widget _bubble(ChatMessage m) => Padding(
    padding: const EdgeInsets.only(bottom: 20),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      textDirection: m.isMine ? TextDirection.rtl : TextDirection.ltr,
      children: [
        Avatar(size: 36, mine: m.isMine),
        const SizedBox(width: 10),
        Flexible(
          child: Column(
            crossAxisAlignment: m.isMine
                ? CrossAxisAlignment.end
                : CrossAxisAlignment.start,
            children: [
              if (!m.isMine && !m.isAi)
                Padding(
                  padding: const EdgeInsets.only(bottom: 4),
                  child: Text(
                    m.name.isEmpty ? '系统' : m.name,
                    style: const TextStyle(fontSize: 11, color: muted),
                  ),
                ),
              Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: 11,
                ),
                decoration: BoxDecoration(
                  color: m.isMine ? blue : Colors.white,
                  borderRadius: BorderRadius.only(
                    topLeft: Radius.circular(m.isMine ? 16 : 4),
                    topRight: Radius.circular(m.isMine ? 4 : 16),
                    bottomLeft: const Radius.circular(16),
                    bottomRight: const Radius.circular(16),
                  ),
                ),
                child: SelectableText(
                  m.text,
                  style: TextStyle(
                    fontSize: 16,
                    height: 1.5,
                    color: m.isMine ? Colors.white : ink,
                  ),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(width: 30),
      ],
    ),
  );
}

class ConnectionPage extends StatefulWidget {
  const ConnectionPage({super.key, this.initial});
  final Connection? initial;
  @override
  State<ConnectionPage> createState() => _ConnectionPageState();
}

class _ConnectionPageState extends State<ConnectionPage> {
  late final url = TextEditingController(text: widget.initial?.url ?? '');
  late final token = TextEditingController(text: widget.initial?.token ?? '');
  late final name = TextEditingController(
    text: widget.initial?.name ?? 'DeepLive',
  );
  bool busy = false, hide = true;
  String? error;
  @override
  void dispose() {
    url.dispose();
    token.dispose();
    name.dispose();
    super.dispose();
  }

  Future<void> save() async {
    setState(() {
      busy = true;
      error = null;
    });
    var connection = Connection(
      url: url.text.trim(),
      token: token.text.trim(),
      name: name.text.trim().isEmpty ? 'DeepLive' : name.text.trim(),
    );
    final api = ChatApi(connection);
    try {
      if (!connection.token.startsWith('dlapp_')) {
        connection = await api.provision();
      }
      final sessionApi = ChatApi(connection);
      try {
        await sessionApi.history();
      } finally {
        sessionApi.close();
      }
      await nativeChannel.invokeMethod('configSave', {
        'value': jsonEncode(connection.toJson()),
      });
      if (mounted) Navigator.pop(context, connection);
    } catch (e) {
      if (mounted) setState(() => error = friendlyError(e));
    } finally {
      api.close();
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('连接 DeepLive')),
    body: ListView(
      padding: const EdgeInsets.all(24),
      children: [
        const Center(child: Avatar(size: 72)),
        const SizedBox(height: 24),
        const Text(
          '把 AI 装进口袋',
          style: TextStyle(fontSize: 25, fontWeight: FontWeight.w700),
        ),
        const SizedBox(height: 8),
        const Text(
          '连接当前框架，建立独立的手机会话',
          style: TextStyle(color: muted, height: 1.6),
        ),
        const SizedBox(height: 28),
        TextField(
          controller: url,
          keyboardType: TextInputType.url,
          autocorrect: false,
          decoration: const InputDecoration(
            labelText: '服务器地址',
            hintText: 'https://ai.example.com',
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 18),
        TextField(
          controller: token,
          obscureText: hide,
          autocorrect: false,
          enableSuggestions: false,
          decoration: InputDecoration(
            labelText: '连接凭证',
            helperText: '首次填写管理 token 开通；之后使用独立会话凭证',
            border: const OutlineInputBorder(),
            suffixIcon: IconButton(
              onPressed: () => setState(() => hide = !hide),
              icon: Icon(
                hide
                    ? Icons.visibility_off_outlined
                    : Icons.visibility_outlined,
              ),
            ),
          ),
        ),
        const SizedBox(height: 18),
        TextField(
          controller: name,
          decoration: const InputDecoration(
            labelText: 'AI 显示名称',
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 20),
        if (error != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 16),
            child: Text(error!, style: const TextStyle(color: Colors.red)),
          ),
        FilledButton(
          onPressed: busy ? null : save,
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Text(busy ? '正在验证连接…' : '连接并保存'),
          ),
        ),
        const SizedBox(height: 20),
        const Text(
          '连接设置使用 Android Keystore 加密保存在本机\n建议使用 HTTPS；HTTP 仅用于可信内网\n手机上的 127.0.0.1 是手机本身，请填服务器可达地址\n首次开通后仅保存会话凭证，不保留管理 token\n会话相互隔离，后台主 AI 不在聊天界面展示',
          style: TextStyle(fontSize: 12, height: 1.8, color: muted),
        ),
      ],
    ),
  );
}

Widget _error(String text, Future<void> Function() retry) => Material(
  color: const Color(0xFFFFF1EE),
  child: ListTile(
    dense: true,
    leading: const Icon(
      Icons.wifi_off_rounded,
      color: Colors.deepOrange,
      size: 20,
    ),
    title: Text(text, style: const TextStyle(fontSize: 12)),
    trailing: TextButton(onPressed: retry, child: const Text('重试')),
  ),
);

String timeLabel(double ts, {bool full = false}) {
  final date = DateTime.fromMillisecondsSinceEpoch((ts * 1000).round())
      .toLocal();
  final now = DateTime.now();
  final time =
      '${date.hour.toString().padLeft(2, '0')}:${date.minute.toString().padLeft(2, '0')}';
  if (date.year == now.year && date.month == now.month && date.day == now.day) {
    return full ? '今天 $time' : time;
  }
  return '${date.month}月${date.day}日${full ? ' $time' : ''}';
}
