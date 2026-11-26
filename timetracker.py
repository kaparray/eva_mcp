"""Time tracking in EVA: worklog (CmfTimeTrackerHistory), timer (CmfTimeTracker), timesheet (calendar).

Protocol taken from the EVA web UI (main.js, chunk of /desk/calendar) and recorded requests:
- worklog of a task: CmfTimeTrackerHistory.list {filter: [parent_id == task, start_date in range, history_type],
  order_by: [-cmf_created_at]} (dialog "Журнал работ"); time_spent / remaining_estimate are MINUTES
  (end_date = start_date + 60000 * time_spent); history_type: fact | plan | plan_oper | draft.
- estimates: CmfTask.op_gantt_task.{sched_work (original estimate), actual_work (spent), timetracker_sched_work};
  remaining = timetracker_sched_work - actual_work.
- calendar / timesheet: CmfTimeTrackerHistory.list {filter: [cmf_owner_id IN [people], history_type,
  OR(start in range, end in range, covers range)]} (recorded live on /desk/calendar).
- log time (task widget, calendar): CmfTask.timetracker_change_time(args=[task], {time_spent, remaining_estimate,
  text, date: ISO, cmf_owner}); the UI sends it when spent > 0 or remaining changed; default remaining = old - spent.
- edit an entry: model save = CmfTimeTrackerHistory.update(args=[id], {start_date, end_date, time_spent,
  remaining_estimate, text}); delete: CmfTimeTrackerHistory.delete(args=[id]).
- timer: CmfTimeTracker.get {filter: [parent == me]} -> timetracker_start(args=[tracker], {task_id}) /
  timetracker_pause(args=[tracker]) / timetracker_stop(args=[tracker]); task flag CmfTask.timetracker_is_running.
Write tools are built from the UI code but NOT verified against live EVA.
"""
import datetime as _dt
import re
import sys

# UI units (ctr-smart-whours-convert): week = 5 days, day = 8 h; a bare number is minutes
UNITS = {"н": 2400, "w": 2400, "нед": 2400, "д": 480, "d": 480, "дн": 480,
         "ч": 60, "h": 60, "час": 60, "м": 1, "m": 1, "мин": 1, "min": 1}
HIST_FIELDS = ["start_date", "end_date", "time_spent", "text", "history_type", "remaining_estimate", "parent",
               "parent.name", "parent.code", "cmf_author", "cmf_owner", "code"]
TYPE_NAMES = {"fact": "факт", "plan": "план (Гант)", "plan_oper": "план", "draft": "черновик"}


def _srv():
    m = sys.modules.get("__main__")
    if m is not None and hasattr(m, "run_tool") and hasattr(m, "eva"):
        return m  # server.py started as a script: use that module, not a second copy
    import server
    return server


