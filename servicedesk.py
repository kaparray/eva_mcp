"""Service Desk tools for EVA MCP: portals, requests (tickets), SLA, portal knowledge base.

How Service Desk works in EVA (from the web client and live requests, see README "Service Desk"):
- A portal is a CmfProject with enable_sdesk = true. Request groups (CmfSDeskRequestGroup) and request types
  (CmfSDeskRequestType: logic_type, default_workflow, task_form, request_group) hang off the project.
- A request is an ordinary CmfTask whose request_type is set (logic_type comes from the request type).
- SLA: CmfSDeskSla (parent = project) -> goals CmfSDeskSlaGoal (goal_time, calendar, ubql) and triggers
  CmfSDeskSlaTrigger (start / pause / stop by status). Each task gets CmfSDeskSlaCycle rows (parent = task):
  start_time, breach_time, stop_time, breached, paused, remaining_time (minutes) counted from
  remaining_time__addon_start; the UI widget shows remaining_time - (now - addon_start) while the cycle runs.
- Knowledge base: CmfSDeskKnowlageCategory (parent = project); articles are CmfDocument of the same project with
  categories IN [category] (recommendation = true -> "popular" block).
- The client portal (/servicedesk/...) runs the same Angular client; on /servicedesk paths every model call X is
  sent as sdesk_X to /servicedesk/sd_api (e.g. CmfTask.sdesk_list, CmfProject.sdesk_list). That endpoint works
  only with the browser session cookie: with the API key it answers the login HTML page. So tools read through
  /api/ (the agent's view, same ORM, your access rights) and the write tools can pack either protocol
  (portal=true reproduces the portal calls; it needs a session, not the key).
"""
import json
import time

# Read-only methods of the portal protocol whose names do not look like reads (server.is_read): they change
# nothing, so EVA_READONLY must not block them in eva_call. sdesk_create* / sdesk_update / sdesk_delete /
# sdesk_save_dummy_task / sdesk_task_transition_run / sdesk_approve_one / sdesk_feedback stay writes.
SDESK_SAFE_NON_READ = {"sdesk_hint_docs_search", "sdesk_fulltext_search", "sdesk_ux_check_perm",
                       "sdesk_count_approvers", "hint_docs_search"}

PROJECT_PROP = {"type": "string", "description": "Portal project: code, CmfProject:... id or name fragment"}
PORTAL_PROP = {"type": "boolean", "default": False,
               "description": "true = portal protocol (sdesk_* on /servicedesk/sd_api, needs a browser session); "
                              "default = /api/ with the API key"}

TOOLS_SDESK = [
    {"name": "eva_sd_portals",
     "description": "Service Desk portals (projects with enable_sdesk) with request groups, request types, SLA and "
                    "knowledge base categories. Read-only.",
     "inputSchema": {"type": "object", "properties": {"project": PROJECT_PROP}}},
    {"name": "eva_sd_requests",
     "description": "Service Desk requests (tasks with request_type) like the portal list 'Запросы': code, name, "
                    "status, type, author, assignee, SLA left/breached. Filter by portal, UBQL query, mine (I am the "
                    "author), open_only. Read-only.",
     "inputSchema": {"type": "object", "properties": {
         "project": PROJECT_PROP,
         "query": {"type": "string", "description": "extra UBQL, e.g. priority = \"Высокий\""},
         "mine": {"type": "boolean", "default": False, "description": "only requests I created (client view)"},
         "open_only": {"type": "boolean", "default": False},
         "limit": {"type": "integer", "default": 25}}}},
    {"name": "eva_sd_request",
     "description": "One Service Desk request by code: fields, description, conversation (comments) and SLA per goal "
                    "(time left / overdue, breached, paused). Read-only.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "comments": {"type": "integer", "default": 30}}, "required": ["code"]}},
    {"name": "eva_sd_kb",
     "description": "Portal knowledge base: without arguments - categories of all portals; project - its categories; "
                    "category (code) - articles in it; query - search article titles; code (DOC-...) - read an article "
                    "as Markdown. Read-only.",
     "inputSchema": {"type": "object", "properties": {
         "project": PROJECT_PROP, "category": {"type": "string"}, "query": {"type": "string"},
         "code": {"type": "string"}, "limit": {"type": "integer", "default": 30}}}},
    {"name": "eva_sd_create_request",
     "description": "Create a Service Desk request on a portal like the portal form: create_dummy_task {parent, "
                    "logic_type, request_type, cmf_author, company} -> fields -> save_dummy_task. Use only when the "
                    "user asked. NOT verified against live EVA.",
     "inputSchema": {"type": "object", "properties": {
         "project": PROJECT_PROP, "request_type": {"type": "string", "description": "type code, id or name"},
         "title": {"type": "string"}, "description": {"type": "string", "description": "Markdown"},
         "fields": {"type": "object"}, "portal": PORTAL_PROP},
         "required": ["project", "request_type", "title"]}},
    {"name": "eva_sd_reply",
     "description": "Reply in a Service Desk request (comment): create_dummy -> update(is_dummy=false, text). "
                    "private = internal note for agents. Use only when the user asked. NOT verified against live EVA.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "text": {"type": "string", "description": "Markdown"},
         "private": {"type": "boolean", "default": False}, "portal": PORTAL_PROP},
         "required": ["code", "text"]}},
    {"name": "eva_sd_set_status",
     "description": "Move a Service Desk request to a status (name or CmfStatus id): update(status), on "
                    "must_transition_run -> task_transition_run with comment. Use only when the user asked. "
                    "NOT verified against live EVA.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "status": {"type": "string"}, "comment": {"type": "string"},
         "fields": {"type": "object"}, "portal": PORTAL_PROP},
         "required": ["code", "status"]}},
]

