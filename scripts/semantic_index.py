import json, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from project_memory.config import load_config
from project_memory.semantic import build

def config_path(argv):
    """支持 --config <path>（README 风格），并兼容旧的单个位置参数；无参数时用内置默认。"""
    if '--config' in argv:
        i = argv.index('--config')
        if i + 1 >= len(argv):
            raise SystemExit('usage: python -X utf8 scripts/semantic_index.py [--config CONFIG]')
        return argv[i + 1]
    positional = [a for a in argv if not a.startswith('--')]
    return positional[0] if positional else None

print(json.dumps(build(load_config(config_path(sys.argv[1:]))),ensure_ascii=False))
