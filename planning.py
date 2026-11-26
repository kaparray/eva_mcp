"""Planning in EVA: Gantt, roadmaps, work calendars, workload. Read-only tools.

How the web UI does it (recorded on /project/Roadmap/RM-…, Gantt view):
  CmfRoadmap.get {ft_bql_sources, …}            roadmap sources = CmfBqlFilter.bql (one filter per source)
  CmfRoadmap.ncget_filtered_roadmap_by_level      {insert_bql_list: [["OR", <bql>, …]], start, end, show_child_tasks,
                                                   sort_fields: ["op_gantt_task.outline_number"], sort_orders: ["asc"]}
                                                  -> {"1": [[task_id, parent_id, task_id], …], "2": …} (tree rows by level)
  CmfTask.list {filter: [id IN …], fields: op_gantt_task.sched_*/actual_*/baseline_*/is_critical_path, …}
  CmfRelationOption.list {filter: OR(in_link IN ids, out_link IN ids)}   dependencies (relation_type system.finish:start …)
  CmfGanttBaseline.list {filter: [parent_id = roadmap]}                   baselines
Durations and work in CmfGanttTask are minutes (a working day of the standard calendar is 480).
Work calendars: CmfCalendar (system:default «Стандартный», personal «Календарь <login>» with parent_calendar),
days in CmfCalendarDay {date, day_type work/weekend/…, interval_total_minutes}; vacations: CmfPerson.vacation_interval.
"""
import datetime as _dt
import json

GANTT_LINK_TYPES = {"system.finish:start": "FS", "system.start:start": "SS", "system.start:finish": "SF",
                    "system.finish:finish": "FF"}
DAY_MIN = 480

_P = {"type": "string", "description": "Project code (my-project), id or name fragment"}
TOOLS_PLANNING = [
    {"name": "eva_roadmaps",
     "description": "Roadmaps (CmfRoadmap, «Планирование → Roadmap»): list by name/project with their data sources (UBQL).",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Name fragment"}, "project": _P,
         "limit": {"type": "integer", "default": 30}}}},
    {"name": "eva_gantt",
     "description": "Gantt chart as a table, like the Gantt view of a roadmap: tasks in outline order with planned "
                    "(sched) and actual dates, duration (working days), work (h), % complete, critical path, baseline, "
                    "and dependencies (FS/SS/SF/FF with lag). Source: roadmap code (RM-000087), or UBQL query, or "
                    "sprint/release code, or project.",
     "inputSchema": {"type": "object", "properties": {
         "roadmap": {"type": "string", "description": "Roadmap code RM-… or id"},
         "query": {"type": "string", "description": "UBQL, e.g. project = my-project and tags = TAG-001"},
         "list": {"type": "string", "description": "Sprint/release (CmfList) code"},
         "project": _P,
         "child_tasks": {"type": "boolean", "default": False, "description": "Show subtasks as the next levels"},
         "all_links": {"type": "boolean", "default": False, "description": "Show all relations, not only Gantt ones"},
         "limit": {"type": "integer", "default": 100}}}},
    {"name": "eva_calendar",
     "description": "Work calendar: working days/hours and non-working days in a period. Calendar by code/name, or a "
                    "person's calendar (inherits its parent calendar, default «Стандартный»).",
     "inputSchema": {"type": "object", "properties": {
         "calendar": {"type": "string", "description": "Calendar code (system:default, CAL-…) or name"},
         "person": {"type": "string", "description": "Login, email or name"},
         "from": {"type": "string", "description": "YYYY-MM-DD (default: today)"},
         "to": {"type": "string", "description": "YYYY-MM-DD (default: from + 30 days)"}}}},
    {"name": "eva_workload",
     "description": "Workload of people in a period: capacity by work calendar minus vacation, planned work of their "
                    "Gantt tasks (op_gantt_task sched dates overlapping the period, work prorated by days), vacation. "
                    "People by login/name, or everyone responsible for tasks of a roadmap/UBQL query.",
     "inputSchema": {"type": "object", "properties": {
         "people": {"type": "array", "items": {"type": "string"}},
         "roadmap": {"type": "string"}, "query": {"type": "string"},
         "from": {"type": "string"}, "to": {"type": "string"},
         "limit": {"type": "integer", "default": 30}}}},
]
NAMES = {t["name"] for t in TOOLS_PLANNING}
WRITE_TOOLS_PLANNING = set()  # nothing here changes EVA


