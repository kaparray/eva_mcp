"""Offline tests for servicedesk.py: request packing (api vs portal sd_api), SLA text, read-only guards."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_offline import Base  # noqa: E402

import server  # noqa: E402
import servicedesk  # noqa: E402


def ok(result):
    return {"jsonrpc": "2.0", "result": result}


class TestSdeskRegistration(Base):
    def test_tools_registered_and_writes_hidden_in_readonly(self):
        names = {t["name"] for t in server.TOOLS}
        self.assertTrue({"eva_sd_portals", "eva_sd_requests", "eva_sd_request", "eva_sd_kb"} <= names)
        self.assertTrue(servicedesk.WRITE_TOOLS_SDESK <= server.WRITE_TOOLS)
        server.READONLY = True
        visible = {t["name"] for t in server.visible_tools()}
        self.assertIn("eva_sd_requests", visible)
        self.assertFalse(visible & servicedesk.WRITE_TOOLS_SDESK)
        with self.assertRaises(server.ReadOnlyError):
            server.run_tool("eva_sd_reply", {"code": "SD-1", "text": "x"})

    def test_readonly_classification(self):
        for m in ("sdesk_list", "sdesk_get", "sdesk_count", "sdesk_ui_get", "sdesk_hint_docs_search"):
            self.assertFalse(server.is_write(m), m)
        for m in ("sdesk_create", "sdesk_update", "sdesk_create_dummy_task", "sdesk_save_dummy_task",
                  "sdesk_task_transition_run", "sdesk_delete", "sdesk_feedback", "create_dummy_task"):
            self.assertTrue(server.is_write(m), m)
        server.READONLY = True
        with self.assertRaises(server.ReadOnlyError):
            servicedesk.sd("CmfTask.update", {"status": "x"}, args=["CmfTask:1"], portal=True)


class TestSdeskEndpoint(Base):
    def test_portal_maps_to_sdesk_method_on_sd_api(self):
        t = self.fake(ok([]))
        servicedesk.sd("CmfTask.list", {"slice": [0, 25]}, portal=True, use_cache=False)
        path, p = t.calls[0]
        self.assertEqual(path, "/servicedesk/sd_api?m=CmfTask.sdesk_list")
        self.assertEqual(p["method"], "CmfTask.sdesk_list")

    def test_default_is_api(self):
        t = self.fake(ok([]))
        servicedesk.sd("CmfTask.list", {}, use_cache=False)
        self.assertEqual(t.calls[0][0], "/api/?m=CmfTask.list")

    def test_login_page_gives_clear_error(self):
        server._request = lambda path, body=None, timeout=None: "<!DOCTYPE html><title>Авторизация</title>"
        with self.assertRaises(RuntimeError) as e:
            servicedesk.sd("CmfTask.list", {}, portal=True, use_cache=False)
        self.assertIn("browser session", str(e.exception))


class TestSdeskReads(Base):
    def test_requests_filter_and_sla(self):
        task = {"id": "CmfTask:1", "code": "SD-1", "name": "Не работает VPN", "status": {"name": "Открыто"},
                "request_type": {"name": "Инцидент"}, "cmf_author": {"name": "Иван"}, "cmf_created_at": "2026-11-19T10:00"}
        cyc = {"id": "CmfSDeskSlaCycle:1", "parent_id": "CmfTask:1", "sla_goal": {"name": "Время реакции"},
               "remaining_time": 90, "remaining_time__addon_start": None, "breached": False, "paused": True}
        t = self.fake(ok([task]), ok([cyc]))
        out = server.run_tool("eva_sd_requests", {"mine": True, "open_only": True, "limit": 5})
        flt = t.calls[0][1]["kwargs"]["filter"]
        self.assertIn(["request_type", "!=", None], flt)
        self.assertIn(["cmf_author", "=", "__G_CURRENT_USER"], flt)
        self.assertIn(["cache_status_type", "!=", "CLOSED"], flt)
        self.assertEqual(t.calls[1][0], "/api/?m=CmfSDeskSlaCycle.list")
        self.assertEqual(t.calls[1][1]["kwargs"]["filter"], [["parent", "IN", ["CmfTask:1"]]])
        self.assertIn("SD-1", out)
        self.assertIn("Время реакции: осталось 1ч 30м", out)

    def test_sla_text_running_and_breached(self):
        now = 1_800_000_000
        from datetime import datetime, timezone
        start = datetime.fromtimestamp(now - 30 * 60, timezone.utc).isoformat()
        c = {"sla_goal": {"name": "Решение"}, "remaining_time": 20, "remaining_time__addon_start": start,
             "breached": True, "paused": False, "breach_time": "2026-11-19T12:00"}
        self.assertEqual(servicedesk.sla_text(c, now), "Решение: просрочено на 0ч 10м, срок 2026-11-19T12:00 [нарушен]")
        self.assertIn("выполнен", servicedesk.sla_text({"stop_time": "2026-11-19T12:00", "remaining_time": 5}, now))

    def test_kb_category_articles(self):
        t = self.fake(ok({"id": "CmfSDeskKnowlageCategory:1", "parent_id": "CmfProject:1"}),
                      ok([{"id": "CmfDocument:1", "code": "DOC-1", "name": "Как сбросить пароль", "recommendation": True}]))
        out = server.run_tool("eva_sd_kb", {"category": "KB-1"})
        self.assertEqual(t.calls[1][1]["kwargs"]["filter"],
                         [["parent", "==", "CmfProject:1"], ["categories", "IN", ["CmfSDeskKnowlageCategory:1"]]])
        self.assertIn("DOC-1  Как сбросить пароль  ★", out)
        self.assertIn("/servicedesk/document/DOC-1", out)


class TestSdeskWrites(Base):
    """Writes are only packed against FakeTransport, never sent to EVA."""

    def _create(self, portal):
        t = self.fake(ok([{"id": "CmfProject:1", "code": "sd"}]),            # resolve_project
                      ok({"id": "CmfSDeskRequestType:1", "name": "Инцидент", "logic_type_id": "CmfLogicType:7"}),
                      ok({"id": "CmfPerson:me", "company_id": "CmfCompany:1"}),
                      ok({"id": "CmfTask:9"}), *[ok(True)] * (2 if portal else 4), ok({"code": "SD-9", "name": "VPN"}))
        out = server.run_tool("eva_sd_create_request", {"project": "sd", "request_type": "INC", "title": "VPN",
                                                        "portal": portal})
        return t, out

    def test_create_request_api(self):
        t, out = self._create(False)
        methods = [c[0] for c in t.calls]
        i = methods.index("/api/?m=CmfTask.create_dummy_task")
        kw = t.calls[i][1]["kwargs"]
        self.assertEqual((kw["parent"], kw["request_type"], kw["logic_type"], kw["cmf_author"], kw["company"]),
                         ("CmfProject:1", "CmfSDeskRequestType:1", "CmfLogicType:7", "CmfPerson:me", "CmfCompany:1"))
        self.assertEqual(methods[i + 1:i + 5], ["/api/?m=CmfTask.lock", "/api/?m=CmfTask.update",
                                                "/api/?m=CmfTask.unlock", "/api/?m=CmfTask.save_dummy_task"])
        self.assertEqual(t.calls[i + 4][1]["args"], ["CmfTask:9"])
        self.assertIn("SD-9", out)

    def test_create_request_portal(self):
        t, _ = self._create(True)
        methods = [c[0] for c in t.calls]
        self.assertIn("/servicedesk/sd_api?m=CmfTask.sdesk_create_dummy_task", methods)
        self.assertIn("/servicedesk/sd_api?m=CmfTask.sdesk_save_dummy_task", methods)
        self.assertNotIn("/api/?m=CmfTask.lock", methods)

    def test_reply_portal(self):
        t = self.fake(ok({"id": "CmfTask:1", "code": "SD-1"}), ok({"id": "CmfComment:5"}), ok(True))
        server.run_tool("eva_sd_reply", {"code": "SD-1", "text": "Проверьте **VPN**", "portal": True})
        self.assertEqual([c[0] for c in t.calls], ["/servicedesk/sd_api?m=CmfTask.sdesk_get",
                                                   "/servicedesk/sd_api?m=CmfComment.sdesk_create_dummy",
                                                   "/servicedesk/sd_api?m=CmfComment.sdesk_update"])
        self.assertEqual(t.calls[1][1]["kwargs"]["parent_id"], "CmfTask:1")
        upd = t.calls[2][1]
        self.assertEqual((upd["args"], upd["kwargs"]["is_dummy"]), (["CmfComment:5"], False))
        self.assertIn("<strong>VPN</strong>", upd["kwargs"]["text"])

    def test_set_status_with_transition(self):
        t = self.fake(ok({"id": "CmfTask:1", "status_id": "CmfStatus:a", "workflow_id": "CmfWorkflow:1"}),
                      ok([{"id": "CmfStatus:b", "name": "Решено"}]),
                      {"jsonrpc": "2.0", "abort": "must_transition_run CmfTask:1 CmfTrans:3 a b"}, ok(True))
        out = server.run_tool("eva_sd_set_status", {"code": "SD-1", "status": "решено", "comment": "готово"})
        self.assertEqual(t.calls[3][0], "/api/?m=CmfTask.task_transition_run")
        self.assertEqual(t.calls[3][1]["args"], ["CmfTask:1", "CmfTrans:3"])
        self.assertEqual(t.calls[3][1]["kwargs"]["changed_fields"], {"status": "CmfStatus:b"})
        self.assertEqual(out, "SD-1 → Решено")


if __name__ == "__main__":
    unittest.main()
