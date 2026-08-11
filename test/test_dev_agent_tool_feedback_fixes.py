"""回归测试：agent 工具反馈失真修复（P0/P1/P2 一批）。

覆盖：
1. shell 输出编码：中文/控制字符/UTF-16LE 残留不再导致输出丢失或误判失败
2. 只读模式 shell 白名单：ls/jq 等放行，重定向/管道/命令链/破坏性参数拒绝
3. 大文件分块读取：bytes 模式 UTF-8 对齐 + 精确续读 offset，lines 模式总行数/行号
4. JSON 字段级查询：$、$.a.b、[0]、[*]、负数下标
5. 模型验证错误分类：真实 HTTP 状态 + 连接阶段结构化返回
"""

import os
import shutil
import sys
import tempfile
import unittest

from core import dev_agent
from core.dev_agent import (
    DevAgentShellManager,
    _decode_shell_output,
    _format_shell_output,
    _is_read_only_shell_command,
    _query_local_file_json,
    _read_local_file_chunk,
    _sanitize_shell_text,
)
from core.model_validation_service import _classify_error

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─────────────────────────── P0: shell 输出编码 ───────────────────────────

class ShellOutputDecodeTests(unittest.TestCase):
    def test_decode_handles_utf16le_broken_bytes_without_crash(self):
        # UTF-16LE 编码的中文警告残留字节 + 正常 UTF-8 混排，decode 不得抛异常
        broken = '你好'.encode('utf-16-le') + 'ok'.encode('utf-8')
        text = _decode_shell_output(broken)
        self.assertIn('ok', text)
        # replace 后不应残留 NUL 字节
        self.assertNotIn('\x00', _sanitize_shell_text(text))

    def test_decode_none_returns_empty(self):
        self.assertEqual(_decode_shell_output(None), '')

    def test_decode_str_passthrough(self):
        self.assertEqual(_decode_shell_output('已有文本'), '已有文本')

    def test_sanitize_keeps_cjk_and_newline(self):
        text = _sanitize_shell_text('行一\n你好世界\r\n行三')
        self.assertIn('你好世界', text)
        self.assertIn('\n', text)
        self.assertIn('\r', text)

    def test_sanitize_removes_c0_controls_and_ansi(self):
        dirty = 'a\x00b\x01c\x1b[31mred'
        clean = _sanitize_shell_text(dirty)
        self.assertIn('abc', clean)
        self.assertNotIn('\x00', clean)
        self.assertNotIn('\x01', clean)
        self.assertNotIn('\x1b', clean)

    def test_format_shell_output_includes_both_streams(self):
        out = _format_shell_output('中文输出', 'warn: \x1b[31mred\x1b[0m')
        self.assertIn('[stdout]', out)
        self.assertIn('中文输出', out)
        self.assertIn('[stderr]', out)
        self.assertNotIn('\x1b', out)

    def test_format_shell_output_empty_returns_placeholder(self):
        self.assertEqual(_format_shell_output(''), '(无输出)')


# ─────────────────────────── P1: 只读 shell 白名单 ────────────────────────

class ReadOnlyShellWhitelistTests(unittest.TestCase):
    def test_allows_plain_readonly_commands(self):
        for cmd in ('ls', 'ls -la', 'cat file.txt', 'grep foo bar.txt',
                    'head -n 20 x.log', 'stat file', 'du -sh .', 'find . -name "*.py"',
                    'echo hello', 'pwd', 'wc -l file', 'diff a b', 'sort x.txt',
                    'sed -n 5,10p f.txt', 'awk "{print $1}" f.txt'):
            allowed, reason = _is_read_only_shell_command(cmd)
            self.assertTrue(allowed, f'{cmd!r} 应放行: {reason}')

    def test_allows_quoted_operators_and_chains(self):
        # 引号内的 | ; < > 是 grep 模式/参数，不是 shell 运算符
        for cmd in ("grep -E 'a|b' file", "grep 'a\\|b' file", 'grep ";" file',
                    'grep "|" file', 'grep "<" file', "echo 'a; b'",
                    # 白名单命令可用 ; / && / || / | 组合（分段支持）
                    'cat a.txt | grep foo | head -5', 'ls ; wc -l x',
                    'cat file && grep x file', 'sort a || cat b',
                    # 单 < 输入重定向只读放行
                    'cat < in.txt', 'sort < in | uniq'):
            allowed, reason = _is_read_only_shell_command(cmd)
            self.assertTrue(allowed, f'{cmd!r} 应放行: {reason}')

    def test_rejects_write_and_pipeline(self):
        for cmd in ('echo hi > f.txt', 'echo hi >> f.txt', 'echo hi >&2',
                    'a & b', 'cat x |& grep', '`ls`', '$(cat x)', 'cd ..',
                    'a; rm x', 'sed -i s/a/b/ f', 'cat a | rm -rf b'):
            allowed, _reason = _is_read_only_shell_command(cmd)
            self.assertFalse(allowed, f'{cmd!r} 应拒绝')

    def test_rejects_heredoc_and_substitution(self):
        # heredoc(<<)、进程替换 <(、子 shell ( ) 均无法做只读静态校验，一律拒绝
        for cmd in ('cat <<EOF', "cat <<'EOF'", 'wc < <(rm -rf /)', '(ls)', 'echo $((1+2))'):
            allowed, _reason = _is_read_only_shell_command(cmd)
            self.assertFalse(allowed, f'{cmd!r} 应拒绝')

    def test_rejects_outside_whitelist(self):
        allowed, reason = _is_read_only_shell_command('rm -rf .')
        self.assertFalse(allowed)
        self.assertIn('rm', reason)

    def test_rejects_find_destructive_args(self):
        for cmd in ('find . -exec rm {} \\;', 'find . -delete', 'find . -execdir echo',
                    "find . -name '*.py' -exec grep x {} \\;"):
            allowed, _reason = _is_read_only_shell_command(cmd)
            self.assertFalse(allowed, f'{cmd!r} 应拒绝')

    def test_rejects_sed_inplace(self):
        for cmd in ('sed -i s/x/y/ f.txt', "sed '-i' s/x/y/ f.txt", 'sed --in-place s/x/y/ f.txt'):
            allowed, _reason = _is_read_only_shell_command(cmd)
            self.assertFalse(allowed, f'{cmd!r} 应拒绝')

    def test_rejects_awk_function_call(self):
        allowed, _reason = _is_read_only_shell_command("awk '{print length($0)}' f")
        self.assertFalse(allowed)

    def test_empty_command_rejected(self):
        allowed, _reason = _is_read_only_shell_command('')
        self.assertFalse(allowed)


