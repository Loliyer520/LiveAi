package cn.loliyc.deeplive_mobile

import android.content.Context
import android.os.Build
import android.widget.EditText
import android.widget.FrameLayout
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

// Native text overlay adapted from /tmp/code/cws-client (2026-11-12).
class MainActivity : FlutterActivity() {
    private var flutter: MethodChannel? = null
    private var _nativeEditView: EditText? = null
    private var _nativeSelf = false
    // ---- 聊天输入框原生像素覆盖 ----
    // ⚠️ 不要"优化"回纯 Flutter TextField：Flutter 自绘输入域的 IME 桥在部分 ROM
    // 上丢 updateExtractedText 回声，输入法独立提取框偶发失同步；原生 EditText 走
    // TextView 完整契约必然同步。也不要改成 PlatformView 混编（引擎 hybrid
    // composition 在这类 ROM 上有渲染玄学）。本覆盖方案=卡片 Flutter 画、文字面
    // 原生钉像素，是两全的唯一路径（2026-10-04 实测定案）。
    private fun ensureNativeEdit(): EditText {
        _nativeEditView?.let { return it }
        val v = EditText(this).apply {
            background = null
            setPadding(0, 0, 0, 0)
            setIncludeFontPadding(false)
            gravity = android.view.Gravity.CENTER_VERTICAL or android.view.Gravity.START
            // MULTI_LINE 必须带上：缺了它 IME 把回车键渲染成"发送"；
            // 相应地 imeOptions 绝不能再给 IME_ACTION_SEND（换行键会被劫成发送键，
            // 用户 1.9.40 实测踩过）。发送只走 UI 右下角按钮 + Ctrl+Enter
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES or
                android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE
            imeOptions = android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI or
                android.view.inputmethod.EditorInfo.IME_FLAG_NO_FULLSCREEN
            maxLines = 4
            // 硬件键盘 Ctrl+Enter 发送（软键盘没有组合键概念，回车就是换行）
            setOnKeyListener { _, keyCode, ev ->
                if (!_nativeSelf && keyCode == android.view.KeyEvent.KEYCODE_ENTER &&
                    ev.action == android.view.KeyEvent.ACTION_DOWN && ev.isCtrlPressed) {
                    flutter?.invokeMethod("nativeEditSend", null)
                    true
                } else false
            }
            addTextChangedListener(object : android.text.TextWatcher {
                override fun afterTextChanged(s: android.text.Editable?) {
                    if (_nativeSelf) return
                    flutter?.invokeMethod("nativeEditChanged", s?.toString() ?: "")
                }
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            })
            setOnFocusChangeListener { _, has ->
                if (!_nativeSelf && !has) flutter?.invokeMethod("nativeEditBlur", null)
            }
        }
        _nativeEditView = v
        return v
    }

