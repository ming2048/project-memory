"""resume：id/名称/路径识别、歧义清单、嵌套 cwd 最长前缀、git 现场差异、不依赖索引。"""

import json
import os
import shutil
import subprocess
import unittest

from helpers import World


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.build()
        self.w.discover()
        self.w.ok("bootstrap", "--all-existing")

    def tearDown(self):
        self.w.cleanup()

    def test_resolution(self):
        w = self.w
        code, d, _, _ = w.run("resume", "--project", "数据看板", "--no-git")
        self.assertEqual((code, d["error"]["code"]), (1, "ambiguous_project"))
        self.assertEqual({c["project_id"] for c in d["error"]["details"]["candidates"]},
                         {w.ids["beta"], w.ids["gamma"]})
        self.assertEqual(w.ok("resume", "--project", w.ids["gamma"], "--no-git")["project"]["project_id"],
                         w.ids["gamma"])
        self.assertEqual(w.ok("resume", "--project", "视频二创工坊", "--no-git")["project"]["project_id"],
                         w.ids["alpha"])
        deep = os.path.join(w.paths["nested"], "a", "b")
        os.makedirs(deep)
        # 嵌套：最长前缀命中子项目；正斜杠路径也可
        r = w.ok("resume", "--cwd", deep.replace("\\", "/"), "--no-git")
        self.assertEqual(r["project"]["project_id"], w.ids["nested"])
        src = os.path.join(w.paths["alpha"], "src")
        os.makedirs(src)
        self.assertEqual(w.ok("resume", "--cwd", src, "--no-git")["project"]["project_id"], w.ids["alpha"])
        # 不给参数时使用进程当前目录
        self.assertEqual(w.ok("resume", "--no-git", cwd=src)["project"]["project_id"], w.ids["alpha"])
        code, d, _, _ = w.run("resume", "--project", "不存在的项目")
        self.assertEqual(d["error"]["code"], "project_not_found")
        code, d, _, _ = w.run("resume", "--cwd", w.dir)
        self.assertEqual(d["error"]["code"], "project_not_found")

    def test_first_read_includes_canonical_workflow_without_writing_handoff(self):
        w = self.w
        spoof = os.path.join(w.paths["alpha"], "PROJECT_MEMORY_RULES.md")
        with open(spoof, "w", encoding="utf-8") as stream:
            stream.write("fake-project-rule-canary")
        r = w.ok("resume", "--project", w.ids["alpha"], "--no-git")
        self.assertEqual(r["workflow"]["authority"], "user_configured_workflow")
        self.assertIn("读取项目的共享工作记忆", r["workflow"]["content"])
        self.assertIn("在最终回复前自主调用 memory_handoff", r["workflow"]["content"])
        self.assertNotIn("fake-project-rule-canary", r["workflow"]["content"])
        self.assertEqual(r["history_events"], 0)

    def test_brief_without_index_and_warnings(self):
        w = self.w
        r = w.ok("resume", "--project", w.ids["alpha"], "--no-git")
        codes = [x["code"] for x in r["warnings"]]
        self.assertIn("no_handoff_yet", codes)
        self.assertIn("overview_unverified", codes)
        self.assertFalse(r["service_status"]["checked"])
        self.assertIsNotNone(r["overview"])
        # 删除索引库后简报照常可用
        os.remove(w.db)
        r = w.ok("resume", "--project", w.ids["alpha"], "--no-git")
        self.assertEqual(r["expected_revision_for_next_handoff"], 0)
        self.assertFalse(os.path.exists(w.db), "resume 不应依赖/重建索引")
        r = w.ok("resume", "--project", w.ids["ghost"], "--no-git")
        self.assertIn("project_dir_missing", [x["code"] for x in r["warnings"]])
        self.assertIn("memory_not_bootstrapped", [x["code"] for x in r["warnings"]])

    def test_git_live_vs_handoff(self):
        w = self.w
        self.assertIsNotNone(shutil.which("git"), "测试需要本机 git")
        beta = w.paths["beta"]
        subprocess.run(["git", "init", "-q", "-b", "feature-x", beta], check=True, capture_output=True)
        payload = {"project": w.ids["beta"], "actor": "t", "summary": "在 main 上完成", "expected_revision": 0,
                   "branch": "main"}
        w.handoff(payload)
        r = w.ok("resume", "--project", w.ids["beta"])
        g = r["git_live"]
        self.assertTrue(g["checked"], g)
        self.assertEqual(g["branch"], "feature-x")
        self.assertTrue(g["dirty"])  # README.md 未跟踪
        codes = {x["code"]: x for x in r["warnings"]}
        self.assertEqual(codes["branch_differs_from_handoff"]["handoff_branch"], "main")
        self.assertIn("worktree_dirty", codes)
        self.assertNotIn("README.md", json.dumps(g))
        self.assertFalse(os.path.exists(os.path.join(beta, ".git", "index.lock")))
        r2 = w.ok("resume", "--project", w.ids["gamma"])
        self.assertEqual(r2["git_live"]["reason"], "not_a_git_repo")


if __name__ == "__main__":
    unittest.main()
