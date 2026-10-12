"""handoff：乐观 revision、两进程并发、冲突留档、幂等、秘密拒绝、崩溃恢复。"""

import json
import os
import subprocess
import unittest

from helpers import ROOT, World, rd


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.build()
        self.w.discover()
        self.w.ok("bootstrap", "--project", self.w.ids["alpha"])
        self.pid = self.w.ids["alpha"]
        self.md = self.w.memdir("alpha")
        self.hist = os.path.join(self.md, "history")

    def tearDown(self):
        self.w.cleanup()

    def payload(self, rev, summary="完成一次工作", **kw):
        p = {"project": self.pid, "actor": "claude-code", "summary": summary, "completed": ["实现 A"],
             "validation": ["单测 3/3"], "pending": ["B"], "next_steps": ["做 C"], "evidence": ["tests/x.py"],
             "expected_revision": rev, "session_id": "s-1", "branch": "main"}
        p.update(kw)
        return p

    def current_meta(self):
        txt = rd(os.path.join(self.md, "current.md"))
        meta = {}
        for line in txt.split("---")[1].strip().splitlines():
            k, v = line.split(":", 1)
            meta[k.strip()] = json.loads(v)
        return meta, txt

    def history_files(self):
        return sorted(f for f in os.listdir(self.hist) if f.endswith(".md"))

    def test_accept_then_conflict_keeps_current(self):
        code, d, _, _ = self.w.handoff(self.payload(0))
        self.assertEqual((code, d["status"], d["revision"]), (0, "accepted", 1))
        meta, txt = self.current_meta()
        self.assertEqual(meta["revision"], 1)
        self.assertRegex(meta["updated_at"], r"[+-]\d\d:\d\d$", "时间必须带时区")
        self.assertIn("单测 3/3", txt)

        code, d, _, _ = self.w.handoff(self.payload(0, summary="基于旧版本的交接"))
        self.assertEqual((code, d["status"], d["current_revision"]), (0, "conflict", 1))
        meta2, txt2 = self.current_meta()
        self.assertEqual(txt2, txt, "冲突不得覆写 current")
        self.assertEqual(len(self.history_files()), 2)
        r = self.w.ok("resume", "--project", self.pid, "--no-git")
        self.assertIn("unresolved_conflicts", [x["code"] for x in r["warnings"]])
        self.assertEqual(r["expected_revision_for_next_handoff"], 1)

        code, d, _, _ = self.w.handoff(self.payload(1, summary="合并后重新提交"))
        self.assertEqual((d["status"], d["revision"]), ("accepted", 2))

    def test_two_processes_concurrent_only_one_wins(self):
        procs = []
        for i in range(2):
            p = subprocess.Popen(self.w.cmd("handoff", "--input", "-"), cwd=ROOT, env=self.w.env(),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            procs.append((p, json.dumps(self.payload(0, summary=f"并发交接 {i}", session_id=f"s-{i}"),
                                        ensure_ascii=False).encode("utf-8")))
        # 两个进程都已启动后再同时喂入输入
        for p, data in procs:
            p.stdin.write(data)
            p.stdin.close()
        results = []
        for p, _ in procs:
            out = p.stdout.read().decode("utf-8")
            p.stderr.read()
            p.wait(timeout=60)
            p.stdout.close()
            p.stderr.close()
            results.append(json.loads(out))
        self.assertEqual(sorted(r["status"] for r in results), ["accepted", "conflict"], results)
        files = self.history_files()
        self.assertEqual(len(files), 2, "两条历史都必须保存")
        self.assertEqual(sum("__conflict__" in f for f in files), 1)
        self.assertEqual(self.current_meta()[0]["revision"], 1)

    def test_idempotency(self):
        _, d1, _, _ = self.w.handoff(self.payload(0))
        _, d2, _, _ = self.w.handoff(self.payload(0))
        self.assertEqual(d1["status"], "accepted")
        self.assertEqual((d2["status"], d2["revision"], d2["history_file"]), ("duplicate", 1, d1["history_file"]))
        self.assertEqual(len(self.history_files()), 1)

        _, d3, _, _ = self.w.handoff(self.payload(1, idempotency_key="k-1"))
        _, d4, _, _ = self.w.handoff(self.payload(1, idempotency_key="k-1"))
        self.assertEqual((d3["status"], d4["status"]), ("accepted", "duplicate"))
        code, d5, _, _ = self.w.handoff(self.payload(2, summary="不同内容", idempotency_key="k-1"))
        self.assertEqual((code, d5["error"]["code"]), (1, "idempotency_key_reused"))
        self.assertEqual(len(self.history_files()), 2)

    def test_secrets_and_validation_rejected(self):
        bad = [
            self.payload(0, summary="数据库 password=abc123 已配置"),
            self.payload(0, evidence=["postgres://u:pw@localhost/db"]),
            self.payload(0, completed=["Authorization: Bearer " + "abcdefghijklmnopqrstu"]),
            self.payload(0, validation=["key " + "sk-" + "abcdefghijklmnopqrstuv123"]),
        ]
        for p in bad:
            code, d, out, _ = self.w.handoff(p)
            self.assertEqual((code, d["error"]["code"]), (1, "secret_detected"))
            self.assertNotIn("abc123", out)
            self.assertNotIn("abcdefghijklmnopqrstu", out)
        for p in (self.payload(-1), {**self.payload(0), "extra": 1}, {**self.payload(0), "summary": ""},
                  {k: v for k, v in self.payload(0).items() if k != "expected_revision"}):
            code, d, _, _ = self.w.handoff(p)
            self.assertEqual((code, d["error"]["code"]), (1, "invalid_handoff"))
        code, d, _, _ = self.w.run("handoff", "--json", "{bad json")
        self.assertEqual(d["error"]["code"], "invalid_json")
        self.assertEqual(self.history_files(), [])
        # “修复 token 过期”这类描述不是秘密值，允许写入（但索引/读取时整行过滤）
        code, d, _, _ = self.w.handoff(self.payload(0, summary="修复 token 过期刷新问题"))
        self.assertEqual(d["status"], "accepted")

    def test_crash_recovery(self):
        self.w.handoff(self.payload(0))
        code, d, out, _ = self.w.handoff(self.payload(1, summary="崩溃前写入的交接"),
                                         extra_env={"PROJECT_MEMORY_FAULT": "after_history_write"})
        self.assertEqual(code, 70)
        self.assertEqual(out.strip(), "")
        self.assertEqual(len(self.history_files()), 2)
        self.assertEqual(self.current_meta()[0]["revision"], 1, "模拟崩溃：current 尚未更新")
        st = self.w.ok("status", "--project", self.pid)["project"]
        self.assertEqual((st["head_revision"], st["current_revision"], st["current_consistent"]), (2, 1, False))
        r = self.w.ok("resume", "--project", self.pid, "--no-git")
        self.assertIn("current_out_of_sync", [x["code"] for x in r["warnings"]])
        self.assertEqual(r["handoff"]["revision"], 2)
        self.assertEqual(r["handoff"]["summary"], "崩溃前写入的交接")

        rec = self.w.ok("recover", "--project", self.pid)
        self.assertTrue(rec["repaired"])
        meta, txt = self.current_meta()
        self.assertEqual(meta["revision"], 2)
        self.assertIn("崩溃前写入的交接", txt)
        self.assertFalse(self.w.ok("recover", "--project", self.pid)["repaired"])

        # current 丢失：下一次 handoff 自动从历史重建后再写
        os.remove(os.path.join(self.md, "current.md"))
        _, d, _, _ = self.w.handoff(self.payload(2, summary="丢失后继续"))
        self.assertEqual((d["status"], d["revision"]), ("accepted", 3))
        self.assertTrue(d["recovered_before_write"]["recovered"])
        self.assertEqual(self.current_meta()[0]["revision"], 3)
        self.assertEqual(len([f for f in os.listdir(self.hist) if f.endswith(".part")]), 0)


if __name__ == "__main__":
    unittest.main()