    // 每次显示刷一遍样式/内容（主题色随行；字号传的是已含 uiScale 的最终 sp 值）
    private fun applyNativeEdit(v: EditText, call: MethodCall) {
        call.argument<Number>("ink")?.let { v.setTextColor(it.toInt()) }
        call.argument<Number>("dim")?.let { v.setHintTextColor(it.toInt()) }
        if (Build.VERSION.SDK_INT >= 29) {
            call.argument<Number>("accent")?.let {
                v.setTextCursorDrawable(android.graphics.drawable.ColorDrawable(it.toInt()))
            }
        }
        v.hint = call.argument<String>("hint") ?: ""
        v.setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP,
            call.argument<Number>("font")?.toFloat() ?: 14.5f)
        val t = call.argument<String>("text") ?: ""
        _nativeSelf = true
        v.setText(t)
        v.setSelection(t.length)
        _nativeSelf = false
    }

    // 像素定位：钉进内容层（与 FlutterView 同层、在其上），坐标是 FlutterView
    // 左上原点系的物理像素
    private fun moveNativeEdit(v: EditText, call: MethodCall) {
        val lp = v.layoutParams as? FrameLayout.LayoutParams
            ?: FrameLayout.LayoutParams(0, 0)
        lp.leftMargin = call.argument<Number>("x")?.toInt() ?: 0
        lp.topMargin = call.argument<Number>("y")?.toInt() ?: 0
        lp.width = call.argument<Number>("w")?.toInt() ?: 0
        // 高度留 4dp 余量：原生/Flutter 字体计量有细微差，防四行时顶到裁字
        lp.height = (call.argument<Number>("h")?.toInt() ?: 0) +
            (4 * resources.displayMetrics.density).toInt()
        v.layoutParams = lp
    }

    private fun hideNativeEdit() {
        val v = _nativeEditView ?: return
        _nativeSelf = true
        try {
            val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as android.view.inputmethod.InputMethodManager
            imm.hideSoftInputFromWindow(v.windowToken, 0)
        } catch (_: Exception) {}
        try { (v.parent as? android.view.ViewGroup)?.removeView(v) } catch (_: Exception) {}
        _nativeSelf = false
        _nativeEditView = null
    }


    private fun configKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey("deeplive.config", null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder("deeplive.config", KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    private fun saveConfig(text: String) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, configKey())
        val encrypted = cipher.doFinal(text.toByteArray(Charsets.UTF_8))
        check(getSharedPreferences("connection", MODE_PRIVATE).edit()
            .putString("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .putString("data", Base64.encodeToString(encrypted, Base64.NO_WRAP)).commit())
    }
    private fun loadConfig(): String? {
        val prefs = getSharedPreferences("connection", MODE_PRIVATE)
        val data = prefs.getString("data", null) ?: return null
        val iv = prefs.getString("iv", null) ?: return null
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, configKey(), GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)))
        return String(cipher.doFinal(Base64.decode(data, Base64.NO_WRAP)), Charsets.UTF_8)
    }
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        flutter = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "cn.loliyc.deeplive_mobile/app")
        flutter!!.setMethodCallHandler { call, result ->
            try {
            when (call.method) {
                "configLoad" -> result.success(loadConfig())
                "configSave" -> { saveConfig(call.argument<String>("value") ?: "{}"); result.success(true) }
                "nativeEditShow" -> {
                    val v = ensureNativeEdit()
                    applyNativeEdit(v, call)
                    if (v.parent == null) {
                        findViewById<FrameLayout>(android.R.id.content).addView(v,
                            FrameLayout.LayoutParams(0, 0))
                    }
                    moveNativeEdit(v, call)
                    v.requestFocus()
                    val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as android.view.inputmethod.InputMethodManager
                    imm.showSoftInput(v, 0)
                    result.success(true)
                }
                "nativeEditMove" -> {
                    _nativeEditView?.let { moveNativeEdit(it, call) }
                    result.success(true)
                }
                "nativeEditHide" -> { hideNativeEdit(); result.success(true) }
                "nativeEditSetText" -> {
                    _nativeEditView?.let { v ->
                        val t = call.argument<String>("text") ?: ""
                        _nativeSelf = true
                        v.setText(t)
                        v.setSelection(t.length)
                        _nativeSelf = false
                    }
                    result.success(true)
                }
                "nativeEditInsert" -> {
                    _nativeEditView?.let { v ->
                        val t = call.argument<String>("text") ?: ""
                        val st = v.selectionStart
                        val en = v.selectionEnd
                        val at = if (st >= 0 && en >= 0) minOf(st, en) else v.text.length
                        _nativeSelf = true
                        if (st >= 0 && en >= 0) v.text.replace(minOf(st, en), maxOf(st, en), t)
                        else v.append(t)
                        v.setSelection(at + t.length)
                        _nativeSelf = false
                        // 程序化改动不触发 watcher：手动把结果镜像回 Dart
                        flutter?.invokeMethod("nativeEditChanged", v.text.toString())
                    }
                    result.success(true)
                }
                // Dart 侧拉真文本：changed 事件偶发丢失时发送前兜底对齐
                "nativeEditGetText" -> result.success(_nativeEditView?.text?.toString())
                else -> result.notImplemented()
            }
            } catch (e: Exception) { result.error("native_error", e.message, null) }
        }
    }
    override fun onDestroy() {
        hideNativeEdit()
        flutter?.setMethodCallHandler(null)
        flutter = null
        super.onDestroy()
    }
}
