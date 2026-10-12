import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from helpers import World, rd
from project_memory import api, initialize
from project_memory.config import load_config
from project_memory.registry import load_registry
from project_memory.util import PMError


class InitializeTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.build()
        self.w.discover()
        self.cfg = load_config(self.w.config)

    def tearDown(self):
        self.w.cleanup()

    def test_first_resume_intake_redaction_idempotent_without_handoff(self):
        w = self.w
        with open(os.path.join(w.paths['alpha'], 'README.md'), 'w', encoding='utf-8') as f:
            f.write('# 新项目\n字幕入口\n## 测试账号\n| 用户 | 密码 |\n|---|---|\n| admin | hidden-canary-42 |\n## 计划\n尚未运行测试\n')
        with open(os.path.join(w.paths['alpha'], 'package.json'), 'w') as f:
            f.write('{"scripts":{"start":"must-not-execute-canary"}}')
        first = w.ok('resume', '--project', w.ids['alpha'], '--no-git')
        self.assertEqual(first['initialization']['status'], 'initialized')
        self.assertIsNone(first['handoff'])
        self.assertEqual(first['expected_revision_for_next_handoff'], 0)
        self.assertIn('未运行测试', first['overview']['body'])
        self.assertIn('package.json', first['initialization']['entry_files'])
        self.assertNotIn('hidden-canary-42', json.dumps(first, ensure_ascii=False))
        mdir = w.memdir('alpha')
        self.assertNotIn('hidden-canary-42', rd(os.path.join(mdir, 'initialization.json')))
        overview = rd(os.path.join(mdir, 'overview.md'))
        second = w.ok('resume', '--project', w.ids['alpha'], '--no-git')
        self.assertEqual(second['initialization']['status'], 'already_initialized')
        self.assertEqual(overview, rd(os.path.join(mdir, 'overview.md')))
        self.assertEqual(second['history_events'], 0)
        self.assertGreater(w.ok('search', '字幕', '--project', w.ids['alpha'])['count'], 0)

    def test_existing_handoff_and_user_overview_preserved(self):
        w = self.w
        w.ok('bootstrap', '--project', w.ids['alpha'])
        overview = os.path.join(w.memdir('alpha'), 'overview.md')
        with open(overview, 'w', encoding='utf-8') as f:
            f.write('# 用户独立整理的概览\n')
        w.ok('resume', '--project', w.ids['alpha'], '--no-git')
        self.assertEqual(rd(overview), '# 用户独立整理的概览\n')
        w.handoff({'project': w.ids['alpha'], 'actor': 'test', 'summary': '真实阶段交接', 'expected_revision': 0})
        current = os.path.join(w.memdir('alpha'), 'current.md')
        before = rd(current)
        result = w.ok('resume', '--project', w.ids['alpha'], '--no-git')
        self.assertEqual(result['initialization']['status'], 'existing_history')
        self.assertEqual(before, rd(current))
        os.remove(current)
        restored = w.ok('resume', '--project', w.ids['alpha'], '--no-git')
        self.assertEqual(restored['handoff']['summary'], '真实阶段交接')
        self.assertEqual(before, rd(current))

    def test_missing_archived_conflict_block_new_memory(self):
        for key in ('ghost', 'old', 'nested'):
            result = self.w.ok('resume', '--project', self.w.ids[key], '--no-git')
            self.assertEqual(result['initialization']['status'], 'blocked')
            self.assertFalse(os.path.isdir(self.w.memdir(key)))

    def test_unknown_read_preserves_ledger_and_exposes_workflow(self):
        path = self.w.p('brand-new')
        os.makedirs(path)
        ledger = rd(self.w.ledger)
        code, result, _, _ = self.w.run('resume', '--project', path, '--no-git')
        self.assertEqual(code, 1)
        details = result['error']['details']
        self.assertEqual(details['initialization']['status'], 'needs_registration')
        self.assertIn('没有共享记忆', details['workflow']['content'])
        self.assertEqual(ledger, rd(self.w.ledger))
        self.assertEqual(len(load_registry(self.cfg)['projects']), 6)

    def test_explicit_registration_only_original_ledger_once(self):
        path = self.w.p('brand-new')
        os.makedirs(path)
        with open(os.path.join(path, 'README.md'), 'w', encoding='utf-8') as f:
            f.write('# 新项目首读说明\n')
        before = rd(self.w.ledger)
        # Unit fixture controls occupancy; integration below uses real Windows.
        with patch('project_memory.initialize._listeners', return_value={'tcp_listeners': 3, 'udp_endpoints': 2}) as listener:
            first = api.op_initialize(self.cfg, project=path, register_missing=True)
            self.assertTrue(listener.called)
            second = api.op_initialize(self.cfg, project=path, register_missing=True)
        self.assertEqual(first['registration']['status'], 'registered')
        self.assertFalse(first['registration']['port_allocated'])
        self.assertEqual(second['project']['project_id'], first['project']['project_id'])
        self.assertIsNone(second['registration'])
        text = rd(self.w.ledger)
        self.assertTrue(text.startswith(before.rstrip()))
        self.assertEqual(text.count('共享记忆首次登记'), 1)
        self.assertIn('未分配端口或项目号', text)
        self.assertEqual(rd(os.path.join(first['registration']['backup_dir'], os.path.basename(self.w.ledger))), before)

    def test_real_cli_registration_rejects_duplicate_name(self):
        path = self.w.p('brand-new')
        os.makedirs(path)
        result = self.w.ok('initialize', '--project', path, '--register')
        self.assertEqual(result['initialization']['status'], 'initialized')
        self.assertIn('tcp_listeners', result['registration']['occupancy_check'])
        other = self.w.p('brand-new-second')
        os.makedirs(other)
        code, failed, _, _ = self.w.run('initialize', '--project', other, '--register', '--name', 'brand-new')
        self.assertEqual(code, 1)
        self.assertEqual(failed['error']['code'], 'registration_ambiguous')

    def test_linux_ipv6_proc_files_missing_count_as_zero(self):
        # 内核禁用 IPv6 时 /proc/net/{tcp6,udp6} 不存在：v6 计为 0，IPv4 读不到仍报 listener_check_failed
        def fake_open(path, *args, **kwargs):
            if str(path).replace('\\', '/').endswith(('tcp6', 'udp6')):
                raise FileNotFoundError(str(path))
            return io.StringIO('  sl local_address rem_address st\n'
                               '0: 0100007F:1F90 00000000:0000 0A 0000\n')
        with patch('builtins.open', side_effect=fake_open):
            self.assertEqual(initialize._proc_net_state_count('tcp'), 1)
            self.assertEqual(initialize._proc_net_row_count('udp'), 1)
            self.assertEqual(initialize._proc_net_state_count('tcp6', missing_ok=True), 0)
            self.assertEqual(initialize._proc_net_row_count('udp6', missing_ok=True), 0)
            with self.assertRaises(PMError) as caught:
                initialize._proc_net_state_count('tcp6')
        self.assertEqual(caught.exception.code, 'listener_check_failed')

    def test_read_only_opt_out_and_name_only_registration_refused(self):
        result = self.w.ok('resume', '--project', self.w.ids['alpha'], '--no-git', '--no-initialize')
        self.assertEqual(result['initialization']['status'], 'disabled')
        self.assertFalse(os.path.isdir(self.w.memdir('alpha')))
        with self.assertRaises(PMError) as raised:
            api.op_initialize(self.cfg, project='不存在名称', register_missing=True)
        self.assertEqual(raised.exception.code, 'registration_refused')

    def test_mcp_unknown_register_resume_then_first_real_handoff(self):
        path = self.w.p('mcp-new-project')
        os.makedirs(path)
        with open(os.path.join(path, 'README.md'), 'w', encoding='utf-8') as stream:
            stream.write('# MCP首读项目\n字幕功能尚待实现\n')
        call = lambda mid, name, args: {'jsonrpc': '2.0', 'id': mid, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'onboarding-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            call(2, 'memory_resume', {'project': path, 'git': False}),
            call(3, 'memory_initialize', {'project': path, 'register_missing': True}),
            call(4, 'memory_resume', {'project': path, 'git': False}),
            call(5, 'memory_handoff', {'project': path, 'actor': 'MCP test fixture', 'summary': '首读初始化与实际协议交接验证', 'validation': ['隔离测试样本协议往返'], 'expected_revision': 0}),
            call(6, 'memory_resume', {'project': path, 'git': False}),
        ]
        proc = subprocess.run(self.w.cmd('mcp'), input=''.join(json.dumps(m)+'\n' for m in messages).encode(), capture_output=True, env=self.w.env(), timeout=45)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = {r['id']: r for r in [json.loads(x) for x in proc.stdout.decode('utf-8').splitlines()]}
        self.assertTrue(replies[2]['result']['isError'])
        details = replies[2]['result']['structuredContent']['error']['details']
        self.assertIn('workflow', details)
        self.assertEqual(replies[3]['result']['structuredContent']['registration']['status'], 'registered')
        self.assertIsNone(replies[4]['result']['structuredContent']['handoff'])
        self.assertEqual(replies[4]['result']['structuredContent']['expected_revision_for_next_handoff'], 0)
        self.assertEqual(replies[5]['result']['structuredContent']['status'], 'accepted')
        self.assertEqual(replies[6]['result']['structuredContent']['handoff']['revision'], 1)
