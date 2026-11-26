"""Reports, dashboards and analytics (read-only) for the EVA MCP server.

How the EVA UI does it (recorded from the web UI and its code):
- Dashboards: CmfDashboard (code DBRD-…, page /dashboard/Dashboard/<code>). Its `text` is HTML with widgets
  `<div data-macros="dashboard-chart" data-code="RPT-…">`; each widget is a CmfReport (parent = the dashboard).
- Project reports: CmfTaskReport (TR-…, "Отчёты" of a project); dashboard widgets: CmfReport (RPT-…).
  Both have `report_type` (agile_burndown, analysis_round, …) and `options` {filterId, statisticType, …}.
  The UI reads the report, then the filter `CmfBqlFilter.get {filter:[id = options.filterId], fields:[bql]}`
  (or the report's own report_bql_filter), lists the tasks (`CmfTask.list` page by page) and draws the chart
  in the browser. There is no server-side "report data" method.
- Server-side aggregation exists in the generic ORM: `<Class>.count {filter, group_by:[<field>_id | <field>]}`
  -> [{<field>: value, count}] and `<Class>.list {fields:["x", "sum__num", "count__person_id", "max__…"], group_by}`.
  Relations are grouped by their `_id` column (grouping by `status` itself is refused).
- "Аналитика" (/desk/analytics): CmfStatAggregDay.list grouped by project_id / person_id / obj_id with sum__num
  (action view/create/update/comment, obj_type task/document/attachment, day range) and CmfSearchStatAggregDay
  grouped by search_query (action search/click).
All calls here are get/list/count, so EVA_READONLY lets them through.
"""
import json
import re
import time

REPORT_TYPES = {
    "agile_burndown": "Диаграмма сгорания задач", "agile_control": "Контрольный график",
    "agile_epic_burn": "Сгорание эпика", "agile_process": "Сводная диаграмма процесса",
    "agile_release_burn": "Сгорание релиза", "agile_speed": "Диаграмма производительности",
    "agile_sprint": "Отчёты по спринтам", "agile_sprint_burn": "Диаграмма Burndown",
    "analysis_period_mean": "Отчёт по среднему периоду", "analysis_round": "Круговая диаграмма",
    "analysis_single_level": "Одноуровневая группировка", "analysis_solution_time": "Время решения",
    "analysis_task_created": "Созданные и решённые задачи", "analysis_task_moment": "Задачи по времени «с момента»",
    "analysis_task_recently": "Недавно созданные задачи", "analysis_time_planned": "Планируемое время",
    "analysis_time_spent": "Отработанные часы", "analysis_plan_fact": "План-факт",
    "average_time_in_status": "Среднее время в статусе", "bubble_diagram": "Пузырьковая диаграмма",
    "favorite_filters": "Избранные фильтры", "line_chart": "Линейная диаграмма",
    "plan_time_tracker": "Отслеживание времени", "plan_user_load": "Загруженность пользователей",
    "plan_version_load": "Нагрузка по версии", "plan_work_load": "Рабочая нагрузка",
    "rich_filter_simple_counter": "Счётчик по фильтру", "test_cycle_result": "Показатели по тест-прогону",
    "test_tester_result": "Показатели по тестировщику",
}

# group keys for eva_task_stats: name -> (column for group_by, model for names or choices field)
GROUPS = {
    "status": ("status_id", "CmfStatus"), "status_type": ("cache_status_type", "choices"),
    "assignee": ("responsible_id", "CmfPerson"), "responsible": ("responsible_id", "CmfPerson"),
    "author": ("cmf_author_id", "CmfPerson"), "owner": ("cmf_owner_id", "CmfPerson"),
    "type": ("logic_type_id", "CmfLogicType"), "project": ("project_id", "CmfProject"),
    "priority": ("priority", "choices"),
}
CHOICE_FIELD = {"cache_status_type": "cache_status_type", "priority": "priority"}

PROJECT_PROP = {"type": "string", "description": "Project code (my-project), id or name fragment"}