def parse_duration(v):
    """'1h 30m', '2ч', '90m', '1д 2ч', '1.5h', '1:30', 90 -> minutes (int)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(round(v))
    s = str(v or "").strip().lower().replace(",", ".")
    if not s:
        raise ValueError("empty duration")
    m = re.fullmatch(r"(\d+):(\d{1,2})", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return int(round(float(s)))
    total, pos = 0.0, 0
    for m in re.finditer(r"\s*(\d+(?:\.\d+)?)\s*([a-zа-я]+)\s*", s):
        if m.start() != pos:
            break
        unit = m.group(2)
        if unit not in UNITS:
            unit = {"hours": "h", "hour": "h", "hr": "h", "mins": "m", "minutes": "m", "days": "d", "day": "d",
                    "часа": "ч", "часов": "ч", "минут": "м", "мн": "м", "дня": "д", "дней": "д"}.get(unit, unit)
        if unit not in UNITS:
            raise ValueError(f"unknown unit '{m.group(2)}' in '{v}' (use н/д/ч/м or w/d/h/m)")
        total += float(m.group(1)) * UNITS[unit]
        pos = m.end()
    if pos != len(s):
        raise ValueError(f"cannot parse duration '{v}' (examples: 1h 30m, 2ч, 90m, 1д)")
    return int(round(total))


def fmt_min(m):
    """Minutes -> '1ч 30м' (hours, as the worklog dialog shows)."""
    if m is None:
        return "-"
    m = int(m)
    sign, m = ("-" if m < 0 else ""), abs(m)
    h, mm = divmod(m, 60)
    return sign + (" ".join(x for x in (f"{h}ч" if h else "", f"{mm}м" if mm or not h else "") if x))


def _local_tz():
    return _dt.datetime.now().astimezone().tzinfo


def _date(s):
    if not s or s == "today":
        return _dt.date.today()
    if s == "yesterday":
        return _dt.date.today() - _dt.timedelta(days=1)
    return _dt.date.fromisoformat(str(s)[:10])


def period_range(period="week", date=None, date_from=None, date_to=None):
    """-> (start, end) local dates inclusive. week starts Monday like the calendar."""
    if date_from or date_to:
        a = _date(date_from or date_to)
        return a, _date(date_to) if date_to else a
    d = _date(date)
    if period == "day":
        return d, d
    if period == "month":
        nxt = (d.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        return d.replace(day=1), nxt - _dt.timedelta(days=1)
    if period == "week":
        a = d - _dt.timedelta(days=d.weekday())
        return a, a + _dt.timedelta(days=6)
    raise ValueError("period: day | week | month (or date_from / date_to)")


def _utc_iso(dt):
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def range_iso(a, b):
    tz = _local_tz()
    start = _dt.datetime.combine(a, _dt.time(0, 0), tz)
    end = _dt.datetime.combine(b, _dt.time(23, 59), tz)
    return _utc_iso(start), _utc_iso(end)


def overlap_filter(owners, history_type, s, e):
    """Exactly the calendar's filter (recorded on /desk/calendar)."""
    ht = ["history_type", "IN", ["plan", "plan_oper"]] if history_type == "plan" else ["history_type", "=", history_type]
    return [["cmf_owner_id", "IN", owners], ht,
            ["OR", [["start_date", ">=", s], ["start_date", "<=", e]],
             [["end_date", ">=", s], ["end_date", "<=", e]],
             [["start_date", "<=", s], ["end_date", ">=", e]]]]


def me():
    r = _srv().eva("CmfPerson.get", {"filter": ["id", "=", "__G_CURRENT_USER"], "fields": ["name", "login"]})
    if not r or not r.get("id"):
        raise RuntimeError("cannot resolve current user")
    return r


def resolve_person(q):
    srv = _srv()
    if not q or q in ("me", "я"):
        return me()
    if re.fullmatch(r"CmfPerson:[0-9a-f-]{36}", q):
        return {"id": q, "name": q}
    p = srv.find_people(q, 10)
    if not p:
        raise ValueError(f"person '{q}' not found")
    exact = [x for x in p if q.lower() in ((x.get("name") or "").lower(), (x.get("login") or "").lower())]
    return (exact or p)[0]


def _task(ref, fields=None):
    srv = _srv()
    tid = srv.resolve_obj("CmfTask", ref)
    t = srv.eva("CmfTask.get", {"filter": ["id", "=", tid], "fields": fields or [
        "code", "name", "timetracker_is_running", "op_gantt_task", "op_gantt_task.sched_work",
        "op_gantt_task.actual_work", "op_gantt_task.timetracker_sched_work"]}, use_cache=False)
    if not t or not t.get("id"):
        raise ValueError(f"task {ref} not found")
    return t


def _estimates(t):
    g = t.get("op_gantt_task") or {}
    if not isinstance(g, dict):
        g = {}
    sched, actual, tsw = g.get("sched_work"), g.get("actual_work"), g.get("timetracker_sched_work")
    remaining = (tsw or 0) - (actual or 0) if g else None
    return sched, actual, remaining


def _hist_id(ref):
    srv = _srv()
    if re.fullmatch(r"CmfTimeTrackerHistory:[0-9a-f-]{36}", ref or ""):
        return ref
    return srv.resolve_obj("CmfTimeTrackerHistory", ref)


