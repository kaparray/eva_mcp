"""UBQL (the text query of the EVA filter editor) -> EVA filter array.

The web UI compiles UBQL on the client: it parses `field op value AND/OR ...`, then uses the model metadata
(field widget) to build each condition: relations resolved to ids, choices to keys, many-to-many with IN,
`~` as ILIKE. Only queries with [ ] go to the server (ubql2bql). This module does the same in Python.
"""
import re

TOKEN = re.compile(r'''\s*(?:
    (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|
    (?P<op>!=|>=|<=|!~|=|~|>|<)|
    (?P<lp>\()|(?P<rp>\))|(?P<comma>,)|
    (?P<word>[^\s()=!<>~,"']+(?:\(\))?)
)''', re.X)

RELATION_WIDGETS = ("CmfRelation", "CmfGenericRelation", "CmfPersonWidget", "CmfStatus", "CmfOrgUnitWidget")
M2M_WIDGETS = ("CmfM2M", "CmfGenericM2M", "CmfPersonMultiSelect", "CmfListsMultiSelect", "CmfTag")
CHOICE_WIDGETS = ("CmfChoice", "CmfPriority")
TEXT_WIDGETS = ("CmfStr", "CmfMarkup", "CmfText", "CmfDocEditor")
CURRENT_USER = "__G_CURRENT_USER"

# Jira-style aliases the UI accepts in UBQL (map RF in the frontend)
ALIASES = {"affectedversion": "affected_versions", "approvals": "approvers_for", "assignee": "responsible",
           "component": "components", "created": "cmf_created_at", "creator": "cmf_author", "description": "text",
           "due": "deadline", "duedate": "deadline", "environment": "text", "epic link": "parent_task",
           "fixversion": "fix_versions", "issuekey": "code", "key": "code", "issuetype": "logic_type",
           "labels": "tags", "remainingestimate": "remaining_estimate", "reporter": "cmf_owner",
           "resolved": "status_closed_at", "resolutiondate": "status_closed_at", "sprint": "lists",
           "statuscategory": "cache_status_type", "summary": "text", "timespent": "time_spent",
           "type": "logic_type", "updated": "cmf_modified_at"}


def alias(field):
    head, _, rest = field.partition(".")
    real = ALIASES.get(head.lower(), head)
    return real + ("." + rest if rest else "")


class UbqlError(ValueError):
    pass