TOOLS_REPORTS = [
    {"name": "eva_reports",
     "description": "List EVA dashboards (DBRD-…), dashboard widget reports (RPT-…) and project reports (TR-…, "
                    "the project 'Отчёты' section). Search by name, narrow by project or kind. "
                    "Open one with eva_report.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Name fragment"},
         "kind": {"type": "string", "enum": ["all", "dashboards", "reports", "project_reports"], "default": "all"},
         "project": PROJECT_PROP,
         "include_lms": {"type": "boolean", "default": False,
                         "description": "Also list LMS course reports (thousands of auto-created ones)"},
         "limit": {"type": "integer", "default": 30}}}},
    {"name": "eva_report",
     "description": "Open a dashboard (DBRD-…) or report (RPT-… / TR-…) and return its data as a readable summary: "
                    "report type, filter (UBQL-like BQL), total tasks and server-side counts grouped by status, "
                    "type, assignee (or the report's own grouping field). For a dashboard: every widget. "
                    "Charts over time (burndown, velocity) are drawn by the EVA UI from the same task list; "
                    "here you get the task set behind them.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string", "description": "DBRD-000018, RPT-005360, TR-000371 or an object id"},
         "group_by": {"type": "array", "items": {"type": "string"},
                      "description": "Override groupings: status, status_type, assignee, author, type, project, priority"},
         "top": {"type": "integer", "default": 15, "description": "Rows per grouping"}},
         "required": ["code"]}},
    {"name": "eva_task_stats",
     "description": "Aggregates over tasks matching a UBQL filter, computed by EVA (count with group_by): total, "
                    "counts by status / status_type / assignee / author / type / project / priority, and overdue "
                    "(deadline passed, not closed). Example query: 'project = my-project and statusCategory != \"Закрыто\"'.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "UBQL as in the EVA filter editor (or a JSON filter list)"},
         "project": PROJECT_PROP,
         "group_by": {"type": "array", "items": {"type": "string"}, "default": ["status", "assignee", "type"],
                      "description": "Any of: status, status_type, assignee, author, owner, type, project, priority; "
                                     "two at once as 'status+assignee' for a cross table"},
         "overdue": {"type": "boolean", "default": True},
         "top": {"type": "integer", "default": 20}}}},
    {"name": "eva_analytics",
     "description": "The 'Аналитика' section (/desk/analytics): views/creates/updates/comments by project, user, "
                    "task, document or attachment, and top search queries, over a date range (CmfStatAggregDay).",
     "inputSchema": {"type": "object", "properties": {
         "by": {"type": "string", "enum": ["projects", "users", "tasks", "documents", "attachments", "search"],
                "default": "projects"},
         "action": {"type": "string", "description": "view (default), create, update, comment; for search: search, click"},
         "date_from": {"type": "string", "description": "YYYY-MM-DD, default 30 days ago"},
         "date_to": {"type": "string", "description": "YYYY-MM-DD, default today"},
         "project": PROJECT_PROP,
         "limit": {"type": "integer", "default": 20}}}},
]

WRITE_TOOLS_REPORTS = set()  # everything here only reads

_CLASS_BY_PREFIX = {"DBRD": "CmfDashboard", "RPT": "CmfReport", "TR": "CmfTaskReport"}


def _name(v):
    return v.get("name") if isinstance(v, dict) else v


def _ok(items):
    return [x for x in (items or []) if x.get("code") and x.get("_acl_obj") != "deny"]


def _names(model, ids):
    import server
    ids = [i for i in ids if i]
    if not ids:
        return {}
    fields = ["name", "login"] if model == "CmfPerson" else ["name", "code"]
    r = server.eva(f"{model}.list", {"filter": ["id", "IN", ids], "fields": fields, "slice": [0, len(ids) + 5]}) or []
    return {x["id"]: x.get("name") or x.get("code") or x["id"] for x in r if x.get("id") and x.get("_acl_obj") != "deny"}


def _choices(field):
    import server
    return (server.class_meta("CmfTask").get(field) or {}).get("choices") or {}


def _label_fn(key):
    col, model = GROUPS[key]
    if model == "choices":
        ch = _choices(CHOICE_FIELD[col])
        return col, lambda vals: {v: ch.get(str(v), v) for v in vals}
    return col, lambda vals: _names(model, vals)


