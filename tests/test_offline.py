"""Offline tests: no network, no token. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("EVA_TOKEN", "test-token-not-real")
AUDIT_FILE = os.path.join(tempfile.mkdtemp(), "audit.jsonl")

import server  # noqa: E402
import ubql  # noqa: E402


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class FakeTransport:
    """Replaces server._request: records payloads, answers from a queue."""

    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    def __call__(self, path, body=None, timeout=None):
        self.calls.append((path, json.loads(body) if body else None))
        ans = self.answers.pop(0) if self.answers else {"jsonrpc": "2.0", "result": None}
        return json.dumps(ans)


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = (server._request, server.READONLY, server.CACHE_TTL, server.AUDIT_PATH)
        server._cache.clear()
        # server may already be imported by another test module with other env (CI sets EVA_AUDIT_LOG=0)
        server.READONLY, server.CACHE_TTL, server.AUDIT_PATH = False, 300, AUDIT_FILE

    def tearDown(self):
        server._request, server.READONLY, server.CACHE_TTL, server.AUDIT_PATH = self._saved

    def fake(self, *answers):
        t = FakeTransport(*answers)
        server._request = t
        return t


class TestEnvelope(Base):
    def test_orm_call_is_jsonrpc_20_without_args(self):
        t = self.fake({"result": []})
        server.eva("CmfTask.list", {"filter": ["code", "=", "A-1"], "limit": 5}, use_cache=False)
        path, p = t.calls[0]
        self.assertEqual(path, "/api/?m=CmfTask.list")
        self.assertEqual(p["jsonrpc"], "2.0")
        self.assertNotIn("args", p)
        self.assertEqual(p["kwargs"]["slice"], [0, 5])  # EVA ignores limit
        self.assertNotIn("limit", p["kwargs"])

    def test_object_method_is_jsonrpc_22_with_args(self):
        t = self.fake({"result": True})
        server.eva("CmfDocument.lock", {}, args=["CmfDocument:1"])
        p = t.calls[0][1]
        self.assertEqual((p["jsonrpc"], p["args"]), ("2.2", ["CmfDocument:1"]))

    def test_empty_args_still_22(self):
        t = self.fake({"result": {"id": "CmfComment:1"}})
        server.eva("CmfComment.create_dummy", {"parent_id": "x"}, args=[])
        self.assertEqual(t.calls[0][1]["jsonrpc"], "2.2")

    def test_top_level_keys(self):
        t = self.fake({"result": True})
        server.eva("CmfDocument.delete_node", {}, top={"filter": ["id", "==", "CmfDocument:1"], "fields": []})
        self.assertEqual(t.calls[0][1]["filter"], ["id", "==", "CmfDocument:1"])

    def test_endpoints(self):
        t = self.fake({"result": None}, {"result": None})
        server.eva("CmfDocument.public_get", {}, endpoint="pub", use_cache=False)
        server.eva("CmfTask.sdesk_get", {}, endpoint="servicedesk", use_cache=False)
        self.assertTrue(t.calls[0][0].startswith("/pub/pub_api?m="))
        self.assertTrue(t.calls[1][0].startswith("/servicedesk/sd_api?m="))

    def test_abort_and_error(self):
        self.fake({"abort": "must_transition_run T X A B"})
        with self.assertRaises(server.EvaAbort):
            server.eva("CmfTask.update", {"status": "s"}, args=["CmfTask:1"])
        self.fake({"error": {"code": -32001, "message": "boom\nTraceback...\nValueError: bad"}})
        with self.assertRaises(RuntimeError) as cm:
            server.eva("CmfTask.get", {}, use_cache=False)
        self.assertIn("ValueError: bad", str(cm.exception))

    def test_read_cache_and_write_invalidation(self):
        t = self.fake({"result": [1]}, {"result": True}, {"result": [2]})
        self.assertEqual(server.eva("CmfTask.list", {"x": 1}), [1])
        self.assertEqual(server.eva("CmfTask.list", {"x": 1}), [1])  # cached
        server.eva("CmfTask.update", {"a": 1}, args=["CmfTask:1"])
        self.assertEqual(server.eva("CmfTask.list", {"x": 1}), [2])  # cache cleared by write
        self.assertEqual(len(t.calls), 3)


class TestReadOnlyAndAudit(Base):
    def test_readonly_blocks_writes_before_network(self):
        server.READONLY = True
        t = self.fake()
        for m in ("CmfDocument.update", "CmfTask.create_dummy_task", "CmfComment.delete", "CmfDocument.delete_node"):
            with self.assertRaises(server.ReadOnlyError):
                server.eva(m, {}, args=["x"])
        self.assertEqual(t.calls, [])

    def test_readonly_allows_reads_and_safe_calls(self):
        server.READONLY = True
        self.fake({"result": []}, {"result": []}, {"result": "[]"})
        server.eva("CmfTask.list", {}, use_cache=False)
        server.eva("CmfFullSearch.fulltext_search", {}, args=["ANY", "ANY", "q"])
        server.eva("CmfTask.ubql2bql", {}, args=["code = 1"])

    def test_readonly_hides_write_tools(self):
        server.READONLY = True
        names = {t["name"] for t in server.visible_tools()}
        self.assertFalse(names & server.WRITE_TOOLS)
        self.assertIn("eva_document", names)  # grouped tool; legacy names are hidden aliases
        doc = [t for t in server.visible_tools() if t["name"] == "eva_document"][0]
        self.assertNotIn("update", doc["inputSchema"]["properties"]["action"]["enum"])
        with self.assertRaises(server.ReadOnlyError):
            server.run_tool("eva_update_document", {"code": "DOC-1", "content": "x"})

    def test_write_tools_exist(self):
        all_names = {t["name"] for t in server.TOOLS}
        self.assertTrue(server.WRITE_TOOLS <= all_names | {"eva_refresh_catalog"}, server.WRITE_TOOLS - all_names)

    def test_audit_logs_writes_not_token(self):
        write(server.AUDIT_PATH, "")
        self.fake({"result": "CmfDocument:1"}, {"result": []})
        server.eva("CmfDocument.update", {"text_draft": "x" * 500}, args=["CmfDocument:1"])
        server.eva("CmfDocument.list", {}, use_cache=False)  # reads are not logged
        lines = read(server.AUDIT_PATH).splitlines()
        self.assertEqual(len(lines), 1)
        e = json.loads(lines[0])
        self.assertEqual((e["method"], e["ok"], e["args"]), ("CmfDocument.update", True, ["CmfDocument:1"]))
        self.assertLess(len(e["kwargs"]["text_draft"]), 260)  # long values are shortened
        self.assertNotIn(os.environ["EVA_TOKEN"], lines[0])

    def test_audit_logs_failed_write(self):
        write(server.AUDIT_PATH, "")
        self.fake({"abort": "no permission"})
        with self.assertRaises(server.EvaAbort):
            server.eva("CmfDocument.archive", {}, args=["CmfDocument:1"])
        e = json.loads(read(server.AUDIT_PATH).splitlines()[0])
        self.assertFalse(e["ok"])
        self.assertIn("no permission", e["error"])


class TestRender(Base):
    def test_text_ver_matches_frontend(self):
        # value the EVA web editor sent for this exact render (recorded in the browser)
        s = ('<p>draft</p>\n<p>typed paragraph</p>\n<p><img src="/files/obj/CmfDocument/CmfDocument%3A4ff/'
             'CmfDocument%3A4ffc1771-e44f-11f1-8800-668c2c43d616/image-mce-20261119-222823754.png?version=1" '
             'data-attach-version="1" data-attach-id="CmfAttachment:63538e3d-e44f-11f1-b69f-ea139fcf7db3"></p>')
        self.assertEqual(server.text_ver(s), "175712411733312")

    def test_render_html_strips_editor_attrs(self):
        stored = '<div><p data-id="abc">x</p><img data-mce-src="/a" src="/a" data-id="q"/></div>'
        # editor serialization: blocks on their own lines, editor-only attributes dropped
        self.assertEqual(server.render_html(stored), '<p>x</p>\n<img src="/a">')

    def test_add_data_ids(self):
        h = server.add_data_ids("<p>a</p><ul><li>b</li></ul><p data-id=\"keep\">c</p>")
        self.assertEqual(h.count("data-id="), 4)
        self.assertIn('data-id="keep"', h)

    def test_markdown(self):
        h = server.md_to_html("# T\n\n**b** `c` [l](https://x)\n\n- a\n- b\n\n```\ncode <x>\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |")
        for frag in ("<h1>T</h1>", "<strong>b</strong>", "<code>c</code>", '<a href="https://x">l</a>',
                     "<ul><li>a</li><li>b</li></ul>", "<pre>code &lt;x&gt;</pre>", 'data-macros="wikitables"',
                     '<th data-x="0" data-y="0">A</th>', '<td data-x="1" data-y="1">2</td>', "<colgroup>"):
            self.assertIn(frag, h)  # tables are written in the form the editor normalizes them to

    def test_mention(self):
        orig = server.find_people
        server.find_people = lambda q, limit=10: [{"id": "CmfPerson:1", "login": "jdoe@example.com", "name": "John Doe"}]
        try:
            h = server.md_to_html("Привет @[jdoe]!")
        finally:
            server.find_people = orig
        self.assertIn('data-mention-type="person" data-object-id="CmfPerson:1"', h)
        self.assertIn('href="/struct/Person/jdoe@example.com"', h)
        self.assertIn(">@John Doe</a>", h)

    def test_html_to_md(self):
        md = server.html_to_md('<h2>H</h2><p>a <strong>b</strong></p><ul><li>x</li></ul><pre>c</pre>')
        self.assertIn("## H", md)
        self.assertIn("**b**", md)
        self.assertIn("- x", md)
        self.assertIn("```", md)


class TestUbql(unittest.TestCase):
    META = {
        "project": {"widget": "CmfRelation", "model": "CmfProject"},
        "status": {"widget": "CmfStatus", "model": "CmfStatus"},
        "responsible": {"widget": "CmfPersonWidget", "models": ["CmfPerson"]},
        "executors": {"widget": "CmfPersonMultiSelect", "models": ["CmfPerson"]},
        "cache_status_type": {"widget": "CmfChoice", "choices": {"OPEN": "Открыт", "CLOSED": "Закрыто"}},
        "priority": {"widget": "CmfPriority", "choices": {"0": "Обычный", "1": "Высокий"}},
        "name": {"widget": "CmfStr"}, "deadline": {"widget": "CmfDateTime"},
        "cmf_created_at": {"widget": "CmfDateTime"}, "lists": {"widget": "CmfListsMultiSelect", "model": "CmfList"},
    }

    def c(self, q):
        ids = {("CmfProject", "my-project"): ["CmfProject:p"], ("CmfPerson", "jdoe"): ["CmfPerson:i"],
               ("CmfList", "SPR-1"): ["CmfList:s"]}
        return ubql.Compiler("CmfTask", self.META, lambda m, v: ids.get((m, v), [])).compile(q)

    def test_relations_status_choice(self):
        f, _ = self.c('project = my-project and status != "Закрыто" and priority = "Высокий"')
        self.assertEqual(f, ["AND", ["project", "=", "CmfProject:p"], ["status.name", "!=", "Закрыто"], ["priority", "=", "1"]])

    def test_aliases_order_in_currentuser(self):
        f, order = self.c('assignee = currentUser() AND statusCategory IN ("Открыт", "CLOSED") ORDER BY created DESC, key')
        self.assertEqual(f, ["AND", ["responsible", "=", "__G_CURRENT_USER"], ["cache_status_type", "IN", ["OPEN", "CLOSED"]]])
        self.assertEqual(order, ["-cmf_created_at", "code"])

    def test_precedence_and_parentheses(self):
        f, _ = self.c('name ~ "a" or name ~ "b" and deadline < "2026-01-01"')
        self.assertEqual(f, ["OR", ["name", "ILIKE", "%a%"], ["AND", ["name", "ILIKE", "%b%"], ["deadline", "<", "2026-01-01"]]])
        f, _ = self.c('(name ~ "a" or name ~ "b") and deadline < "2026-01-01"')
        self.assertEqual(f[0], "AND")

    def test_m2m_and_empty(self):
        f, _ = self.c("executors = jdoe and sprint = SPR-1 and deadline is empty")
        self.assertEqual(f, ["AND", ["executors", "IN", ["CmfPerson:i"]], ["lists", "IN", ["CmfList:s"]], ["deadline", "=", None]])

    def test_unknown_value_is_error(self):
        with self.assertRaises(ubql.UbqlError):
            self.c("project = nope")


class TestExtractor(unittest.TestCase):
    JS = ('class a extends e.CmfDocument{foo(x){return this.callMethod("export_to_pdf",this.id.value)}'
          'static bar(x){return this.objRpc.call(this.className,"copy_tree_node",{src_tree_node_id:x,recursive:!0})}'
          'baz(x,y){return this.callMethod("approve_one",this.id.value,{text:x},{filter:y})}}')
    PLUGIN = ("var M = 'CmfTask.acme_import_batch';\nfunction rpc(method, kwargs){}\n"
              "rpc('CmfTask.acme_archive_run', {task_ref: t, action: a});\n"
              "const payload = {task_ref: r, mode: 1};\nrpc(M, payload);\n"
              "task.callMethod(x === 'kt' ? 'kt_open' : 'kpe_status_open', id);\n")

    def test_extract(self):
        d = tempfile.mkdtemp()
        main, plug = os.path.join(d, "main.js"), os.path.join(d, "plugins_acme_static_script.js")
        write(main, self.JS)
        write(plug, self.PLUGIN)
        out = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "tools", "extract_protocol.py"), main, plug],
                             capture_output=True, text=True, check=True).stdout
        p = json.loads(out)
        doc = p["CmfDocument"]
        self.assertEqual(doc["export_to_pdf"]["kind"], "object")
        self.assertEqual(doc["copy_tree_node"]["variants"][0]["kwargs"], ["src_tree_node_id", "recursive"])
        self.assertEqual(doc["approve_one"]["variants"][0]["top"], ["filter"])
        t = p["CmfTask"]
        self.assertEqual(t["acme_archive_run"]["variants"][0]["kwargs"], ["task_ref", "action"])
        self.assertEqual(t["acme_import_batch"]["variants"][0]["kwargs"], ["task_ref", "mode"])
        self.assertIn("kpe_status_open", t)
        self.assertNotIn("kt", t)
        self.assertIn("update", p["*"])


class TestMcpStdio(unittest.TestCase):
    def run_server(self, env_extra, *msgs):
        env = dict(os.environ, **env_extra)
        inp = "".join(json.dumps(m) + "\n" for m in msgs)
        out = subprocess.run([sys.executable, os.path.join(ROOT, "server.py")], input=inp, capture_output=True,
                             text=True, env=env, timeout=60).stdout
        return [json.loads(l) for l in out.splitlines() if l.strip()]

    def test_handshake_and_tools(self):
        r = self.run_server({}, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                            {"jsonrpc": "2.0", "method": "notifications/initialized"},
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                            {"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(r[0]["result"]["serverInfo"]["name"], "eva")
        tools = r[1]["result"]["tools"]
        self.assertGreater(len(tools), 5)
        for t in tools:
            self.assertTrue(t["name"].startswith("eva_") and t["description"] and t["inputSchema"]["type"] == "object")
        self.assertEqual(r[2]["error"]["code"], -32601)

    def test_readonly_has_fewer_tools(self):
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        rw = len(self.run_server({"EVA_READONLY": ""}, req)[0]["result"]["tools"])
        ro = len(self.run_server({"EVA_READONLY": "1"}, req)[0]["result"]["tools"])
        self.assertLess(ro, rw)


if __name__ == "__main__":
    unittest.main()


class TestGit(Base):
    RID = "CmfEvagitRepo:817269d1-d16b-11f1-8310-0abc87ac023c"

    def test_read_calls_follow_ui_signatures(self):
        t = self.fake({"result": []}, {"result": []}, {"result": []}, {"result": "content"})
        server.run_tool("eva_git", {"action": "tree", "repo": self.RID, "branch": "dev", "path": "src/"})
        server.run_tool("eva_git", {"action": "commits", "repo": self.RID, "branch": "dev", "limit": 5, "offset": 10})
        server.run_tool("eva_git", {"action": "diff", "repo": self.RID, "sha_from": "a1", "sha_to": "b2"})
        self.assertEqual(server.run_tool("eva_git", {"action": "file", "repo": self.RID, "path": "README.md"}), "content")
        got = [(c[0].split("m=")[1], c[1]["jsonrpc"], c[1]["args"]) for c in t.calls]
        self.assertEqual(got, [
            ("CmfEvagitRepo.fetch_repo_tree", "2.2", [self.RID, "dev", "src/"]),
            ("CmfEvagitRepo.fetch_commits", "2.2", [self.RID, "dev", 5, 10]),
            ("CmfEvagitRepo.fetch_commit_diff", "2.2", [self.RID, "a1", "b2"]),
            ("CmfEvagitRepo.fetch_file_content", "2.2", [self.RID, "master", "README.md"])])

    def test_fetch_is_read(self):
        server.READONLY = True
        self.fake({"result": []})
        server.run_tool("eva_git", {"action": "branches", "repo": self.RID})

    def test_writes_blocked_and_hidden_in_readonly(self):
        server.READONLY = True
        t = self.fake()
        with self.assertRaises(server.ReadOnlyError):
            server.run_tool("eva_git", {"action": "create_branch", "repo": self.RID, "name": "x"})
        self.assertEqual(t.calls, [])
        git = [x for x in server.visible_tools() if x["name"] == "eva_git"][0]
        self.assertNotIn("merge", git["inputSchema"]["properties"]["action"]["enum"])

    def test_create_branch_payload(self):
        t = self.fake({"result": True})
        server.run_tool("eva_git", {"action": "create_branch", "repo": self.RID, "from_branch": "master", "name": "feat"})
        self.assertEqual(t.calls[0][1]["args"], [self.RID, "master", "feat"])
