"""
【ai_runtime.py 补丁 — 多轮对话 tool_use 历史保留修复】

根因：存 AI 回复时只存了 text 字段，丢失了含 tool_use block 的 raw_content；
      回放 assistant 消息时用纯文本 content，模型看不到自己曾调用过工具的历史，
      导致越来越倾向直接输出文字而不调用 send_message。

参考正确写法：core/dev_agent.py（run_dev_agent 函数，约 280-310 行）

需要在 ai_runtime.py 中定位并修改两处代码。

==============================================================================
【第一处】存储 AI 回复到 repository
==============================================================================

搜索关键词：在 AIOrchestrator 中搜索 "append_message" 和 "reply.text" 同时出现的位置。
通常是一个存 assistant 消息的 dict，形如：

  # ── 修改前（有问题的写法）──
  self.repository.append_message(scope_type, scope_id, {
      'role': 'assistant',
      'text': reply.text,           # 只存了纯文本
      'timestamp': ...,
      ...
  }, limit)

  # ── 修改后（正确写法）──
  self.repository.append_message(scope_type, scope_id, {
      'role': 'assistant',
      'text': reply.text,
      'raw_content': reply.raw_content,   # ← 新增：保存完整 block 列表（含 tool_use）
      'timestamp': ...,
      ...
  }, limit)

紧接在存 assistant 消息之后，如果代码有处理工具调用结果（tool_results）的循环，
需要确认 tool_result 消息也被存入 repository，格式如下：

  # ── tool_result 存储（如果当前缺失，需要新增）──
  for call, result_text in zip(reply.tool_calls, tool_results):
      self.repository.append_message(scope_type, scope_id, {
          'role': 'tool_result',          # 用专用 role 区分
          'tool_use_id': call.call_id,
          'content': result_text,
          'timestamp': time.time(),
      }, limit)

注意：如果当前代码根本没有存 tool_result 到 repository（因为觉得多轮时不需要），
那必须补上这段，否则回放时 tool_use 和 tool_result 会不配对，Anthropic API 报错。

==============================================================================
【第二处】从 list_messages() 取历史、构建发给模型的 messages 列表
==============================================================================

搜索关键词：在 AIOrchestrator 中搜索 "list_messages" 与 "messages.append" 同时出现的位置。
通常是一个循环，遍历历史消息并重建 messages 列表。

  # ── 修改前（有问题的写法）──
  for msg in self.repository.list_messages(scope_type, scope_id):
      role = msg.get('role')
      if role == 'user':
          messages.append({'role': 'user', 'content': msg.get('text', '')})
      elif role == 'assistant':
          text = msg.get('text', '')
          if text:
              messages.append({'role': 'assistant', 'content': text})  # 丢失 tool_use 历史

  # ── 修改后（正确写法）──
  pending_tool_results: list[dict] = []
  for msg in self.repository.list_messages(scope_type, scope_id):
      role = msg.get('role')

      if role == 'tool_result':
          # 收集 tool_result，待合并为一条 user 消息
          pending_tool_results.append({
              'type': 'tool_result',
              'tool_use_id': msg.get('tool_use_id', ''),
              'content': msg.get('content', ''),
          })
          continue

      # 在切换到非 tool_result 消息前，先把积累的 tool_results 作为一条 user 消息刷出
      if pending_tool_results:
          messages.append({'role': 'user', 'content': pending_tool_results})
          pending_tool_results = []

      if role == 'user':
          text = msg.get('text', '')
          if text:
              messages.append({'role': 'user', 'content': text})
      elif role == 'assistant':
          raw = msg.get('raw_content')
          if raw:
              # 优先用 raw_content 还原（含 tool_use block），让模型看到完整调用历史
              messages.append({'role': 'assistant', 'content': raw})
          else:
              # 兜底：旧数据没有 raw_content，回退到纯文本
              text = msg.get('text', '')
              if text:
                  messages.append({'role': 'assistant', 'content': [{'type': 'text', 'text': text}]})

  # 循环结束后别忘了刷剩余的 tool_results
  if pending_tool_results:
      messages.append({'role': 'user', 'content': pending_tool_results})

==============================================================================
【应用说明】
==============================================================================

1. 在 ai_runtime.py 中搜索上述关键词，定位两处代码，按照"修改后"版本替换。
2. 两处必须同时改，单独改其中一处会导致 tool_use / tool_result 不配对的 API 报错。
3. 改动最小化原则：只改这两处，不动其他逻辑。
4. 改完后建议清空一个测试 scope 的历史（/clear 或直接清 data/）再测，
   避免旧格式历史消息与新格式混合引发 API 错误。

参考：core/dev_agent.py run_dev_agent 函数中的正确实现
      pack/anthropic_chat_model.py AnthropicReply.raw_content 字段定义
"""
