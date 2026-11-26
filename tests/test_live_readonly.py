"""Live smoke tests against EVA, read-only by construction (EVA_READONLY=1 is forced before import).

Skipped unless EVA_LIVE=1. Needs a token (Keychain / EVA_TOKEN / ~/.config/eva-mcp/token) and network access.
  EVA_LIVE=1 python3 -m unittest tests.test_live_readonly -v
Sample objects come only from env (no defaults): EVA_TEST_DOC, EVA_TEST_TASK, EVA_TEST_PROJECT,
EVA_TEST_TT_TASK, EVA_TEST_ROADMAP. A test that needs an unset one is skipped.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

DOC = os.environ.get("EVA_TEST_DOC", "")
TASK = os.environ.get("EVA_TEST_TASK", "")
PROJECT = os.environ.get("EVA_TEST_PROJECT", "")
TT_TASK = os.environ.get("EVA_TEST_TT_TASK", "")
ROADMAP = os.environ.get("EVA_TEST_ROADMAP", "")


def need(*pairs):
    """Skip the test unless every (env name, value) pair is set."""
    missing = [n for n, v in pairs if not v]
    if missing:
        raise unittest.SkipTest("set " + ", ".join(missing))


@unittest.skipUnless(os.environ.get("EVA_LIVE") == "1", "set EVA_LIVE=1 to run live read-only tests")
class LiveReadOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # whatever the caller set: these tests must never write (the guard is checked on every call)
        cls._saved = (server.READONLY, server.CACHE_TTL)
        server.READONLY, server.CACHE_TTL = True, 0

    @classmethod
    def tearDownClass(cls):
        server.READONLY, server.CACHE_TTL = cls._saved

    def tool(self, name, **a):
        return server.run_tool(name, a)

    def test_00_readonly_is_on(self):
        self.assertTrue(server.READONLY)
        with self.assertRaises(server.ReadOnlyError):
            server.eva("CmfDocument.update", {}, args=["CmfDocument:x"])

    def test_whoami(self):
        self.assertIn("CmfPerson:", self.tool("eva_whoami"))

    def test_document(self):
        need(("EVA_TEST_DOC", DOC))
        out = self.tool("eva_get_document", code=DOC)
        self.assertTrue(out.startswith("# "))
        self.assertIn(server.doc_url(DOC), out)

    def test_versions_and_attachments(self):
        need(("EVA_TEST_DOC", DOC))
        self.assertRegex(self.tool("eva_document_versions", code=DOC, limit=3), r"\d{14,}")
        self.tool("eva_list_attachments", code=DOC)

    def test_global_search(self):
        out = self.tool("eva_search", query="Test IT", model="CmfDocument", limit=5)
        self.assertRegex(out, r"DOC-\d+")

    def test_ubql(self):
        need(("EVA_TEST_PROJECT", PROJECT))
        out = self.tool("eva_find_tasks", query=f"project = {PROJECT} ORDER BY updated DESC", limit=2)
        self.assertRegex(out, r"^\d+ found")

    def test_task_and_statuses(self):
        need(("EVA_TEST_TASK", TASK))
        self.assertIn(TASK, self.tool("eva_get_task", code=TASK))
        self.assertIn("[", self.tool("eva_task_statuses", code=TASK))

    def test_my_tasks_and_notifications(self):
        for v in ("doing", "approving", "assigned"):
            self.assertRegex(self.tool("eva_my_tasks", view=v, limit=1), rf"^{v}: \d+")
        self.assertRegex(self.tool("eva_notifications", limit=1), r"^\d+ unread")

    def test_people_and_boards(self):
        me = server.eva("CmfPerson.get_current_user", use_cache=False)
        self.assertIn(me["id"], self.tool("eva_find_person", query=me.get("login") or me["name"]))
        if PROJECT:
            self.tool("eva_boards", project=PROJECT)
        self.tool("eva_sprints", limit=2)

    def test_protocol_signature(self):
        out = self.tool("eva_list_methods", class_name="CmfDocument", query="export_to_pdf")
        self.assertIn("ui: (id)", out)

    def test_reports_and_dashboards(self):
        out = self.tool("eva_reports", limit=3)
        self.assertIn("## Дашборды", out)
        m = __import__("re").search(r"DBRD-\d+", out)
        if m:
            self.assertIn("# Дашборд", self.tool("eva_report", code=m.group(0)))

    def test_task_stats_and_analytics(self):
        need(("EVA_TEST_PROJECT", PROJECT))
        out = self.tool("eva_task_stats", project=PROJECT, group_by=["status_type"])
        self.assertRegex(out, r"^Всего задач: \d+")
        self.assertRegex(self.tool("eva_analytics", by="projects", limit=3), r"\n +\d+  ")

    def test_servicedesk_reads(self):
        need(("EVA_TEST_TASK", TASK))
        # this EVA has no portals for the test user: the requests must pass and answer, data may be empty
        self.assertRegex(self.tool("eva_sd_portals"), r"servicedesk")
        out = self.tool("eva_sd_requests", mine=True, limit=3)
        self.assertTrue(out.startswith("No Service Desk requests") or "/project/Task/" in out, out)
        self.assertIn("SLA:", self.tool("eva_sd_request", code=TASK, comments=0))
        self.tool("eva_sd_kb")
        with self.assertRaises(server.ReadOnlyError):
            self.tool("eva_sd_reply", code=TASK, text="x")

    def test_tls_verified(self):
        self.assertFalse(server.INSECURE, "EVA_INSECURE is on: TLS is not verified")




@unittest.skipUnless(os.environ.get("EVA_LIVE") == "1", "set EVA_LIVE=1 to run live read-only tests")
class LiveTimeTracking(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._saved = (server.READONLY, server.CACHE_TTL)
        server.READONLY, server.CACHE_TTL = True, 0

    @classmethod
    def tearDownClass(cls):
        server.READONLY, server.CACHE_TTL = cls._saved

    def test_my_time_and_timer(self):
        self.assertRegex(server.run_tool("eva_my_time", {"period": "week"}), r"всего .+ \(\d+ мин\)")
        self.assertIn("таймер", server.run_tool("eva_timer_status", {}))

    def test_task_worklog(self):
        need(("EVA_TEST_TT_TASK", TT_TASK))
        out = server.run_tool("eva_task_worklog", {"task": TT_TASK, "type": "all", "limit": 3})
        self.assertIn(TT_TASK, out)
        self.assertIn("Оценка:", out)
        with self.assertRaises(server.ReadOnlyError):
            server.run_tool("eva_log_time", {"task": TT_TASK, "time": "1m"})


@unittest.skipUnless(os.environ.get("EVA_LIVE") == "1", "set EVA_LIVE=1 to run live read-only tests")
class LivePlanning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (server.READONLY, server.CACHE_TTL)
        server.READONLY, server.CACHE_TTL = True, 0

    @classmethod
    def tearDownClass(cls):
        server.READONLY, server.CACHE_TTL = cls._saved

    def test_roadmaps_and_gantt(self):
        need(("EVA_TEST_ROADMAP", ROADMAP), ("EVA_TEST_PROJECT", PROJECT))
        self.assertIn(ROADMAP, server.run_tool("eva_roadmaps", {"project": PROJECT}))
        out = server.run_tool("eva_gantt", {"roadmap": ROADMAP, "limit": 5})
        self.assertIn("Гант: " + ROADMAP, out)
        self.assertRegex(out, r"\| [A-Z]+-\d+ \|")

    def test_calendar_and_workload(self):
        cal = server.run_tool("eva_calendar", {"from": "2027-01-04", "to": "2027-01-10"})
        self.assertIn("Стандартный", cal)
        self.assertIn("weekend", cal)
        me = server.eva("CmfPerson.get_current_user", {}) or {}
        out = server.run_tool("eva_workload", {"people": [me.get("login")], "from": "2027-01-04", "to": "2027-01-10"})
        self.assertIn("рабочих часов", out)


if __name__ == "__main__":
    unittest.main()