def _s():
    import server
    return server


def _name(v):
    return (v or {}).get("name") if isinstance(v, dict) else None


def _d(v):
    return (v or "")[:10]


def _date(v, default):
    return _dt.date.fromisoformat(v) if v else default


# ---------- roadmaps ----------

def roadmap_get(ref):
    s = _s()
    flt = ["id", "=", ref] if ref.startswith("CmfRoadmap:") else ["code", "=", ref]
    r = s.eva("CmfRoadmap.get", {"filter": flt, "fields": ["code", "name", "project", "ft_bql_sources", "ft_sources",
                                                           "parent.main_gantt_project_id"]})
    if not r or not r.get("code"):
        raise ValueError(f"Roadmap {ref} not found (or no access)")
    return r


def roadmap_sources(rm):
    """Filters of the roadmap sources (CmfBqlFilter.bql) and their UBQL text."""
    s = _s()
    ids = [x["id"] for x in rm.get("ft_bql_sources") or []]
    if not ids:
        return [], []
    r = s.eva("CmfBqlFilter.list", {"filter": ["id", "IN", ids], "fields": ["bql", "ubql"]}) or []
    flts = [json.loads(x["bql"]) if isinstance(x.get("bql"), str) else x.get("bql") for x in r if x.get("bql")]
    return flts, [x.get("ubql") or x.get("bql") for x in r]


def tool_roadmaps(a):
    s = _s()
    conds = []
    if a.get("query"):
        conds.append(["name", "ILIKE", f"%{a['query']}%"])
    if a.get("project"):
        conds.append(["project_id", "=", s.resolve_project(a["project"])])
    lim = int(a.get("limit") or 30)
    kw = {"fields": ["code", "name", "project", "ft_bql_sources"], "order_by": ["-cmf_created_at"],
          "slice": [0, lim * 3]}
    if conds:
        kw["filter"] = ["AND", *conds] if len(conds) > 1 else conds[0]
    out = []
    for x in s.eva("CmfRoadmap.list", kw) or []:
        if not x.get("code") or x.get("_acl_obj") == "deny":
            continue
        _, ubql = roadmap_sources(x)
        proj = (x.get("project") or {}).get("code") or ""
        out.append(f"{x['code']}  {(x.get('name') or '').strip()}  [{proj}]  "
                   f"{'источники: ' + '; '.join(ubql) if ubql else 'без источников'}  {s.BASE}/project/Roadmap/{x['code']}")
        if len(out) >= lim:
            break
    return "\n".join(out) or "Nothing found"


# ---------- gantt ----------

GANTT_FIELDS = ["code", "name", "status", "responsible", "parent_task_id", "deadline", "is_milestone",
                "op_gantt_task.outline_number", "op_gantt_task.sched_start_date", "op_gantt_task.sched_finish_date",
                "op_gantt_task.sched_duration", "op_gantt_task.sched_work", "op_gantt_task.actual_start_date",
                "op_gantt_task.actual_finish_date", "op_gantt_task.actual_work", "op_gantt_task.actual_complete",
                "op_gantt_task.is_critical_path", "op_gantt_task.total_slack", "op_gantt_task.baseline_start_date",
                "op_gantt_task.baseline_finish_date", "op_gantt_task.is_manual"]


