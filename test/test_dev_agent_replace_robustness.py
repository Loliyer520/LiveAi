"""回归测试：文本锚点替换的鲁棒性修复。

覆盖：
1. CRLF/LF 行尾差异不再导致锚点匹配失败，且落盘保持原文件行尾风格
2. 锚点 0 命中时返回最相近行号与内容，引导改用按行定位，避免反复猜锚点
3. 命中次数与预期不符时返回实际命中行号
4. 普通 LF 替换与 replace_all 行为不回退
"""

import os
import tempfile
import unittest

from core.dev_agent import _replace_local_file_text


class ReplaceLocalFileTextRobustnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _write(self, name: str, content: str, newline: str | None = '') -> str:
        # 默认 newline=''：原始写入，LF 测试文件在 Windows 上也是真正的 LF，不受平台文本模式转换影响
        path = os.path.join(self.root, name)
        with open(path, 'w', encoding='utf-8', newline=newline) as f:
            f.write(content)
        return name

    def _read_raw(self, name: str) -> str:
        with open(os.path.join(self.root, name), 'r', encoding='utf-8', newline='') as f:
            return f.read()

    def test_lf_anchor_matches_crlf_file_and_keeps_crlf(self):
        # 文件是 CRLF（如 SSH cat 拉下来的 Windows 文件），锚点用 LF 也要能匹配
        name = self._write(
            'crlf.txt',
            'line one\r\nold_block\r\nline three\r\n',
            newline='',
        )
        result = _replace_local_file_text(
            self.root, name, 'old_block', 'new_block', dry_run=False,
        )
        self.assertIn('已定点替换', result)
        raw = self._read_raw(name)
        self.assertNotIn('old_block', raw)
        # 未触碰的行保持 CRLF，不得混入 LF 或把整个文件转成 LF
        self.assertEqual('line one\r\nnew_block\r\nline three\r\n', raw)

    def test_crlf_anchor_matches_lf_file_and_keeps_lf(self):
        # 锚点带着 CRLF（从 raw 读取/上传文件拷贝来的），LF 文件也要能匹配
        name = self._write('lf.txt', 'line one\nold_block\nline three\n')
        result = _replace_local_file_text(
            self.root, name, 'old_block\r\n', 'new_block\n',
        )
        self.assertIn('已定点替换', result)
        raw = self._read_raw(name)
        self.assertIn('new_block\n', raw)
        self.assertNotIn('\r', raw, 'LF 文件不应被引入 CRLF')

    def test_replace_all_on_crlf_file_keeps_crlf(self):
        name = self._write('multi.txt', 'a\r\nX\r\nb\r\nX\r\nc\r\n', newline='')
        result = _replace_local_file_text(
            self.root, name, 'X', 'Y', replace_all=True,
        )
        self.assertIn('命中 2 次', result)
        raw = self._read_raw(name)
        self.assertEqual('a\r\nY\r\nb\r\nY\r\nc\r\n', raw)

    def test_zero_match_returns_nearest_line_hints(self):
        # 生产文件缩进/内容与锚点有出入时，失败信息必须给出可用的真实行号
        name = self._write(
            'admin.html',
            '<div class="panel">\n    <span id="old">原始</span>\n</div>\n',
        )
        result = _replace_local_file_text(
            self.root, name, '<span id="oldx">原始</span>', '<span id="newx">新</span>',
        )
        self.assertIn('未找到要替换的原文本', result)
        self.assertIn('最相近的真实内容', result)
        self.assertIn('行 2', result)
        self.assertIn('replace_local_file_lines', result, '必须引导改用按行定位')
        # 失败不得落盘
        self.assertIn('原始', self._read_raw(name))

    def test_expected_count_mismatch_reports_actual_line_numbers(self):
        name = self._write('dup.txt', 'x\ntoken\ny\ntoken\nz\n')
        result = _replace_local_file_text(
            self.root, name, 'token', 'T', expected_count=1,
        )
        self.assertIn('实际 2 次，预期 1 次', result)
        self.assertIn('实际命中行号：2、4', result)

    def test_lf_replace_still_works(self):
        name = self._write('plain.txt', 'before\nanchor\nafter\n')
        result = _replace_local_file_text(self.root, name, 'anchor', 'replaced')
        self.assertIn('已定点替换', result)
        self.assertEqual('before\nreplaced\nafter\n', self._read_raw(name))

    def test_occurrence_targeting_works(self):
        name = self._write('occ.txt', 'a\nX\nb\nX\nc\n')
        result = _replace_local_file_text(self.root, name, 'X', 'Y', occurrence=2)
        self.assertIn('本次计划修改 1 处', result)
        self.assertEqual('a\nX\nb\nY\nc\n', self._read_raw(name))

    def test_dry_run_does_not_modify_crlf_file(self):
        name = self._write('dry.txt', 'a\r\nX\r\nb\r\n', newline='')
        result = _replace_local_file_text(self.root, name, 'X', 'Y', dry_run=True)
        self.assertIn('dry_run', result)
        self.assertEqual('a\r\nX\r\nb\r\n', self._read_raw(name))


if __name__ == '__main__':
    unittest.main()