def group_counts(flt, keys, top=20):
    """Server-side counts: CmfTask.count {filter, group_by:[col, …]} -> printable lines."""
    import server
    keys = [k.strip() for k in keys]
    for k in keys:
        if k not in GROUPS:
            raise ValueError(f"Unknown grouping '{k}'; use: {', '.join(GROUPS)}")
    cols, labelers = zip(*[_label_fn(k) for k in keys])
    r = server.eva("CmfTask.count", {"filter": flt, "group_by": list(cols)}) or []
    if not isinstance(r, list):
        return [f"  (no grouping returned: {r})"]
    labels = [lab([x.get(c) for x in r]) for c, lab in zip(cols, labelers)]
    r.sort(key=lambda x: -(x.get("count") or 0))
    out = []
    for x in r[:top]:
        parts = [str(lb.get(x.get(c)) or ("—" if x.get(c) in (None, "") else x.get(c))) for c, lb in zip(cols, labels)]
        out.append(f"  {x.get('count'):>6}  {' / '.join(parts)}")
    if len(r) > top:
        out.append(f"  … ещё {len(r) - top} групп, {sum(x.get('count') or 0 for x in r[top:])} задач")
    return out or ["  —"]


def task_stats(flt, groups, overdue=True, top=20):
    import server
    total = server.eva("CmfTask.count", {"filter": flt})
    lines = [f"Всего задач: {total}"]
    if overdue:
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        od = ["AND", flt, ["deadline", "<", now], ["cache_status_type", "!=", "CLOSED"]] if flt else \
            ["AND", ["deadline", "<", now], ["cache_status_type", "!=", "CLOSED"]]
        lines.append(f"Просрочено (срок прошёл, не закрыта): {server.eva('CmfTask.count', {'filter': od})}")
    if total:
        for g in groups:
            keys = g.split("+")
            lines.append(f"\nПо {' + '.join(keys)}:")
            lines += group_counts(flt, keys, top)
    return "\n".join(lines)


def _bql_of(filter_obj):
    if not isinstance(filter_obj, dict):
        return None
    b = filter_obj.get("bql")
    return json.loads(b) if isinstance(b, str) and b.strip() else b


def _flt(bql):
    """CmfBqlFilter.bql is a list of conditions (implicit AND) -> one filter."""
    if isinstance(bql, list) and bql and all(isinstance(x, list) for x in bql) and not isinstance(bql[0], str):
        return bql[0] if len(bql) == 1 else ["AND", *bql]
    return bql


def report_filter(rep):
    """The task filter a report draws from: options.filterId -> CmfBqlFilter, else report_bql_filter."""
    import server
    opts = rep.get("options") or {}
    fid = opts.get("filterId")
    if fid:
        f = server.eva("CmfBqlFilter.get", {"filter": ["id", "=", fid], "fields": ["bql", "name", "code"]})
        if f and f.get("_acl_obj") != "deny":
            return _flt(_bql_of(f)), f.get("name") or f.get("code")
        return None, f"{fid} (нет доступа)"
    if opts.get("bql"):
        b = opts["bql"]
        return _flt(json.loads(b) if isinstance(b, str) else b), "bql в настройках"
    if opts.get("ubql"):
        import server as s
        return s.to_filter(opts["ubql"]), "ubql в настройках"
    for k in ("report_bql_filter", "tmp_bql_filter"):
        if isinstance(rep.get(k), dict) and rep[k].get("bql"):
            return _flt(_bql_of(rep[k])), k
    return None, None


STAT_TYPE_GROUP = {"status": "status", "responsible": "assignee", "assignee": "assignee", "author": "author",
                   "cmf_author": "author", "logic_type": "type", "type": "type", "project": "project", "priority": "priority"}


def render_report(cls, rep, groups=None, top=15):
    import server
    rt = rep.get("report_type")
    opts = rep.get("options") or {}
    head = [f"## {rep.get('name')} ({rep.get('code')})",
            f"Тип: {REPORT_TYPES.get(rt, rt)} [{rt}]"]
    if _name(rep.get("project")):
        head.append(f"Проект: {_name(rep.get('project'))}")
    shown = {k: v for k, v in opts.items() if k not in ("color",) and v not in (None, "", [], {})}
    if shown:
        head.append("Настройки: " + json.dumps(shown, ensure_ascii=False)[:500])
    if rep.get("date_start") or rep.get("date_end"):
        head.append(f"Период: {(rep.get('date_start') or '')[:10]} — {(rep.get('date_end') or '')[:10]}")
    flt, fname = report_filter(rep)
    if not flt:
        head.append("Фильтр: " + (fname or "не задан (отчёт не настроен — в интерфейсе откроются настройки)"))
        return "\n".join(head)
    head.append(f"Фильтр: {fname}: {json.dumps(flt, ensure_ascii=False)[:400]}")
    if not groups:
        st = STAT_TYPE_GROUP.get(str(opts.get("statisticType") or "").replace("_id", ""))
        groups = [st] if st else ["status_type", "status", "type", "assignee"]
    return "\n".join(head) + "\n" + task_stats(flt, groups, overdue=True, top=top)