def gantt_sources(a):
    """-> (title, list of filters) like the UI builds insert_bql_list."""
    s = _s()
    if a.get("roadmap"):
        rm = roadmap_get(a["roadmap"])
        flts, ubql = roadmap_sources(rm)
        if not flts:
            raise ValueError(f"Roadmap {rm['code']} has no data sources")
        return f"{rm['code']} {(rm.get('name') or '').strip()} ({'; '.join(ubql)})", flts, rm
    if a.get("query"):
        return a["query"], [s.to_filter(a["query"])], None
    if a.get("list"):
        r = s.eva("CmfList.get", {"filter": ["code", "=", a["list"]], "fields": ["code", "name"]})
        if not r or not r.get("id"):
            raise ValueError(f"Sprint/release {a['list']} not found")
        return f"{r.get('code')} {r.get('name')}", [["lists", "IN", [r["id"]]]], None
    if a.get("project"):
        return a["project"], [["project_id", "=", s.resolve_project(a["project"])]], None
    raise ValueError("Give roadmap, query, list or project")


def gantt_request(flts, start, end, child_tasks):
    return {"insert_bql_list": [["OR", *flts]], "start": start, "end": end, "show_child_tasks": bool(child_tasks),
            "sort_fields": ["op_gantt_task.outline_number"], "sort_orders": ["asc"]}


def gantt_tree(flts, limit, child_tasks=False):
    """Ordered [(task_id, parent_id, level)] from ncget_filtered_roadmap_by_level (as the UI tree loads it)."""
    s = _s()
    r = s.eva("CmfRoadmap.ncget_filtered_roadmap_by_level", gantt_request(flts, 0, limit, child_tasks),
              args=[], top={"fields": []}) or {}
    by_parent, top = {}, []
    for lvl in sorted(r, key=lambda k: int(k) if str(k).isdigit() else 0):
        for row in r[lvl] or []:
            tid, parent = row[0], row[1] if len(row) > 1 else None
            if parent:
                by_parent.setdefault(parent, []).append(tid)
            else:
                top.append(tid)
    out, seen = [], set()

    def walk(tid, lvl):
        if tid in seen:
            return
        seen.add(tid)
        out.append((tid, lvl))
        for c in by_parent.get(tid, []):
            walk(c, lvl + 1)
    for t in top:
        walk(t, 0)
    for p, cs in by_parent.items():  # children whose parent did not come back
        for c in cs:
            walk(c, 0)
    return out


def links_of(ids, all_links=False):
    s = _s()
    if not ids:
        return []
    r = s.eva("CmfRelationOption.list", {
        "filter": ["OR", ["in_link_id", "IN", ids], ["out_link_id", "IN", ids]],
        "fields": ["in_link_id", "out_link_id", "constrain_lag", "relation_type.code", "relation_type.name"],
        "slice": [0, 2000]}) or []
    out = []
    for x in r:
        code = ((x.get("relation_type") or {}).get("code")) or ""
        if all_links or code in GANTT_LINK_TYPES:
            out.append(x)
    return out


def _days(minutes):
    if minutes in (None, ""):
        return ""
    d = minutes / DAY_MIN
    return f"{d:g}д" if d == int(d) else f"{d:.1f}д"


def _hours(minutes):
    return "" if not minutes else f"{minutes / 60:g}ч"