WRITE_TOOLS_SDESK = {"eva_sd_create_request", "eva_sd_reply", "eva_sd_set_status"}

REQUEST_FIELDS = ["name", "code", "status", "parent", "cmf_owner", "cmf_author", "cmf_created_at", "status_closed_at",
                  "responsible", "priority", "request_type", "cache_status_type"]
SLA_FIELDS = ["parent", "sla", "sla_goal", "start_time", "breach_time", "stop_time", "breached", "paused",
              "within_calendar_hours", "elapsed_time", "remaining_time", "remaining_time__addon_start", "current"]


def _s():
    import server
    server.SAFE_NON_READ.update(SDESK_SAFE_NON_READ)
    return server


def sd(cls_method, kwargs=None, args=None, top=None, portal=False, use_cache=True):
    """Call a model method the way the client does: on the portal X -> sdesk_X on /servicedesk/sd_api."""
    s = _s()
    cls, _, m = cls_method.partition(".")
    if not portal:
        return s.eva(cls_method, kwargs, args=args, top=top, use_cache=use_cache)
    try:
        return s.eva(f"{cls}.sdesk_{m}", kwargs, args=args, top=top, use_cache=use_cache, endpoint="servicedesk")
    except json.JSONDecodeError:
        raise RuntimeError("/servicedesk/sd_api answered the login page: the portal protocol needs a browser "
                           "session, the API key works only with /api/ (call without portal=true)")


def _name(v):
    return v.get("name") if isinstance(v, dict) else v


def _ok(rows):
    return [r for r in (rows or []) if r.get("id") and r.get("_acl_obj") != "deny"]


def _portals(project=None):
    s = _s()
    flt = [["enable_sdesk", "=", True]]
    if project:
        flt.append(["id", "=", s.resolve_project(project)])
    return _ok(s.eva("CmfProject.list", {"filter": flt, "fields": ["code", "name", "show_knowlage_base"],
                                         "limit": 200}))


def _minutes(m):
    m = int(round(abs(m)))
    d, r = divmod(m, 1440)
    h, mi = divmod(r, 60)
    return (f"{d}д " if d else "") + f"{h}ч {mi:02d}м"


def _iso_min(v):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() / 60
    except (TypeError, ValueError):
        return None


def sla_text(c, now=None):
    """One SLA cycle as the portal widget shows it (remaining_time is in minutes)."""
    goal = _name(c.get("sla_goal")) or _name(c.get("sla")) or "SLA"
    rem = c.get("remaining_time")
    now = (now if now is not None else time.time()) / 60
    if c.get("stop_time"):
        state = "нарушен, завершён" if c.get("breached") else "выполнен"
        return f"{goal}: {state} ({str(c['stop_time'])[:16]})"
    if rem is not None and not c.get("paused"):
        start = _iso_min(c.get("remaining_time__addon_start"))
        if start is not None:
            rem = rem - (now - start)
    left = "" if rem is None else (f"просрочено на {_minutes(rem)}" if rem < 0 else f"осталось {_minutes(rem)}")
    flags = [x for x in ("нарушен" if c.get("breached") else "", "на паузе" if c.get("paused") else "") if x]
    due = f", срок {str(c['breach_time'])[:16]}" if c.get("breach_time") else ""
    return f"{goal}: {left}{due}" + (f" [{', '.join(flags)}]" if flags else "")


