"""
【ai_runtime.py 补丁 — _handle_model_command 方法替换】

将此方法完整替换 ai_runtime.py 中 AIOrchestrator 类的 _handle_model_command 方法。
原方法位于 _is_admin_message 与 _is_master_message 之间（约第330-345行）。

新方法兼容原有 /model 和 /model <模型名> 行为，并扩展以下管理员子命令：
  /model add <channel_name> <base_url> <api_key>
  /model add-model <channel_name> <model_name> <model_id>
  /model remove <channel_name>
  /model remove-model <channel_name> <model_name>
  /model list
  /model verify <channel_name>
  /model set-default <channel_name> <model_name>
"""

# ── 替换以下整个方法 ──────────────────────────────────────────

    async def _handle_model_command(self, message: ChatMessage, cleaned: str):
        if not self._is_admin_message(message):
            self.bot.send_text(message.chat_type, message.chat_id, '这个指令你先别动。')
            return

        parts = cleaned.split()

        # /model（无参数）— 列出所有模型（原行为）
        if len(parts) == 1:
            list_text = self.model_manager.list_models()
            self.bot.send_text(message.chat_type, message.chat_id, list_text)
            return

        subcmd = parts[1].lower()

        # ── 子命令：/model list ──
        if subcmd == 'list':
            text = self.model_manager.list_channels_detail()
            self.bot.send_text(message.chat_type, message.chat_id, text)
            return

        # ── 子命令：/model add <channel_name> <base_url> <api_key> ──
        if subcmd == 'add':
            if len(parts) < 5:
                self.bot.send_text(message.chat_type, message.chat_id,
                                   '用法: /model add <渠道名> <base_url> <api_key>')
                return
            name, base_url, api_key = parts[2], parts[3], parts[4]
            try:
                self.model_manager.add_channel(name, base_url, api_key)
                self.model_manager.reload_config()
                current = self.model_manager.get_current_model()
                if current:
                    self._update_model_from_config(current)
                self.bot.send_text(message.chat_type, message.chat_id, f'渠道 {name} 已添加。')
            except Exception as e:
                self.bot.send_text(message.chat_type, message.chat_id, f'添加失败: {e}')
            return

        # ── 子命令：/model add-model <channel_name> <model_name> <model_id> ──
        if subcmd == 'add-model':
            if len(parts) < 5:
                self.bot.send_text(message.chat_type, message.chat_id,
                                   '用法: /model add-model <渠道名> <模型显示名> <模型ID>')
                return
            ch_name, model_name, model_id = parts[2], parts[3], parts[4]
            try:
                self.model_manager.add_model_to_channel(ch_name, model_name, model_id)
                self.model_manager.reload_config()
                current = self.model_manager.get_current_model()
                if current:
                    self._update_model_from_config(current)
                self.bot.send_text(message.chat_type, message.chat_id,
                                   f'模型 {model_name}({model_id}) 已添加到渠道 {ch_name}。')
            except Exception as e:
                self.bot.send_text(message.chat_type, message.chat_id, f'添加失败: {e}')
            return

        # ── 子命令：/model remove <channel_name> ──
        if subcmd == 'remove':
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '用法: /model remove <渠道名>')
                return
            name = parts[2]
            try:
                self.model_manager.remove_channel(name)
                self.model_manager.reload_config()
                current = self.model_manager.get_current_model()
                if current:
                    self._update_model_from_config(current)
                self.bot.send_text(message.chat_type, message.chat_id, f'渠道 {name} 已删除。')
            except Exception as e:
                self.bot.send_text(message.chat_type, message.chat_id, f'删除失败: {e}')
            return

        # ── 子命令：/model remove-model <channel_name> <model_name> ──
        if subcmd == 'remove-model':
            if len(parts) < 4:
                self.bot.send_text(message.chat_type, message.chat_id,
                                   '用法: /model remove-model <渠道名> <模型显示名>')
                return
            ch_name, model_name = parts[2], parts[3]
            try:
                self.model_manager.remove_model_from_channel(ch_name, model_name)
                self.model_manager.reload_config()
                current = self.model_manager.get_current_model()
                if current:
                    self._update_model_from_config(current)
                self.bot.send_text(message.chat_type, message.chat_id,
                                   f'模型 {model_name} 已从渠道 {ch_name} 移除。')
            except Exception as e:
                self.bot.send_text(message.chat_type, message.chat_id, f'移除失败: {e}')
            return

        # ── 子命令：/model verify <channel_name> ──
        if subcmd == 'verify':
            if len(parts) < 3:
                self.bot.send_text(message.chat_type, message.chat_id, '用法: /model verify <渠道名>')
                return
            name = parts[2]
            self.bot.send_text(message.chat_type, message.chat_id, f'正在验证渠道 {name} …')
            try:
                ok, msg = await asyncio.to_thread(self.model_manager.verify_channel, name)
                self.bot.send_text(message.chat_type, message.chat_id, msg)
            except Exception as e:
                self.bot.send_text(message.chat_type, message.chat_id, f'验证异常: {e}')
            return

        # ── 子命令：/model set-default <channel_name> <model_name> ──
        if subcmd == 'set-default':
            if len(parts) < 4:
                self.bot.send_text(message.chat_type, message.chat_id,
                                   '用法: /model set-default <渠道名> <模型显示名>')
                return
            ch_name, model_name = parts[2], parts[3]
            try:
                self.model_manager.set_default(ch_name, model_name)
                self.model_manager.reload_config()
                current = self.model_manager.get_current_model()
                if current:
                    self._update_model_from_config(current)
                self.bot.send_text(message.chat_type, message.chat_id,
                                   f'默认模型已设为 {ch_name}/{model_name}。')
            except Exception as e:
                self.bot.send_text(message.chat_type, message.chat_id, f'设置失败: {e}')
            return

        # ── 原逻辑：/model <模型名/序号> — 切换模型 ──
        target = parts[1]
        success, msg = self.model_manager.switch_model(target)
        if success:
            current = self.model_manager.get_current_model()
            if current:
                self._update_model_from_config(current)
        self.bot.send_text(message.chat_type, message.chat_id, msg)