def tool_gantt(a):
    s = _s()
    lim = int(a.get("limit") or 100)
    title, flts, rm = gantt_sources(a)
    tree = gantt_tree(flts, lim, a.get("child_tasks"))
    ids = [t for t, _ in tree]
    if not ids:
        return f"Гант: {title}\nNothing found"
    tasks = {}
    for i in range(0, len(ids), 200):
        for t in s.eva("CmfTask.list", {"filter": ["id", "IN", ids[i:i + 200]], "fields": GANTT_FIELDS,
                                        "slice": [0, 200]}) or []:
            tasks[t["id"]] = t
    code = {i: (tasks.get(i) or {}).get("code") or "?" for i in ids}
    pred, succ = {}, {}
    for l in links_of(ids, a.get("all_links")):
        rt = l.get("relation_type") or {}
        kind = GANTT_LINK_TYPES.get(rt.get("code")) or rt.get("name") or "?"
        lag = l.get("constrain_lag")
        lag = f"{'+' if lag > 0 else ''}{_days(lag)}" if lag else ""
        o, n = l.get("out_link_id"), l.get("in_link_id")  # out_link -> in_link: predecessor -> successor
        pred.setdefault(n, []).append(f"{code.get(o) or _code_of(o)}{kind}{lag}")
        succ.setdefault(o, []).append(f"{code.get(n) or _code_of(n)}{kind}{lag}")
    lines = [f"Гант: {title}", f"{len(ids)} задач" + (f" (показаны первые {lim})" if len(ids) >= lim else ""),
             "№ | код | задача | статус | ответственный | план (начало → конец, длит.) | трудозатр. план/факт | "
             "факт (начало → конец) | % | базовый план | предшественники | крит."]
    for tid, lvl in tree:
        t = tasks.get(tid)
        if not t or t.get("_acl_obj") == "deny" or not t.get("code"):
            lines.append(f"{'  ' * lvl}(нет доступа {tid})")
            continue
        g = t.get("op_gantt_task") or {}
        plan = (f"{_d(g.get('sched_start_date'))} → {_d(g.get('sched_finish_date'))} {_days(g.get('sched_duration'))}".strip()
                if g.get("sched_start_date") else "")
        fact = f"{_d(g.get('actual_start_date'))} → {_d(g.get('actual_finish_date'))}" if g.get("actual_start_date") else ""
        base = f"{_d(g.get('baseline_start_date'))} → {_d(g.get('baseline_finish_date'))}" if g.get("baseline_start_date") else ""
        work = "/".join(x for x in (_hours(g.get("sched_work")), _hours(g.get("actual_work"))) if x) if (g.get("sched_work") or g.get("actual_work")) else ""
        lines.append(" | ".join([
            "  " * lvl + str(g.get("outline_number") or ""), t["code"], (t.get("name") or "").strip() + (" ◆" if t.get("is_milestone") else ""),
            _name(t.get("status")) or "", _name(t.get("responsible")) or "", plan, work, fact,
            f"{g.get('actual_complete') or 0}%", base, ", ".join(pred.get(tid, [])), "КП" if g.get("is_critical_path") else ""]))
    if rm:
        bl = s.eva("CmfGanttBaseline.list", {"filter": ["parent_id", "=", rm["id"]], "fields": ["name", "is_operate"]}) or []
        if bl:
            lines.append("Базовые планы: " + ", ".join(f"{b.get('name')}{' (активный)' if b.get('is_operate') else ''}" for b in bl))
    lines.append("Связи: FS — окончание→начало, SS — начало→начало, SF, FF; КП — критический путь (как его последний "
                 "раз посчитала EVA). Длительность — рабочие дни по 8 ч.")
    return "\n".join(lines)


_CODES = {}


def _code_of(tid):
    if not tid:
        return "?"
    if tid not in _CODES:
        r = _s().eva("CmfTask.get", {"filter": ["id", "=", tid], "fields": ["code"]}) or {}
        _CODES[tid] = r.get("code") or "?"
    return _CODES[tid]


# ---------- calendars ----------

def find_person(q):
    s = _s()
    if q.startswith("CmfPerson:"):
        flt = ["id", "=", q]
    else:
        flt = ["OR", ["login", "=", q], ["email", "=", q], ["name", "ILIKE", f"%{q}%"]]
    r = [x for x in s.eva("CmfPerson.list", {"filter": flt, "fields": ["login", "name", "calendar_id", "vacation_interval",
                                                                       "on_vacation", "does_not_work"],
                                              "slice": [0, 5]}) or [] if x.get("id") and x.get("_acl_obj") != "deny"]
    if not r:
        raise ValueError(f"Person '{q}' not found")
    return r[0]


