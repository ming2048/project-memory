import json,os,unittest
from pathlib import Path
from unittest.mock import patch
from helpers import World
from project_memory import api,project_docs,registry,index,initialize
from project_memory.config import load_config
from project_memory.util import atomic_write_json

class ProjectDocsTest(unittest.TestCase):
    def setUp(self):
        self.w=World();self.w.build();self.w.discover();self.cfg=load_config(self.w.config)
        self.pid=self.w.ids['alpha']
    def tearDown(self):self.w.cleanup()

    def test_initialize_runbook_links_without_copying_server_contents(self):
        r=api.op_resume(self.cfg,project=self.pid,git=False)
        docs=r['project_documents'];self.assertTrue(docs['operations_exists'])
        server=Path(self.w.dir)/'server.md';server.write_text('server-private-canary-459',encoding='utf-8')
        data=json.loads(Path(docs['profile']).read_text(encoding='utf-8'))
        data['deployment']['entries']=[{'server_manual':str(server)}]
        atomic_write_json(docs['profile'],data)
        Path(docs['operations']).write_text('# 专用运维\n蓝鲸回滚操作。\n',encoding='utf-8')
        r=api.op_resume(self.cfg,project=self.pid,git=False)
        self.assertEqual(r['project_documents']['server_manuals'],[{'path':str(server),'exists':True}])
        self.assertNotIn('server-private-canary-459',json.dumps(r))
        found=api.op_search(self.cfg,'蓝鲸回滚',project=self.pid,mode='keyword')
        self.assertTrue(any(x['ref']=='memory/operations.md' for x in found['results']))
        self.assertEqual(Path(docs['operations']).read_text(encoding='utf-8'),'# 专用运维\n蓝鲸回滚操作。\n')
        before=Path(docs['profile']).read_bytes()
        api.op_resume(self.cfg,project=self.pid,git=False)
        self.assertEqual(Path(docs['profile']).read_bytes(),before)

    def test_invalid_profile_does_not_override_project_identity(self):
        r=api.op_resume(self.cfg,project=self.pid,git=False)
        atomic_write_json(r['project_documents']['profile'],{'project_id':'other'})
        r=api.op_resume(self.cfg,project=self.pid,git=False,initialize=False)
        self.assertEqual(r['project_documents']['status'],'invalid_profile')
        self.assertEqual(r['project']['project_id'],self.pid)

    def test_compact_registration_appends_one_row_without_process_details(self):
        target=Path(self.w.dir)/'brand-new';target.mkdir()
        Path(self.w.ledger).write_text('# 项目台账\n\n| 项目名称 | 类型/关联 | 项目说明 | 本地路径 | 项目号/端口 | 项目资料 | 服务器手册 | 状态 |\n|---|---|---|---|---|---|---|---|\n<!-- project-directory:end -->\n',encoding='utf-8')
        with patch('project_memory.initialize._listeners',return_value={'tcp_listeners':0,'udp_endpoints':0}):
            initialize.register(self.cfg,str(target),'全新项目')
        text=Path(self.w.ledger).read_text(encoding='utf-8')
        self.assertIn('| 全新项目 |',text)
        self.assertNotIn('### 共享记忆首次登记',text)
        self.assertEqual(text.count('project-directory:end'),1)
        from project_memory.discover import parse_ledger_text
        rows,_=parse_ledger_text(text)
        self.assertEqual(len(rows),1)
