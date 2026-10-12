"""Absolute-path stdio entrypoint for clients started outside this project."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from project_memory.cli import main

if __name__=='__main__':
    args=sys.argv[1:]
    if 'mcp' not in args:  # 用户已显式给出 mcp 子命令时不重复追加
        args=args+['mcp']
    raise SystemExit(main(args))
