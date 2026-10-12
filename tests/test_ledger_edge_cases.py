import unittest
from project_memory.discover import parse_ledger_text, status_enum
from project_memory.handoff import validate_payload
from project_memory.util import PMError


class LedgerEdgeCases(unittest.TestCase):
    def test_comment_does_not_truncate_project_table(self):
        entries, _ = parse_ledger_text('| 项目 | 路径 |\n|---|---|\n| A | `E:\\A` |\n<!-- factory-port {} -->\n| B | `E:\\B` |\n')
        self.assertEqual([x['name'] for x in entries], ['A', 'B'])

    def test_archived_folder_does_not_make_live_project_archived(self):
        self.assertEqual(status_enum('开发中；源码 `D:\\归档\\项目`'), 'active')

    def test_archive_relative_directory_uses_declared_root(self):
        entries, _ = parse_ledger_text('> **归档策略**：源码入 `D:\\归档\\`\n## 已归档\n| 项目 | 归档位置 |\n|---|---|\n| A | `旧A` |\n')
        self.assertEqual(entries[0]['path'], 'D:\\归档\\旧A')
        self.assertTrue(entries[0]['archived_hint'])

    def test_handoff_credential_table_rejected_without_echo(self):
        payload = {'project': 'example', 'actor': 'tester', 'summary': '## 测试账号\n| 用户 | 密码 |\n|---|---|\n| admin | canary-123 |', 'expected_revision': 0}
        with self.assertRaises(PMError) as error:
            validate_payload(payload)
        self.assertNotIn('canary-123', str(error.exception))