def _row(h, with_task=True):
    p = h.get("parent") or {}
    who = (h.get("cmf_owner") or {}).get("name") if isinstance(h.get("cmf_owner"), dict) else None
    parts = [(h.get("start_date") or "")[:16].replace("T", " "), fmt_min(h.get("time_spent"))]
    if with_task:
        parts.append(f"{p.get('code', '?')} {p.get('name') or ''}".strip())
    parts.append(TYPE_NAMES.get(h.get("history_type"), h.get("history_type") or ""))
    if who:
        parts.append(who)
    if h.get("text"):
        parts.append("«" + h["text"].strip().replace("\n", " ")[:120] + "»")
    parts.append(h.get("code") or h.get("id"))
    return "  ".join(x for x in parts if x)


def _visible(rows):
    return [x for x in (rows or []) if x.get("id") and x.get("_acl_obj") != "deny"]


# ---------- tools ----------

TYPE_PROP = {"type": "string", "enum": ["fact", "plan", "all"], "default": "fact",
             "description": "fact = записи о работе (списания), plan = планы (Гант/календарь), all"}
UNVERIFIED = " NOT verified against live EVA (built from the web UI protocol). Use only when the user asked."

TOOLS_TIME = [
    {"name": "eva_task_worklog",
     "description": "Worklog (Журнал работ) of a task: entries with date, time, author, comment; totals and estimates "
                    "(original estimate, spent, remaining) and whether a timer is running.",
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string", "description": "Task code (ABC-123) or id"},
         "type": TYPE_PROP,
         "limit": {"type": "integer", "default": 50}}, "required": ["task"]}},
    {"name": "eva_my_time",
     "description": "Time logged by me (or another person) for a period, as the EVA calendar (/desk/calendar) shows it: "
                    "entries plus totals per day and per task. period=day|week|month around date, or date_from/date_to.",
     "inputSchema": {"type": "object", "properties": {
         "period": {"type": "string", "enum": ["day", "week", "month"], "default": "week"},
         "date": {"type": "string", "description": "YYYY-MM-DD inside the period (default today)"},
         "date_from": {"type": "string"}, "date_to": {"type": "string"},
         "person": {"type": "string", "description": "name / login / CmfPerson id; default me"},
         "type": TYPE_PROP,
         "details": {"type": "boolean", "default": True, "description": "list every entry"}}}},
    {"name": "eva_timer_status",
     "description": "My EVA timer (timetracker): running / paused / stopped, on which task, since when.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "eva_log_time",
     "description": "Log work time on a task (like the task's time widget: CmfTask.timetracker_change_time). "
                    "time: '1h 30m', '2ч', '90m', '1д' (day = 8h, week = 5d), bare number = minutes. "
                    "remaining: new remaining estimate (default: old remaining - time, not below 0)." + UNVERIFIED,
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string"},
         "time": {"type": "string"},
         "date": {"type": "string", "description": "start of work, YYYY-MM-DD or YYYY-MM-DDTHH:MM (local); default now"},
         "text": {"type": "string", "description": "description"},
         "remaining": {"type": "string"},
         "dry_run": {"type": "boolean", "default": False, "description": "only show the request"}},
         "required": ["task", "time"]}},
    {"name": "eva_update_worklog",
     "description": "Change my worklog entry (TTH-… code or id): time, date, text, remaining. "
                    "Same as editing an entry in the worklog dialog (model save)." + UNVERIFIED,
     "inputSchema": {"type": "object", "properties": {
         "entry": {"type": "string"},
         "time": {"type": "string"}, "date": {"type": "string"}, "text": {"type": "string"},
         "remaining": {"type": "string"},
         "dry_run": {"type": "boolean", "default": False}}, "required": ["entry"]}},
    {"name": "eva_delete_worklog",
     "description": "Delete my worklog entry (TTH-… code or id)." + UNVERIFIED,
     "inputSchema": {"type": "object", "properties": {
         "entry": {"type": "string"}, "dry_run": {"type": "boolean", "default": False}}, "required": ["entry"]}},
    {"name": "eva_timer",
     "description": "Start my timer on a task, pause or stop it (CmfTimeTracker.timetracker_start/pause/stop, "
                    "like the play button on a task)." + UNVERIFIED,
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["start", "pause", "stop"]},
         "task": {"type": "string", "description": "for start"},
         "dry_run": {"type": "boolean", "default": False}}, "required": ["action"]}},
]