def dashboard_widgets(text):
    return re.findall(r'data-macros="dashboard-chart"[^>]*?data-code="([^"]+)"|data-code="([^"]+)"[^>]*?data-macros="dashboard-chart"',
                      text or "")


def _get(cls, code):
    import server
    flt = ["id", "=", code] if code.startswith("Cmf") else ["code", "=", code]
    fields = ["code", "name", "report_type", "options", "project", "parent", "date_start", "date_end"]
    if cls == "CmfTaskReport":
        fields += ["report_bql_filter", "tmp_bql_filter"]
    if cls == "CmfDashboard":
        fields = ["code", "name", "text", "project", "cmf_owner"]
    r = server.eva(f"{cls}.get", {"filter": flt, "fields": fields})
    if not r or r.get("_acl_obj") == "deny" or not r.get("code"):
        raise ValueError(f"{code} not found (or no access)")
    return r


def open_any(code, groups=None, top=15):
    import server
    cls = code.split(":")[0] if code.startswith("Cmf") else _CLASS_BY_PREFIX.get(code.split("-")[0].upper())
    if cls not in ("CmfDashboard", "CmfReport", "CmfTaskReport"):
        raise ValueError("Expected DBRD-…, RPT-…, TR-… or a CmfDashboard/CmfReport/CmfTaskReport id")
    obj = _get(cls, code)
    if cls != "CmfDashboard":
        return render_report(cls, obj, groups, top)
    codes = [a or b for a, b in dashboard_widgets(obj.get("text"))]
    out = [f"# Дашборд {obj.get('name')} ({obj.get('code')})",
           f"Ссылка: {server.BASE}/dashboard/Dashboard/{obj.get('code')}",
           f"Владелец: {_name(obj.get('cmf_owner')) or '—'}", f"Виджетов: {len(codes)}"]
    if not codes:  # older dashboards: widgets are just child reports
        kids = _ok(server.eva("CmfReport.list", {"filter": ["parent_id", "=", obj["id"]], "fields": ["code"], "slice": [0, 50]}))
        codes = [k["code"] for k in kids]
    for c in codes:
        try:
            out.append("\n" + render_report("CmfReport", _get("CmfReport", c), groups, top))
        except Exception as e:  # one broken widget must not hide the rest
            out.append(f"\n## {c}: {e}")
    return "\n".join(out)


def list_reports(a):
    import server
    q, kind, lim = a.get("query"), a.get("kind") or "all", int(a.get("limit") or 30)
    pid = server.resolve_project(a.get("project"))
    out = []
    plan = [("dashboards", "CmfDashboard", ["code", "name", "cmf_owner", "cmf_created_at"]),
            ("reports", "CmfReport", ["code", "name", "report_type", "parent"]),
            ("project_reports", "CmfTaskReport", ["code", "name", "report_type", "project"])]
    for k, cls, fields in plan:
        if kind not in ("all", k):
            continue
        conds = []
        if q:
            conds.append(["name", "ILIKE", f"%{q}%"])
        if pid:
            conds.append(["project_id", "=", pid])
        if cls == "CmfReport" and not a.get("include_lms"):
            conds.append(["report_type", "NOT IN", ["lms_course_summary", "lms_job", "lms_student_summary", "lms_exam_result"]])
        flt = conds[0] if len(conds) == 1 else (["AND", *conds] if conds else None)
        kw = {"fields": fields, "order_by": ["-cmf_modified_at"], "slice": [0, lim * 3]}
        if flt:
            kw["filter"] = flt
        items = _ok(server.eva(f"{cls}.list", kw))[:lim]
        title = {"dashboards": "Дашборды", "reports": "Отчёты дашбордов", "project_reports": "Отчёты проектов"}[k]
        out.append(f"## {title} ({len(items)})")
        for x in items:
            rt = x.get("report_type")
            where = _name(x.get("project")) or _name(x.get("parent")) or _name(x.get("cmf_owner")) or ""
            out.append(f"{x['code']}  {x.get('name')}" + (f"  [{REPORT_TYPES.get(rt, rt)}]" if rt else "")
                       + (f"  — {where}" if where else ""))
        if not items:
            out.append("—")
    return "\n".join(out)