# ─────────────────────────── P1: 大文件分块读取 ───────────────────────────

class ReadLocalFileChunkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _write(self, name: str, content: str) -> str:
        path = os.path.join(self.root, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return name

    def test_bytes_mode_utf8_alignment(self):
        # “中文” 的 UTF-8 为 E4 B8 AD E6 96 87，从偏移 1 切开会落在续字节上
        name = self._write('cjk.txt', '中文测试内容\n第二行\n')
        result = _read_local_file_chunk(self.root, name, offset_bytes=1, max_bytes=64)
        self.assertIn('模式: bytes', result)
        # 对齐后应跳过续字节，从“文”字开头（E6），offset_bytes 指向合法字符边界
        self.assertIn('offset_bytes: 3', result)
        self.assertIn('文测试内容', result)
        self.assertIn('start_line: 1', result)
        self.assertIn('total_lines: 2', result)

    def test_bytes_mode_returns_precise_resume_offset(self):
        name = self._write('seq.txt', 'A' * 100 + '\n' + 'B' * 100 + '\n')
        first = _read_local_file_chunk(self.root, name, offset_bytes=0, max_bytes=60)
        # 继续读取必须用返回值里的 offset_bytes 而非猜测
        self.assertIn('read_bytes:', first)
        offset_line = next(line for line in first.splitlines() if line.startswith('offset_bytes:'))
        resume_offset = int(offset_line.split(': ', 1)[1])
        second = _read_local_file_chunk(self.root, name, offset_bytes=resume_offset, max_bytes=1000)
        self.assertIn('offset_bytes:', second)
        self.assertGreater(int(next(l for l in second.splitlines() if l.startswith('read_bytes:'))[len('read_bytes: '):]), 0)

    def test_lines_mode_reports_totals_and_line_numbers(self):
        lines = '\n'.join(f'line-{i:03d}' for i in range(1, 101)) + '\n'
        name = self._write('big.txt', lines)
        result = _read_local_file_chunk(self.root, name, start_line=10, line_count=5)
        self.assertIn('模式: lines', result)
        self.assertIn('起始行: 10', result)
        self.assertIn('结束行: 14', result)
        self.assertIn('总行数: 100', result)
        self.assertIn('line-010', result)
        self.assertNotIn('line-015', result)

    def test_offset_beyond_eof_returns_clear_message(self):
        name = self._write('small.txt', 'abc')
        result = _read_local_file_chunk(self.root, name, offset_bytes=100)
        self.assertIn('超出文件末尾', result)


# ─────────────────────────── P2: JSON 字段级查询 ───────────────────────────

class QueryLocalFileJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        payload = (
            '{"config": {"timeout": 30, "name": "测试"}, '
            '"items": [{"id": 1}, {"id": 2}, {"id": 3}]}'
        )
        with open(os.path.join(self.root, 'conf.json'), 'w', encoding='utf-8') as f:
            f.write(payload)

    def test_root_query(self):
        result = _query_local_file_json(self.root, 'conf.json', '$')
        self.assertIn('命中: 1 项', result)
        self.assertIn('"timeout": 30', result)

    def test_nested_field_query(self):
        result = _query_local_file_json(self.root, 'conf.json', '$.config.timeout')
        self.assertIn('$.config.timeout = 30', result)
        # 只需读字段，不应把整个文件都灌进上下文
        self.assertNotIn('items', result)

    def test_index_query(self):
        result = _query_local_file_json(self.root, 'conf.json', '$.items[0].id')
        self.assertIn('$.items[0].id = 1', result)

    def test_negative_index_query(self):
        result = _query_local_file_json(self.root, 'conf.json', '$.items[-1].id')
        self.assertIn('命中: 1 项', result)
        # 负数下标命中末元素；路径按归一化后的真实索引展示
        self.assertIn('$.items[2].id = 3', result)

    def test_wildcard_query(self):
        result = _query_local_file_json(self.root, 'conf.json', '$.items[*].id')
        self.assertIn('命中: 3 项', result)
        self.assertIn('$.items[0].id = 1', result)
        self.assertIn('$.items[2].id = 3', result)

    def test_missing_path_reports_no_hit(self):
        result = _query_local_file_json(self.root, 'conf.json', '$.nope.deep')
        self.assertIn('无命中', result)

    def test_invalid_json_reports_parse_error(self):
        with open(os.path.join(self.root, 'bad.json'), 'w') as f:
            f.write('{not json')
        result = _query_local_file_json(self.root, 'bad.json', '$')
        self.assertIn('不是合法 JSON', result)

    def test_invalid_query_reports_error(self):
        result = _query_local_file_json(self.root, 'conf.json', '$.items[abc]')
        self.assertIn('查询表达式无效', result)


# ─────────────────────────── P1: 模型验证错误分类 ──────────────────────────

class ClassifyErrorTests(unittest.TestCase):
    def _assert(self, exc, category, status=None, stage=None):
        result = _classify_error(exc)
        self.assertEqual(result['category'], category)
        self.assertEqual(result['http_status'], status)
        self.assertEqual(result['stage'], stage)
        self.assertEqual(set(result), {'category', 'http_status', 'stage'})

    def test_http_502_classified_as_server_error(self):
        self._assert(RuntimeError('anthropic request failed status=502 body=err'),
                     'server_error', 502, 'response')

    def test_http_401_classified_as_unauthorized(self):
        self._assert(RuntimeError('status=401 invalid api key'),
                     'unauthorized', 401, 'response')

    def test_http_429_classified_as_rate_limited(self):
        self._assert(RuntimeError('status=429 rate limited'),
                     'rate_limited', 429, 'response')

    def test_connection_reset_no_status(self):
        self._assert(ConnectionError('Connection reset by peer'),
                     'connection_reset', None, 'read')

    def test_connect_timeout_message(self):
        self._assert(TimeoutError('timed out while connecting to host'),
                     'connect_timeout', None, 'connect')

    def test_dns_error(self):
        self._assert(OSError('getaddrinfo failed: Name or service not known'),
                     'dns_error', None, 'connect')

    def test_tls_handshake_error(self):
        self._assert(OSError('SSL: CERTIFICATE_VERIFY_FAILED certificate verify failed'),
                     'tls_error', None, 'tls')

    def test_generic_timeout(self):
        self._assert(TimeoutError('timed out'),
                     'timeout', None, 'read')

    def test_unknown_falls_back_to_protocol_error(self):
        self._assert(RuntimeError('something weird happened'),
                     'protocol_error', None, 'unknown')


# ─────────────────────────── P0: 真实 shell 执行 ───────────────────────────

class ShellExecEncodingExecutionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('bash'), '本机没有 bash，跳过真实执行用例')
    def test_exec_returns_chinese_output(self):
        manager = DevAgentShellManager(PROJECT_ROOT)
        result = manager.exec("echo '你好 世界'")
        self.assertIn('你好 世界', result)
        self.assertIn('exit_code: 0', result)

    @unittest.skipUnless(shutil.which('bash'), '本机没有 bash，跳过真实执行用例')
    def test_exec_preserves_exit_code_semantics(self):
        manager = DevAgentShellManager(PROJECT_ROOT)
        result = manager.exec('false; echo "rc=$?"')
        self.assertIn('rc=1', result)

    @unittest.skipUnless(shutil.which('bash'), '本机没有 bash，跳过真实执行用例')
    def test_exec_heredoc_and_control_chars(self):
        manager = DevAgentShellManager(PROJECT_ROOT)
        result = manager.exec("printf 'a\\x01b\\x1b[31mc\\n'; cat <<'EOF'\nheredoc 中文\nEOF")
        # 0x01 被清洗、ESC 被清洗，但 ANSI 可见字符 [31m 保留（不干扰 c 存在性判断）
        self.assertIn('ab', result)
        self.assertIn('c', result)
        self.assertIn('heredoc 中文', result)
        self.assertNotIn('\x01', result)
        self.assertNotIn('\x1b', result)
        self.assertIn('exit_code: 0', result)


if __name__ == '__main__':
    unittest.main()