def resolve_calendar(ref=None, person=None):
    """-> (calendar with days, chain of names). Personal calendars keep no days: walk parent_calendar."""
    s = _s()
    cal_id = None
    if person:
        cal_id = person.get("calendar_id")
    elif ref:
        r = s.eva("CmfCalendar.list", {"filter": ["OR", ["code", "=", ref], ["name", "ILIKE", f"%{ref}%"]],
                                       "fields": ["code"], "slice": [0, 1]}) or []
        if not r:
            raise ValueError(f"Calendar '{ref}' not found")
        cal_id = r[0]["id"]
    if not cal_id:
        cal_id = (s.eva("CmfCalendar.get", {"filter": ["is_default", "=", True], "fields": ["code"]}) or {}).get("id")
    chain = []
    for _ in range(5):
        c = s.eva("CmfCalendar.get", {"filter": ["id", "=", cal_id], "fields": ["code", "name", "parent_calendar_id", "timezone"]})
        chain.append(c)
        if s.eva("CmfCalendarDay.count", {"filter": ["parent_id", "=", cal_id]}) or not c.get("parent_calendar_id"):
            break
        cal_id = c["parent_calendar_id"]
    return chain[-1], [f"{c.get('name')} ({c.get('code')})" for c in chain]


def calendar_days(cal_id, d1, d2):
    r = _s().eva("CmfCalendarDay.list", {
        "filter": ["AND", ["parent_id", "=", cal_id], ["date", ">=", d1.isoformat()], ["date", "<=", d2.isoformat()]],
        "fields": ["date", "day_type", "interval_total_minutes", "interval_json"], "order_by": ["date"],
        "slice": [0, 1000]}) or []
    return [x for x in r if x.get("date")]


def in_vacation(p, day):
    v = p.get("vacation_interval") or []
    return len(v) == 2 and v[0] and v[1] and v[0] <= day.isoformat() <= v[1]


def tool_calendar(a):
    d1 = _date(a.get("from"), _dt.date.today())
    d2 = _date(a.get("to"), d1 + _dt.timedelta(days=30))
    p = find_person(a["person"]) if a.get("person") else None
    cal, chain = resolve_calendar(a.get("calendar"), p)
    days = calendar_days(cal["id"], d1, d2)
    work = [x for x in days if (x.get("interval_total_minutes") or 0) > 0]
    vac = [x for x in work if p and in_vacation(p, _dt.date.fromisoformat(x["date"]))]
    mins = sum(x["interval_total_minutes"] for x in work if x not in vac)
    lines = [f"Календарь: {' → '.join(chain)}" + (f"; сотрудник {p.get('name')} ({p.get('login')})" if p else ""),
             f"Период {d1} — {d2}: рабочих дней {len(work) - len(vac)}, часов {mins / 60:g}"
             + (f"; отпуск {p['vacation_interval'][0]} — {p['vacation_interval'][1]} ({len(vac)} раб. дн.)" if vac else "")]
    if not days:
        lines.append("В календаре нет дней за этот период (рассчитан на calc_from–calc_until)")
    wd = "пн вт ср чт пт сб вс".split()
    for x in days:
        dd = _dt.date.fromisoformat(x["date"])
        if x.get("day_type") != "work" or dd.weekday() >= 5 or (x.get("interval_total_minutes") or 0) != DAY_MIN:
            iv = ", ".join("–".join(i) for i in x.get("interval_json") or [])
            lines.append(f"  {x['date']} {wd[dd.weekday()]}  {x.get('day_type')}  {(x.get('interval_total_minutes') or 0) / 60:g} ч {iv}")
    lines.append("Остальные дни — рабочие по 8 ч.")
    return "\n".join(lines)


# ---------- workload ----------

def _overlap_work(g, d1, d2, field="sched_work"):
    """Planned work of a gantt task inside [d1, d2], prorated by calendar days (EVA spreads work evenly)."""
    s_, f_ = _d(g.get("sched_start_date")), _d(g.get("sched_finish_date"))
    if not s_ or not f_:
        return 0
    s_, f_ = _dt.date.fromisoformat(s_), _dt.date.fromisoformat(f_)
    total = (f_ - s_).days + 1
    inside = (min(f_, d2) - max(s_, d1)).days + 1
    return (g.get(field) or 0) * max(0, inside) / max(1, total)


