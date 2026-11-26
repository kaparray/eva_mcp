"""Offline tests for timetracker.py: duration parsing and request packing (writes are only recorded, never sent)."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EVA_TOKEN", "test-token-not-real")

import server  # noqa: E402
import timetracker as tt  # noqa: E402
from test_offline import Base  # noqa: E402

TID = "CmfTask:11111111-1111-1111-1111-111111111111"
HID = "CmfTimeTrackerHistory:22222222-2222-2222-2222-222222222222"
TRK = "CmfTimeTracker:33333333-3333-3333-3333-333333333333"
ME = "CmfPerson:44444444-4444-4444-4444-444444444444"
TASK = {"id": TID, "code": "ABC-1", "name": "T", "op_gantt_task": {
    "sched_work": 600, "actual_work": 120, "timetracker_sched_work": 600}}


class TestDuration(unittest.TestCase):
    def test_parse(self):
        for s, m in [("1h 30m", 90), ("2ч", 120), ("90m", 90), ("90", 90), (45, 45), ("1д", 480), ("1н", 2400),
                     ("1w 1d", 2880), ("1.5h", 90), ("1:30", 90), ("1ч30м", 90), ("2 часа", 120), ("30 мин", 30)]:
            self.assertEqual(tt.parse_duration(s), m, s)

    def test_bad(self):
        for s in ["", "abc", "1x", "1h junk"]:
            with self.assertRaises(ValueError):
                tt.parse_duration(s)

    def test_fmt(self):
        self.assertEqual((tt.fmt_min(90), tt.fmt_min(480), tt.fmt_min(0), tt.fmt_min(45)), ("1ч 30м", "8ч", "0м", "45м"))

    def test_periods(self):
        d = dt.date(2026, 11, 19)
        self.assertEqual(tt.period_range("week", "2026-11-19"), (dt.date(2026, 11, 16), dt.date(2026, 11, 22)))
        self.assertEqual(tt.period_range("month", "2026-02-10"), (dt.date(2026, 2, 1), dt.date(2026, 2, 28)))
        self.assertEqual(tt.period_range("day", "2026-11-19"), (d, d))
        self.assertEqual(tt.period_range(date_from="2026-11-01", date_to="2026-11-05"),
                         (dt.date(2026, 11, 1), dt.date(2026, 11, 5)))

    def test_calendar_filter(self):
        f = tt.overlap_filter([ME], "fact", "S", "E")
        self.assertEqual(f[:2], [["cmf_owner_id", "IN", [ME]], ["history_type", "=", "fact"]])
        self.assertEqual(f[2][0], "OR")
        self.assertEqual(tt.overlap_filter([ME], "plan", "S", "E")[1], ["history_type", "IN", ["plan", "plan_oper"]])


class TestRequests(Base):
    def last(self, t):
        return t.calls[-1]

    def test_my_time_sums(self):
        rows = [{"id": "h1", "start_date": "2026-11-17T10:00:00+03:00", "time_spent": 90, "history_type": "fact",
                 "parent": {"code": "ABC-1", "name": "T"}},
                {"id": "h2", "start_date": "2026-11-17T15:00:00+03:00", "time_spent": 30, "history_type": "fact",
                 "parent": {"code": "ABC-2", "name": "U"}},
                {"id": "h3", "_acl_obj": "deny"}]
        t = self.fake({"result": {"id": ME, "name": "Я"}}, {"result": rows})
        out = server.run_tool("eva_my_time", {"period": "week", "date": "2026-11-19"})
        path, p = self.last(t)
        self.assertEqual(path, "/api/?m=CmfTimeTrackerHistory.list")
        self.assertEqual(p["kwargs"]["filter"][0], ["cmf_owner_id", "IN", [ME]])
        self.assertIn("всего 2ч (120 мин)", out)
        self.assertIn("2026-11-17  2ч", out)
        self.assertIn("1ч 30м  ABC-1 T", out)

    def test_log_time_payload(self):
        t = self.fake({"result": {"id": TID}}, {"result": TASK}, {"result": True})
        out = server.run_tool("eva_log_time", {"task": "ABC-1", "time": "1h 30m", "date": "2026-11-19T10:00",
                                               "text": "работа"})
        path, p = self.last(t)
        self.assertEqual(path, "/api/?m=CmfTask.timetracker_change_time")
        self.assertEqual((p["jsonrpc"], p["args"]), ("2.2", [TID]))
        k = p["kwargs"]
        self.assertEqual((k["time_spent"], k["remaining_estimate"], k["text"]), (90, 390, "работа"))  # 600-120-90
        self.assertRegex(k["date"], r"^2026-11-1\dT\d\d:00:00\.000Z$")
        self.assertIn("1ч 30м", out)

    def test_log_time_dry_run_sends_nothing(self):
        t = self.fake({"result": {"id": TID}}, {"result": TASK})
        out = server.run_tool("eva_log_time", {"task": "ABC-1", "time": "2ч", "remaining": "0", "dry_run": True})
        self.assertTrue(out.startswith("DRY RUN"))
        self.assertNotIn("timetracker_change_time", " ".join(c[0] for c in t.calls))

    def test_update_and_delete(self):
        h = {"id": HID, "code": "TTH-1", "cmf_owner_id": ME, "start_date": "2026-11-19T10:00:00+03:00", "time_spent": 60}
        t = self.fake({"result": h}, {"result": {"id": ME}}, {"result": True})
        server.run_tool("eva_update_worklog", {"entry": HID, "time": "2h", "text": "x"})
        path, p = self.last(t)
        self.assertEqual((path, p["args"]), ("/api/?m=CmfTimeTrackerHistory.update", [HID]))
        k = p["kwargs"]
        self.assertEqual((k["time_spent"], k["text"]), (120, "x"))
        self.assertEqual((k["start_date"], k["end_date"]), ("2026-11-19T07:00:00.000Z", "2026-11-19T09:00:00.000Z"))
        t = self.fake({"result": h}, {"result": {"id": ME}}, {"result": True})
        server.run_tool("eva_delete_worklog", {"entry": HID})
        self.assertEqual(self.last(t)[0], "/api/?m=CmfTimeTrackerHistory.delete")
        self.assertEqual(self.last(t)[1]["args"], [HID])

    def test_not_my_entry_refused(self):
        h = {"id": HID, "cmf_owner_id": "CmfPerson:other", "start_date": "2026-11-19T10:00:00+03:00"}
        t = self.fake({"result": h}, {"result": {"id": ME}})
        with self.assertRaises(PermissionError):
            server.run_tool("eva_delete_worklog", {"entry": HID})
        self.assertEqual(len(t.calls), 2)

    def test_timer(self):
        t = self.fake({"result": {"id": ME}}, {"result": [{"id": TRK}]}, {"result": {"id": TID}},
                      {"result": {"id": TID, "code": "ABC-1"}}, {"result": True})
        server.run_tool("eva_timer", {"action": "start", "task": "ABC-1"})
        path, p = self.last(t)
        self.assertEqual((path, p["args"], p["kwargs"]), ("/api/?m=CmfTimeTracker.timetracker_start", [TRK], {"task_id": TID}))
        t = self.fake({"result": {"id": ME}}, {"result": [{"id": TRK}]}, {"result": True})
        server.run_tool("eva_timer", {"action": "pause"})
        self.assertEqual(self.last(t)[0], "/api/?m=CmfTimeTracker.timetracker_pause")

    def test_readonly(self):
        server.READONLY = True
        names = {x["name"] for x in server.visible_tools()}
        self.assertIn("eva_my_time", names)
        for w in tt.WRITE_TIME_TOOLS:
            self.assertNotIn(w, names)
            self.assertIn(w, server.WRITE_TOOLS)
        t = self.fake()
        with self.assertRaises(server.ReadOnlyError):
            server.run_tool("eva_log_time", {"task": "ABC-1", "time": "1h"})
        with self.assertRaises(server.ReadOnlyError):
            server.eva("CmfTask.timetracker_change_time", {"time_spent": 1}, args=[TID])
        self.assertEqual(t.calls, [])


if __name__ == "__main__":
    unittest.main()
