#!/usr/bin/env python3
"""Extract the RPC protocol the EVA web UI uses from its frontend bundle (main.*.js).

Every model wrapper in the bundle looks like
    class X extends e.CmfTask{ foo(a){ return this.callMethod("foo", this.id.value, {k:a}) } static bar(a){ return this.objRpc.call(this.className, "bar", {k:a}) } }
callMethod/objRpc.call pack their arguments with one helper: plain values -> args, first object -> kwargs,
second object -> merged into the top level of the request, jsonrpc "2.2".

Usage: extract_protocol.py main.js > protocol.json
"""
import json
import os
import re
import sys


def split_top(s):
    """Split a JS argument list on top-level commas."""
    out, depth, cur, q = [], 0, "", None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in "\"'`":
            q = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def call_args(t, start):
    """Return the text inside the parentheses starting at t[start] == '('."""
    depth, i, q = 0, start, None
    while i < len(t):
        ch = t[i]
        if q:
            if ch == q:
                q = None
        elif ch in "\"'`":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return t[start + 1:i]
        i += 1
    return ""


def obj_keys(arg):
    if not arg.startswith("{"):
        return None
    keys = []
    for part in split_top(arg[1:-1]):
        m = re.match(r'^(?:"([^"]+)"|([A-Za-z_$][\w$]*))\s*(:|$)', part)
        if m:
            keys.append(m.group(1) or m.group(2))
        elif part.startswith("..."):
            keys.append(part)
    return keys


def describe(args):
    """Classify the packed arguments the same way the frontend helper does."""
    pos, kw, top, objs = [], None, None, 0
    for a in args:
        if a.startswith("{"):
            objs += 1
            if objs == 1:
                kw = obj_keys(a)
            elif objs == 2:
                top = obj_keys(a)
        elif objs == 0:
            pos.append("id" if re.search(r"\.id\.value$", a) else a)
        else:  # a variable after an object: we cannot know if it is an object
            pos.append(a)
    return pos, kw, top


def main(path, plugin_paths=()):
    t = open(path, encoding="utf-8", errors="replace").read()
    spec = {}
    headers = [(m.start(), m.group(1), m.group(3)) for m in
               re.finditer(r"class ([\w$]+)(?: extends ([\w$]+\.)?([\w$]+))?\{", t)]
    starts = [h[0] for h in headers]
    import bisect

    def owner(pos):
        """Model class a call at pos belongs to: wrapper `extends x.CmfName`, or a base class with className="..."."""
        i = bisect.bisect_right(starts, pos) - 1
        while i >= 0:
            hs, _, parent = headers[i]
            if parent and parent.startswith("Cmf"):
                return parent
            m = re.search(r'className\s*=\s*"(Cmf\w+)"', t[hs:hs + 6000])
            if m:
                return m.group(1) + "*"  # base class / mixin: inherited by many models
            if parent is None and t[hs:hs + 60].startswith("class Rt{"):
                return "*"
            i -= 1
            if pos - headers[i + 1][0] > 2_000_000:
                break
        return "?"

    for m in re.finditer(r'(?:callMethod|objRpc\.call)\(', t):
        inner = call_args(t, m.end() - 1)
        parts = split_top(inner)
        if t[m.start():m.start() + 10] == "objRpc.cal":
            parts = parts[1:]  # className
        if not parts or not re.fullmatch(r'"[\w]+"', parts[0]):
            continue
        name = parts[0].strip('"')
        pos, kw, top = describe(parts[1:])
        kind = "object" if pos[:1] == ["id"] else "class"
        cls = owner(m.start())
        entry = spec.setdefault(cls, {}).setdefault(name, {"kind": kind, "variants": []})
        v = {"args": pos[1:] if kind == "object" else pos, "kwargs": kw, "top": top}
        if v not in entry["variants"]:
            entry["variants"].append(v)
    spec.pop("?", None)
    # Generic ORM layer (class Le in the bundle): same for every model, options are sent as-is.
    generic = {
        "create": {"kind": "class", "variants": [{"args": [], "kwargs": ["<field>", "..."], "top": None}]},
        "get": {"kind": "class", "variants": [{"args": [], "kwargs": ["filter", "fields"], "top": None}]},
        "list": {"kind": "class", "variants": [{"args": [], "kwargs": ["filter", "fields", "order_by", "slice"], "top": None}]},
        "update": {"kind": "object", "variants": [{"args": [], "kwargs": ["<field>", "..."], "top": None}]},
        "delete": {"kind": "object", "variants": [{"args": [], "kwargs": None, "top": None}]},
        "lock": {"kind": "object", "variants": [{"args": [], "kwargs": None, "top": None}]},
        "lock_ping": {"kind": "object", "variants": [{"args": [], "kwargs": None, "top": None}]},
        "unlock": {"kind": "object", "variants": [{"args": [], "kwargs": None, "top": None}]},
    }
    for k, v in generic.items():
        spec.setdefault("*", {}).setdefault(k, v)
    for cls, methods in extract_plugins(plugin_paths).items():
        for name, entry in methods.items():
            spec.setdefault(cls, {}).setdefault(name, entry)
    json.dump(spec, sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)


