// Adapted from CWS ChatPage: Flutter draws the shell, Android EditText owns IME.
// Do not replace with a normal editable Flutter TextField or a PlatformView.
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

const nativeChannel = MethodChannel('cn.loliyc.deeplive_mobile/app');

class NativeComposer extends StatefulWidget {
  const NativeComposer({super.key, required this.onSend, this.enabled = true});
  final Future<bool> Function(String text) onSend;
  final bool enabled;
  @override
  State<NativeComposer> createState() => _NativeComposerState();
}

class _NativeComposerState extends State<NativeComposer>
    with WidgetsBindingObserver {
  final _controller = TextEditingController();
  final _fieldKey = GlobalKey();
  bool _native = false, _sending = false, _frameSync = false;
  int? _frameId;
  Rect _last = Rect.zero;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    nativeChannel.setMethodCallHandler((call) async {
      if (!mounted) return;
      switch (call.method) {
        case 'nativeEditChanged':
          _changed(call.arguments as String);
        case 'nativeEditSend':
          await _send();
        case 'nativeEditBlur':
          _detach();
      }
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) _detach();
  }

  void _changed(String text) {
    if (!mounted || _controller.text == text) return;
    setState(() => _controller.text = text);
  }

  Rect? _rect() {
    final context = _fieldKey.currentContext;
    final box = context?.findRenderObject();
    if (context == null || box is! RenderBox || !box.hasSize) return null;
    final dpr = View.of(context).devicePixelRatio;
    final tl = box.localToGlobal(Offset.zero),
        br = box.localToGlobal(box.size.bottomRight(Offset.zero));
    return Rect.fromLTRB(tl.dx * dpr, tl.dy * dpr, br.dx * dpr, br.dy * dpr);
  }

  Map<String, double> _coords(Rect r) => {
    'x': r.left,
    'y': r.top,
    'w': r.width,
    'h': r.height,
  };
  Future<void> _attach() async {
    if (_native || !widget.enabled || !Platform.isAndroid) return;
    final r = _rect();
    if (r == null) return;
    setState(() => _native = true);
    _last = r;
    _frameSync = true;
    _frameId = WidgetsBinding.instance.scheduleFrameCallback(_frame);
    try {
      await nativeChannel.invokeMethod('nativeEditShow', {
        ..._coords(r),
        'text': _controller.text,
        'hint': '发消息…',
        'font': 16.0,
        'ink': const Color(0xFF202939).toARGB32(),
        'dim': const Color(0xFF9AA5B5).toARGB32(),
        'accent': const Color(0xFF1687FF).toARGB32(),
      });
    } catch (_) {
      _detach();
    }
  }

  void _frame(Duration _) {
    if (!mounted || !_frameSync || !_native) return;
    final r = _rect();
    if (r != null &&
        (r.topLeft - _last.topLeft).distance +
                (r.bottomRight - _last.bottomRight).distance >
            .5) {
      _last = r;
      nativeChannel.invokeMethod('nativeEditMove', _coords(r));
    }
    // Same cancellable frame chain as CWS: idle frames do not force rendering.
    _frameId = WidgetsBinding.instance.scheduleFrameCallback(_frame);
  }

  void _detach() {
    _frameSync = false;
    if (_frameId != null) {
      WidgetsBinding.instance.cancelFrameCallbackWithId(_frameId!);
    }
    _frameId = null;
    if (_native) {
      _native = false;
      nativeChannel.invokeMethod('nativeEditHide');
      if (mounted) setState(() {});
    }
  }

  Future<void> _send() async {
    if (_sending || !widget.enabled) return;
    // Pull native truth before sending; IME echo may lag by one frame.
    if (_native) {
      final text = await nativeChannel.invokeMethod<String>(
        'nativeEditGetText',
      );
      if (!mounted) return;
      if (text != null) _changed(text);
    }
    final text = _controller.text.trim();
    if (text.isEmpty) return;
    setState(() => _sending = true);
    final accepted = await widget.onSend(text);
    if (!mounted) return;
    // Preserve changes typed while the network request was in flight.
    if (accepted && _controller.text.trim() == text) {
      _controller.clear();
      if (_native) {
        await nativeChannel.invokeMethod('nativeEditSetText', {'text': ''});
      }
    }
    if (mounted) setState(() => _sending = false);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _frameSync = false;
    if (_frameId != null) {
      WidgetsBinding.instance.cancelFrameCallbackWithId(_frameId!);
    }
    if (_native) nativeChannel.invokeMethod('nativeEditHide');
    nativeChannel.setMethodCallHandler(null);
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ColoredBox(
    color: const Color(0xFFF6F7FA),
    child: SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                decoration: BoxDecoration(
                  color: Colors.white,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: GestureDetector(
                  behavior: HitTestBehavior.opaque,
                  onTap: _attach,
                  child: IgnorePointer(
                    ignoring: Platform.isAndroid,
                    child: TextField(
                      key: _fieldKey,
                      controller: _controller,
                      readOnly: Platform.isAndroid,
                      canRequestFocus: !Platform.isAndroid,
                      minLines: 1,
                      maxLines: 4,
                      onChanged: (_) => setState(() {}),
                      style: TextStyle(
                        fontSize: 16,
                        color: _native
                            ? Colors.transparent
                            : const Color(0xFF202939),
                      ),
                      decoration: InputDecoration(
                        border: InputBorder.none,
                        hintText: '发消息…',
                        hintStyle: TextStyle(
                          color: _native
                              ? Colors.transparent
                              : const Color(0xFF9AA5B5),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 10),
            SizedBox(
              height: 48,
              child: FilledButton(
                onPressed:
                    !widget.enabled ||
                        _sending ||
                        _controller.text.trim().isEmpty
                    ? null
                    : _send,
                style: FilledButton.styleFrom(
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                ),
                child: _sending
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('发送'),
              ),
            ),
          ],
        ),
      ),
    ),
  );
}
