import unittest

from core.dev_agent import (
    _apply_note_write,
    _apply_todo_write,
    _plan_history_compaction,
    _trim_old_tool_results,
)


class DevAgentContextTests(unittest.TestCase):
    def test_plan_history_compaction_preserves_head_and_recent_tail(self):
        messages = [{'role': 'user', 'content': f'msg-{i}'} for i in range(121)]
        removed, kept = _plan_history_compaction(messages)
        self.assertEqual(61, len(kept))
        self.assertEqual('msg-0', kept[0]['content'])
        self.assertEqual('msg-61', kept[1]['content'])
        self.assertEqual('msg-120', kept[-1]['content'])
        self.assertEqual(60, len(removed))
        self.assertEqual('msg-1', removed[0]['content'])
        self.assertEqual('msg-61', kept[1]['content'])

    def test_trim_old_tool_results_keeps_recent_ten_rounds(self):
        messages = []
        for idx in range(12):
            messages.append(
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'tool_result',
                            'tool_use_id': f'call-{idx}',
                            'content': f'round-{idx}-' + ('x' * 260),
                        }
                    ],
                }
            )
        _trim_old_tool_results(messages)
        self.assertIn('已截断', messages[0]['content'][0]['content'])
        self.assertIn('已截断', messages[1]['content'][0]['content'])
        self.assertNotIn('已截断', messages[-1]['content'][0]['content'])
        self.assertTrue(messages[-1]['content'][0]['content'].endswith('x' * 260))

    def test_plan_history_compaction_keeps_tool_use_and_tool_result_pair_together(self):
        messages = [{'role': 'user', 'content': 'msg-0'}]
        for idx in range(1, 60):
            messages.append({'role': 'user', 'content': f'msg-{idx}'})
        messages.append(
            {
                'role': 'assistant',
                'content': [
                    {'type': 'tool_use', 'id': 'call-1', 'name': 'shell', 'input': {'command': 'echo hi'}},
                ],
            }
        )
        messages.append(
            {
                'role': 'user',
                'content': [
                    {'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'ok'},
                ],
            }
        )
        for idx in range(62, 121):
            messages.append({'role': 'user', 'content': f'msg-{idx}'})

        removed, kept = _plan_history_compaction(messages)

        self.assertEqual('msg-0', kept[0]['content'])
        self.assertEqual('assistant', kept[1]['role'])
        self.assertEqual('tool_use', kept[1]['content'][0]['type'])
        self.assertEqual('user', kept[2]['role'])
        self.assertEqual('tool_result', kept[2]['content'][0]['type'])
        self.assertEqual(59, len(removed))
        self.assertEqual('msg-1', removed[0]['content'])
        self.assertEqual('msg-59', removed[-1]['content'])

    def test_todo_and_note_tools_update_state(self):
        todos = []
        notes = []
        add_todo = _apply_todo_write(todos, {'action': 'add', 'content': '检查 token 飙升', 'status': 'in_progress'})
        self.assertIn('已新增 todo', add_todo)
        todo_id = todos[0]['todo_id']
        update_todo = _apply_todo_write(todos, {'action': 'update', 'todo_id': todo_id, 'status': 'completed'})
        self.assertIn('completed', update_todo)

        add_note = _apply_note_write(notes, {'action': 'add', 'content': '不要碰 data/msgs'})
        self.assertIn('已新增备注', add_note)
        note_id = notes[0]['note_id']
        update_note = _apply_note_write(notes, {'action': 'update', 'note_id': note_id, 'content': '不要碰 data/msgs 和 .env'})
        self.assertIn('已更新备注', update_note)


if __name__ == '__main__':
    unittest.main()