def _day(s, default):
    s = s or default
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError("Dates are YYYY-MM-DD")
    return s


def analytics(a):
    import server
    by, lim = a.get("by") or "projects", int(a.get("limit") or 20)
    d1 = _day(a.get("date_from"), time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400)))
    d2 = _day(a.get("date_to"), time.strftime("%Y-%m-%d"))
    tz = time.strftime("%z")
    tz = tz[:3] + ":" + tz[3:]
    period = [["day", ">=", f"{d1}T00:00:00{tz}"], ["day", "<=", f"{d2}T23:59:59{tz}"]]
    pid = server.resolve_project(a.get("project"))
    if pid:
        period.append(["project_id", "=", pid])
    head = f"Аналитика: {by}, {d1} — {d2}"
    if by == "search":
        act = a.get("action") or "search"
        r = server.eva("CmfSearchStatAggregDay.list", {
            "filter": ["AND", *period, ["action", "==", act], ["obj_id", "!=", None]],
            "fields": ["search_query", "sum__num"], "group_by": ["search_query"], "order_by": ["-sum__num"],
            "slice": [0, lim]}) or []
        return head + f", action={act}\n" + "\n".join(f"{x.get('sum__num'):>8}  {x.get('search_query')}" for x in r)
    act = a.get("action") or "view"
    col, model, extra = {"projects": ("project_id", "CmfProject", ["project_id", "!=", None]),
                         "users": ("person_id", "CmfPerson", ["person_id", "!=", None]),
                         "tasks": ("obj_id", "CmfTask", ["obj_type", "==", "task"]),
                         "documents": ("obj_id", "CmfDocument", ["obj_type", "==", "document"]),
                         "attachments": ("obj_id", "CmfAttachment", ["obj_type", "==", "attachment"])}[by]
    r = server.eva("CmfStatAggregDay.list", {
        "filter": ["AND", *period, extra, ["action", "==", act]],
        "fields": [col, "sum__num", "max__cmf_modified_at"], "group_by": [col], "order_by": ["-sum__num"],
        "slice": [0, lim]}) or []
    ids = [x.get(col) for x in r]
    names = {}
    if ids:
        lr = server.eva(f"{model}.list", {"filter": ["id", "IN", ids], "fields": ["name", "code"],
                                          "include_deleted": True, "slice": [0, len(ids) + 5]}) or []
        names = {x["id"]: (x.get("code") + "  " if x.get("code") and by in ("tasks", "documents") else "") + (x.get("name") or "")
                 for x in lr if x.get("id") and x.get("_acl_obj") != "deny"}
    lines = [f"{x.get('sum__num'):>8}  {names.get(x.get(col)) or x.get(col)}  (последнее {(x.get('max__cmf_modified_at') or '')[:10]})"
             for x in r]
    return head + f", action={act}\n" + ("\n".join(lines) or "—")


def run(name, a):
    """Returns the tool output, or None if the tool is not one of ours."""
    import server
    if name == "eva_reports":
        return list_reports(a)
    if name == "eva_report":
        return open_any(a["code"].strip(), a.get("group_by"), int(a.get("top") or 15))
    if name == "eva_task_stats":
        conds = []
        if a.get("query"):
            conds.append(server.to_filter(a["query"]))
        pid = server.resolve_project(a.get("project"))
        if pid:
            conds.append(["project_id", "=", pid])
        if not conds:
            raise ValueError("Give a query (UBQL) or a project: counting all tasks of EVA times out")
        flt = conds[0] if len(conds) == 1 else ["AND", *conds]
        groups = a.get("group_by") or ["status", "assignee", "type"]
        return task_stats(flt, groups, a.get("overdue", True), int(a.get("top") or 20))
    if name == "eva_analytics":
        return analytics(a)
    return None
