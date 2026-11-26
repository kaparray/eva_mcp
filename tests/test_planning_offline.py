"""Offline tests for planning.py (Gantt, roadmaps, calendars, workload): request packing, no network."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_offline import Base  # noqa: E402

import planning  # noqa: E402
import server  # noqa: E402

RM = "CmfRoadmap:eb99b27c-df3c-11f1-a79e-e65ba2f3bead"
T1, T2, T3 = ("CmfTask:00000000-0000-0000-0000-00000000000%d" % i for i in (1, 2, 3))


def ok(result):
    return {"jsonrpc": "2.0", "result": result}


class TestPlanning(Base):
    def test_ncget_is_safe_in_readonly(self):
        server.READONLY = True
        self.assertFalse(server.is_write("ncget_filtered_roadmap_by_level"))
        t = self.fake(ok({"1": []}))
        planning.gantt_tree([["project_id", "=", "CmfProject:x"]], 10)
        self.assertEqual(len(t.calls), 1)

    def test_gantt_request_like_ui(self):
        t = self.fake(ok({"1": [[T1, None, T1], [T2, None, T2]], "2": [[T3, T1, T3]]}))
        tree = planning.gantt_tree([["tags", "IN", ["CmfTag:a"]]], 300, child_tasks=True)
        path, body = t.calls[0]
        self.assertEqual(path, "/api/?m=CmfRoadmap.ncget_filtered_roadmap_by_level")
        self.assertEqual(body["jsonrpc"], "2.2")
        self.assertEqual(body["args"], [])
        self.assertEqual(body["fields"], [])
        self.assertEqual(body["kwargs"], {"insert_bql_list": [["OR", ["tags", "IN", ["CmfTag:a"]]]], "start": 0,
                                          "end": 300, "show_child_tasks": True,
                                          "sort_fields": ["op_gantt_task.outline_number"], "sort_orders": ["asc"]})
        self.assertEqual(tree, [(T1, 0), (T3, 1), (T2, 0)])

    def test_gantt_tool_roadmap(self):
        t = self.fake(
            ok({"id": RM, "code": "RM-1", "name": "Plan", "ft_bql_sources": [{"id": "CmfBqlFilter:f"}]}),
            ok([{"id": "CmfBqlFilter:f", "bql": '["tags","IN",["CmfTag:a"]]', "ubql": "tags = TAG-1"}]),
            ok({"1": [[T1, None, T1], [T2, None, T2]]}),
            ok([{"id": T1, "code": "A-1", "name": "First", "status": {"name": "Открыто"},
                 "op_gantt_task": {"outline_number": "000001", "sched_start_date": "2027-01-11T08:00:00+03:00",
                                   "sched_finish_date": "2027-01-15T08:00:00+03:00", "sched_duration": 1920,
                                   "sched_work": 600, "is_critical_path": True}},
                {"id": T2, "code": "A-2", "name": "Second", "op_gantt_task": {"outline_number": "000002"}}]),
            ok([{"out_link_id": T1, "in_link_id": T2, "constrain_lag": 480,
                 "relation_type": {"code": "system.finish:start", "name": "Порядок следования"}},
                {"out_link_id": T1, "in_link_id": T2, "relation_type": {"code": "system.clone", "name": "Клон"}}]),
            ok([]))
        out = planning.run("eva_gantt", {"roadmap": "RM-1"})
        methods = [c[1]["method"] for c in t.calls]
        self.assertEqual(methods, ["CmfRoadmap.get", "CmfBqlFilter.list", "CmfRoadmap.ncget_filtered_roadmap_by_level",
                                   "CmfTask.list", "CmfRelationOption.list", "CmfGanttBaseline.list"])
        self.assertEqual(t.calls[3][1]["kwargs"]["filter"], ["id", "IN", [T1, T2]])
        self.assertEqual(t.calls[4][1]["kwargs"]["filter"],
                         ["OR", ["in_link_id", "IN", [T1, T2]], ["out_link_id", "IN", [T1, T2]]])
        self.assertIn("2027-01-11 → 2027-01-15 4д", out)
        self.assertIn("10ч", out)
        self.assertIn("A-1FS+1д", out)   # predecessor of A-2 with 1 day lag
        self.assertNotIn("Клон", out)    # non-Gantt relation hidden by default
        self.assertIn("КП", out)

    def test_calendar_walks_parent_and_counts(self):
        t = self.fake(
            ok([{"id": "CmfPerson:p", "login": "u", "name": "U", "calendar_id": "CmfCalendar:own",
                 "vacation_interval": ["2027-01-12", "2027-01-20"]}]),
            ok({"id": "CmfCalendar:own", "code": "CAL-1", "name": "Календарь u", "parent_calendar_id": "CmfCalendar:std"}),
            ok(0),
            ok({"id": "CmfCalendar:std", "code": "system:default", "name": "Стандартный"}),
            ok(365),
            ok([{"date": "2027-01-11", "day_type": "work", "interval_total_minutes": 480},
                {"date": "2027-01-12", "day_type": "work", "interval_total_minutes": 480},
                {"date": "2027-01-16", "day_type": "weekend", "interval_total_minutes": 0}]))
        out = planning.run("eva_calendar", {"person": "u", "from": "2027-01-11", "to": "2027-01-16"})
        self.assertEqual(t.calls[5][1]["kwargs"]["filter"],
                         ["AND", ["parent_id", "=", "CmfCalendar:std"], ["date", ">=", "2027-01-11"],
                          ["date", "<=", "2027-01-16"]])
        self.assertIn("Календарь u (CAL-1) → Стандартный (system:default)", out)
        self.assertIn("рабочих дней 1, часов 8", out)
        self.assertIn("2027-01-16 сб  weekend", out)

    def test_overlap_work(self):
        g = {"sched_start_date": "2027-01-01T08:00", "sched_finish_date": "2027-01-10T18:00", "sched_work": 1000}
        import datetime as dt
        self.assertEqual(planning._overlap_work(g, dt.date(2027, 1, 6), dt.date(2027, 1, 31)), 500)

    def test_tools_registered_and_not_write(self):
        names = {t["name"] for t in server.visible_tools()}
        self.assertTrue({"eva_gantt", "eva_roadmaps", "eva_calendar", "eva_workload"} <= names)
        self.assertIsNone(planning.run("eva_whoami", {}))
        self.assertFalse(planning.NAMES & server.WRITE_TOOLS)


if __name__ == "__main__":
    unittest.main()
