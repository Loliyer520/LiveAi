# 诊断脚本

这里存放需要人工执行、可能访问真实网络或真实 provider 的诊断脚本。

规则：

- 不把这类脚本放进 `test/`
- 不使用 `test_*.py` 命名
- 默认不纳入 `unittest discover`
- 运行前确认本地 `data/models_config.json` 已配置好目标上游

当前脚本：

- `all_channels.py`：逐个探测 `models_config.json` 中各 channel/model 是否可用
- `aipai_opus.py`：直接走原始 HTTP 请求测试 `aipai`
- `aipai_chat_model.py`：通过 `AnthropicChatModel` 诊断 `aipai`
- `aipai_via_class.py`：捕获请求与响应细节，定位 `aipai` 类封装问题
- `kiro_opus.py`：诊断 `kiro`
- `kirof5_opus.py`：诊断 `kirof5`