def _sla_by_task(ids):
    if not ids:
        return {}
    r = _ok(_s().eva("CmfSDeskSlaCycle.list", {"filter": [["parent", "IN", ids]], "fields": SLA_FIELDS,
                                                "limit": 500}, use_cache=False))
    out = {}
    for c in r:
        out.setdefault(c.get("parent_id") or (c.get("parent") or {}).get("id"), []).append(c)
    return out


def _resolve_type(project_id, ref):
    s = _s()
    if ref.startswith("CmfSDeskRequestType:"):
        flt = ["id", "=", ref]
    else:
        flt = [["parent", "=", project_id], ["OR", [["code", "=", ref], ["name", "ILIKE", ref]]]]
    t = s.eva("CmfSDeskRequestType.get", {"filter": flt, "fields": ["name", "code", "logic_type", "parent"]},
              use_cache=False)
    if not t:
        raise ValueError(f"Request type '{ref}' not found on this portal (see eva_sd_portals)")
    return t


def run(name, a):
    if not name.startswith("eva_sd_"):
        return None
    s = _s()

    if name == "eva_sd_portals":
        ps = _portals(a.get("project"))
        if not ps:
            return ("No Service Desk portals visible to you (CmfProject enable_sdesk = true): the portal "
                    f"{s.BASE}/servicedesk shows «Отсутствуют порталы Servicedesk»")
        ids = [p["id"] for p in ps]
        types = _ok(s.eva("CmfSDeskRequestType.list", {"filter": ["parent", "IN", ids], "limit": 500,
                                                       "fields": ["name", "code", "parent", "request_group",
                                                                  "logic_type", "default_workflow"]}))
        slas = _ok(s.eva("CmfSDeskSla.list", {"filter": ["parent", "IN", ids], "fields": ["name", "parent"],
                                              "limit": 200}))
        cats = _ok(s.eva("CmfSDeskKnowlageCategory.list", {"filter": ["parent", "IN", ids],
                                                           "fields": ["name", "code", "parent"], "limit": 500}))
        out = []
        for p in ps:
            pid = p["id"]
            mine = lambda rows: [r for r in rows if (r.get("parent_id") or (r.get("parent") or {}).get("id")) == pid]
            out.append(f"{p.get('code')}  {p.get('name')}  {s.BASE}/servicedesk/Project/{p.get('code')}")
            for t in mine(types):
                grp = _name(t.get("request_group"))
                out.append(f"  type {t.get('code')}  {t.get('name')}" + (f"  [группа {grp}]" if grp else ""))
            for x in mine(slas):
                out.append(f"  SLA {x.get('name')}")
            if mine(cats):
                out.append("  KB: " + ", ".join(f"{c.get('code')} {c.get('name')}" for c in mine(cats)))
        return "\n".join(out)

    if name == "eva_sd_requests":
        limit = int(a.get("limit") or 25)
        flt = [["request_type", "!=", None]]
        if a.get("project"):
            flt.append(["parent", "=", s.resolve_project(a["project"])])
        if a.get("mine"):
            flt.append(["cmf_author", "=", "__G_CURRENT_USER"])
        if a.get("open_only"):
            flt.append(["cache_status_type", "!=", "CLOSED"])
        order = ["-cmf_created_at"]
        if a.get("query"):
            q, o = s.to_filter(a["query"], with_order=True)
            flt.append(q)
            order = o or order
        r = _ok(s.eva("CmfTask.list", {"filter": flt, "fields": REQUEST_FIELDS, "order_by": order, "limit": limit},
                      use_cache=False))
        if not r:
            return "No Service Desk requests found (tasks with request_type)"
        sla = _sla_by_task([t["id"] for t in r])
        out = []
        for t in r:
            parts = [_name(t.get("request_type")), _name(t.get("status")),
                     _name(t.get("responsible")) and "→ " + _name(t.get("responsible")),
                     _name(t.get("cmf_author")) and "от " + _name(t.get("cmf_author")),
                     (t.get("cmf_created_at") or "")[:10]]
            line = f"{t.get('code')}  {t.get('name')}  [{'  '.join(x for x in parts if x)}]  {s.task_url(t.get('code'))}"
            cur = [c for c in sla.get(t["id"], []) if c.get("current") is not False]
            if cur:
                line += "\n    SLA: " + "; ".join(sla_text(c) for c in cur)
            out.append(line)
        return "\n".join(out)

    if name == "eva_sd_request":
        t = s.eva("CmfTask.get", {"filter": ["code", "=", a["code"]],
                                  "fields": REQUEST_FIELDS + ["text", "deadline", "company"]}, use_cache=False)
        if not t:
            raise ValueError(f"{a['code']} not found (or no access)")
        lines = [f"{t.get('code')}  {t.get('name')}", s.task_url(t.get("code"))]
        for k, title in (("request_type", "Тип запроса"), ("parent", "Портал"), ("status", "Статус"),
                         ("priority", "Приоритет"), ("cmf_author", "Автор"), ("responsible", "Исполнитель"),
                         ("company", "Компания"), ("cmf_created_at", "Создано"), ("status_closed_at", "Закрыто"),
                         ("deadline", "Срок")):
            v = _name(t.get(k))
            if v:
                lines.append(f"{title}: {str(v)[:16] if k.endswith(('_at', 'deadline')) else v}")
        if not t.get("request_type"):
            lines.append("(not a Service Desk request: request_type is empty)")
        cycles = _sla_by_task([t["id"]]).get(t["id"], [])
        lines.append("SLA: " + ("\n  " + "\n  ".join(sla_text(c) for c in cycles) if cycles else "нет циклов SLA"))
        if t.get("text"):
            lines += ["", s.html_to_md(t["text"])]
        n = int(a.get("comments") or 30)
        if n:
            lines += ["", "## Переписка", s.run_tool("eva_list_comments", {"code": a["code"], "limit": n})]
        return "\n".join(lines)

    if name == "eva_sd_kb":
        limit = int(a.get("limit") or 30)
        if a.get("code"):
            return s.run_tool("eva_get_document", {"code": a["code"]})
        if a.get("category") or a.get("query"):
            if a.get("category"):
                c = s.eva("CmfSDeskKnowlageCategory.get", {"filter": ["code", "=", a["category"]],
                                                           "fields": ["name", "parent"]}, use_cache=False)
                if not c:
                    raise ValueError(f"Category {a['category']} not found")
                flt = [["parent", "==", c.get("parent_id") or (c.get("parent") or {}).get("id")],
                       ["categories", "IN", [c["id"]]]]
            else:  # categories is m2m: only IN / NOT IN / = None, so search inside the visible categories
                cf = ["parent", "=", s.resolve_project(a["project"])] if a.get("project") else []
                cats = [c["id"] for c in _ok(s.eva("CmfSDeskKnowlageCategory.list",
                                                    {"filter": cf, "fields": ["code"], "limit": 1000}))]
                if not cats:
                    return "No knowledge base categories visible (CmfSDeskKnowlageCategory), nothing to search"
                flt = [["categories", "IN", cats]]
            if a.get("query"):
                flt.append(["name", "ILIKE", f"%{a['query']}%"])
            r = _ok(s.eva("CmfDocument.list", {"filter": flt, "fields": ["code", "name", "recommendation"],
                                               "limit": limit}, use_cache=False))
            return "\n".join(f"{d.get('code')}  {d.get('name')}{'  ★' if d.get('recommendation') else ''}  "
                             f"{s.BASE}/servicedesk/document/{d.get('code')}" for d in r) or "No articles found"
        flt = ["parent", "=", s.resolve_project(a["project"])] if a.get("project") else []
        r = _ok(s.eva("CmfSDeskKnowlageCategory.list", {"filter": flt, "fields": ["code", "name", "parent"],
                                                        "limit": limit}, use_cache=False))
        return "\n".join(f"{c.get('code')}  {c.get('name')}  ({_name(c.get('parent'))})  "
                         f"{s.BASE}/servicedesk/sdeskknowlagecategory/{c.get('code')}" for c in r) \
            or "No knowledge base categories visible (CmfSDeskKnowlageCategory)"

    portal = bool(a.get("portal"))

    if name == "eva_sd_create_request":
        project = s.resolve_project(a["project"])
        rt = _resolve_type(project, a["request_type"])
        me = s.eva("CmfPerson.get", {"filter": ["id", "=", "__G_CURRENT_USER"], "fields": ["company"]}) or {}
        save_kwargs = {"history_logic_type": None}
        kw = {"parent": project, "logic_type": rt.get("logic_type_id") or (rt.get("logic_type") or {}).get("id"),
              "request_type": rt["id"], "cmf_author": me.get("id"),
              "company": me.get("company_id") or (me.get("company") or {}).get("id"), "save_kwargs": save_kwargs}
        dummy = sd("CmfTask.create_dummy_task", kw, args=[], portal=portal)
        tid = dummy.get("id") if isinstance(dummy, dict) else dummy
        changed = {"name": a["title"]}
        if a.get("description"):
            changed["text"] = s.add_data_ids(s.md_to_html(a["description"]))
        changed.update(a.get("fields") or {})
        try:
            if not portal:  # the agent form locks the task, the portal form does not
                s.eva("CmfTask.lock", {}, top={"filter": ["id", "==", tid]})
            try:
                for k, v in changed.items():
                    sd("CmfTask.update", {"save_kwargs": save_kwargs, k: v}, args=[tid], portal=portal)
            finally:
                if not portal:
                    s.eva("CmfTask.unlock", {}, top={"filter": ["id", "==", tid]})
            sd("CmfTask.save_dummy_task", {"changed_fields": changed, "save_kwargs": save_kwargs}, args=[tid],
               portal=portal)
        except Exception:
            try:
                sd("CmfTask.delete_dummy_task", {}, args=[tid], portal=portal)
            except Exception:
                pass
            raise
        t = sd("CmfTask.get", {"filter": ["id", "=", tid], "fields": ["code", "name"]}, portal=portal,
               use_cache=False) or {}
        return f"Created request {t.get('code')} «{t.get('name')}» ({rt.get('name')})\n{s.task_url(t.get('code'))}"

    if name == "eva_sd_reply":
        t = sd("CmfTask.get", {"filter": ["code", "=", a["code"]], "fields": ["code"]}, portal=portal, use_cache=False)
        if not t:
            raise ValueError(f"{a['code']} not found (or no access)")
        dummy = sd("CmfComment.create_dummy", {"parent_id": t["id"], "text": "", "tree_parent": None,
                                               "private": bool(a.get("private")), "perm_encrypt": False,
                                               "fields": ["id"]}, args=[], portal=portal)
        cid = dummy.get("id") if isinstance(dummy, dict) else dummy
        sd("CmfComment.update", {"is_dummy": False, "text": s.add_data_ids(s.md_to_html(a["text"]))}, args=[cid],
           portal=portal)
        return f"Reply {cid} added to {a['code']}"

    if name == "eva_sd_set_status":
        t = sd("CmfTask.get", {"filter": ["code", "=", a["code"]], "fields": ["code", "status_id", "workflow_id"]},
               portal=portal, use_cache=False)
        if not t:
            raise ValueError(f"{a['code']} not found (or no access)")
        sts = _ok(s.eva("CmfStatus.list", {"filter": ["workflow_id", "=", t["workflow_id"]],
                                           "fields": ["name", "status_type"], "limit": 100}))
        want = a["status"]
        st = next((x for x in sts if x["id"] == want or (x.get("name") or "").lower() == want.lower()), None)
        if not st:
            raise ValueError(f"Status '{want}' not in this workflow: " + ", ".join(x.get("name") or "" for x in sts))
        try:
            sd("CmfTask.update", {"status": st["id"]}, args=[t["id"]], portal=portal)
        except s.EvaAbort as e:
            parts = str(e).split()
            if not parts or parts[0] != "must_transition_run":
                raise
            changed = dict(a.get("fields") or {}, status=st["id"])
            comment = s.add_data_ids(s.md_to_html(a["comment"])) if a.get("comment") else None
            sd("CmfTask.task_transition_run", {"comment_append": comment, "changed_fields": changed},
               args=[t["id"], parts[2]], portal=portal)
        return f"{a['code']} → {st.get('name')}"

    return None