def tool_workload(a):
    s = _s()
    d1 = _date(a.get("from"), _dt.date.today())
    d2 = _date(a.get("to"), d1 + _dt.timedelta(days=13))
    period = ["AND", ["op_gantt_task.sched_start_date", "<=", d2.isoformat() + "T23:59:59"],
              ["op_gantt_task.sched_finish_date", ">=", d1.isoformat()]]
    people = []
    scope = None
    if a.get("roadmap") or a.get("query"):
        _, flts, _ = gantt_sources({"roadmap": a.get("roadmap"), "query": a.get("query")})
        scope = ["OR", *flts]
        r = s.eva("CmfTask.list", {"filter": ["AND", scope, period], "fields": ["responsible_id"], "slice": [0, 2000]}) or []
        pids = sorted({x.get("responsible_id") for x in r if x.get("responsible_id")})
        people = [find_person(pid) for pid in pids[:int(a.get("limit") or 30)]]
    for q in a.get("people") or []:
        people.append(find_person(q))
    if not people:
        raise ValueError("Give people, or roadmap/query with assigned tasks in the period")
    cal_cache = {}
    lines = [f"Загрузка {d1} — {d2} (план по Ганту: op_gantt_task, трудозатраты распределены по дням задачи)",
             "сотрудник | календарь | рабочих часов (без отпуска) | план. трудозатраты | загрузка | длительность задач | задач | отпуск"]
    for p in people:
        key = p.get("calendar_id")
        if key not in cal_cache:
            cal, chain = resolve_calendar(person=p)
            cal_cache[key] = (chain[-1], calendar_days(cal["id"], d1, d2))
        cname, days = cal_cache[key]
        cap = sum(x.get("interval_total_minutes") or 0 for x in days
                  if not in_vacation(p, _dt.date.fromisoformat(x["date"])))
        flt = ["AND", ["responsible_id", "=", p["id"]], period] + ([scope] if scope else [])
        tasks = [t for t in s.eva("CmfTask.list", {"filter": flt, "fields": ["code", "op_gantt_task.sched_start_date",
                                                                             "op_gantt_task.sched_finish_date",
                                                                             "op_gantt_task.sched_work",
                                                                             "op_gantt_task.sched_duration"],
                                                   "slice": [0, 500]}) or [] if t.get("code")]
        work = sum(_overlap_work(t.get("op_gantt_task") or {}, d1, d2) for t in tasks)
        busy = sum(_overlap_work(t.get("op_gantt_task") or {}, d1, d2, "sched_duration") for t in tasks)
        v = p.get("vacation_interval") or []
        vac = f"{v[0]} — {v[1]}" if len(v) == 2 and v[0] and (v[1] or "") >= d1.isoformat() and v[0] <= d2.isoformat() else ""
        load = f"{100 * work / cap:.0f}%" if cap else "—"
        lines.append(f"{p.get('name')} ({p.get('login')}) | {cname} | {cap / 60:g} ч | {work / 60:.1f} ч | {load} | {_days(round(busy))} | "
                     f"{len(tasks)}{': ' + ', '.join(t['code'] for t in tasks[:8]) if tasks else ''} | {vac}"
                     + (" | не работает" if p.get("does_not_work") else ""))
    lines.append("Отпуск — поле профиля «В отпуске» (vacation_interval). Экран «Рабочая нагрузка» в EVA выключен, "
                 "загрузка считается MCP по тем же данным.")
    return "\n".join(lines)


HANDLERS = {"eva_roadmaps": tool_roadmaps, "eva_gantt": tool_gantt, "eva_calendar": tool_calendar,
            "eva_workload": tool_workload}


def run(name, args):
    """Return the tool result, or None if the tool is not a planning one."""
    h = HANDLERS.get(name)
    return h(args or {}) if h else None
