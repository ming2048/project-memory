import json, os, sys, tempfile, unittest, subprocess, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from project_memory import api
from project_memory.config import load_config
from project_memory.registry import load_registry,memory_dir
from project_memory.util import PMError

class Acceptance(unittest.TestCase):
    def setUp(self):
        (ROOT/'work').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='independent-',dir=ROOT/'work')
        self.root=Path(self.tmp.name)
        self.a=self.root/'视频甲'; self.b=self.root/'视频乙'
        self.a.mkdir();self.b.mkdir()
        ledger=self.root/'ledger.md'
        ledger.write_text(f'# 项目\n| 项目 | 路径 | 状态 |\n|---|---|---|\n| 视频甲 | `{self.a}` | 开发中 |\n| 视频乙 | `{self.b}` | 开发中 |\n',encoding='utf-8')
        config={'ledger_path':str(ledger),'memory_root':str(self.root/'memories'),'registry_path':str(self.root/'registry.json'),'db_path':str(self.root/'index.sqlite'),'report_dir':str(self.root/'reports'),'semantic_model_file':str(self.root/'fake-model.gguf'),'semantic_db_path':str(self.root/'semantic/index.sqlite')}
        self.cp=self.root/'config.json';self.cp.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
        self.cfg=load_config(str(self.cp))
        api.op_discover(self.cfg)
        api.op_bootstrap(self.cfg,all_existing=True)
        self.reg=load_registry(self.cfg)
        self.pa=next(p for p in self.reg['projects'].values() if p['name']=='视频甲')
        self.pb=next(p for p in self.reg['projects'].values() if p['name']=='视频乙')
    def tearDown(self): self.tmp.cleanup()
    def payload(self,project=None,**kw):
        p={'project':project or self.pa['project_id'],'actor':'Independent Review','summary':'字幕断点恢复实测交接','completed':['修复字幕导出'],'validation':['隔离样本验证通过，业务服务未启动'],'pending':['真实视频待用户验收'],'next_steps':['核对批量任务恢复'],'expected_revision':0,'session_id':'review-session'}
        p.update(kw);return p
    def cli(self,*args,env=None):
        cp=subprocess.run([sys.executable,'-m','project_memory','--config',str(self.cp),*args],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',env=env,timeout=45)
        return cp,json.loads(cp.stdout)
    def test_cross_session_project_isolation_and_duplicate(self):
        first=api.op_handoff(self.cfg,self.payload())
        self.assertEqual(first['status'],'accepted')
        self.assertEqual(api.op_handoff(self.cfg,self.payload())['status'],'duplicate')
        cp,result=self.cli('resume','--project',str(self.a),'--no-git')
        self.assertEqual(cp.returncode,0)
        self.assertIn('字幕断点恢复',json.dumps(result,ensure_ascii=False))
        self.assertGreater(api.op_search(self.cfg,'字幕',project=self.pa['project_id'])['count'],0)
        self.assertEqual(api.op_search(self.cfg,'字幕',project=self.pb['project_id'])['count'],0)
        current=Path(memory_dir(self.cfg,self.pa))/'current.md'
        before=current.read_bytes()
        conflict=api.op_handoff(self.cfg,self.payload(actor='Other Tool',summary='另一个软件的旧版本交接'))
        self.assertEqual(conflict['status'],'conflict')
        self.assertEqual(before,current.read_bytes())
        self.assertEqual(len(list(current.parent.joinpath('history').glob('*.md'))),2)
    def test_context_secret_redaction_read_slice_and_cache(self):
        canary='canary-R7n31zvQ'
        doc=self.a/'README.md'
        doc.write_text('# 说明\n正常信息\n## 测试账号\n| 用户 | 密码 |\n|---|---|\n| admin | '+canary+' |\n## 工作\n字幕处理\n',encoding='utf-8')
        api.op_sources(self.cfg,self.pa['project_id'],approve=['README.md'])
        result=api.op_read(self.cfg,self.pa['project_id'],'source/README.md',start_line=6,end_line=6)
        self.assertNotIn(canary,json.dumps(result,ensure_ascii=False))
        result=api.op_search(self.cfg,'canary',project=self.pa['project_id'])
        self.assertEqual(result['count'],0)
        self.assertNotIn(canary.encode(),Path(self.cfg.db_path).read_bytes())
    def test_source_update_and_revocation(self):
        source=self.a/'README.md';source.write_text('旧线索香蕉',encoding='utf-8')
        api.op_sources(self.cfg,self.pa['project_id'],approve=['README.md'])
        self.assertGreater(api.op_search(self.cfg,'香蕉',project=self.pa['project_id'])['count'],0)
        source.write_text('新线索苹果',encoding='utf-8')
        self.assertEqual(api.op_search(self.cfg,'香蕉',project=self.pa['project_id'])['count'],0)
        self.assertGreater(api.op_search(self.cfg,'苹果',project=self.pa['project_id'])['count'],0)
        api.op_sources(self.cfg,self.pa['project_id'],revoke=['README.md'])
        self.assertEqual(api.op_search(self.cfg,'苹果',project=self.pa['project_id'])['count'],0)
        with self.assertRaises(PMError): api.op_read(self.cfg,self.pa['project_id'],'source/README.md')
    def test_resume_rejects_link_memory_escape(self):
        mdir=Path(memory_dir(self.cfg,self.pa))
        saved=mdir.with_name(mdir.name+'-saved')
        mdir.rename(saved)
        outside=self.root/'outside-memory';outside.mkdir()
        (outside/'current.md').write_text('# outside-current-canary\n',encoding='utf-8')
        quote=lambda p:"'"+str(p).replace("'","''")+"'"
        if os.name=='nt':
            command='New-Item -ItemType Junction -Path '+quote(mdir)+' -Target '+quote(outside)+' | Out-Null'
            created=subprocess.run(['powershell','-NoProfile','-Command',command],capture_output=True,text=True,timeout=30)
            self.assertEqual(created.returncode,0,created.stderr)
        else:
            os.symlink(outside,mdir)
        try:
            with self.assertRaises(PMError): api.op_resume(self.cfg,project=self.pa['project_id'],git=False)
        finally:
            if os.name=='nt':
                removed=subprocess.run(['powershell','-NoProfile','-Command','[System.IO.Directory]::Delete('+quote(mdir)+')'],capture_output=True,text=True,timeout=30)
                self.assertEqual(removed.returncode,0,removed.stderr)
            else:
                os.unlink(mdir)
            saved.rename(mdir)
    def test_two_process_conflict(self):
        processes=[]
        for actor in ('Writer A','Writer B'):
            infile=self.root/(actor+'.json');infile.write_text(json.dumps(self.payload(actor=actor),ensure_ascii=False),encoding='utf-8')
            processes.append(subprocess.Popen([sys.executable,'-m','project_memory','--config',str(self.cp),'handoff','--input',str(infile)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8'))
        results=[]
        for proc in processes:
            out,err=proc.communicate(timeout=45)
            self.assertEqual(proc.returncode,0,err)
            results.append(json.loads(out)['status'])
        self.assertEqual(sorted(results),['accepted','conflict'])
        hdir=Path(memory_dir(self.cfg,self.pa))/'history'
        self.assertEqual(len(list(hdir.glob('*.md'))),2)
    def test_process_crash_recovery(self):
        infile=self.root/'handoff.json';infile.write_text(json.dumps(self.payload(),ensure_ascii=False),encoding='utf-8')
        env=os.environ.copy();env['PROJECT_MEMORY_FAULT']='after_history_write'
        cp=subprocess.run([sys.executable,'-m','project_memory','--config',str(self.cp),'handoff','--input',str(infile)],cwd=ROOT,env=env,capture_output=True,timeout=45)
        self.assertEqual(cp.returncode,70)
        recovered=api.op_recover(self.cfg,self.pa['project_id'])
        self.assertTrue(recovered['repaired'])
        self.assertEqual(recovered['head_revision'],1)
        cp,result=self.cli('resume','--project',self.pa['project_id'],'--no-git')
        self.assertEqual(cp.returncode,0)
        self.assertIn('字幕断点恢复',json.dumps(result,ensure_ascii=False))
    def test_mcp_fresh_process(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'independent-acceptance','version':'1'}}},{'jsonrpc':'2.0','method':'notifications/initialized'},{'jsonrpc':'2.0','id':2,'method':'tools/list'},{'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'memory_resume','arguments':{'project':self.pa['project_id'],'git':False}}}]
        cp=subprocess.run([sys.executable,'-m','project_memory','--config',str(self.cp),'mcp'],input=''.join(json.dumps(m)+'\n' for m in messages),cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=45)
        self.assertEqual(cp.returncode,0,cp.stderr)
        replies=[json.loads(line) for line in cp.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies],[1,2,3])
        self.assertIn('memory_handoff',[x['name'] for x in replies[1]['result']['tools']])
        self.assertFalse(replies[2]['result'].get('isError',False),replies[2])
    def test_relocation_then_registry_rebuild_keeps_identity(self):
        api.op_handoff(self.cfg,self.payload())
        moved=self.root/'视频甲新位置';self.a.rename(moved)
        api.op_relocate(self.cfg,self.pa['project_id'],str(moved))
        api.op_alias(self.cfg,self.pa['project_id'],add=['已搬迁视频甲'])
        ledger=Path(self.cfg.ledger_path)
        ledger.write_text(ledger.read_text(encoding='utf-8').replace(str(self.a),str(moved)),encoding='utf-8')
        Path(self.cfg.registry_path).unlink()
        api.op_discover(self.cfg)
        rebuilt=api.op_resume(self.cfg,project=str(moved),git=False)
        self.assertEqual(rebuilt['project']['project_id'],self.pa['project_id'])
        self.assertIn('字幕断点恢复',json.dumps(rebuilt,ensure_ascii=False))
        by_alias=api.op_resume(self.cfg,project='已搬迁视频甲',git=False)
        self.assertEqual(by_alias['project']['project_id'],self.pa['project_id'])

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance)
    start=time.perf_counter()
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    summary={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'elapsed_seconds':round(time.perf_counter()-start,3),'passed':result.wasSuccessful()}
    (ROOT/'work/independent-acceptance-result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)
