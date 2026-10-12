"""stdio MCP 真子进程：initialize/版本协商/initialized/ping/tools/list/tools/call/错误路径。"""

import json
import subprocess
import unittest

from helpers import ROOT, World


class McpTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.build()
        self.w.discover()
        self.w.ok("bootstrap", "--all-existing")

    def tearDown(self):
        self.w.cleanup()

    def session(self, messages):
        p = subprocess.Popen(self.w.cmd("mcp"), cwd=ROOT, env=self.w.env(), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        lines = []
        for m in messages:
            lines.append(m if isinstance(m, str) else json.dumps(m, ensure_ascii=False))
        out, err = p.communicate(("\n".join(lines) + "\n").encode("utf-8"), timeout=120)
        self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))
        resp = []
        for line in out.decode("utf-8").splitlines():
            obj = json.loads(line)  # stdout 每一行都必须是 JSON-RPC
            self.assertEqual(obj["jsonrpc"], "2.0")
            resp.append(obj)
        self.assertIn("MCP stdio server started", err.decode("utf-8"))
        return resp, {r.get("id"): r for r in resp}

    @staticmethod
    def call(i, name, args):
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}

    def test_full_session(self):
        w = self.w
        alpha_fwd = w.paths["alpha"].replace("\\", "/") + "/src/deep"
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            self.call(4, "memory_projects", {"state": "active"}),
            self.call(5, "memory_resume", {"cwd": alpha_fwd, "git": False}),
            self.call(6, "memory_handoff", {"project": w.ids["alpha"], "actor": "mcp", "summary": "通过 MCP 写入的交接",
                                            "completed": ["协议测试"], "expected_revision": 0, "session_id": "m1"}),
            self.call(7, "memory_search", {"query": "协议测试", "limit": 5}),
            self.call(8, "memory_read", {"project": w.ids["alpha"], "ref": "memory/current.md", "start_line": 1,
                                         "end_line": 30}),
            self.call(9, "memory_status", {"project": w.ids["alpha"]}),
            self.call(10, "memory_resume", {"project": "数据看板"}),
            self.call(11, "memory_search", {"query": "x", "project": "根本不存在"}),
            self.call(12, "memory_search", {"query": "x", "bogus": 1}),
            self.call(13, "memory_handoff", {"project": w.ids["alpha"], "actor": "mcp", "summary": "旧版本",
                                             "expected_revision": 0}),
            self.call(14, "memory_read", {"project": w.ids["alpha"], "ref": "memory/../../data/registry.json"}),
            self.call(15, "no_such_tool", {}),
            {"jsonrpc": "2.0", "id": 16, "method": "resources/list"},
            "{这不是 JSON",
            self.call(17, "memory_resume", {}),
            self.call(18, "memory_search", {"query": "x", "limit": 0}),
            {"jsonrpc": "2.0", "id": 19, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            [{"jsonrpc": "2.0", "id": 20, "method": "ping"}],
        ]
        resp, by = self.session(msgs)
        self.assertEqual(by[1]["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", by[1]["result"]["capabilities"])
        self.assertEqual(by[19]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(by[2]["result"], {})
        names = {t["name"] for t in by[3]["result"]["tools"]}
        self.assertEqual(names, {"memory_projects", "memory_resume", "memory_search", "memory_read",
                                 "memory_handoff", "memory_status", "memory_initialize"})
        for t in by[3]["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"]["additionalProperties"])

        def sc(i):
            r = by[i]["result"]
            self.assertEqual(json.loads(r["content"][0]["text"]), r["structuredContent"])
            return r["isError"], r["structuredContent"]

        err, d = sc(4)
        self.assertFalse(err)
        self.assertEqual(d["count"], 3)  # alpha、beta、gamma（nested 需核实、old 归档、ghost 缺失）
        err, d = sc(5)
        self.assertFalse(err)
        self.assertEqual(d["project"]["project_id"], w.ids["alpha"])
        err, d = sc(6)
        self.assertEqual((err, d["status"], d["revision"]), (False, "accepted", 1))
        err, d = sc(7)
        self.assertFalse(err)
        self.assertGreater(d["count"], 0)
        self.assertEqual(d["results"][0]["project_name"], "视频二创工坊")
        err, d = sc(8)
        self.assertFalse(err)
        self.assertIn("通过 MCP 写入的交接", "\n".join(x["text"] for x in d["lines"]))
        err, d = sc(9)
        self.assertEqual(d["project"]["head_revision"], 1)
        err, d = sc(10)
        self.assertTrue(err)
        self.assertEqual(d["error"]["code"], "ambiguous_project")
        err, d = sc(11)
        self.assertEqual((err, d["error"]["code"]), (True, "project_not_found"))
        err, d = sc(12)
        self.assertEqual((err, d["error"]["code"]), (True, "invalid_arguments"))
        err, d = sc(13)
        self.assertFalse(err)
        self.assertEqual(d["status"], "conflict")
        err, d = sc(14)
        self.assertTrue(err)
        self.assertEqual(by[15]["error"]["code"], -32602)
        self.assertEqual(by[16]["error"]["code"], -32601)
        parse_err = [r for r in resp if r.get("id") is None and r.get("error", {}).get("code") == -32700]
        self.assertEqual(len(parse_err), 1)
        batch_err = [r for r in resp if r.get("id") is None and r.get("error", {}).get("code") == -32600]
        self.assertEqual(len(batch_err), 1)
        err, d = sc(17)
        self.assertEqual((err, d["error"]["code"]), (True, "project_required"))
        err, d = sc(18)
        self.assertEqual((err, d["error"]["code"]), (True, "invalid_arguments"))
        # 通知没有响应：响应数 = 带 id 的请求数 + 2 个无 id 错误
        self.assertEqual(len(resp), 19 + 2)


class CliHelpTest(unittest.TestCase):
    def test_every_command_has_help(self):
        w = World()
        try:
            for c in ("discover", "projects", "bootstrap", "sources", "search", "read", "handoff", "resume",
                      "recover", "relocate", "alias", "reindex", "status", "mcp"):
                p = subprocess.run(w.cmd(c, "--help"), cwd=ROOT, env=w.env(), capture_output=True, timeout=60)
                self.assertEqual(p.returncode, 0, c)
                self.assertIn("usage", p.stdout.decode("utf-8"))
            d = w.ok("status")
            self.assertFalse(d["registry"]["exists"])
            self.assertFalse(d["ledger"]["exists"])
        finally:
            w.cleanup()


if __name__ == "__main__":
    unittest.main()