def tokenize(q):
    out, pos = [], 0
    while pos < len(q):
        if q[pos:].strip() == "":
            break
        m = TOKEN.match(q, pos)
        if not m or m.end() == pos:
            raise UbqlError(f"Cannot parse near: {q[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "str":
            val = bytes(val[1:-1], "utf-8").decode("unicode_escape") if "\\" in val else val[1:-1]
        out.append((kind, val))
    return out


class Compiler:
    def __init__(self, cls, meta, resolve):
        """meta: {field: {widget, model|models, choices}}; resolve(model, value) -> [ids]."""
        self.cls, self.meta, self.resolve = cls, meta, resolve

    def compile(self, q):
        self.toks, self.i = tokenize(q), 0
        order = []
        # split ORDER BY
        for k in range(len(self.toks) - 1):
            if self.toks[k][1].upper() == "ORDER" and self.toks[k + 1][1].upper() == "BY":
                order_toks = self.toks[k + 2:]
                self.toks = self.toks[:k]
                order = self._order(order_toks)
                break
        flt = self._expr() if self.toks else []
        if self.i < len(self.toks):
            raise UbqlError(f"Unexpected {self.toks[self.i][1]!r}")
        return flt, order

    def _order(self, toks):
        out, cur = [], None
        for kind, v in toks:
            if kind == "comma":
                cur = None
            elif v.upper() in ("ASC", "DESC") and out:
                if v.upper() == "DESC":
                    out[-1] = "-" + out[-1].lstrip("-")
            else:
                out.append(alias(v))
        return out

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def _next(self):
        t = self._peek()
        self.i += 1
        return t

    def _expr(self):
        left = self._term()
        parts, op = [left], None
        while True:
            kind, v = self._peek()
            if kind == "word" and v.upper() in ("AND", "OR"):
                self._next()
                nop = v.upper()
                if op and nop != op:  # mixed AND/OR without parentheses: AND binds tighter
                    if nop == "AND":
                        parts[-1] = ["AND", parts[-1], self._term()]
                        continue
                    left = [op, *parts]
                    parts, op = [left], None
                op = nop
                parts.append(self._term())
            else:
                break
        return parts[0] if len(parts) == 1 else [op, *parts]

    def _term(self):
        kind, v = self._peek()
        if kind == "word" and v.upper() == "NOT":
            self._next()
            inner = self._term()
            return ["NOT", inner]
        if kind == "lp":
            self._next()
            e = self._expr()
            if self._next()[0] != "rp":
                raise UbqlError("Missing )")
            return e
        return self._clause()

    def _values(self):
        kind, v = self._peek()
        if kind == "lp":
            self._next()
            vals = []
            while True:
                k, x = self._next()
                if k == "rp":
                    break
                if k != "comma":
                    vals.append(x)
            return vals
        self._next()
        return [v]

    def _clause(self):
        kind, field = self._next()
        if kind != "word":
            raise UbqlError(f"Field expected, got {field!r}")
        field = alias(field)
        kind, op = self._next()
        neg = False
        if kind == "word" and op.upper() == "NOT":
            neg, (kind, op) = True, self._next()
        if kind == "word" and op.upper() == "IS":
            k2, w = self._next()
            if w and w.upper() == "NOT":
                neg, (k2, w) = True, self._next()
            if not w or w.upper() not in ("EMPTY", "NULL"):
                raise UbqlError("IS EMPTY / IS NOT EMPTY expected")
            return self._empty(field, neg)
        if kind == "word" and op.upper() == "IN":
            return self._cond(field, "NOT IN" if neg else "IN", self._values())
        if kind != "op":
            raise UbqlError(f"Operator expected after {field}")
        return self._cond(field, op, self._values())

    def _field(self, field):
        base = field.split(".")[0]
        return self.meta.get(base) or {}

    def _empty(self, field, neg):
        w = self._field(field).get("widget", "")
        if w in M2M_WIDGETS:
            return [field, "!=" if neg else "=", []] if not neg else [field, "!=", []]
        return [field, "!=" if neg else "=", None]

    def _ids(self, f, field, vals):
        if "." in field:
            return vals
        models = f.get("models") or ([f["model"]] if f.get("model") else [])
        out = []
        for v in vals:
            if v in ("currentUser()", "me()"):
                out.append(CURRENT_USER)
            elif re.fullmatch(r"Cmf\w+:[0-9a-f-]{36}", v):
                out.append(v)
            else:
                ids = []
                for m in models or ["CmfPerson"]:
                    ids = self.resolve(m, v)
                    if ids:
                        break
                if not ids:
                    raise UbqlError(f"{field}: '{v}' not found")
                out.extend(ids)
        return out

    def _cond(self, field, op, vals):
        f = self._field(field)
        w = f.get("widget", "")
        if w in CHOICE_WIDGETS and "." not in field:
            ch = f.get("choices") or {}
            keys = []
            for v in vals:
                k = [key for key, label in ch.items() if v in (key, label) or v.lower() == str(label).lower()]
                keys.extend(k or [v])
            vals = keys
        if w == "CmfStatus" and "." not in field and not all(re.fullmatch(r"Cmf\w+:[0-9a-f-]{36}", v) for v in vals):
            field = "status.name"  # status names repeat across workflows: match by name like the UI's status picker
        elif (w in RELATION_WIDGETS or w in M2M_WIDGETS) and "." not in field:
            vals = self._ids(f, field, vals)
        if op in ("~", "!~"):
            conds = [[field, "ILIKE" if op == "~" else "NOT ILIKE", f"%{v}%"] for v in vals]
            return conds[0] if len(conds) == 1 else ["OR", *conds]
        if w in M2M_WIDGETS and "." not in field:
            if op in ("=", "IN"):
                return [field, "IN", vals]
            if op in ("!=", "NOT IN"):
                return [field, "NOT IN", vals]
        if op in ("IN", "NOT IN"):
            return [field, op, vals]
        if len(vals) > 1:
            return [field, "IN" if op == "=" else "NOT IN", vals]
        v = vals[0]
        if v.lower() in ("true", "false") and w == "CmfBool":
            v = v.lower() == "true"
        return [field, op, v]
