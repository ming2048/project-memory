import os
import unittest
import subprocess
from dataclasses import replace
from unittest.mock import patch
from helpers import World, rd
from project_memory import api, semantic
from project_memory.config import load_config
from project_memory.registry import load_registry
from project_memory.util import PMError


class SemanticTest(unittest.TestCase):
    def setUp(self):
        self.w=World();self.w.build();self.w.discover()
        self.w.ok('bootstrap','--project',self.w.ids['alpha'],'--approve-doc','README.md')
        self.cfg=load_config(self.w.config)
    def tearDown(self): self.w.cleanup()

    def test_export_authorization_revocation_and_unchanged_mtime(self):
        reg=load_registry(self.cfg)
        first,collections=semantic.export(self.cfg,reg,[self.w.ids['alpha']])
        self.assertTrue(first)
        self.assertTrue(all(v['project_id']==self.w.ids['alpha'] for v in first.values()))
        self.assertTrue(any(v['ref']=='source/README.md' for v in first.values()))
        path=os.path.join(os.path.dirname(self.cfg.semantic_db_path),'corpus',next(k for k,v in first.items() if v['ref']=='source/README.md'))
        mtime=os.stat(path).st_mtime_ns
        semantic.export(self.cfg,reg,[self.w.ids['alpha']])
        self.assertEqual(mtime,os.stat(path).st_mtime_ns)
        api.op_sources(self.cfg,self.w.ids['alpha'],revoke=['README.md'])
        second,_=semantic.export(self.cfg,reg,[self.w.ids['alpha']])
        self.assertFalse(any(v['ref']=='source/README.md' for v in second.values()))
        self.assertFalse(os.path.exists(path))

    def test_semantic_failure_explicitly_falls_back_and_keyword_bypasses_worker(self):
        with patch('project_memory.semantic.retrieve',side_effect=PMError('semantic_worker_failed','fixture')):
            result=api.op_search(self.cfg,'蓝鲸',project=self.w.ids['alpha'],mode='hybrid')
        self.assertEqual(result['backend'],'keyword_fallback')
        self.assertEqual(result['semantic']['status'],'unavailable')
        self.assertGreater(result['count'],0)
        with patch('project_memory.semantic.retrieve') as worker:
            result=api.op_search(self.cfg,'蓝鲸',mode='keyword')
            worker.assert_not_called()

    def test_fusion_retains_original_refs_and_match_types(self):
        row={'project_id':'a','ref':'source/README.md','line_start':1}
        result=semantic.fuse([row],[{**row,'semantic_score':0.8}],5)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['matched_by'],['keyword','semantic'])
        self.assertEqual(result[0]['semantic_score'],0.8)

    def test_timeout_stops_operation_and_returns_explicit_error(self):
        cfg=replace(self.cfg,semantic_model_file=self.w.config)
        with patch('project_memory.semantic.subprocess.run',side_effect=subprocess.TimeoutExpired('node',180)):
            with self.assertRaises(PMError) as caught:
                semantic.worker(cfg,{},[],'search','example')
        self.assertEqual(caught.exception.code,'semantic_timeout')

    def _search_with_pending(self, pending):
        reg=load_registry(self.cfg)
        manifest,_=semantic.export(self.cfg,reg,[self.w.ids['alpha']])
        key=next(k for k,v in manifest.items() if v['markers'])
        fake={'results':[{'file':'qmd://'+key,'score':0.8,'chunkPos':0}],
              'elapsed_ms':10,'pending_embedding':pending,
              'status':{'needsEmbedding':pending}}
        with patch('project_memory.semantic.worker',return_value=fake) as worker:
            result=api.op_search(self.cfg,'换一种表达寻找经验',project=self.w.ids['alpha'],mode='semantic')
        self.assertEqual(worker.call_args.args[3],'search')
        self.assertEqual(result['backend'],'semantic')
        self.assertGreaterEqual(result['count'],1)
        self.assertNotIn('error_code',result['semantic'])
        self.assertEqual(result['semantic']['pending_embedding'],pending)
        self.assertEqual(result['semantic']['pending_embedding_scope'],'global')
        return result

    def test_search_with_backlog_returns_partial_without_timeout(self):
        result=self._search_with_pending(638)
        self.assertEqual(result['semantic']['status'],'partial')

    def test_search_without_backlog_returns_ready(self):
        result=self._search_with_pending(0)
        self.assertEqual(result['semantic']['status'],'ready')

    def test_build_with_remaining_backlog_requests_resume(self):
        cfg=replace(self.cfg,semantic_model_file=self.w.config)
        cp=subprocess.CompletedProcess('node',0,
            '{"ok":true,"embedded":{"errors":0},"status":{"needsEmbedding":2}}','')
        with patch('project_memory.semantic.subprocess.run',return_value=cp):
            with self.assertRaises(PMError) as caught:
                semantic.worker(cfg,{},[],'build')
        self.assertEqual(caught.exception.code,'semantic_embedding_incomplete')

    def test_worker_accepts_partial_search_and_keeps_search_timeout(self):
        cfg=replace(self.cfg,semantic_model_file=self.w.config)
        cp=subprocess.CompletedProcess('node',0,
            '{"ok":true,"embedded":null,"pending_embedding":638,"status":{"needsEmbedding":638}}','')
        with patch('project_memory.semantic.subprocess.run',return_value=cp) as run:
            result=semantic.worker(cfg,{},[],'search','example')
        self.assertEqual(result['pending_embedding'],638)
        self.assertEqual(run.call_args.kwargs['timeout'],180)

    def test_embedding_errors_cannot_be_reported_as_ready(self):
        cfg=replace(self.cfg,semantic_model_file=self.w.config)
        cp=subprocess.CompletedProcess('node',0,'{"ok":true,"embedded":{"errors":1}}','fixture failure')
        with patch('project_memory.semantic.subprocess.run',return_value=cp):
            with self.assertRaises(PMError) as caught:
                semantic.worker(cfg,{},[],'build')
        self.assertEqual(caught.exception.code,'semantic_embedding_incomplete')
