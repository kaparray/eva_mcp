"""Offline tests for reports.py: request packing of the report/dashboard/analytics tools (no network)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_offline import Base  # noqa: E402  (sets EVA_TOKEN, imports server)

import server  # noqa: E402
import reports  # noqa: E402

P = "CmfProject:2dfbb857-8523-11f1-96ae-a2ff741253a3"


class TestReportsRegistered(Base):
    def test_tools_listed_and_readonly(self):
        names = {t["name"] for t in server.visible_tools()}
        self.assertTrue({"eva_reports", "eva_report", "eva_task_stats", "eva_analytics"} <= names)
        server.READONLY = True
        self.assertTrue({"eva_reports", "eva_report", "eva_task_stats", "eva_analytics"}
                        <= {t["name"] for t in server.visible_tools()})
        for m in ("count", "list", "get"):
            self.assertFalse(server.is_write(m))


class TestTaskStats(Base):
    def test_count_group_by_id_columns_and_names(self):
        self.addCleanup(server._META.pop, "CmfTask", None)
        server._META["CmfTask"] = {}
        t = self.fake({"result": 7},                                   # total
                      {"result": 1},                                   # overdue
                      {"result": [{"status_id": "CmfStatus:a", "responsible_id": None, "count": 4},
                                  {"status_id": "CmfStatus:a", "responsible_id": "CmfPerson:p", "count": 3}]},
                      {"result": [{"id": "CmfStatus:a", "name": "Открыто"}]},
                      {"result": [{"id": "CmfPerson:p", "name": "Иванов"}]})
        out = server.run_tool("eva_task_stats", {"query": '["project_id","=","%s"]' % P,
                                                  "group_by": ["status+assignee"]})
        total, overdue, grouped = (c[1] for c in t.calls[:3])
        self.assertEqual(total["method"], "CmfTask.count")
        self.assertEqual(total["kwargs"], {"filter": ["project_id", "=", P]})
        self.assertEqual(overdue["kwargs"]["filter"][0], "AND")
        self.assertIn(["cache_status_type", "!=", "CLOSED"], overdue["kwargs"]["filter"])
        self.assertEqual(grouped["kwargs"]["group_by"], ["status_id", "responsible_id"])
        self.assertEqual(grouped["jsonrpc"], "2.0")
        self.assertIn("Всего задач: 7", out)
        self.assertIn("4  Открыто / —", out)
        self.assertIn("3  Открыто / Иванов", out)

    def test_choices_grouping_and_unknown(self):
        self.addCleanup(server._META.pop, "CmfTask", None)
        server._META["CmfTask"] = {"priority": {"choices": {"1": "Высокий"}}}
        self.fake({"result": 2}, {"result": [{"priority": 1, "count": 2}]})
        out = server.run_tool("eva_task_stats", {"query": '["code","=","A-1"]', "group_by": ["priority"], "overdue": False})
        self.assertIn("2  Высокий", out)
        with self.assertRaises(ValueError):
            reports.group_counts(["code", "=", "A"], ["nope"])

    def test_needs_filter(self):
        self.fake()
        with self.assertRaises(ValueError):
            server.run_tool("eva_task_stats", {})


class TestDashboard(Base):
    def test_dashboard_widgets_and_filter(self):
        html = ('<div data-code="RPT-1" data-param-name="x" data-macros="dashboard-chart"></div>'
                '<div data-macros="dashboard-chart" data-code="RPT-2"></div>')
        self.assertEqual([a or b for a, b in reports.dashboard_widgets(html)], ["RPT-1", "RPT-2"])
        t = self.fake({"result": {"id": "CmfDashboard:d", "code": "DBRD-1", "name": "D",
                                  "text": '<div data-macros="dashboard-chart" data-code="RPT-9"></div>'}},
                      {"result": {"id": "CmfReport:r", "code": "RPT-9", "name": "R", "report_type": "analysis_round",
                                  "options": {"filterId": "CmfBqlFilter:f", "statisticType": "responsible"}}},
                      {"result": {"id": "CmfBqlFilter:f", "name": "F", "bql": '[["project.code", "IN", ["X"]]]'}},
                      {"result": 0}, {"result": 0})
        out = server.run_tool("eva_report", {"code": "DBRD-1"})
        self.assertEqual(t.calls[0][1]["method"], "CmfDashboard.get")
        self.assertEqual(t.calls[0][1]["kwargs"]["filter"], ["code", "=", "DBRD-1"])
        self.assertEqual(t.calls[1][1]["kwargs"]["filter"], ["code", "=", "RPT-9"])
        self.assertEqual(t.calls[2][1]["kwargs"]["filter"], ["id", "=", "CmfBqlFilter:f"])
        self.assertEqual(t.calls[3][1]["kwargs"], {"filter": ["project.code", "IN", ["X"]]})
        self.assertIn("Круговая диаграмма", out)
        self.assertIn("Всего задач: 0", out)


class TestAnalytics(Base):
    def test_projects_view(self):
        t = self.fake({"result": [{"project_id": P, "sum__num": 52047, "max__cmf_modified_at": "2026-11-19T10:00"}]},
                      {"result": [{"id": P, "name": "My Project"}]})
        out = server.run_tool("eva_analytics", {"date_from": "2026-09-01", "date_to": "2026-11-19"})
        kw = t.calls[0][1]["kwargs"]
        self.assertEqual(t.calls[0][1]["method"], "CmfStatAggregDay.list")
        self.assertEqual(kw["group_by"], ["project_id"])
        self.assertEqual(kw["order_by"], ["-sum__num"])
        self.assertIn(["action", "==", "view"], kw["filter"])
        self.assertTrue(any(c[0] == "day" and c[1] == ">=" and c[2].startswith("2026-09-01T00:00:00") for c in kw["filter"][1:]))
        self.assertIn("52047  My Project", out)

    def test_search(self):
        t = self.fake({"result": [{"search_query": "отпуск", "sum__num": 3}]})
        out = server.run_tool("eva_analytics", {"by": "search", "date_from": "2026-09-01"})
        self.assertEqual(t.calls[0][1]["method"], "CmfSearchStatAggregDay.list")
        self.assertEqual(t.calls[0][1]["kwargs"]["group_by"], ["search_query"])
        self.assertIn("3  отпуск", out)
        with self.assertRaises(ValueError):
            server.run_tool("eva_analytics", {"date_from": "01.09.2026"})


class TestList(Base):
    def test_kinds_and_lms_excluded(self):
        t = self.fake({"result": [{"id": "CmfReport:x", "code": "RPT-1", "name": "R", "report_type": "agile_speed"}]})
        out = server.run_tool("eva_reports", {"kind": "reports", "query": "R"})
        kw = t.calls[0][1]["kwargs"]
        self.assertEqual(t.calls[0][1]["method"], "CmfReport.list")
        self.assertEqual(kw["filter"][0], "AND")
        self.assertIn(["name", "ILIKE", "%R%"], kw["filter"])
        self.assertEqual(kw["filter"][2][:2], ["report_type", "NOT IN"])
        self.assertIn("RPT-1  R  [Диаграмма производительности]", out)


if __name__ == "__main__":
    unittest.main()