# ---------- plugin scripts (/plugins/*/static/*.js) ----------
# They are plain JS and call the API with their own helper:
#   rpc('CmfTask.acme_archive_run', {task_ref, action})  -> POST /api/?m=..., jsonrpc 2.2, args [] (or explicit), kwargs
#   task.callMethod('some_plugin_method', taskId) / model.objRpc.call(model.className, 'm', {...})

def _resolve_strings(src, expr, upto):
    """String literals an expression can evaluate to: literals, ternaries, or a variable assigned literals."""
    lits = re.findall(r"""['"]([\w.]+)['"]""", expr)
    if lits:
        # in `x === 'kt' ? 'A.method_one' : 'A.method_two'` keep only method-looking strings
        named = [x for x in lits if "_" in x or "." in x]
        return named or lits
    name = expr.strip()
    if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
        defs = list(re.finditer(rf"\b{re.escape(name)}\s*=\s*(\(?[^;]*?)\s*;", src[:upto]))
        if defs:
            found = re.findall(r"""['"]([\w.]+)['"]""", defs[-1].group(1))
            return [x for x in found if "_" in x or "." in x] or found
    return []


def _resolve_kwargs(src, expr, upto):
    expr = expr.strip()
    if expr.startswith("{"):
        return obj_keys(expr)
    if re.fullmatch(r"[A-Za-z_$][\w$]*", expr):
        m = list(re.finditer(rf"\b{re.escape(expr)}\s*=\s*\{{", src[:upto]))
        if m:
            start = m[-1].end() - 1
            depth, i = 0, start
            while i < len(src):
                depth += {"{": 1, "}": -1}.get(src[i], 0)
                if depth == 0:
                    return obj_keys(src[start:i + 1])
                i += 1
        return [f"<{expr}>"]
    return None


def extract_plugins(paths):
    spec = {}

    def add(cls, name, kind, args, kw, plugin):
        entry = spec.setdefault(cls, {}).setdefault(name, {"kind": kind, "variants": [], "plugin": plugin})
        v = {"args": args, "kwargs": kw, "top": None}
        if v not in entry["variants"]:
            entry["variants"].append(v)

    for path in paths:
        src = open(path, encoding="utf-8", errors="replace").read()
        plugin = re.sub(r"_static_.*$", "", re.sub(r"^plugins_", "", os.path.basename(path)))
        for m in re.finditer(r"\b(rpc|evaRpc|callMethod|objRpc\.call)\(", src):
            before = src[max(0, m.start() - 12):m.start()]
            if re.search(r"function\s*$", before):
                continue  # helper definition
            parts = split_top(call_args(src, m.end() - 1))
            if not parts:
                continue
            fn = m.group(1)
            if fn == "objRpc.call":
                parts = parts[1:]
            names = _resolve_strings(src, parts[0], m.start())
            if fn in ("callMethod", "objRpc.call"):
                names = [n if "." in n else "CmfTask." + n for n in names]  # plugins act on the task card
            kind = "object" if fn == "callMethod" and len(parts) > 1 else "class"
            kw = _resolve_kwargs(src, parts[1], m.start()) if len(parts) > 1 and fn != "callMethod" else None
            args = []
            if fn == "rpc" and len(parts) > 2 and parts[2].strip().startswith("["):
                args = [a for a in split_top(parts[2].strip()[1:-1])]
            for full in names:
                cls, _, name = full.partition(".")
                if cls.startswith("Cmf") and name:
                    add(cls, name, kind, args, kw, plugin)
    return spec


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