WRITE_TIME_TOOLS = {"eva_log_time", "eva_update_worklog", "eva_delete_worklog", "eva_timer"}


def _when(s):
    """'YYYY-MM-DD[THH:MM]' local -> aware datetime; default now."""
    tz = _local_tz()
    if not s:
        return _dt.datetime.now(tz).replace(microsecond=0)
    s = str(s).replace(" ", "T")
    d = _dt.datetime.fromisoformat(s if "T" in s else s + "T09:00")
    return d if d.tzinfo else d.replace(tzinfo=tz)


def _call(method, kwargs, args, dry):
    srv = _srv()
    if dry:
        import json
        return "DRY RUN, not sent:\n" + json.dumps({"method": method, "args": args, "kwargs": kwargs},
                                                   ensure_ascii=False, indent=1)
    srv.eva(method, kwargs, args=args, use_cache=False)
    return None


def run(name, a):
    """Returns tool output, or None if name is not a time-tracking tool."""
    if name not in {t["name"] for t in TOOLS_TIME}:
        return None
    srv = _srv()
    if srv.READONLY and name in WRITE_TIME_TOOLS:
        raise srv.ReadOnlyError(f"EVA_READONLY is on: {name} is disabled")
    eva = srv.eva

    if name == "eva_task_worklog":
        t = _task(a["task"])
        flt = [["parent_id", "==", t["id"]]]
        typ = a.get("type") or "fact"
        if typ == "plan":
            flt.append(["history_type", "IN", ["plan", "plan_oper"]])
        elif typ != "all":
            flt.append(["history_type", "=", typ])
        r = _visible(eva("CmfTimeTrackerHistory.list", {"filter": flt, "fields": HIST_FIELDS,
                                                       "order_by": ["-start_date"], "slice": [0, 1000]}))
        sched, actual, remaining = _estimates(t)
        total = sum(x.get("time_spent") or 0 for x in r)
        lim = int(a.get("limit") or 50)
        head = (f"{t['code']} {t.get('name')}  {srv.task_url(t['code'])}\n"
                f"Оценка: {fmt_min(sched)}; списано: {fmt_min(actual)}; осталось: {fmt_min(remaining)}; "
                f"таймер {'запущен' if t.get('timetracker_is_running') else 'не запущен'}\n"
                f"Записей ({typ}): {len(r)}, сумма {fmt_min(total)}")
        return head + ("\n" + "\n".join(_row(x, False) for x in r[:lim]) if r else "")

    if name == "eva_my_time":
        p = resolve_person(a.get("person"))
        d1, d2 = period_range(a.get("period") or "week", a.get("date"), a.get("date_from"), a.get("date_to"))
        s, e = range_iso(d1, d2)
        typ = a.get("type") or "fact"
        types = ["fact", "plan"] if typ == "all" else [typ]
        r = []
        for ty in types:
            r += _visible(eva("CmfTimeTrackerHistory.list", {"filter": overlap_filter([p["id"]], ty, s, e),
                                                            "fields": HIST_FIELDS, "slice": [0, 2000]}))
        r.sort(key=lambda x: x.get("start_date") or "")
        by_day, by_task = {}, {}
        for x in r:
            day = (x.get("start_date") or "")[:10]
            by_day[day] = by_day.get(day, 0) + (x.get("time_spent") or 0)
            pt = x.get("parent") or {}
            k = f"{pt.get('code', '?')} {pt.get('name') or ''}".strip()
            by_task[k] = by_task.get(k, 0) + (x.get("time_spent") or 0)
        total = sum(by_day.values())
        out = [f"{p.get('name')}: {d1}…{d2} ({typ}), всего {fmt_min(total)} ({total} мин), записей {len(r)}"]
        if r:
            out.append("По дням:")
            out += [f"  {k}  {fmt_min(v)}" for k, v in sorted(by_day.items())]
            out.append("По задачам:")
            out += [f"  {fmt_min(v)}  {k}" for k, v in sorted(by_task.items(), key=lambda kv: -kv[1])]
            if a.get("details", True):
                out.append("Записи:")
                out += ["  " + _row(x) for x in r]
        return "\n".join(out)

    if name == "eva_timer_status":
        m = me()
        r = _visible(eva("CmfTimeTracker.list", {"filter": ["parent_id", "=", m["id"]], "slice": [0, 5],
                                                 "fields": ["status", "play_start_date", "task", "task.code",
                                                            "task.name"]}))
        if not r:
            return f"{m.get('name')}: таймера ещё нет (EVA создаёт его при первом запуске)"
        x = r[0]
        tk = x.get("task") or {}
        st = {"in_progress": "запущен", "pause": "на паузе", "open": "не запущен", "closed": "остановлен"}
        return (f"{m.get('name')}: таймер {st.get(x.get('status'), x.get('status'))}"
                + (f", задача {tk.get('code')} {tk.get('name')}" if tk else "")
                + (f", с {x['play_start_date'][:16].replace('T', ' ')}" if x.get("play_start_date") else "")
                + f"  ({x['id']})")

    dry = bool(a.get("dry_run"))

    if name == "eva_log_time":
        spent = parse_duration(a["time"])
        if spent <= 0:
            raise ValueError("time must be > 0")
        t = _task(a["task"])
        _, _, old_rem = _estimates(t)
        rem = parse_duration(a["remaining"]) if a.get("remaining") not in (None, "") else max(0, (old_rem or 0) - spent)
        kw = {"time_spent": spent, "remaining_estimate": rem, "text": a.get("text") or "",
              "date": _utc_iso(_when(a.get("date")))}
        res = _call("CmfTask.timetracker_change_time", kw, [t["id"]], dry)
        return res or f"Списано {fmt_min(spent)} на {t['code']} ({kw['date']}), остаток {fmt_min(rem)}"

    if name in ("eva_update_worklog", "eva_delete_worklog"):
        hid = _hist_id(a["entry"])
        h = eva("CmfTimeTrackerHistory.get", {"filter": ["id", "=", hid], "fields": HIST_FIELDS}, use_cache=False)
        if not h or h.get("_acl_obj") == "deny":
            raise ValueError(f"worklog entry {a['entry']} not found or no access")
        mine = me()["id"]
        if h.get("cmf_owner_id") != mine and (h.get("cmf_owner") or {}).get("id") != mine:
            raise PermissionError(f"{a['entry']} is not your entry")
        if name == "eva_delete_worklog":
            return _call("CmfTimeTrackerHistory.delete", {}, [hid], dry) or f"Удалено: {_row(h)}"
        kw = {}
        start = _when(a["date"]) if a.get("date") else _dt.datetime.fromisoformat(h["start_date"])
        spent = parse_duration(a["time"]) if a.get("time") not in (None, "") else int(h.get("time_spent") or 0)
        if a.get("date") or a.get("time") not in (None, ""):
            kw.update(start_date=_utc_iso(start), end_date=_utc_iso(start + _dt.timedelta(minutes=spent)),
                      time_spent=spent)
        if a.get("remaining") not in (None, ""):
            kw["remaining_estimate"] = parse_duration(a["remaining"])
        if a.get("text") is not None:
            kw["text"] = a["text"]
        if not kw:
            raise ValueError("nothing to change: time, date, text or remaining")
        return _call("CmfTimeTrackerHistory.update", kw, [hid], dry) or f"Изменено {h.get('code')}: {kw}"

    if name == "eva_timer":
        act = a["action"]
        m = me()
        r = _visible(eva("CmfTimeTracker.list", {"filter": ["parent_id", "=", m["id"]], "fields": ["status"],
                                                 "slice": [0, 1]}, use_cache=False))
        if not r:
            raise RuntimeError("no CmfTimeTracker for you yet; start the timer once from a task in EVA")
        tr = r[0]["id"]
        if act == "start":
            if not a.get("task"):
                raise ValueError("task is required for start")
            t = _task(a["task"], ["code"])
            return _call("CmfTimeTracker.timetracker_start", {"task_id": t["id"]}, [tr], dry) or f"Таймер запущен: {t['code']}"
        if act in ("pause", "stop"):
            return _call(f"CmfTimeTracker.timetracker_{act}", {}, [tr], dry) or f"Таймер: {act}"
        raise ValueError("action: start | pause | stop")
