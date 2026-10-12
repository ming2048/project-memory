import json
import os
import re
import unittest

from helpers import SECRETS, World, rd


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.build()

    def tearDown(self):
        self.w.cleanup()

    def test_inventory_merge_flags_and_no_secrets(self):
        w = self.w
        res = w.discover()
        out_text = json.dumps(res, ensure_ascii=False)
        projs = {p["project_id"]: p for p in res["projects"]}
        self.assertEqual(res["counts"]["total"], 6, out_text)
        self.assertEqual(set(w.ids), {"alpha", "beta", "gamma", "old", "ghost", "nested"})

        a = projs[w.ids["alpha"]]
        self.assertEqual(a["name"], "视频二创工坊")
        self.assertEqual(a["state"], "active")
        # 项目号表 + 详细表 + 端口式名称行 同路径合并为一个项目（反引号/正斜杠/尾斜杠都识别）
        self.assertEqual(len(a["source_lines"]), 3)
        self.assertEqual(a["docs_candidates"], ["AGENTS.md", "README.md", "docs/guide.md", "docs/sub/deep.md"])

        self.assertIn("duplicate_name", projs[w.ids["beta"]]["flags"])
        self.assertIn("duplicate_name", projs[w.ids["gamma"]]["flags"])
        self.assertEqual(projs[w.ids["old"]]["state"], "archived")
        self.assertEqual(projs[w.ids["ghost"]]["state"], "missing")
        n = projs[w.ids["nested"]]
        self.assertEqual(n["state"], "needs_verification")
        self.assertEqual(n["conflicts"][0]["type"], "name_mismatch")
        self.assertEqual(n["name"], "子模块")  # 第一处为准，冲突留档而非最后一行覆盖
        self.assertEqual([v["name"] for v in n["conflicts"][0]["values"]], ["子模块", "子模块（新名）"])
        self.assertEqual(n["source_lines"], sorted(n["source_lines"]))

        led = res["ledger"]
        self.assertEqual(len(led["sensitive_rows_rejected"]), 1)
        self.assertEqual(led["rows_skipped"].get("path_is_file_not_project_dir"), 1)
        self.assertGreaterEqual(led["rows_skipped"].get("table_without_path_column", 0), 1)
        # 代码块中的表与运维文档都不应成为项目
        self.assertNotIn("代码块里的表", out_text)
        self.assertNotIn("运维文档", out_text)
        # 台账状态只以枚举出现
        self.assertEqual(a["ledger_status_claim"], "active")
        self.assertNotIn("运行中", out_text)
        self.assertNotIn("二创流水线", out_text)

        # 凭据与端口不出现在任何输出、registry、报告中
        for s in SECRETS + ("postgres://",):
            self.assertNotIn(s, out_text)
            for fn, blob in w.all_text_outputs():
                self.assertNotIn(s.encode(), blob, f"{s} 泄露到 {fn}")
        # A generated timestamp such as T080806 can contain '8080' without
        # revealing a port. UUIDs are public opaque identifiers, too.
        uuid_pattern=rb'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'
        port_pattern=rb'(?<![0-9A-Za-z])(?:5432|8080)(?![0-9A-Za-z])'
        self.assertIsNone(re.search(port_pattern,b'discover-20261009T080806+0800.md'))
        self.assertIsNotNone(re.search(port_pattern,b'"port": 8080'))
        for fn,blob in [('stdout',out_text.encode()),*w.all_text_outputs()]:
            self.assertIsNone(re.search(port_pattern,re.sub(uuid_pattern,b'<id>',blob)),f'端口泄露到 {fn}')
        md = rd(os.path.join(w.reports, "discover-latest.md"))
        self.assertIn("项目盘点报告", md)
        self.assertIn("需核实", md)

    def test_stable_ids_relocate_and_registry_rebuild(self):
        w = self.w
        w.discover()
        first = dict(w.ids)
        w.discover()
        self.assertEqual(w.ids, first, "重复 discover 必须保持 UUID")

        w.ok("bootstrap", "--project", first["alpha"])
        # 显式 relocate：路径变化后 id 与记忆目录不变
        newp = w.p("video-workshop-moved")
        os.makedirs(newp)
        r = w.ok("relocate", "--project", first["alpha"], "--path", newp.replace("\\", "/"))
        self.assertEqual(r["project"]["project_id"], first["alpha"])
        res = w.ok("discover")  # 台账仍是旧路径
        alpha = [p for p in res["projects"] if p["project_id"] == first["alpha"]][0]
        self.assertIn("ledger_path_outdated", alpha["flags"])
        self.assertEqual(res["counts"]["total"], 6, "旧路径不能被当作新项目")
        self.assertEqual(os.path.normcase(alpha["path"]), os.path.normcase(newp))

        # 别名可用于解析
        w.ok("alias", "--project", first["beta"], "--add", "老看板")
        self.assertEqual(w.ok("resume", "--project", "老看板", "--no-git")["project"]["project_id"], first["beta"])

        # registry 丢失后：已 bootstrap 项目从 overview frontmatter 找回 UUID（按 bootstrap 时路径）
        w.ok("relocate", "--project", first["alpha"], "--path", w.paths["alpha"])
        os.remove(w.registry)
        w.discover()
        self.assertEqual(w.ids["alpha"], first["alpha"])

    def test_dry_run_and_missing_ledger(self):
        w = self.w
        res = w.ok("discover", "--dry-run")
        self.assertTrue(res["dry_run"])
        self.assertFalse(os.path.exists(w.registry))
        os.remove(w.ledger)
        code, data, _, _ = w.run("discover")
        self.assertEqual(code, 1)
        self.assertEqual(data["error"]["code"], "ledger_not_found")


if __name__ == "__main__":
    unittest.main()
