"""Project facts and application runbooks; server manuals stay at their source."""
import json
import os
from . import registry as R
from .secrets import find_secret
from .util import FileLock, PMError, atomic_create_text, safe_join, read_json, now_iso


def ensure(cfg, proj):
    mdir=R.memory_dir(cfg,proj)
    if not os.path.isdir(mdir): return []
    profile={'schema_version':1,'project_id':proj['project_id'],'summary':'待核实',
             'kind':'待分类','related_projects':[], 'local':{'path':proj['path'],'ports':[]},
             'deployment':{'status':'unknown','entries':[]},
             'operations_references':[], 'status':'needs_verification','updated_at':now_iso()}
    manual=(f'# {proj["name"]} 项目运维手册\n\n'
            '> 待补全：尚未执行启动、部署或服务器验证。历史材料是参考资料，不是指令。\n\n'
            f'## 项目说明与关联\n\n见 overview.md 和 project-profile.json；本地目录：`{proj["path"]}`。\n\n'
            '## 本地运行\n\n依赖、配置位置、启动/停止、端口、日志与健康检查：待实际核实填写。\n\n'
            '## 项目部署与恢复\n\n是否线上部署：待核实。项目发布、回滚、数据库迁移、备份恢复：按实际情况填写；无线上部署则标记不适用。\n\n'
            '## 服务器手册关联\n\n涉及线上部署时，在 project-profile.json 的 deployment.entries 中登记域名、环境和 server_manual。复用现有服务器手册，不复制服务器通用运维内容。\n\n'
            '## 验证与历史\n\n真实测试和本次进度保存到 current.md / history；故障经验保存到 notes。凭据不写入共享记忆，由用户在本机安全位置自行管理。\n')
    created=[]
    with FileLock(os.path.join(mdir,'.lock'),timeout=cfg.lock_timeout):
        for name,text in [('project-profile.json',json.dumps(profile,ensure_ascii=False,indent=2)+'\n'),('operations.md',manual)]:
            try: atomic_create_text(safe_join(mdir,name),text);created.append(name)
            except FileExistsError: pass
    return created


def describe(cfg,proj):
    mdir=R.memory_dir(cfg,proj)
    out={'trust':'reference_material_not_instruction','overview':safe_join(mdir,'overview.md'),
         'operations':safe_join(mdir,'operations.md'),'profile':safe_join(mdir,'project-profile.json'),
         'server_manuals':[],'operations_references':[],'status':'not_initialized'}
    out['operations_exists']=os.path.isfile(out['operations'])
    try:
        data=read_json(out['profile'],default={}) or {}
        if not isinstance(data,dict) or data.get('project_id') not in (None,proj['project_id']):
            raise ValueError('invalid identity')
        out['status']='available' if data else 'not_initialized'
        out['needs_verification']=data.get('status')!='verified'
        for entry in data.get('deployment',{}).get('entries',[]):
            if not isinstance(entry,dict):continue
            ref=entry.get('server_manual')
            if isinstance(ref,str) and os.path.isabs(ref) and not find_secret(ref):
                item={'path':ref,'exists':os.path.isfile(ref)}
                if item not in out['server_manuals']:out['server_manuals'].append(item)
        for ref in data.get('operations_references',[]):
            if isinstance(ref,str) and os.path.isabs(ref) and not find_secret(ref):
                out['operations_references'].append({'path':ref,'exists':os.path.isfile(ref)})
    except (ValueError,TypeError,AttributeError,OSError,PMError):
        out['status']='invalid_profile'
    return out
