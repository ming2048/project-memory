"""无台账模式：ledger_path 为空（null）时 discover 返回空集合、登记只写 registry、不碰任何台账文件。"""

import json
import os
import unittest

from helpers import World, rd


class NoLedgerTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.build()
        # 覆盖为无台账配置：不设置 ledger_path；其余路径键显式写出，全部解析到测试临时目录内
        self.config = os.path.join(self.w.dir, "config-no-ledger.json")
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"memory_root": self.w.memory_root, "db_path": "data/index.sqlite",
                       "registry_path": "data/registry.json", "report_dir": "data/reports",
                       "semantic_model_file": "data/models/fake-model.gguf",
                       "semantic_db_path": "data/semantic/index.sqlite"}, f)
        self.w.config = self.config
        self.registry = os.path.join(self.w.dir, "data", "registry.json")
        self.ledger_before = rd(self.w.ledger)
        self.ledger_dir = os.path.dirname(self.w.ledger)
        self.project = self.w.p("standalone")
        os.makedirs(self.project)

    def tearDown(self):
        self.w.cleanup()

    def test_discover_returns_empty_without_error(self):
        res = self.w.ok("discover")
        self.assertEqual(res["counts"]["total"], 0)
        self.assertEqual(res["projects"], [])
        self.assertFalse(os.path.exists(self.registry), "无台账 discover 不得写 registry")

    def test_register_initialize_resume_search_without_ledger(self):
        res = self.w.ok("initialize", "--project", self.project, "--register")
        self.assertEqual(res["registration"]["status"], "registered")
        self.assertFalse(res["registration"]["port_allocated"])
        self.assertEqual(res["registration"]["mode"], "registry_only")
        self.assertEqual(res["initialization"]["status"], "initialized")
        pid = res["project"]["project_id"]

        # 第二次 initialize：项目已在 registry 中，直接解析成功，不再登记
        again = self.w.ok("initialize", "--project", self.project, "--register")
        self.assertIsNone(again["registration"])
        self.assertEqual(again["project"]["project_id"], pid)

        r = self.w.ok("resume", "--cwd", self.project, "--no-git")
        self.assertEqual(r["project"]["project_id"], pid)
        self.assertEqual(r["expected_revision_for_next_handoff"], 0)

        s = self.w.ok("search", "任意词", "--project", self.project)
        self.assertEqual(s["count"], 0, "空项目检索应为 0 条且不报错")

        # 全程不读取、不创建、不修改任何台账文件
        self.assertEqual(rd(self.w.ledger), self.ledger_before)
        self.assertEqual(os.listdir(self.ledger_dir), [os.path.basename(self.w.ledger)])

    def test_duplicate_name_refused_without_ledger(self):
        # 先登记 standalone，再用另一个目录以同名登记，必须被拒绝
        self.w.ok("initialize", "--project", self.project, "--register")
        other = self.w.p("standalone-2")
        os.makedirs(other)
        code, failed, _, _ = self.w.run("initialize", "--project", other, "--register", "--name", "standalone")
        self.assertEqual(code, 1)
        self.assertEqual(failed["error"]["code"], "registration_ambiguous")


if __name__ == "__main__":
    unittest.main()
