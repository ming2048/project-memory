"""配置路径隔离：World 配置 load_config 后，所有非空 PATH_KEYS 都解析在测试临时目录内。

防止回归：任何路径键缺省回退到内置默认 ~/.project-memory/... 时，
进程内语义导出等操作会把派生文件写进真实家目录。
"""

import os
import unittest

from helpers import World
from project_memory.config import PATH_KEYS, load_config


class ConfigPathsTest(unittest.TestCase):
    def test_world_config_paths_stay_in_test_dir(self):
        w = World()
        try:
            cfg = load_config(w.config)
            base = os.path.abspath(w.dir)
            for key in PATH_KEYS:
                value = getattr(cfg, key)
                if not value:  # ledger_path 允许为 null（无台账模式）
                    self.assertEqual(key, "ledger_path")
                    continue
                resolved = os.path.abspath(value)
                self.assertTrue(resolved == base or resolved.startswith(base + os.sep),
                                f"{key} 解析到测试临时目录之外: {resolved}")
        finally:
            w.cleanup()


if __name__ == "__main__":
    unittest.main()
