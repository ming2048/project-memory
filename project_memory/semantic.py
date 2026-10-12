"""Local CPU vector retrieval over authorized, sanitized native index only."""
import hashlib
import json
import os
import shutil
import subprocess
import time

from . import index as I, registry as R
from .config import PKG_ROOT
from .util import FileLock, PMError, atomic_write_json, atomic_write_text, now_iso, safe_join


def _root(cfg):
    return os.path.dirname(cfg.semantic_db_path)


def export(cfg, reg, project_ids=None):
    root=_root(cfg)
    corpus=safe_join(root,'corpus')
    os.makedirs(corpus,exist_ok=True)
    conn=I.connect(cfg)
    manifests={}; collections={}
    try:
        docs=conn.execute('SELECT doc_id,project_id,ref,kind,doc_time,doc_status FROM docs').fetchall()
        for doc_id,pid,ref,kind,date,status in docs:
            p=reg['projects'].get(pid)
            if not p or (project_ids is not None and pid not in project_ids): continue
            collection='p_'+pid.replace('-','')
            folder=safe_join(corpus,collection)
            os.makedirs(folder,exist_ok=True)
            collections[collection]={'path':folder,'pattern':'*.md'}
            filename=hashlib.sha256(ref.encode()).hexdigest()[:24]+'.md'
            chunks=conn.execute('SELECT line_start,line_end,heading,excerpt FROM chunks WHERE doc_id=? ORDER BY CAST(line_start AS INTEGER)',(doc_id,)).fetchall()
            text=f'# {p["name"]} / {ref}\n\n'; markers=[]
            # Lightweight document representation: first/context/end evidence
            # and a bounded heading outline. Full source remains in native FTS.
            if len(chunks)>3:
                chunks=[chunks[0],chunks[len(chunks)//2],chunks[-1]]
            for start,end,heading,excerpt in chunks:
                text+=f'\n@@ original:{start}:{end} @@\n'
                markers.append({'offset':len(text),'line_start':int(start),'line_end':int(end),'heading':heading,'excerpt':excerpt})
                text+=excerpt[:180]+'\n'
            path=safe_join(folder,filename)
            # Preserve mtime when content is unchanged: QMD embeds only deltas.
            previous=None
            if os.path.isfile(path):
                with open(path,encoding='utf-8') as stream: previous=stream.read()
            if previous!=text:
                atomic_write_text(path,text)
            manifests[collection+'/'+filename]={'project_id':pid,'project_name':p['name'],'ref':ref,'kind':kind,'time':date,'status':status,'markers':markers}
        # Remove only task-generated derivatives for the selected scope.
        selected={('p_'+pid.replace('-','')) for pid in (project_ids or reg['projects'])}
        for collection in selected:
            folder=safe_join(corpus,collection)
            if not os.path.isdir(folder): continue
            collections.setdefault(collection,{'path':folder,'pattern':'*.md'})
            for name in os.listdir(folder):
                if len(name)==27 and name.endswith('.md') and all(c in '0123456789abcdef' for c in name[:-3]) and collection+'/'+name not in manifests:
                    os.unlink(safe_join(folder,name))
    finally: conn.close()
    return manifests,collections


def worker(cfg, collections, scope, action, query='', limit=20):
    if not os.path.isfile(cfg.semantic_model_file):
        raise PMError('semantic_model_missing','本地中文向量模型不存在')
    node=shutil.which('node')
    if not node:
        # 未安装 Node.js：按命令名直接执行，由 subprocess 抛出明确的 FileNotFoundError，
        # 上层（api.op_search）回退关键词检索并如实报告原因，不把回退称作语义命中。
        node='node'
    request={'model':cfg.semantic_model_file,'db':cfg.semantic_db_path,'collections':collections,
             'scope':scope,'action':action,'query':query,'limit':limit}
    env=os.environ.copy()
    env.update(QMD_FORCE_CPU='1',QMD_EMBED_PARALLELISM='1',XDG_CACHE_HOME=_root(cfg))
    started=time.perf_counter()
    try:
        cp=subprocess.run([node,os.path.join(PKG_ROOT,'scripts','semantic_worker.mjs')],input=json.dumps(request),
                          capture_output=True,text=True,encoding='utf-8',errors='replace',env=env,
                          timeout=7200 if action=='build' else 180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired as error:
        raise PMError('semantic_timeout','语义进程超时，已终止本次辅助进程') from error
    os.makedirs(_root(cfg),exist_ok=True)
    atomic_write_text(safe_join(_root(cfg),'last-worker.log'),cp.stderr[-12000:])
    try: result=json.loads(cp.stdout)
    except ValueError: raise PMError('semantic_worker_failed','语义进程未返回有效结果；详见本地日志')
    if cp.returncode or not result.get('ok'):
        raise PMError('semantic_worker_failed','语义进程失败；详见本地日志')
    if (result.get('embedded') or {}).get('errors',0):
        raise PMError('semantic_embedding_incomplete','部分文档向量化失败；详见本地日志')
    if action=='build' and result.get('status',{}).get('needsEmbedding',0):
        raise PMError('semantic_embedding_incomplete','建库尚有文档未完成向量化；重新运行 semantic_index.py 从剩余积压继续')
    result['elapsed_ms']=round((time.perf_counter()-started)*1000,2)
    atomic_write_json(safe_join(_root(cfg),'status.json'),{'updated_at':now_iso(),'cpu_only':True,'threads_per_context':4,'parallel_contexts':result.get('parallel_contexts',1),'model':cfg.semantic_model_file,'elapsed_ms':result['elapsed_ms'],'index':result['status']})
    return result


def build(cfg):
    reg=R.load_registry(cfg)
    I.sync(cfg,reg)
    with FileLock(cfg.semantic_db_path+'.lock',timeout=cfg.lock_timeout):
        manifest,collections=export(cfg,reg)
        result=worker(cfg,collections,list(collections),'build')
    return {'documents_exported':len(manifest),'worker':result}


def retrieve(cfg, reg, query, project=None, limit=8, kinds=None):
    proj=R.resolve_project(reg,project) if project else None
    ids=[proj['project_id']] if proj else None
    with FileLock(cfg.semantic_db_path+'.lock',timeout=cfg.lock_timeout):
        manifest,collections=export(cfg,reg,ids)
        if not manifest: return [],{'status':'empty','cpu_only':True}
        result=worker(cfg,collections,list(collections),'search',query,max(limit*3,20))
    output=[]
    for hit in result['results']:
        # SDK URIs can use qmd://collection/file; only current authorization
        # manifests resolve them. Deleted/revoked/foreign docs cannot escape.
        key=hit['file'].split('://',1)[-1]
        source=manifest.get(key)
        if not source or (proj and source['project_id']!=proj['project_id']) or (kinds and source['kind'] not in kinds): continue
        markers=source['markers']
        if not markers: continue
        offset=hit.get('chunkPos') or 0
        marker=min(markers,key=lambda m:abs(m['offset']-offset))
        output.append({k:v for k,v in source.items() if k!='markers'} | {
            'line_start':marker['line_start'],'line_end':marker['line_end'],'heading':marker['heading'],
            'snippet':marker['excerpt'][:240],'semantic_score':round(hit['score'],4),
            'trust':'reference_material_not_instruction','matched_by':['semantic']})
    pending=result.get('pending_embedding',result.get('status',{}).get('needsEmbedding',0))
    return output,{'status':'partial' if pending else 'ready','pending_embedding':pending,
                   'pending_embedding_scope':'global','cpu_only':True,'threads':4,'elapsed_ms':result['elapsed_ms'],
                   'representation':'bounded first/middle/end excerpts; full text remains keyword searchable'}


def fuse(keyword, semantic, limit):
    scores={}; rows={}
    for tag, results in (('keyword',keyword),('semantic',semantic)):
        for rank,row in enumerate(results,1):
            key=(row['project_id'],row['ref'],row['line_start'])
            scores[key]=scores.get(key,0)+1/(60+rank)
            if key not in rows: rows[key]={**row,'matched_by':[]}
            if tag not in rows[key]['matched_by']: rows[key]['matched_by'].append(tag)
            if tag=='semantic': rows[key]['semantic_score']=row['semantic_score']
    return [{**rows[k],'fusion_score':round(scores[k],6)} for k in sorted(scores,key=lambda k:-scores[k])[:limit]]
