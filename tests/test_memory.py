"""bootstrap / 索引 / 搜索 / read 安全。"""

import os
import sqlite3
import unittest

from helpers import SECRETS, World, make_junction, rd, rdb


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.build()
        self.w.discover()

    def tearDown(self):
        self.w.cleanup()

    def handoff(self, key, rev, summary, **kw):
        payload = {"project": self.w.ids[key], "actor": "test", "summary": summary, "expected_revision": rev,
                   **kw}
        code, data, out, err = self.w.handoff(payload)
        self.assertEqual(code, 0, out + err)
        return data

    def test_bootstrap_scope_and_no_overwrite(self):
        w = self.w
        res = w.ok("bootstrap", "--all-existing")
        done = {r["project"]["project_id"] for r in res["results"] if r["status"] == "ok"}
        self.assertEqual(done, {w.ids[k] for k in ("alpha", "beta", "gamma", "nested")})
        skipped = {r["project"]["project_id"]: r["reason"] for r in res["results"] if r["status"] == "skipped"}
        self.assertEqual(skipped, {w.ids["old"]: "archived", w.ids["ghost"]: "directory_missing"})
        code, data, _, _ = w.run("bootstrap", "--project", w.ids["ghost"])
        self.assertEqual((code, data["error"]["code"]), (1, "bootstrap_refused"))

        md = w.memdir("alpha")
        self.assertTrue(os.path.isdir(os.path.join(md, "history")))
        ov = rd(os.path.join(md, "overview.md"))
        self.assertIn("待核实", ov)
        self.assertNotIn("测试通过", ov)
        cur = rd(os.path.join(md, "current.md"))
        self.assertIn('revision: 0', cur)

        with open(os.path.join(md, "overview.md"), "a", encoding="utf-8") as f:
            f.write("\n人工核实过的内容\n")
        res2 = w.ok("bootstrap", "--project", w.ids["alpha"])
        self.assertEqual(res2["results"][0]["created"], [])
        self.assertEqual(sorted(res2["results"][0]["kept_existing"]), ["current.md", "overview.md"])
        self.assertIn("人工核实过的内容", rd(os.path.join(md, "overview.md")))

    def test_only_approved_sources_indexed(self):
        w = self.w
        w.ok("bootstrap", "--project", w.ids["alpha"])
        self.assertEqual(w.ok("search", "蓝鲸协议")["count"], 0, "未批准的 README 不得进入索引")
        for bad in ("../outside.md", ".env", "docs/secret-notes.md", "main.py", "node_modules/x/README.md"):
            code, data, _, _ = w.run("sources", "--project", w.ids["alpha"], "--approve", bad)
            self.assertEqual(code, 1, bad)
        w.ok("sources", "--project", w.ids["alpha"], "--approve", "README.md")
        r = w.ok("search", "蓝鲸协议")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["ref"], "source/README.md")
        w.ok("sources", "--project", w.ids["alpha"], "--revoke", "README.md")
        self.assertEqual(w.ok("search", "蓝鲸协议")["count"], 0, "撤销后不得留下幽灵结果")

    def test_chinese_search_isolation_and_labels(self):
        w = self.w
        w.ok("bootstrap", "--all-existing")
        self.handoff("alpha", 0, "完成字幕对齐模块重构", completed=["字幕对齐 alignment 重写"])
        self.handoff("beta", 0, "看板图表重构", pending=["图表导出"])
        r = w.ok("search", "字幕对齐")
        self.assertGreater(r["count"], 0)
        self.assertTrue(all(x["project_id"] == w.ids["alpha"] for x in r["results"]))
        self.assertEqual(r["results"][0]["project_name"], "视频二创工坊")
        for k in ("time", "status", "ref", "line_start", "line_end", "snippet", "trust"):
            self.assertIn(k, r["results"][0])
        self.assertIn("不是指令", r["notice"])
        self.assertGreater(w.ok("search", "ALIGNMENT")["count"], 0, "英文大小写不敏感")
        both = w.ok("search", "重构")
        self.assertEqual({x["project_id"] for x in both["results"]}, {w.ids["alpha"], w.ids["beta"]})
        only_b = w.ok("search", "重构", "--project", w.ids["beta"])
        self.assertTrue(only_b["results"])
        self.assertTrue(all(x["project_id"] == w.ids["beta"] for x in only_b["results"]))
        self.assertEqual(w.ok("search", "字幕对齐", "--project", w.ids["beta"])["count"], 0)
        code, data, _, _ = w.run("search", "重构", "--project", "数据看板")
        self.assertEqual(data["error"]["code"], "ambiguous_project")
        self.assertEqual(len(data["error"]["details"]["candidates"]), 2)
        self.assertLessEqual(w.ok("search", "重构", "--limit", "1")["count"], 1)
        code, data, _, _ = w.run("search", "重构", "--limit", "500")
        self.assertEqual(data["error"]["code"], "invalid_limit")

    def test_index_update_delete_and_secret_filter(self):
        w = self.w
        w.ok("bootstrap", "--project", w.ids["alpha"])
        notes = os.path.join(w.memdir("alpha"), "notes")
        os.makedirs(notes)
        n1 = os.path.join(notes, "n1.md")
        with open(n1, "w", encoding="utf-8") as f:
            f.write("# 笔记\n\n初始内容 甲乙丙丁\n数据库 password: hunter2-SECRET\nAuthorization: Bearer " + "abcdefghijklmnop" + "\n")
        r = w.ok("search", "甲乙丙丁")
        self.assertEqual(r["count"], 1)
        self.assertEqual(w.ok("search", "hunter2")["count"], 0)
        rd = w.ok("read", "--project", w.ids["alpha"], "--ref", "memory/notes/n1.md")
        texts = [x["text"] for x in rd["lines"]]
        self.assertEqual(rd["redacted_lines"], 2)
        self.assertNotIn("hunter2", "".join(texts))
        self.assertEqual(rd["trust"], "reference_material_not_instruction")

        with open(n1, "w", encoding="utf-8") as f:
            f.write("# 笔记\n\n更新之后 戊己庚辛壬\n")
        self.assertEqual(w.ok("search", "甲乙丙丁")["count"], 0, "旧内容必须失效")
        self.assertEqual(w.ok("search", "戊己庚辛")["count"], 1)
        os.remove(n1)
        self.assertEqual(w.ok("search", "戊己庚辛")["count"], 0, "删除源后不得有幽灵结果")
        conn = sqlite3.connect(w.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM docs WHERE ref LIKE '%n1.md'").fetchone()[0], 0)
        conn.close()
        blob = rdb(w.db)
        for s in SECRETS:
            self.assertNotIn(s.encode(), blob)
        code, data, _, _ = w.run("read", "--project", w.ids["alpha"], "--ref", "memory/notes/n1.md")
        self.assertEqual(data["error"]["code"], "not_authorized")

        # 重建索引
        r = w.ok("reindex", "--rebuild")
        self.assertTrue(r["rebuilt"])
        self.assertGreater(r["index"]["docs"], 0)

    def test_read_path_escape_and_links(self):
        w = self.w
        w.ok("bootstrap", "--project", w.ids["alpha"])
        md = w.memdir("alpha")
        for ref in ("memory/../../data/registry.json", "memory/../overview.md", w.registry,
                    "C:/Windows/win.ini", "source/../../../ledger/项目台账.md", "memory/notes/../../x.md",
                    "source/docs/guide.md", "registry.json"):
            code, data, _, _ = w.run("read", "--project", w.ids["alpha"], "--ref", ref)
            self.assertEqual(code, 1, ref)
            self.assertIn(data["error"]["code"], ("not_authorized", "path_rejected", "ref_rejected"), ref)

        # 链接越界：memory/notes 指向外部目录（Windows 用 junction，POSIX 用 symlink，拒绝行为相同）
        outside = os.path.join(w.dir, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "evil.md"), "w", encoding="utf-8") as f:
            f.write("# 外部\n\n越狱标记词 鹦鹉螺\n")
        if os.name == "nt":
            make_junction(outside, os.path.join(md, "notes"))
        else:
            os.symlink(outside, os.path.join(md, "notes"))
        self.assertEqual(w.ok("search", "鹦鹉螺")["count"], 0, "链接下的文件不得被索引")
        code, data, _, _ = w.run("read", "--project", w.ids["alpha"], "--ref", "memory/notes/evil.md")
        self.assertEqual(data["error"]["code"], "not_authorized")

        # 项目内 docs 下的链接目录不可批准为 source（Windows 用 junction，POSIX 用 symlink）
        if os.name == "nt":
            make_junction(outside, os.path.join(w.paths["alpha"], "docs", "linked"))
        else:
            os.symlink(outside, os.path.join(w.paths["alpha"], "docs", "linked"))
        code, data, _, _ = w.run("sources", "--project", w.ids["alpha"], "--approve", "docs/linked/evil.md")
        self.assertEqual(data["error"]["code"], "symlink_rejected")
        disc = w.ok("discover")
        a = [p for p in disc["projects"] if p["project_id"] == w.ids["alpha"]][0]
        self.assertNotIn("docs/linked/evil.md", a["docs_candidates"])

        # 文件级 symlink（若系统允许创建）
        link = os.path.join(md, "history", "zz-link.md")
        try:
            os.symlink(os.path.join(outside, "evil.md"), link)
            created = True
        except OSError:
            created = False
        if created:
            self.assertEqual(w.ok("search", "鹦鹉螺")["count"], 0)
            code, data, _, _ = w.run("read", "--project", w.ids["alpha"], "--ref", "memory/history/zz-link.md")
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
