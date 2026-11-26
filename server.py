#!/usr/bin/env python3
"""Stdio MCP server for EVA (EvaTeam) CMF RPC API. Python 3.8+, no dependencies.

Covers the whole API: catalog.json holds every class and method from EVA's own
dump (GET /api/?m=), and eva_call can invoke any of them. On top of that there
are convenience tools for documents, tasks and projects.

Token lookup order:
  1. macOS Keychain item "eva-api-token"
  2. env EVA_TOKEN
  3. file ~/.config/eva-mcp/token (chmod 600)
Settings (each one: env -> ~/.config/eva-mcp/config.json -> site.json next to server.py):
  EVA_URL        / url        base URL of your EVA instance (required; install.sh asks for it)
  EVA_TIMEOUT    / timeout    request timeout, seconds (default 120: cold full-text scans take 20-45 s)
  EVA_CA_BUNDLE  / ca_bundle  extra CA chain (PEM) to verify TLS; default: system trust store
  EVA_INSECURE   / insecure   1 = do NOT verify TLS certificates (warns on stderr; avoid)
  EVA_READONLY   / readonly   1 = refuse every call that changes data
  EVA_CACHE_TTL  / cache_ttl  read cache TTL, seconds (default 300, 0 disables)
  EVA_CONFIG     path to config.json (default ~/.config/eva-mcp/config.json)
CLI:
  server.py --refresh-catalog   re-download the method catalog
  server.py --refresh-protocol  re-extract protocol.json from the EVA web UI
  server.py --check             verify token, TLS and connectivity
"""
import html
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import mimetypes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VERSION = "0.12.0"
HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(HERE, "catalog.json")
CONFIG_PATH = os.path.expanduser(os.environ.get("EVA_CONFIG", "~/.config/eva-mcp/config.json"))
SITE_PATH = os.path.join(HERE, "site.json")


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_USER_CFG, _SITE_CFG = _load_json(CONFIG_PATH), _load_json(SITE_PATH)


def setting(key, default=None):
    """env EVA_<KEY> -> ~/.config/eva-mcp/config.json -> site.json (build defaults) -> default."""
    v = os.environ.get("EVA_" + key.upper())
    if v not in (None, ""):
        return v
    for cfg, base_dir in ((_USER_CFG, os.path.dirname(CONFIG_PATH)), (_SITE_CFG, HERE)):
        if cfg.get(key) not in (None, ""):
            v = cfg[key]
            if key == "ca_bundle" and isinstance(v, str):  # relative paths are relative to the file that names them
                v = os.path.join(base_dir, os.path.expanduser(v))
            return v
    return default


def _flag(key):
    return str(setting(key, "")).lower() in ("1", "true", "yes")


BASE = str(setting("url", "")).rstrip("/")
TIMEOUT = float(setting("timeout", 120))
CACHE_TTL = float(setting("cache_ttl", 300))
TOKEN_FILE = os.path.expanduser("~/.config/eva-mcp/token")
READONLY = _flag("readonly")
AUDIT_PATH = os.path.expanduser(os.environ.get("EVA_AUDIT_LOG", "~/.local/state/eva-mcp/audit.jsonl"))

CA_DEFAULT = os.path.expanduser("~/.config/eva-mcp/ca.pem")  # legacy location written by older install.sh
CA_BUNDLE = os.path.expanduser(str(setting("ca_bundle", "") or (CA_DEFAULT if os.path.isfile(CA_DEFAULT) else "")))
INSECURE = _flag("insecure")
if INSECURE:
    _CTX = ssl.create_default_context()
    _CTX.check_hostname = False
    _CTX.verify_mode = ssl.CERT_NONE
    print("eva-mcp: WARNING: EVA_INSECURE=1, TLS certificates are NOT verified", file=sys.stderr)
elif CA_BUNDLE:
    _CTX = ssl.create_default_context(cafile=CA_BUNDLE)
else:
    _CTX = ssl.create_default_context()  # system trust store


def base_url():
    if not BASE:
        raise RuntimeError("EVA URL is not configured: set EVA_URL (e.g. https://eva.example.com) "
                           "or run install.sh, which saves it to ~/.config/eva-mcp/config.json")
    return BASE


READ_PREFIXES = ("get", "list", "count", "search", "fetch", "sget", "slist", "public_get", "public_list",
                 "sdesk_get", "sdesk_list", "field_options_list", "attach_list", "all_relation_persons",
                 "html_diff", "macros_page_tree_get", "ui_get", "tmplt_get")
READ_SUFFIXES = ("_list", "_available", "_get", "_meta", "_count")


# ---------- auth ----------

_token_cache = None


def token():
    global _token_cache
    if _token_cache:
        return _token_cache
    t = ""
    if sys.platform == "darwin":
        try:
            t = subprocess.run(
                ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", "eva-api-token", "-w"],
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            t = ""
    t = t or os.environ.get("EVA_TOKEN", "").strip()
    if not t and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            t = f.read().strip()
    if not t:
        raise RuntimeError("EVA token not found. macOS: security add-generic-password -a \"$USER\" -s eva-api-token -w ; "
                           f"other OS: put it into {TOKEN_FILE} (chmod 600) or set EVA_TOKEN")
    _token_cache = t
    return t


# ---------- transport ----------

def _request(path, body=None, timeout=None):
    req = urllib.request.Request(
        f"{base_url()}{path}", data=body, method="POST" if body is not None else "GET",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT, context=_CTX) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(f"EVA rejected the token (HTTP {e.code}). Re-issue it and update the Keychain/EVA_TOKEN.")
        raise


def upload_file(obj_id, filename, data, content_type="application/octet-stream", embedded=False):
    """Attach a file to obj_id the way the web UI does: CmfAttachment.create, then POST the bytes
    to /files/obj/... Returns (url_path, attachment_id). embedded=True for images shown inline in the text."""
    att_id = eva("CmfAttachment.create", {"parent": obj_id, "name": filename, "size": len(data),
                                          "embedded": bool(embedded), "perm_encrypt": False, "category": None})
    cls, _, uid = obj_id.partition(":")
    q = urllib.parse.quote
    path = f"/files/obj/{cls}/{q(cls + ':' + uid[:3], safe='')}/{q(obj_id, safe='')}/{q(filename)}"
    boundary = f"----evamcp{time.time_ns()}"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{base_url()}{path}", data=body, method="POST", headers={
        "Authorization": f"Bearer {token()}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
        res = json.loads(r.read().decode("utf-8", "replace"))
    audit({"method": "POST " + path, "file": filename, "size": len(data), "ok": res.get("result") == "OK"})
    if res.get("result") != "OK":
        raise RuntimeError(f"Upload failed: {res}")
    return res["attach_url"], att_id if isinstance(att_id, str) else None


def download_file(path, dest, wait=0):
    """GET a file EVA serves (/files/obj/... from attachments and exports) and save it. Returns the local path.
    wait: seconds to keep polling while the file does not exist yet (background exports), like the UI's
    periodicallyCheckedDownload which checks once a second."""
    url = path if path.startswith("http") else f"{base_url()}/{path.lstrip('/')}"
    deadline = time.time() + wait
    while True:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
                data = r.read()
                ctype = r.headers.get("Content-Type", "")
            break
        except urllib.error.HTTPError as e:
            if e.code == 404 and time.time() < deadline:
                time.sleep(2)
                continue
            raise
    if ctype.startswith("text/html") and data[:15].lower().startswith(b"<!doctype"):
        raise RuntimeError(f"EVA returned an HTML page instead of a file for {path}")
    dest = os.path.expanduser(dest)
    if os.path.isdir(dest) or dest.endswith("/"):
        os.makedirs(dest, exist_ok=True)
        name = urllib.parse.unquote(url.split("?")[0].rsplit("/", 1)[-1])
        dest = os.path.join(dest, re.sub(r'[:\\/*?"<>|]', "_", name))
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return dest, len(data)


DOWNLOAD_DIR = os.path.expanduser(os.environ.get("EVA_DOWNLOAD_DIR", "~/Downloads/eva"))

ENDPOINTS = {"api": "/api/", "pub": "/pub/pub_api", "servicedesk": "/servicedesk/sd_api"}

_cache = {}


class EvaAbort(RuntimeError):
    """EVA answered {"abort": "..."}: the UI shows it as an error or starts a dialog (must_transition_run)."""


def eva(method, kwargs=None, args=None, use_cache=True, top=None, endpoint="api"):
    """top: extra top-level payload keys (the UI sends e.g. filter for delete_node at top level)."""
    kwargs = dict(kwargs or {})
    # EVA ignores 'limit'; 'slice' [start, end] is what actually limits rows
    if "limit" in kwargs:
        lim = kwargs.pop("limit")
        if lim and "slice" not in kwargs:
            kwargs["slice"] = [0, int(lim)]
    # The UI helper sends jsonrpc 2.2 whenever it packs args (even an empty list); plain ORM calls use 2.0
    payload = {"jsonrpc": "2.2" if args is not None else "2.0", "method": method, "callid": f"mcp-{time.time_ns()}", "kwargs": kwargs}
    if args is not None:
        payload["args"] = args
    if top:
        payload.update(top)
    key = None
    m = method.partition(".")[2]
    write = is_write(m)
    if write and READONLY:
        raise ReadOnlyError(f"EVA_READONLY is on: {method} would change data in EVA")
    if use_cache and CACHE_TTL > 0 and is_read(m):
        key = json.dumps([endpoint, method, kwargs, args], sort_keys=True, ensure_ascii=False)
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    try:
        data = json.loads(_request(f"{ENDPOINTS[endpoint]}?m={method}", json.dumps(payload).encode()))
    except Exception as e:
        if write:
            audit({"method": method, "args": _short(args), "kwargs": _short(kwargs), "top": _short(top),
                   "ok": False, "error": str(e)[:300]})
        raise
    if write:
        res_ = data.get("result")
        audit({"method": method, "args": _short(args), "kwargs": _short(kwargs), "top": _short(top),
               "ok": not data.get("abort") and "error" not in data,
               "error": _short(str(data.get("abort") or (data.get("error") or {}).get("message") or ""), 300) or None,
               "result": res_ if isinstance(res_, (str, bool, int)) or res_ is None else (res_ or {}).get("id")})
    if data.get("abort"):  # the server refuses with HTTP 200 + abort (permissions, must_transition_run ...)
        raise EvaAbort(data["abort"])
    if "error" in data:
        err = data["error"]
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        if "Traceback" in msg:  # keep the meaningful tail of server tracebacks
            msg = msg.split("\n")[0] + "\n..." + msg.strip().splitlines()[-1]
        raise RuntimeError(msg)
    res = data.get("result")
    if not is_read(m):
        _cache.clear()  # writes invalidate cached reads
    if key:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (time.time() + CACHE_TTL, res)
    return res


# ---------- catalog ----------

def parse_dump(text):
    text = html.unescape(text)
    out, cls = {}, None
    for line in text.splitlines():
        m = re.match(r"^[ {]'(\w+)': \{'_class'", line)
        if m:
            cls = m.group(1)
            out.setdefault(cls, set())
            continue
        m = re.search(r"'name': '(\w+)'", line)
        if m and cls and m.group(1) != cls:
            out[cls].add(m.group(1))
    return {k: sorted(v) for k, v in out.items() if v}


def refresh_catalog():
    cat = parse_dump(_request("/api/?m=", timeout=300))
    if len(cat) < 50:
        raise RuntimeError(f"Catalog looks broken ({len(cat)} classes), keeping the old one")
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(cat, f, ensure_ascii=False, separators=(",", ":"))
    return cat


try:
    with open(CATALOG_PATH, encoding="utf-8") as f:
        CATALOG = json.load(f)
except FileNotFoundError:
    CATALOG = {}


PROTOCOL_PATH = os.path.join(HERE, "protocol.json")
try:
    with open(PROTOCOL_PATH, encoding="utf-8") as f:
        PROTOCOL = json.load(f)  # how the web UI calls each method: tools/extract_protocol.py
except FileNotFoundError:
    PROTOCOL = {}


def proto(cls, m):
    """UI calling convention for cls.m: own wrapper first, then base classes / generic ORM ('*')."""
    if m in PROTOCOL.get(cls, {}):
        return PROTOCOL[cls][m], cls
    for base, methods in PROTOCOL.items():
        if base.endswith("*") and m in methods:
            return methods[m], base
    return None, None


def proto_sig(entry):
    out = []
    for v in entry["variants"]:
        parts = (["id"] if entry["kind"] == "object" else []) + [f"<{x}>" for x in v["args"]]
        if v["kwargs"] is not None:
            parts.append("{" + ", ".join(v["kwargs"]) + "}")
        if v["top"]:
            parts.append("top{" + ", ".join(v["top"]) + "}")
        out.append("(" + ", ".join(parts) + ")")
    return " | ".join(out)


def resolve_obj(cls, ref):
    """Accept an object id (Class:uuid) or a code (DOC-123, ABC-45) and return the id."""
    if re.fullmatch(r"Cmf\w+:[0-9a-f-]{36}", ref or ""):
        return ref
    r = eva(f"{cls}.get", {"filter": ["code", "=", ref], "fields": ["code"]}, use_cache=False)
    if not r or not r.get("id"):
        raise ValueError(f"{cls} with code {ref} not found (or no access)")
    return r["id"]


# Not read-only by name, but they change nothing others can see (search, conversions, permission checks,
# exports into your own file area). Everything else that is not is_read() counts as a write.
SAFE_NON_READ = {"fulltext_search", "ubql2bql", "ux_check_perm", "get_in_approve", "all_relation_persons",
                 "export_to_pdf", "export_to_docx", "export2file", "html_diff", "enabled_plugin_nclist",
                 "ncget_filtered_roadmap_by_level"}  # roadmap/Gantt tree rows by level: a read with an unusual name


class ReadOnlyError(PermissionError):
    pass


def is_write(method_name):
    return not is_read(method_name) and method_name not in SAFE_NON_READ


def _short(v, n=200):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"…(+{len(v) - n})"
    if isinstance(v, (list, tuple)):
        return [_short(x, n) for x in v[:20]]
    if isinstance(v, dict):
        return {k: _short(x, n) for k, x in list(v.items())[:30]}
    return v


def audit(entry):
    """Append one JSON line per change MCP makes in EVA (EVA_AUDIT_LOG=0 disables). The token is never logged."""
    if AUDIT_PATH in ("0", "", os.devnull):
        return
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "base": BASE, **entry}
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def is_read(method_name):
    return method_name.startswith(READ_PREFIXES) or method_name.endswith(READ_SUFFIXES)


# ---------- helpers ----------

def html_to_md(s):
    """Rough HTML -> Markdown: keeps headings, lists, code blocks, links, tables as rows."""
    s = s or ""
    s = re.sub(r"<svg.*?</svg>", "", s, flags=re.S)
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", s, flags=re.S)
    s = re.sub(r'<span[^>]*aria-label="Макрос"[^>]*>.*?</span>', "", s, flags=re.S)
    # code blocks
    s = re.sub(r"<pre[^>]*>(.*?)</pre>",
               lambda m: "\n```\n" + re.sub(r"<[^>]+>", "", m.group(1)) + "\n```\n", s, flags=re.S)
    s = re.sub(r"<code[^>]*>(.*?)</code>", lambda m: "`" + re.sub(r"<[^>]+>", "", m.group(1)) + "`", s, flags=re.S)
    for i in range(1, 7):
        s = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>", lambda m, i=i: "\n" + "#" * i + " " + m.group(1) + "\n", s, flags=re.S)
    s = re.sub(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"[\2](\1)", s, flags=re.S)
    s = re.sub(r"<li[^>]*>", "\n- ", s)
    s = re.sub(r"<(strong|b)[^>]*>(.*?)</\1>", r"**\2**", s, flags=re.S)
    s = re.sub(r"</t[dh]>", " | ", s)
    s = re.sub(r"<(br|/p|/div|/tr|/ul|/ol|/table)[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = [l.rstrip() for l in s.splitlines()]
    out, blank = [], False
    for l in lines:
        if not l.strip():
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(l)
            blank = False
    return "\n".join(out).strip()


def mention_html(query):
    """Same anchor the editor inserts for an @-mention."""
    p = find_people(query, 1)
    if not p:
        return "@" + html.escape(query)
    p = p[0]
    login = p.get("login") or p.get("code") or ""
    return (f'<a class="external" data-macros="mentions" data-mention-type="person" data-object-id="{p["id"]}" '
            f'data-title="{html.escape(login)}" rel="noopener" href="/struct/Person/{urllib.parse.quote(login, safe="@")}">'
            f'@{html.escape(p.get("name") or login)}</a>')


def find_people(query, limit=10):
    q = query.strip().lstrip("@")
    flt = ["OR", ["name", "ILIKE", f"%{q}%"], ["login", "ILIKE", f"%{q}%"], ["email", "ILIKE", f"%{q}%"]]
    r = eva("CmfPerson.list", {"filter": flt, "fields": ["name", "login", "email", "code", "does_not_work"],
                               "order_by": ["name"], "slice": [0, limit * 3]}) or []
    return [x for x in r if x.get("id") and x.get("_acl_obj") != "deny" and not x.get("does_not_work")][:limit]


def _inline_md(t, img_cb):
    mentions = []

    def keep(m):
        mentions.append(mention_html(m.group(1)))
        return f"\x00M{len(mentions) - 1}\x00"
    t = re.sub(r"@\[([^\]]+)\]", keep, t)
    t = html.escape(t, quote=False)
    t = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", t)
    t = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", lambda m: img_cb(html.unescape(m.group(2)), m.group(1)), t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", t)
    return re.sub(r"\x00M(\d+)\x00", lambda m: mentions[int(m.group(1))], t)


TABLE_HTML_NOTE = ("format=html: EVA stores table settings as classes on the table wrapper "
                   "<div class=\"tableView-content-wrap ...\" data-macros=\"wikitables\">: x-fixed = pin top row "
                   "(Закрепить верхнюю строчку), y-fixed = pin first column, y-numbered = number rows. Do not copy them "
                   "from other pages: set them on a new table only if the user asked, and keep them as they are on "
                   "existing tables when editing. Markdown tables are created without them.")


def md_table(rows_html, rows_text=None):
    """A table in the form the EVA editor stores after its first render (wikitables macro wrapper, data-x/data-y,
    fixed pixel widths in <colgroup>). Stored like this, the editor neither measures nor re-wraps the table,
    so render_html() gives the same text_render/text_ver as the browser and no re-save happens on first open."""
    rows_text = rows_text or rows_html
    n = max(len(r) for r in rows_html)
    rows_html = [r + [""] * (n - len(r)) for r in rows_html]
    widths = []
    for x in range(n):
        longest = max((len(html.unescape(r[x])) for r in rows_text if x < len(r)), default=0)
        widths.append(max(80, min(400, longest * 8 + 24)))

    def row(y, tag):
        return "<tr>" + "".join(f'<{tag} data-x="{x}" data-y="{y}">{c}</{tag}>' for x, c in enumerate(rows_html[y])) + "</tr>"
    body = "".join(row(y, "td") for y in range(1, len(rows_html)))
    cols = "".join(f'<col style="width: {w}px;">' for w in widths)
    return ('<div class="tableView-content-wrap" data-macros="wikitables">'
            '<div class="table-wrapper cmf-scrollbar" data-table-wrapper="true">'
            f'<table border="1" style="border-collapse: collapse; width: {sum(widths)}px;">'
            f"<thead>{row(0, 'th')}</thead><tbody>{body}</tbody><colgroup>{cols}</colgroup></table></div></div>")


def md_to_html(md, img_cb=None):
    """Small Markdown -> HTML: headings, paragraphs, lists, ``` code, > quotes, tables, **bold**, `code`, links, images."""
    img_cb = img_cb or (lambda src, alt: f'<img src="{html.escape(src)}" alt="{html.escape(alt)}">')
    out, lines, i = [], md.replace("\r\n", "\n").split("\n"), 0
    para = []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline_md(l, img_cb) for l in para) + "</p>")
            para.clear()

    while i < len(lines):
        l = lines[i]
        if l.strip().startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre>" + html.escape("\n".join(code), quote=False) + "</pre>")
        elif re.match(r"^#{1,6} ", l):
            flush()
            n = len(l) - len(l.lstrip("#"))
            out.append(f"<h{n}>{_inline_md(l[n:].strip(), img_cb)}</h{n}>")
        elif re.match(r"^\s*([-*+]|\d+[.)]) ", l):
            flush()
            ordered = bool(re.match(r"^\s*\d", l))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+[.)]) ", lines[i]):
                items.append("<li>" + _inline_md(re.sub(r"^\s*([-*+]|\d+[.)]) ", "", lines[i]), img_cb) + "</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        elif l.startswith(">"):
            flush()
            q = []
            while i < len(lines) and lines[i].startswith(">"):
                q.append(_inline_md(lines[i].lstrip("> ").rstrip(), img_cb))
                i += 1
            out.append("<blockquote><p>" + "<br>".join(q) + "</p></blockquote>")
            continue
        elif l.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-+", lines[i + 1]):
            flush()
            cells = lambda s: [c.strip() for c in s.strip().strip("|").split("|")]
            grid = [cells(l)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                grid.append(cells(lines[i]))
                i += 1
            out.append(md_table([[_inline_md(c, img_cb) for c in r] for r in grid],
                                [[re.sub(r"<[^>]+>", "", _inline_md(c, img_cb)) for c in r] for r in grid]))
            continue
        elif re.match(r"^\s*(---|\*\*\*)\s*$", l):
            flush()
            out.append("<hr>")
        elif not l.strip():
            flush()
        else:
            para.append(l)
        i += 1
    flush()
    return "\n".join(out)


def _iso(dt):
    import datetime as _dt
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def my_tasks_filter(view, when="today"):
    """Filters of the 'Мои задачи' tabs, as the UI builds them (__G_CURRENT_USER is resolved by the server)."""
    import datetime as _dt
    now = _dt.datetime.now().astimezone()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = now.replace(hour=23, minute=59, second=59, microsecond=59000)
    D = _dt.timedelta(days=1)
    me = "__G_CURRENT_USER"
    if view == "doing":
        base = ["OR", [["logic_prefix", "LIKE", "task%"], ["cache_status_type", "!=", "IN_REVIEW"],
                       ["cache_status_type", "!=", "CLOSED"],
                       ["OR", ["approve_for", "!=", me], ["approve_for", "==", None]],
                       ["OR", ["waiting_for", "==", me], ["executors", "IN", [me]]]],
                [["approve_for", "==", me], ["cache_status_type", "==", "IN_REVIEW"],
                 ["approve_for_place", "==", "approve1_review"], ["status_review_at", "<=", _iso(day0 - 2 * D)]]]
        if when == "tomorrow":
            return [base, ["alarm_date", ">=", _iso(day0 + D)], ["alarm_date", "<", _iso(day0 + 2 * D)]]
        if when == "later":
            return [base, ["alarm_date", ">=", _iso(day0 + 2 * D)]]
        if when == "all":
            return base
        return [base, ["OR", ["alarm_date", "<", _iso(day_end)], ["alarm_date", "=", None]]]
    if view == "approving":
        return ["OR", [["cache_status_type", "!=", "CLOSED"], ["approve_for", "==", me],
                       ["OR", ["approve_for_place", "IN", ["approve2", "approve1_review"]],
                        [["approve_for_place", "=", "approve3_notassigned"],
                         ["OR", [["parent_id", "NOT ILIKE", "CmfProject:%"], ["alarm_date", "==", None]],
                          ["alarm_date", "<", _iso(day_end)]]]],
                       ["logic_prefix", "LIKE", "task%"]],
                ["AND", ["approvers_for", "IN", [me]], ["cache_status_type", "!=", "CLOSED"]]]
    if view == "assigned":
        return [["cache_status_type", "!=", "CLOSED"],
                ["OR", [["parent_id", "==", None], ["cmf_owner", "==", me], ["responsible_id", "!=", None],
                        ["responsible", "!=", me], ["alarm_date", "!=", None]],
                 [["parent_id", "!=", None], ["cmf_owner", "==", me], ["responsible", "!=", me]],
                 [["spectators", "IN", [me]]]]]
    raise ValueError("view: doing | approving | assigned")


_META = {}


def class_meta(cls):
    if cls not in _META:
        _META[cls] = (eva(f"{cls}.get_meta") or {}).get("fields") or {}
    return _META[cls]


def resolve_values(model, value):
    """Relation value as the UI pickers find it: by code, login or exact name."""
    conds = [["code", "=", value], ["name", "=", value]]
    if model == "CmfPerson":
        conds += [["login", "=", value], ["email", "=", value]]
    r = eva(f"{model}.list", {"filter": ["OR", *conds], "fields": ["code"], "slice": [0, 20]}) or []
    return [x["id"] for x in r if x.get("id") and x.get("_acl_obj") != "deny"]


def to_filter(query, cls="CmfTask", with_order=False):
    """UBQL text -> filter like the UI filter editor (client-side compile with model metadata);
    queries with [ ] go to the server (ubql2bql) as the UI does; a list is used as a filter as-is."""
    import ubql
    if isinstance(query, list):
        return (query, []) if with_order else query
    q = (query or "").strip()
    if q.startswith("[") and q.endswith("]"):
        flt, order = json.loads(q), []
    elif "[" in q or "]" in q:
        res = eva(f"{cls}.ubql2bql", {}, args=[q], use_cache=False)
        flt, order = (json.loads(res) if isinstance(res, str) else res), []
    else:
        flt, order = ubql.Compiler(cls, class_meta(cls), resolve_values).compile(q)
    return (flt, order) if with_order else flt


TASK_ROW_FIELDS = ["code", "name", "cache_status_type", "status", "responsible", "deadline", "alarm_date", "project"]


def task_rows(r, limit):
    out = []
    for t in (r or [])[:limit * 3]:
        if not t.get("code") or t.get("_acl_obj") == "deny":
            continue
        name_of = lambda v: (v or {}).get("name") if isinstance(v, dict) else None
        extra = "  ".join(x for x in (name_of(t.get("status")), name_of(t.get("responsible")) and "→ " + name_of(t.get("responsible")),
                                      (t.get("deadline") or "")[:10] and "срок " + (t.get("deadline") or "")[:10]) if x)
        out.append(f"{t['code']}  {t.get('name')}  [{extra or t.get('cache_status_type')}]  {task_url(t['code'])}")
        if len(out) >= limit:
            break
    return "\n".join(out) or "Nothing found"


def find_document(code):
    r = eva("CmfDocument.get", {"filter": ["code", "=", code], "fields": ["code", "name", "text"]}, use_cache=False)
    if not r or not r.get("id"):
        raise ValueError(f"Document {code} not found (or no access)")
    return r


def render_content(obj_id, content, fmt, base_dir=None):
    """Turn Markdown/HTML into EVA HTML; local images (![](path) or <img src="path">) are uploaded as attachments."""
    uploaded = {}

    def local_src(src):
        if re.match(r"^(https?:|data:|/files/)", src):
            return None
        p = os.path.expanduser(src)
        if not os.path.isabs(p) and base_dir:
            p = os.path.join(base_dir, p)
        return p if os.path.isfile(p) else None

    def upload(p):
        """Same as the web editor: embedded attachment, then <img> with data-attach-id and ?version=1."""
        if p not in uploaded:
            with open(p, "rb") as f:
                url, att = upload_file(obj_id, f"{int(time.time())}-{os.path.basename(p)}", f.read(),
                                       mimetypes.guess_type(p)[0] or "application/octet-stream", embedded=True)
            uploaded[p] = (url + "?version=1", att)
        return uploaded[p]

    def img_tag(url, att, alt):
        attrs = f' data-attach-id="{att}" data-attach-version="1"' if att else ""
        return f'<img src="{html.escape(url)}" alt="{html.escape(alt)}"{attrs}>'

    def img_cb(src, alt):
        p = local_src(src)
        return img_tag(*upload(p), alt) if p else img_tag(src, None, alt)

    if fmt == "html":
        def repl(m):
            src = re.search(r'\ssrc="([^"]+)"', m.group(0)).group(1)
            p = local_src(html.unescape(src))
            if not p:
                return m.group(0)
            alt = re.search(r'\salt="([^"]*)"', m.group(0))
            return img_tag(*upload(p), html.unescape(alt.group(1)) if alt else "")
        out = re.sub(r'<img\b[^>]*\ssrc="[^"]+"[^>]*>', repl, content)
    else:
        out = md_to_html(content, img_cb)
    return add_data_ids(out), [u for u, _ in uploaded.values()]


_DATA_ID_ABC = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_BLOCK_TAGS = r"p|h[1-6]|li|ul|ol|pre|blockquote|table|tr|td|th|img|hr|div"


def add_data_ids(h):
    """The web editor gives every block (and image) a 14-char data-id; inline comments anchor to them."""
    import random

    def repl(m):
        if "data-id=" in m.group(0):
            return m.group(0)
        did = "".join(random.choice(_DATA_ID_ABC) for _ in range(14))
        return f'<{m.group(1)} data-id="{did}"{m.group(2)}'
    return re.sub(rf"<({_BLOCK_TAGS})\b((?:\s[^>]*)?/?>)", repl, h)


_INDENT = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "div", "title", "style", "pre", "script", "td", "th",
           "ul", "ol", "li", "dl", "dt", "dd", "area", "table", "thead", "tfoot", "tbody", "tr", "section", "summary",
           "article", "hgroup", "aside", "figure", "figcaption", "option", "optgroup", "datalist"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def _attr_esc(v):
    return (v or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def render_html(text):
    """What the editor sends to save_render (getValue(true) -> Qv): TinyMCE serialization of the stored text.

    - editor-only attributes (data-id, data-mce-*) are dropped, the outer <div> is unwrapped;
    - block tags get TinyMCE's line breaks (indent_before/indent_after), void tags are written as <br>;
    - attributes come out in reverse of the stored (alphabetical) order, e.g. <img src data-attach-version data-attach-id alt>.
    Tables must already be stored in the editor's wikitables form (see md_table), otherwise the editor measures them."""
    from html.parser import HTMLParser
    t = (text or "").strip()
    m = re.fullmatch(r"<div>(.*)</div>", t, flags=re.S)
    t = m.group(1) if m else t
    out = []

    def nl():
        if out and out[-1] and not out[-1].endswith("\n"):
            out.append("\n")

    class P(HTMLParser):
        pre = 0

        def handle_starttag(self, tag, attrs, closing=False):
            self.pre += tag == "pre"
            attrs = [(k, v) for k, v in attrs if k != "data-id" and not k.startswith("data-mce-")]
            attrs.reverse()  # the editor re-creates attributes in reverse of the stored (alphabetical) order
            if tag in _INDENT:
                nl()
            out.append("<" + tag + "".join(f' {k}="{_attr_esc(v)}"' for k, v in attrs) + ">")
            if tag in _VOID and tag in _INDENT:
                nl()

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if tag not in _VOID:
                self.handle_endtag(tag)

        def handle_endtag(self, tag):
            if tag in _VOID:
                return
            self.pre -= tag == "pre" and self.pre > 0
            out.append(f"</{tag}>")
            if tag in _INDENT:
                nl()

        def handle_data(self, d):
            if not d.strip() and "\n" in d and not self.pre:
                return  # whitespace between blocks: TinyMCE drops it
            out.append(d)

        def handle_entityref(self, name):
            out.append(f"&{name};")

        def handle_charref(self, name):
            out.append(f"&#{name};")

        def handle_comment(self, d):
            out.append(f"<!--{d}-->")

    p = P(convert_charrefs=False)
    p.feed(t)
    p.close()
    return "".join(out).strip()


def text_ver(render):
    """Frontend hash: SHA-256 of the rendered HTML, first 6 bytes as uint16*2^32 + uint32 (decimal string)."""
    import hashlib
    d = hashlib.sha256(render.encode("utf-8")).digest()
    return str(int.from_bytes(d[:2], "big") * 2 ** 32 + int.from_bytes(d[2:6], "big"))


def save_document(obj_id, new_html, publish=True, notify=False):
    """Replicates the editor: ux_check_perm -> lock -> update(text_draft) -> unlock -> do_publish -> save_render."""
    if not eva("CmfDocument.ux_check_perm", {"obj": obj_id, "field_name": "text", "perm": "update"}, use_cache=False):
        raise PermissionError("No permission to edit this page")
    if not eva("CmfDocument.lock", {}, args=[obj_id]):
        raise RuntimeError("Page is being edited by someone else (lock failed); try later")
    try:
        eva("CmfDocument.update", {"text_draft": new_html}, args=[obj_id])
    finally:
        eva("CmfDocument.unlock", {}, args=[obj_id])
    if publish:
        publish_document(obj_id, notify)


def publish_document(obj_id, notify=False):
    """'Выпустить' in the UI: do_publish, then save_render of the published text."""
    if True:  # keep block structure of the original code
        eva("CmfDocument.do_publish", {"notify_persons": bool(notify)}, args=[obj_id])
        stored = eva("CmfDocument.get", {"filter": ["id", "=", obj_id], "fields": ["text", "text_render_ver"]},
                     use_cache=False) or {}
        rendered = render_html(stored.get("text"))
        ver = text_ver(rendered)
        if ver != str(stored.get("text_render_ver")):
            eva("CmfDocument.save_render", {"text_render": rendered, "text_ver": ver}, args=[obj_id])


def delete_document(obj_id):
    """Same call as the UI 'Удалить': moves the page (with its subtree) to the trash."""
    return eva("CmfDocument.delete_node", {}, top={"filter": ["id", "==", obj_id], "fields": []})


def doc_url(code):
    return f"{BASE}/project/Document/{code}"


def task_url(code):
    return f"{BASE}/project/Task/{code}"


def dump(obj, limit=200000):
    s = json.dumps(obj, ensure_ascii=False, indent=1)
    return s if len(s) <= limit else s[:limit] + f"\n... truncated ({len(s)} chars)"


def resolve_project(p):
    """Accept project id (CmfProject:...), code (my-project) or name fragment."""
    if not p:
        return None
    if p.startswith("CmfProject:"):
        return p
    r = eva("CmfProject.list", {"filter": ["OR", ["code", "=", p], ["name", "ILIKE", f"%{p}%"]],
                                "fields": ["code", "name"], "slice": [0, 5]})
    r = [x for x in (r or []) if x.get("id")]
    if not r:
        raise ValueError(f"Project '{p}' not found (use eva_list_projects)")
    exact = [x for x in r if x.get("code") == p or (x.get("name") or "").lower() == p.lower()]
    return (exact or r)[0]["id"]


def rows(items, fmt, limit=None):
    items = [x for x in (items or []) if x.get("code")]  # ACL-hidden rows come without code
    items = items[:limit] if limit else items
    return "\n".join(fmt(x) for x in items) if items else "Nothing found"


# ---------- tools ----------

FILTER_HELP = ("Filter: ['field','OP',value]; OP uppercase: = != > < >= <= IN, NOT IN, LIKE, ILIKE, NOT ILIKE, "
               "EXISTS, MEMBER_OF, @@; combine: ['AND',[...],[...]] / ['OR',...]. Limit rows with 'limit':N "
               "(converted to 'slice':[0,N]; EVA ignores plain limit). Order: 'order_by':['-cmf_modified_at'].")

PROJECT_PROP = {"type": "string", "description": "Project code (my-project), id (CmfProject:...) or name fragment"}

TOOLS = [
    {"name": "eva_search_documents",
     "description": "Search EVA documents (wiki). in='title' (fast, default) or 'text' (full text, 20-45 s; "
                    "narrow it with project). Returns code, title, link.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "in": {"type": "string", "enum": ["title", "text", "both"], "default": "title"},
         "project": PROJECT_PROP,
         "limit": {"type": "integer", "default": 20}},
         "required": ["query"]}},
    {"name": "eva_get_document",
     "description": "Read an EVA document by code (DOC-123) as Markdown, with link, project and parent.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "raw_html": {"type": "boolean"}}, "required": ["code"]}},
    {"name": "eva_create_document",
     "description": "Create an EVA page (document) under a parent page (DOC-...) or at a project root, fill it and publish. "
                    "content is Markdown by default (headings, lists, ``` code, tables, **bold**, links); "
                    "local images ![alt](/path/to/file.png) are uploaded as attachments. Use only when the user asked to create a page.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"},
         "parent": {"type": "string", "description": "Parent document code DOC-..."},
         "project": PROJECT_PROP,
         "content": {"type": "string"},
         "format": {"type": "string", "enum": ["markdown", "html"], "default": "markdown",
                    "description": TABLE_HTML_NOTE},
         "publish": {"type": "boolean", "default": True, "description": "false = keep as draft"},
         "notify": {"type": "boolean", "default": False, "description": "notify watchers"}},
         "required": ["title"]}},
    {"name": "eva_update_document",
     "description": "Replace or append content of an existing EVA page and publish (creates a new version). "
                    "Markdown by default; local images are uploaded. Use only when the user asked to change the page.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"},
         "content": {"type": "string"},
         "mode": {"type": "string", "enum": ["replace", "append", "prepend"], "default": "replace"},
         "format": {"type": "string", "enum": ["markdown", "html"], "default": "markdown",
                    "description": TABLE_HTML_NOTE},
         "publish": {"type": "boolean", "default": True},
         "notify": {"type": "boolean", "default": False}},
         "required": ["code", "content"]}},
    {"name": "eva_delete_document",
     "description": "Move an EVA page (with its child pages) to the trash, like the UI 'Удалить'. Use only when the user asked to delete it.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "eva_create_task",
     "description": "Create an EVA task in a project the way the UI does: create_dummy_task -> lock -> update fields -> unlock "
                    "-> save_dummy_task (the draft is removed with delete_dummy_task if anything fails). description is Markdown. "
                    "fields: any CmfTask fields (see eva_describe_class CmfTask), e.g. {\"responsible\": \"CmfPerson:...\", \"deadline\": \"2026-12-01\"}. "
                    "Built from the UI code; saving was not verified on a live project. Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "project": PROJECT_PROP, "title": {"type": "string"}, "description": {"type": "string"},
         "fields": {"type": "object"}},
         "required": ["project", "title"]}},
    {"name": "eva_update_task",
     "description": "Change fields of an EVA task like the UI (lock -> update -> unlock). description is Markdown. "
                    "For status use eva_set_task_status. Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "description": {"type": "string"}, "fields": {"type": "object"}},
         "required": ["code"]}},
    {"name": "eva_task_statuses",
     "description": "Statuses of the task's workflow (id, name, type) and its current status.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "eva_set_task_status",
     "description": "Move an EVA task to a status (name or CmfStatus id) like the UI: update(status); if the transition needs "
                    "a form the server answers must_transition_run and task_transition_run is called with comment and fields. "
                    "Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "status": {"type": "string"},
         "comment": {"type": "string", "description": "comment for the transition (Markdown)"},
         "fields": {"type": "object", "description": "fields the transition form requires"}},
         "required": ["code", "status"]}},
    {"name": "eva_delete_task",
     "description": "Delete an EVA task (CmfTask.delete, as the UI does for objects; goes to the trash). Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "eva_export_document",
     "description": "Export an EVA page to PDF or DOCX like the UI (export_to_pdf / export_to_docx -> file link) and save it locally.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "format": {"type": "string", "enum": ["pdf", "docx"], "default": "pdf"},
         "dest": {"type": "string", "description": "file or folder; default ~/Downloads/eva"}},
         "required": ["code"]}},
    {"name": "eva_list_attachments",
     "description": "Attachments of an EVA page (DOC-...) or task: name, size, URL.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "eva_download_attachments",
     "description": "Download attachments of an EVA page or task: all as a zip (like the UI 'Скачать все'; EVA keeps that zip "
                    "as a new attachment of the page) or the ones whose name contains name_contains, one by one.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "name_contains": {"type": "string"},
         "dest": {"type": "string", "description": "folder; default ~/Downloads/eva"}},
         "required": ["code"]}},
    {"name": "eva_download",
     "description": "Download any file EVA serves (a /files/... path or URL returned by another call) and save it locally.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "dest": {"type": "string"}}, "required": ["path"]}},
    {"name": "eva_export_list",
     "description": "Export a list of objects to a file like the UI table export (<Class>.export2file {field_names, bql, "
                    "format_file, include_archived}) and save it. bql is the EVA query language used by filters.",
     "inputSchema": {"type": "object", "properties": {
         "class_name": {"type": "string", "default": "CmfTask"},
         "bql": {"description": "BQL string or a filter array like ['code','=','ABC-1']"},
         "field_names": {"type": "array", "items": {"type": "string"}},
         "wait": {"type": "integer", "default": 300, "description": "seconds to wait for the file to be generated"},
         "format": {"type": "string", "default": "csv"}, "include_archived": {"type": "boolean", "default": False},
         "dest": {"type": "string"}},
         "required": ["bql"]}},
    {"name": "eva_move_document",
     "description": "Move an EVA page under another page (DOC-...) or to a project root, like the UI 'Переместить' "
                    "(changes tree_parent/parent and saves). Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "to_parent": {"type": "string", "description": "DOC-... of the new parent page"},
         "to_project": PROJECT_PROP}, "required": ["code"]}},
    {"name": "eva_copy_document",
     "description": "Copy an EVA page (optionally with child pages) under another page, like the UI (copy_tree_node). "
                    "find/replace rewrite text in the copies. Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "to_parent": {"type": "string", "description": "DOC-... where to put the copy"},
         "recursive": {"type": "boolean", "default": False}, "name_prefix": {"type": "string"},
         "find": {"type": "string"}, "replace": {"type": "string"}}, "required": ["code", "to_parent"]}},
    {"name": "eva_archive_document",
     "description": "Archive an EVA page (restore=false) or restore it from the archive (restore=true), like the UI. "
                    "Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "restore": {"type": "boolean", "default": False}}, "required": ["code"]}},
    {"name": "eva_document_versions",
     "description": "Version history of an EVA page (CmfDocumentHistory): version number, date, author, title.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "limit": {"type": "integer", "default": 30}},
                     "required": ["code"]}},
    {"name": "eva_diff_versions",
     "description": "Compare two versions of an EVA page with the server's html_diff (as the UI history view). "
                    "a/b are cmf_version numbers from eva_document_versions; b omitted = current text.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "a": {"type": "string"}, "b": {"type": "string"}}, "required": ["code", "a"]}},
    {"name": "eva_restore_version",
     "description": "Restore an EVA page to an older version (restore_version with history_version = cmf_version) or drop the "
                    "unpublished draft (drop_draft=true). Like the UI, the old version comes back as a draft; publish=true "
                    "then releases it ('Выпустить черновик'). Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "version": {"type": "string"}, "drop_draft": {"type": "boolean", "default": False},
         "publish": {"type": "boolean", "default": True}, "notify": {"type": "boolean", "default": False}},
         "required": ["code", "version"]}},
    {"name": "eva_create_from_template",
     "description": "Create a page from a template page like the UI (create_doc_from_template {id, params}). "
                    "Not verified on a live template. Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "template": {"type": "string", "description": "DOC-... of the template"},
         "to_parent": {"type": "string", "description": "DOC-... of the parent page"},
         "params": {"type": "object", "description": "extra params the template form asks for"}},
         "required": ["template", "to_parent"]}},
    {"name": "eva_rate_document",
     "description": "Set your star rating (1-5, or 0 to clear) for an EVA page, like the stars under the page.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "rate": {"type": "integer"}},
                     "required": ["code", "rate"]}},
    {"name": "eva_approval",
     "description": "Page approval like the UI: action=status (get_in_approve), start (start_approve), "
                    "approve/reject (approve_one with resolution, text, doc_version). Not verified live. Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "action": {"type": "string", "enum": ["status", "start", "approve", "reject"]},
         "text": {"type": "string"}, "approve_group": {"type": "string"}}, "required": ["code", "action"]}},
    {"name": "eva_inline_comment",
     "description": "Inline comment on a fragment of an EVA page, like selecting text and pressing 'Комментарий': "
                    "quote = exact text to highlight (or data_id of the block; whole block if no quote). "
                    "action=delete removes a thread (comment_id = thread id). Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "action": {"type": "string", "enum": ["add", "delete"], "default": "add"},
         "quote": {"type": "string"}, "data_id": {"type": "string"}, "text": {"type": "string"},
         "comment_id": {"type": "string"}},
         "required": ["code"]}},
    {"name": "eva_search",
     "description": "Global search like the EVA header search box (CmfFullSearch.fulltext_search): fuzzy, fast, over tasks, "
                    "documents, boards and more. model: ANY | CmfTask | CmfDocument | CmfKanbanBoard | CmfProject ...",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "model": {"type": "string", "default": "ANY"},
         "titles_only": {"type": "boolean", "default": False}, "archived": {"type": "boolean", "default": False},
         "limit": {"type": "integer", "default": 20}}, "required": ["query"]}},
    {"name": "eva_find_tasks",
     "description": "Find tasks (or other objects) with an EVA query like the filter editor: UBQL text "
                    "(e.g. 'project = \"my-project\" and status != \"Закрыто\" and responsible = \"login@...\"') "
                    "is converted by the server (ubql2bql); a filter array works too. Returns rows and total count.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"description": "UBQL string or filter array"}, "class_name": {"type": "string", "default": "CmfTask"},
         "order_by": {"type": "array", "items": {"type": "string"}, "default": ["-cmf_modified_at"]},
         "limit": {"type": "integer", "default": 30}}, "required": ["query"]}},
    {"name": "eva_saved_filters",
     "description": "Saved EVA filters (CmfBqlFilter): search by name; with code runs the filter and returns its tasks.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "code": {"type": "string", "description": "filter code to run, e.g. RFBF-041117"},
         "limit": {"type": "integer", "default": 30}}}},
    {"name": "eva_my_tasks",
     "description": "'Мои задачи' like the EVA home page: view=doing (Делаю; when=today|tomorrow|later|all by alarm date), "
                    "approving (Согласовать), assigned (Поручил).",
     "inputSchema": {"type": "object", "properties": {
         "view": {"type": "string", "enum": ["doing", "approving", "assigned"], "default": "doing"},
         "when": {"type": "string", "enum": ["today", "tomorrow", "later", "all"], "default": "all"},
         "limit": {"type": "integer", "default": 50}}}},
    {"name": "eva_notifications",
     "description": "Your EVA notifications (bell): unread by default, newest first.",
     "inputSchema": {"type": "object", "properties": {
         "all": {"type": "boolean", "default": False}, "limit": {"type": "integer", "default": 30}}}},
    {"name": "eva_find_person",
     "description": "Find EVA users by name, login or e-mail (id, login, name). Use the login/name in @[...] to mention "
                    "someone in eva_add_comment / pages, or the id for task fields like responsible.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]}},
    {"name": "eva_boards",
     "description": "Kanban boards: search by name/project; with code shows the board like the UI: columns "
                    "(mapped statuses) and the cards of the board filter in each column.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "project": PROJECT_PROP, "code": {"type": "string", "description": "KBB-..."},
         "cards_per_column": {"type": "integer", "default": 15}}}},
    {"name": "eva_sprints",
     "description": "Sprints (kind=sprint) or releases (kind=release) — CmfList with logic_prefix list.agile_sprint / "
                    "list.release; with code lists the tasks in it.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["sprint", "release"], "default": "sprint"},
         "project": PROJECT_PROP, "query": {"type": "string"}, "open_only": {"type": "boolean", "default": True},
         "code": {"type": "string", "description": "SPR-... to list its tasks"}, "limit": {"type": "integer", "default": 30}}}},
    {"name": "eva_task_lists",
     "description": "Add a task to a sprint/release/list or remove it (task field 'lists'), like dragging it in the UI. "
                    "Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string", "description": "task code"}, "list": {"type": "string", "description": "SPR-... / list code"},
         "action": {"type": "string", "enum": ["add", "remove"], "default": "add"}}, "required": ["code", "list"]}},
    {"name": "eva_list_comments",
     "description": "Comments of an EVA page (DOC-...) or task (code), newest last, as plain text with author and id.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "limit": {"type": "integer", "default": 50}},
                     "required": ["code"]}},
    {"name": "eva_add_comment",
     "description": "Add a comment to an EVA page (DOC-...) or task (code), like the UI: create_dummy -> update(is_dummy=false, text). "
                    "Markdown by default; @[login or name] mentions a person (as the editor's @). reply_to = comment id to "
                    "answer a thread. Use only when the user asked to comment.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"}, "text": {"type": "string"},
         "format": {"type": "string", "enum": ["markdown", "html"], "default": "markdown"},
         "reply_to": {"type": "string", "description": "CmfComment:... to reply to"},
         "private": {"type": "boolean", "default": False}},
         "required": ["code", "text"]}},
    {"name": "eva_delete_comment",
     "description": "Delete a comment (CmfComment:...) like the UI. Use only when the user asked.",
     "inputSchema": {"type": "object", "properties": {"comment_id": {"type": "string"}}, "required": ["comment_id"]}},
    {"name": "eva_upload_attachment",
     "description": "Attach a local file to an EVA page (DOC-...) or task. Returns its URL (usable as <img src> / link).",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string", "description": "DOC-... or task code"},
         "path": {"type": "string", "description": "Absolute path to a local file"}},
         "required": ["code", "path"]}},
    {"name": "eva_list_documents",
     "description": "List child documents of a document (parent=DOC-...) or top-level documents of a project.",
     "inputSchema": {"type": "object", "properties": {
         "parent": {"type": "string", "description": "Parent document code DOC-..."},
         "project": PROJECT_PROP,
         "limit": {"type": "integer", "default": 100}}}},
    {"name": "eva_list_projects",
     "description": "Find EVA projects/spaces by name or code fragment.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer", "default": 30}}}},
    {"name": "eva_get_task",
     "description": "Read an EVA task by code (ABC-123): main fields, description as Markdown, link.",
     "inputSchema": {"type": "object", "properties": {
         "code": {"type": "string"},
         "fields": {"type": "array", "items": {"type": "string"}, "description": "extra fields to include"}},
         "required": ["code"]}},
    {"name": "eva_search_tasks",
     "description": "Find EVA tasks by name fragment; optional project and status type (OPEN / IN_PROGRESS / CLOSED).",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "project": PROJECT_PROP, "status_type": {"type": "string"},
         "limit": {"type": "integer", "default": 20}}, "required": ["query"]}},
    {"name": "eva_list_classes",
     "description": "List EVA API classes (343: CmfDocument, CmfTask, CmfProject, CmfPerson, CmfComment, ...). Optional substring filter.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "eva_list_methods",
     "description": "List API methods of an EVA class, marked [r] read-only or [w] modifying. Optional substring filter.",
     "inputSchema": {"type": "object", "properties": {"class_name": {"type": "string"}, "query": {"type": "string"}},
                     "required": ["class_name"]}},
    {"name": "eva_describe_class",
     "description": "Fields of an EVA class (via get_meta): name, widget/type, caption, flags. Use before filters or create/update.",
     "inputSchema": {"type": "object", "properties": {"class_name": {"type": "string"}, "query": {"type": "string"}},
                     "required": ["class_name"]}},
    {"name": "eva_call",
     "description": "Call ANY EVA API method '<Class>.<method>' exactly as the web UI does. "
                    "Object methods (eva_list_methods shows `id` in the signature) need id: it goes to args[0] with jsonrpc 2.2. "
                    "Typical: .get / .list / .count with {'filter':..., 'fields':[...], 'limit':N}; "
                    ".create / .update with field values. " + FILTER_HELP +
                    " Modifying methods ([w]) require allow_write=true; set it only when the user asked for the change.",
     "inputSchema": {"type": "object", "properties": {
         "method": {"type": "string", "description": "e.g. CmfTask.get"},
         "kwargs": {"type": "object"},
         "args": {"type": "array", "description": "extra positional args (after id)"},
         "id": {"type": "string", "description": "object id or code (DOC-1, ABC-2) for object methods: sent as args[0]"},
         "top": {"type": "object", "description": "extra top-level request keys (UI sends e.g. filter there)"},
         "endpoint": {"type": "string", "enum": ["api", "pub", "servicedesk"], "default": "api",
                      "description": "api = /api/ (default), pub = /pub/pub_api (public_* methods), servicedesk = /servicedesk/sd_api"},
         "allow_write": {"type": "boolean", "default": False}},
         "required": ["method"]}},
    {"name": "eva_audit_log",
     "description": "Local journal of every change this MCP made in EVA (method, ids, result), newest last.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 30}}}},
    {"name": "eva_whoami",
     "description": "Show the EVA user the token belongs to (connectivity check).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "eva_refresh_catalog",
     "description": "Re-download the API method catalog from EVA (after an EVA upgrade). Takes ~1 min.",
     "inputSchema": {"type": "object", "properties": {}}},
]


WRITE_TOOLS = {"eva_create_document", "eva_update_document", "eva_delete_document", "eva_upload_attachment",
               "eva_add_comment", "eva_delete_comment", "eva_inline_comment", "eva_create_task", "eva_update_task",
               "eva_set_task_status", "eva_delete_task", "eva_task_lists", "eva_move_document", "eva_copy_document",
               "eva_archive_document", "eva_restore_version", "eva_create_from_template", "eva_rate_document",
               "eva_approval", "eva_download_attachments", "eva_refresh_catalog"}
WRITE_TOOLS |= __import__("planning").WRITE_TOOLS_PLANNING

from timetracker import TOOLS_TIME, WRITE_TIME_TOOLS  # noqa: E402  time tracking lives in timetracker.py
TOOLS += TOOLS_TIME
WRITE_TOOLS |= WRITE_TIME_TOOLS


import gittools  # noqa: E402  (feature modules register their tools here)
TOOLS += gittools.TOOLS_GIT


try:  # reports, dashboards, analytics: reports.py (it imports this module as "server")
    sys.modules.setdefault("server", sys.modules[__name__])
    import reports
    TOOLS += reports.TOOLS_REPORTS
    WRITE_TOOLS |= reports.WRITE_TOOLS_REPORTS
except ImportError:
    reports = None


# ---------------------------------------------------------------- live changes (eva_watch)

class _WS:
    """Minimal RFC 6455 client over the stdlib (text frames, ping/pong, close) — enough for Socket.IO v4."""

    def __init__(self, path, timeout=30):
        import base64
        import socket
        u = urllib.parse.urlparse(base_url())
        port = u.port or (443 if u.scheme == "https" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        self.s = _CTX.wrap_socket(raw, server_hostname=u.hostname) if u.scheme == "https" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                        f"Authorization: Bearer {token()}\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake: connection closed")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket handshake failed: {head.split(chr(13).encode())[0].decode(errors='replace')}")

    def _frame(self, op, data):
        import struct
        n, mask = len(data), os.urandom(4)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                     bytes([0xFE]) + struct.pack(">H", n) if n < 65536 else
                                     bytes([0xFF]) + struct.pack(">Q", n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, text):
        self._frame(1, text.encode("utf-8"))

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError("WebSocket closed by server")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        """Next text message; answers pings itself. Raises socket.timeout when nothing arrives in time."""
        import struct
        parts = []
        while True:
            b1, b2 = self._need(2)
            n = b2 & 127
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
            payload = self._need(n)
            op = b1 & 15
            if op == 9:
                self._frame(10, payload)
                continue
            if op == 8:
                raise EOFError("WebSocket closed by server")
            if op in (1, 2, 0):
                parts.append(payload)
                if b1 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self._frame(8, b"")
            self.s.close()
        except OSError:
            pass


def _watch_target(a):
    """-> (object id or None, class name or None, label)."""
    ref = (a.get("target") or a.get("code") or "").strip()
    cls = (a.get("class_name") or "").strip() or None
    if ref:
        m = re.fullmatch(r"(Cmf\w+):[0-9a-f-]{36}", ref)
        if m:
            return ref, m.group(1), ref
        c = "CmfDocument" if ref.upper().startswith("DOC-") else (cls or "CmfTask")
        return resolve_obj(c, ref), c, ref
    if cls:
        return None, cls, cls
    raise ValueError("Pass target (DOC-…, task code or Class:uuid) or class_name (CmfTask, CmfDocument…)")


def watch(a):
    """Listen to the same Socket.IO events the web UI gets (is_changed-<Class>) and report changes of one object
    (and of its comments/attachments/history: events whose parent_id is the object) or of a whole class."""
    import socket
    obj, cls, label = _watch_target(a)
    timeout = max(1.0, min(float(a.get("timeout") or 60), 600.0))
    settle = float(a.get("settle") if a.get("settle") is not None else 3)
    until_timeout = bool(a.get("until_timeout"))
    max_events = int(a.get("max_events") or 50)
    me = eva("CmfPerson.get_current_user", {}) or {}
    ws = _WS("/socket.io/?EIO=4&transport=websocket")
    events, start = [], time.time()
    deadline = start + timeout
    try:
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            ws.s.settimeout(min(left, 30))
            try:
                msg = ws.recv()
            except socket.timeout:
                continue
            if msg.startswith("0"):              # Engine.IO open -> Socket.IO connect
                ws.send("40")
            elif msg == "2":                     # Engine.IO ping
                ws.send("3")
            elif msg.startswith("40"):           # connected: register like the UI, then join the object's room
                ws.send("42" + json.dumps(["user_regisrty", {"user_id": me.get("id"), "hash": 123123}]))
                if obj:
                    ws.send("42" + json.dumps(["join_room", {"room_name": obj, "user_id": me.get("id"),
                                                             "user_login": me.get("login"), "user_name": me.get("name")}]))
            elif msg.startswith("44"):
                raise ConnectionError(f"Socket.IO refused the connection: {msg[2:200]}")
            elif msg.startswith("42"):
                try:
                    ev, data = (json.loads(msg[2:]) + [None])[:2]
                except ValueError:
                    continue
                if not str(ev).startswith("is_changed-") or not isinstance(data, dict):
                    continue
                ev_cls = ev[len("is_changed-"):]
                if obj:
                    hit = obj in (data.get("id"), data.get("parent_id"))
                else:
                    hit = ev_cls == cls
                if not hit:
                    continue
                events.append((time.time(), ev_cls, data))
                if len(events) >= max_events:
                    break
                if not until_timeout:
                    deadline = min(deadline, time.time() + settle)
    finally:
        ws.close()
    if not events:
        return f"No changes of {label} in {timeout:.0f}s"
    out = [f"{len(events)} change event(s) of {label} in {time.time() - start:.0f}s:"]
    for ts, ev_cls, d in events:
        who = d.get("event_current_person")
        who = "you" if who and who == me.get("id") else (who or "?")
        rel = "" if d.get("id") == obj or not obj else f" (child of {label})"
        out.append(f"{time.strftime('%H:%M:%S', time.localtime(ts))}  {ev_cls} {d.get('id')}{rel}  "
                   f"{','.join(d.get('action') or [])}  fields: {', '.join(d.get('changed_fields') or [])}  by {who}")
    return "\n".join(out)


WATCH_TOOL = {
    "name": "eva_watch",
    "description": "Wait for live changes in EVA, the same WebSocket events (is_changed-<Class>) the web UI uses to refresh. "
                   "Pass target (DOC-…, task code like ABC-12, or Class:uuid) to watch one object together with its "
                   "comments, attachments and versions, or class_name to watch every change of a class. Returns as soon as "
                   "changes arrive (plus `settle` seconds to collect the burst) or after `timeout` with 'No changes'. "
                   "Each event: time, class, id, action, changed field names, who changed it.",
    "inputSchema": {"type": "object", "properties": {
        "target": {"type": "string", "description": "DOC-…, task code or Class:uuid"},
        "class_name": {"type": "string", "description": "Watch a whole class instead (CmfTask, CmfDocument, CmfComment…)"},
        "timeout": {"type": "number", "default": 60, "description": "Seconds to wait, max 600"},
        "settle": {"type": "number", "default": 3, "description": "After the first event keep collecting this many seconds"},
        "until_timeout": {"type": "boolean", "default": False, "description": "Collect events for the whole timeout"},
        "max_events": {"type": "integer", "default": 50}}}}


# ---------------------------------------------------------------- grouped tools (what tools/list shows)

def _fixed(**kw):
    return lambda a: dict(a, **kw)


# tool -> (summary, {action: (legacy tool, args transform or None)})
GROUPS = {
    "eva_document": ("EVA pages (documents): read, search, write, history, approval.", {
        "get": ("eva_get_document", None),
        "search": ("eva_search_documents", None),
        "list": ("eva_list_documents", None),
        "create": ("eva_create_document", None),
        "update": ("eva_update_document", None),
        "delete": ("eva_delete_document", None),
        "move": ("eva_move_document", None),
        "copy": ("eva_copy_document", None),
        "archive": ("eva_archive_document", None),
        "export": ("eva_export_document", None),
        "versions": ("eva_document_versions", None),
        "diff": ("eva_diff_versions", None),
        "restore": ("eva_restore_version", None),
        "rate": ("eva_rate_document", None),
        "template": ("eva_create_from_template", None),
        "approval_status": ("eva_approval", _fixed(action="status")),
        "approval_start": ("eva_approval", _fixed(action="start")),
        "approve": ("eva_approval", _fixed(action="approve")),
        "reject": ("eva_approval", _fixed(action="reject")),
    }),
    "eva_comment": ("Comments on pages and tasks, including inline comments on a fragment of page text.", {
        "list": ("eva_list_comments", None),
        "add": ("eva_add_comment", None),
        "delete": ("eva_delete_comment", None),
        "inline_add": ("eva_inline_comment", _fixed(action="add")),
        "inline_delete": ("eva_inline_comment", _fixed(action="delete")),
    }),
    "eva_task": ("Tasks: read, search, create, edit, workflow status, sprint/release lists.", {
        "get": ("eva_get_task", None),
        "search": ("eva_search_tasks", None),
        "statuses": ("eva_task_statuses", None),
        "create": ("eva_create_task", None),
        "update": ("eva_update_task", None),
        "set_status": ("eva_set_task_status", None),
        "delete": ("eva_delete_task", None),
        "list_add": ("eva_task_lists", _fixed(action="add")),
        "list_remove": ("eva_task_lists", _fixed(action="remove")),
    }),
    "eva_attachment": ("Files attached to pages and tasks, and any EVA file by path.", {
        "list": ("eva_list_attachments", None),
        "download": ("eva_download_attachments", None),
        "upload": ("eva_upload_attachment", None),
        "download_file": ("eva_download", None),
    }),
    "eva_find": ("Finding things: global search, UBQL queries, saved filters, My tasks, notifications, people, projects.", {
        "search": ("eva_search", None),
        "ubql": ("eva_find_tasks", None),
        "saved_filters": ("eva_saved_filters", None),
        "my_tasks": ("eva_my_tasks", None),
        "notifications": ("eva_notifications", None),
        "people": ("eva_find_person", None),
        "projects": ("eva_list_projects", None),
    }),
    "eva_board": ("Agile: kanban boards and sprints/releases.", {
        "boards": ("eva_boards", None),
        "sprints": ("eva_sprints", None),
    }),
    "eva_api": ("The whole EVA API and service: call any method, browse classes/methods, export lists, whoami, audit log.", {
        "call": ("eva_call", None),
        "list_classes": ("eva_list_classes", None),
        "list_methods": ("eva_list_methods", None),
        "describe_class": ("eva_describe_class", None),
        "export_list": ("eva_export_list", None),
        "whoami": ("eva_whoami", None),
        "audit_log": ("eva_audit_log", None),
        "refresh_catalog": ("eva_refresh_catalog", None),
    }),
}
_LEGACY = {t["name"]: t for t in TOOLS}
ACTION_DESC = {
    ("eva_document", "approval_status"): "Approval state of the page (get_in_approve)",
    ("eva_document", "approval_start"): "Start approval of the page (start_approve)",
    ("eva_document", "approve"): "Approve the page as a member of an approval group (approve_one), text = comment",
    ("eva_document", "reject"): "Reject the page in approval (approve_one), text = reason",
    ("eva_document", "export"): "Export the page to PDF or DOCX (format=pdf|docx) and save it locally",
    ("eva_comment", "inline_add"): "Inline comment on a fragment of page text: quote = exact text (or data_id of the block), text = comment",
    ("eva_comment", "inline_delete"): "Delete an inline comment thread (comment_id)",
    ("eva_task", "list_add"): "Add the task to a sprint / release (list = its code)",
    ("eva_task", "list_remove"): "Remove the task from a sprint / release (list = its code)",
    ("eva_document", "create"): "Create a page under parent (DOC-…) or at a project root from Markdown/HTML and publish it "
                                "(tables are stored in the editor's form, so the browser does not re-render them)",
}
WRITE_ACTIONS = {(g, act) for g, (_, acts) in GROUPS.items() for act, (old, _) in acts.items()
                 if old in WRITE_TOOLS and not (old == "eva_approval" and act == "approval_status")}
# legacy tools whose own "action" argument selects read or write
WRITE_TOOLS.discard("eva_approval")


def _merge_prop(cur, new):
    if cur is None:
        return dict(new)
    if cur.get("type") != new.get("type"):
        cur.pop("type", None)
    if "enum" in cur or "enum" in new:
        cur["enum"] = list(dict.fromkeys((cur.get("enum") or []) + (new.get("enum") or [])))
    return cur


def group_schema(gname):
    summary, acts = GROUPS[gname]
    actions = [act for act in acts if not (READONLY and (gname, act) in WRITE_ACTIONS)]
    props, lines = {}, []
    for act in actions:
        old, fix = acts[act]
        s = _LEGACY[old]["inputSchema"]
        fixed = set((fix({}) if fix else {}).keys())
        names = [k for k in s.get("properties", {}) if k not in fixed]
        for k in names:
            props[k] = _merge_prop(props.get(k), s["properties"][k])
        req = [k for k in (s.get("required") or []) if k not in fixed]
        opt = [k for k in names if k not in req]
        desc = ACTION_DESC.get((gname, act)) or _LEGACY[old]["description"].split(". ")[0].rstrip(".")
        lines.append(f"- {act}{' ⚠write' if (gname, act) in WRITE_ACTIONS else ''}: {desc}. "
                     f"Needs: {', '.join(req) or '—'}" + (f"; optional: {', '.join(opt)}" if opt else ""))
    props = {"action": {"type": "string", "enum": actions, "description": "What to do, see the tool description"}, **props}
    return {"name": gname, "description": summary + " Pick `action`:\n" + "\n".join(lines),
            "inputSchema": {"type": "object", "properties": props, "required": ["action"]}}


import servicedesk  # noqa: E402  Service Desk tools (servicedesk.py)
TOOLS += servicedesk.TOOLS_SDESK
WRITE_TOOLS |= servicedesk.WRITE_TOOLS_SDESK
SAFE_NON_READ |= servicedesk.SDESK_SAFE_NON_READ


def visible_tools():
    """Grouped tools (with action enums) + eva_watch + feature-module tools (git, time tracking, reports, planning, service desk).
    In EVA_READONLY mode write actions / write tools are not offered at all."""
    out = [group_schema(g) for g in GROUPS] + [WATCH_TOOL]
    import planning
    module_tools = (list(TOOLS_TIME) + list(gittools.TOOLS_GIT) + (list(reports.TOOLS_REPORTS) if reports else [])
                    + list(planning.TOOLS_PLANNING) + list(servicedesk.TOOLS_SDESK))
    for t in module_tools:
        if READONLY and t["name"] in WRITE_TOOLS:
            continue
        if READONLY and t["name"] == "eva_git":
            t = json.loads(json.dumps(t))
            t["inputSchema"]["properties"]["action"]["enum"] = list(gittools.READ_ACTIONS)
        out.append(t)
    return out


def resolve_group(name, a):
    """eva_document + action=get -> ('eva_get_document', args). Legacy names pass through unchanged."""
    if name not in GROUPS:
        return name, a
    acts = GROUPS[name][1]
    act = a.get("action")
    if act not in acts:
        raise ValueError(f"{name}: unknown action {act!r}; one of: {', '.join(acts)}")
    if READONLY and (name, act) in WRITE_ACTIONS:
        raise ReadOnlyError(f"EVA_READONLY is on: {name} action={act} is disabled")
    old, fix = acts[act]
    args = {k: v for k, v in a.items() if k != "action"}
    args = fix(args) if fix else args
    missing = [k for k in (_LEGACY[old]["inputSchema"].get("required") or []) if args.get(k) in (None, "")]
    if missing:
        raise ValueError(f"{name} action={act} needs: {', '.join(missing)}")
    return old, args


def run_tool(name, a):
    name, a = resolve_group(name, a)
    if name == "eva_watch":
        return watch(a)
    if READONLY and name in WRITE_TOOLS:
        raise ReadOnlyError(f"EVA_READONLY is on: {name} is disabled")
    if READONLY and name == "eva_approval" and (a.get("action") or "status") != "status":
        raise ReadOnlyError(f"EVA_READONLY is on: eva_approval action={a.get('action')} is disabled")
    res = gittools.run(name, a)
    if res is not None:
        return res
    import timetracker
    out = timetracker.run(name, a)
    if out is not None:
        return out
    if reports:
        out = reports.run(name, a)
        if out is not None:
            return out
    import planning
    res = planning.run(name, a)
    if res is not None:
        return res
    sd_res = servicedesk.run(name, a)
    if sd_res is not None:
        return sd_res
    if name == "eva_audit_log":
        if not os.path.exists(AUDIT_PATH):
            return f"No changes logged yet ({AUDIT_PATH})"
        with open(AUDIT_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-int(a.get("limit") or 30):]
        out = []
        for l in lines:
            try:
                e = json.loads(l)
            except ValueError:
                continue
            ids = e.get("args") or e.get("file") or ""
            out.append(f"{e.get('ts')}  {'OK ' if e.get('ok') else 'ERR'}  {e.get('method')}  {json.dumps(ids, ensure_ascii=False)[:120]}"
                       + (f"  — {e['error']}" if e.get("error") else ""))
        return f"{AUDIT_PATH}\n" + "\n".join(out)

    lim = lambda d: int(a.get("limit") or d)

    if name == "eva_search_documents":
        q, where = a["query"], a.get("in") or "title"
        conds = {"title": [["name", "ILIKE", f"%{q}%"]],
                 "text": [["text", "ILIKE", f"%{q}%"]],
                 "both": [["OR", ["name", "ILIKE", f"%{q}%"], ["text", "ILIKE", f"%{q}%"]]]}[where]
        pid = resolve_project(a.get("project"))
        if pid:
            conds.insert(0, ["project_id", "=", pid])
        flt = conds[0] if len(conds) == 1 else ["AND", *conds]
        r = eva("CmfDocument.list", {"filter": flt, "fields": ["code", "name", "cmf_modified_at"],
                                     "order_by": ["-cmf_modified_at"], "limit": lim(20) * 3})
        return rows(r, lambda d: f"{d['code']}  {d.get('name')}  ({(d.get('cmf_modified_at') or '')[:10]})  {doc_url(d['code'])}", lim(20))

    if name == "eva_get_document":
        r = eva("CmfDocument.get", {"filter": ["code", "=", a["code"]],
                                    "fields": ["code", "name", "text", "tree_parent", "project", "cmf_modified_at"]})
        if not r:
            return "Not found (or no access)"
        def _ref(x):  # page tree lives in tree_parent; parent is always the project
            if not isinstance(x, dict):
                return str(x)
            return " ".join(v for v in (x.get("code"), x.get("name")) if v) or x.get("id", "")
        body = r.get("text") if a.get("raw_html") else html_to_md(r.get("text"))
        meta = (f"Link: {doc_url(r['code'])}\nUpdated: {(r.get('cmf_modified_at') or '')[:10]}\n"
                f"Project: {_ref(r.get('project'))}\nParent: {_ref(r.get('tree_parent'))}")
        return f"# {r.get('name')}\n\n{meta}\n\n---\n\n{body}"

    if name == "eva_create_document":
        if a.get("parent"):
            tree_parent = find_document(a["parent"])["id"]
        elif a.get("project"):
            tree_parent = resolve_project(a["project"])
        else:
            raise ValueError("Pass parent (DOC-...) or project")
        before = time.time()
        res = eva("CmfDocument.create", {"tree_parent_id": tree_parent, "name": a["title"]})
        doc_id = res.get("id") if isinstance(res, dict) else (res if isinstance(res, str) else None)
        if not doc_id:  # create may return nothing: find the page we just made
            r = eva("CmfDocument.list", {"filter": ["AND", ["tree_parent_id", "=", tree_parent], ["name", "=", a["title"]]],
                                         "fields": ["code", "cmf_created_at"], "order_by": ["-cmf_created_at"], "limit": 1},
                    use_cache=False)
            doc_id = r[0]["id"] if r else None
        if not doc_id:
            raise RuntimeError("EVA did not return the new page")
        d = eva("CmfDocument.get", {"filter": ["id", "=", doc_id], "fields": ["code", "name"]}, use_cache=False)
        uploads = []
        if a.get("content"):
            body, uploads = render_content(doc_id, a["content"], a.get("format") or "markdown")
            save_document(doc_id, body, a.get("publish", True), a.get("notify", False))
        elif a.get("publish", True):
            eva("CmfDocument.do_publish", {"notify_persons": bool(a.get("notify"))}, args=[doc_id])
        return (f"Created {d['code']} «{d['name']}» in {time.time() - before:.1f}s"
                f"{' (draft)' if not a.get('publish', True) else ''}\n{doc_url(d['code'])}"
                + (f"\nUploaded {len(uploads)} file(s)" if uploads else ""))

    if name == "eva_update_document":
        d = find_document(a["code"])
        body, uploads = render_content(d["id"], a["content"], a.get("format") or "markdown")
        old = re.sub(r"^\s*<div>(.*)</div>\s*$", r"\1", d.get("text") or "", flags=re.S)  # EVA wraps stored text in <div>
        mode = a.get("mode") or "replace"
        new = body if mode == "replace" else (old + "\n" + body if mode == "append" else body + "\n" + old)
        save_document(d["id"], new, a.get("publish", True), a.get("notify", False))
        return (f"Updated {d['code']} «{d['name']}» ({mode}{', draft' if not a.get('publish', True) else ''})\n"
                f"{doc_url(d['code'])}" + (f"\nUploaded {len(uploads)} file(s)" if uploads else ""))

    if name == "eva_delete_document":
        d = find_document(a["code"])
        delete_document(d["id"])
        return f"Moved {d['code']} «{d['name']}» to the trash (with child pages)"

    if name == "eva_create_task":
        project = resolve_project(a["project"])
        save_kwargs = {"history_logic_type": None}
        dummy = eva("CmfTask.create_dummy_task", {"name": None, "parent": project, "save_kwargs": save_kwargs}, args=[])
        tid = dummy.get("id") if isinstance(dummy, dict) else dummy
        changed = {"name": a["title"]}
        if a.get("description"):
            changed["text"] = add_data_ids(md_to_html(a["description"]))
        changed.update(a.get("fields") or {})
        try:
            eva("CmfTask.lock", {}, top={"filter": ["id", "==", tid]})
            try:
                for k, v in changed.items():  # the form saves each field as it is edited
                    eva("CmfTask.update", {"save_kwargs": save_kwargs, k: v}, args=[tid])
            finally:
                eva("CmfTask.unlock", {}, top={"filter": ["id", "==", tid]})
            res = eva("CmfTask.save_dummy_task", {"changed_fields": changed, "save_kwargs": save_kwargs}, args=[tid])
        except Exception:
            try:
                eva("CmfTask.delete_dummy_task", {}, args=[tid])
            except Exception:
                pass
            raise
        t = eva("CmfTask.get", {"filter": ["id", "=", tid], "fields": ["code", "name"]}, use_cache=False) or {}
        return f"Created task {t.get('code')} «{t.get('name')}»\n{task_url(t.get('code'))}" if t.get("code") else dump(res)

    if name == "eva_update_task":
        tid = resolve_obj("CmfTask", a["code"])
        changed = dict(a.get("fields") or {})
        if a.get("description") is not None:
            changed["text"] = add_data_ids(md_to_html(a["description"]))
        if not changed:
            raise ValueError("Nothing to change: pass fields and/or description")
        eva("CmfTask.lock", {}, top={"filter": ["id", "==", tid]})
        try:
            eva("CmfTask.update", changed, args=[tid])
        finally:
            eva("CmfTask.unlock", {}, top={"filter": ["id", "==", tid]})
        return f"Updated {a['code']}: {', '.join(changed)}"

    if name in ("eva_task_statuses", "eva_set_task_status"):
        t = eva("CmfTask.get", {"filter": ["code", "=", a["code"]], "fields": ["code", "status_id", "workflow_id"]},
                use_cache=False)
        if not t:
            raise ValueError(f"Task {a['code']} not found (or no access)")
        sts = eva("CmfStatus.list", {"filter": ["workflow_id", "=", t["workflow_id"]], "fields": ["name", "status_type"],
                                     "limit": 100}) or []
        sts = [x for x in sts if x.get("id") and x.get("_acl_obj") != "deny"]
        if name == "eva_task_statuses":
            return "\n".join(f"{'→ ' if x['id'] == t.get('status_id') else '  '}{x.get('name')}  [{x.get('status_type')}]  {x['id']}"
                             for x in sts) or "No statuses visible"
        want = a["status"]
        st = next((x for x in sts if x["id"] == want or (x.get("name") or "").lower() == want.lower()), None)
        if not st:
            raise ValueError(f"Status '{want}' not in this workflow. Use eva_task_statuses.")
        try:
            eva("CmfTask.update", {"status": st["id"]}, args=[t["id"]])
        except EvaAbort as e:
            parts = str(e).split()
            if not parts or parts[0] != "must_transition_run":
                raise
            trans_id = parts[2]
            changed = dict(a.get("fields") or {})
            changed["status"] = st["id"]
            comment = add_data_ids(md_to_html(a["comment"])) if a.get("comment") else None
            eva("CmfTask.task_transition_run", {"comment_append": comment, "changed_fields": changed},
                args=[t["id"], trans_id])
        return f"{a['code']} → {st.get('name')}"

    if name == "eva_delete_task":
        tid = resolve_obj("CmfTask", a["code"])
        eva("CmfTask.delete", {}, args=[tid])
        return f"Deleted task {a['code']}"

    if name == "eva_export_document":
        d = find_document(a["code"])
        fmt = a.get("format") or "pdf"
        link = eva(f"CmfDocument.export_to_{fmt}", {}, args=[d["id"]])
        path, size = download_file(link, a.get("dest") or DOWNLOAD_DIR + "/")
        return f"Saved {path} ({size} bytes)"

    if name in ("eva_list_attachments", "eva_download_attachments"):
        code = a["code"]
        cls = "CmfDocument" if code.upper().startswith("DOC-") else "CmfTask"
        obj = resolve_obj(cls, code)
        if name == "eva_download_attachments" and not a.get("name_contains"):
            link = eva(f"{cls}.download_all_attachment", {}, top={"filter": ["id", "==", obj]})
            path, size = download_file(link, a.get("dest") or DOWNLOAD_DIR + "/")
            return f"Saved {path} ({size} bytes)"
        atts = eva("CmfAttachment.list", {"filter": ["AND", ["parent_id", "=", obj], ["cmf_deleted", "!=", True]],
                                          "fields": ["name", "url", "size", "cmf_created_at"]}, use_cache=False) or []
        uniq = {}
        for x in atts:
            if x.get("url") and x.get("_acl_obj") != "deny":
                uniq.setdefault(x["url"], x)
        atts = list(uniq.values())
        if name == "eva_list_attachments":
            return "\n".join(f"{x.get('name')}  {x.get('size') or ''}  {BASE}{x['url']}" for x in atts) or "No attachments"
        q = a["name_contains"].lower()
        saved = [download_file(x["url"], a.get("dest") or DOWNLOAD_DIR + "/")
                 for x in atts if q in (x.get("name") or "").lower()]
        return "\n".join(f"Saved {p} ({n} bytes)" for p, n in saved) or "No matching attachments"

    if name == "eva_download":
        path, size = download_file(a["path"], a.get("dest") or DOWNLOAD_DIR + "/")
        return f"Saved {path} ({size} bytes)"

    if name == "eva_export_list":
        cls = a.get("class_name") or "CmfTask"
        res = eva(f"{cls}.export2file", {"field_names": a.get("field_names"), "bql": a["bql"],
                                         "format_file": a.get("format") or "csv",
                                         "include_archived": bool(a.get("include_archived"))}, args=[])
        link = res if isinstance(res, str) else (res or {}).get("url") or (res or {}).get("href")
        if not link:
            return "EVA started the export in the background:\n" + dump(res)
        path, size = download_file(link, a.get("dest") or DOWNLOAD_DIR + "/", wait=int(a.get("wait") or 300))
        return f"Saved {path} ({size} bytes)"

    if name == "eva_move_document":
        d = find_document(a["code"])
        if a.get("to_parent"):
            p = eva("CmfDocument.get", {"filter": ["code", "=", a["to_parent"]], "fields": ["code", "project_id"]},
                    use_cache=False)
            if not p:
                raise ValueError(f"{a['to_parent']} not found")
            tree_parent, parent = p["id"], p.get("project_id") or p.get("parent_id")
        elif a.get("to_project"):
            tree_parent = parent = resolve_project(a["to_project"])
        else:
            raise ValueError("Pass to_parent (DOC-...) or to_project")
        eva("CmfDocument.update", {"tree_parent": tree_parent, "parent": parent}, args=[d["id"]])
        return f"Moved {d['code']} «{d['name']}» under {a.get('to_parent') or a.get('to_project')}"

    if name == "eva_copy_document":
        d = find_document(a["code"])
        dst = find_document(a["to_parent"])
        res = eva("CmfDocument.copy_tree_node", {"src_tree_node_id": d["id"], "dst_tree_node_id": dst["id"],
                                                 "recursive": bool(a.get("recursive")),
                                                 "name_startswith": a.get("name_prefix") or "",
                                                 "find_to": a.get("find") or "", "replace_to": a.get("replace") or ""},
                  args=[])
        new_id = res.get("id") if isinstance(res, dict) else res
        nd = eva("CmfDocument.get", {"filter": ["id", "=", new_id], "fields": ["code", "name"]}, use_cache=False) or {} \
            if isinstance(new_id, str) else {}
        return f"Copied {d['code']} → {nd.get('code')} «{nd.get('name')}» under {dst['code']}\n{doc_url(nd.get('code'))}"

    if name == "eva_archive_document":
        d = find_document(a["code"])
        eva("CmfDocument.archive_restore" if a.get("restore") else "CmfDocument.archive", {}, args=[d["id"]])
        return f"{d['code']} «{d['name']}» {'restored from' if a.get('restore') else 'moved to'} the archive"

    if name in ("eva_document_versions", "eva_diff_versions", "eva_restore_version"):
        d = find_document(a["code"])
        if name == "eva_document_versions":
            r = eva("CmfDocumentHistory.list", {"filter": ["parent_id", "==", d["id"]],
                                               "fields": ["name", "cmf_version", "doc_version", "cmf_created_at",
                                                          "cmf_author"], "order_by": ["-cmf_version"],
                                               "limit": int(a.get("limit") or 30)}, use_cache=False) or []
            rows_ = []
            for h in r:
                if h.get("_acl_obj") == "deny":
                    continue
                who = (h.get("cmf_author") or {}).get("name") if isinstance(h.get("cmf_author"), dict) else ""
                rows_.append(f"{h.get('cmf_version')}  {(h.get('cmf_created_at') or '')[:16]}  {who}"
                             + (f"  {h['name']}" if h.get("name") else ""))
            return "\n".join(rows_) or "No history"
        if name == "eva_restore_version":
            eva("CmfDocument.restore_version", {"history_version": a["version"], "drop_draft": bool(a.get("drop_draft"))},
                args=[d["id"]])
            if a.get("drop_draft"):
                return f"{d['code']}: draft dropped"
            if a.get("publish", True):
                publish_document(d["id"], a.get("notify", False))
                return f"{d['code']}: version {a['version']} restored and published"
            return f"{d['code']}: version {a['version']} restored as a draft (publish it to release)"

        def text_of(ver):
            h = eva("CmfDocumentHistory.get", {"filter": ["AND", ["parent_id", "==", d["id"]], ["cmf_version", "==", ver]],
                                              "fields": ["text"]}, use_cache=False)
            if not h:
                raise ValueError(f"Version {ver} not found (see eva_document_versions)")
            return h.get("text") or ""
        b = text_of(a["b"]) if a.get("b") else (d.get("text") or "")
        diff = eva("CmfDocument.html_diff", {"a": text_of(a["a"]), "b": b}, args=[])
        diff = diff if isinstance(diff, str) else dump(diff)
        diff = re.sub(r"<ins[^>]*>(.*?)</ins>", r"[+\1+]", diff, flags=re.S)
        diff = re.sub(r"<del[^>]*>(.*?)</del>", r"[-\1-]", diff, flags=re.S)
        return html_to_md(diff)

    if name == "eva_create_from_template":
        tpl = find_document(a["template"])
        parent = eva("CmfDocument.get", {"filter": ["code", "=", a["to_parent"]], "fields": ["code", "project_id"]},
                     use_cache=False)
        if not parent:
            raise ValueError(f"{a['to_parent']} not found")
        params = {"tree_parent": parent["id"], "parent": parent.get("project_id")}
        params.update(a.get("params") or {})
        new_id = eva("CmfDocument.create_doc_from_template", {"id": tpl["id"], "params": params})
        nd = eva("CmfDocument.get", {"filter": ["id", "=", new_id], "fields": ["code", "name"]}, use_cache=False) or {}
        return f"Created {nd.get('code')} «{nd.get('name')}» from {tpl['code']}\n{doc_url(nd.get('code'))}"

    if name == "eva_rate_document":
        d = find_document(a["code"])
        rate = int(a["rate"]) or None
        eva("CmfDocument.set_user_rate", {"rate": rate}, args=[d["id"]])
        return f"{d['code']}: rating {'cleared' if rate is None else rate}"

    if name == "eva_approval":
        d = find_document(a["code"])
        act = a["action"]
        if act == "status":
            return dump(eva("CmfDocument.get_in_approve", {"obj": d["id"]}, args=[], use_cache=False))
        if act == "start":
            return dump(eva("CmfDocument.start_approve", {}, args=[d["id"]]))
        info = eva("CmfDocument.get", {"filter": ["id", "=", d["id"]], "fields": ["doc_version"]}, use_cache=False) or {}
        res = eva("CmfDocument.approve_one", {"approve_group": a.get("approve_group"), "resolution": act == "approve",
                                              "text": a.get("text") or "", "doc_version": info.get("doc_version")},
                  args=[d["id"]])
        return f"{d['code']}: {act}\n" + dump(res)

    if name == "eva_inline_comment":
        d = find_document(a["code"])
        if (a.get("action") or "add") == "delete":
            eva("CmfDocument.delete_inline_comment", {"data_comment_id": a["comment_id"]}, args=[d["id"]])
            return f"Inline comment {a['comment_id']} deleted"
        # find the block (data-id) and the fragment position inside its first text node
        blocks = re.findall(r'<(\w+)[^>]*\sdata-id="([^"]+)"[^>]*>(.*?)</\1>', d.get("text") or "", flags=re.S)
        quote = a.get("quote")
        target = None
        for _, did, inner in blocks:
            plain = html.unescape(re.sub(r"<[^>]+>", "", inner))
            if (a.get("data_id") and did == a["data_id"]) or (not a.get("data_id") and quote and quote in plain):
                target = (did, plain)
                break
        if not target:
            raise ValueError("Block not found: pass quote (text on the page) or data_id")
        did, plain = target
        frag = quote if quote and quote in plain else plain
        start = plain.index(frag)
        thread = eva("CmfComment.create", {"parent_id": d["id"], "inline": True})
        hid = "".join(__import__("random").choice(_DATA_ID_ABC) for _ in range(14))
        eva("CmfComment.update", {"html_context":
            f'<span data-id="{hid}" class="inline-highlight" data-object-id="{thread}" data-mark-type="annotation" '
            f'data-new="1" data-mark-annotation-type="inlineComment">{html.escape(frag, quote=False)}</span>'}, args=[thread])
        msg = eva("CmfComment.create", {"tree_parent": thread, "parent": d["id"], "inline": True,
                                        "text": a.get("text") or ""})
        eva("CmfDocument.highlight_inline_comment", {"version": "text", "comment_id": thread, "data": [
            {"data-id": did, "text_node_idx": 0, "start_pos": start, "end_pos": start + len(frag)}]}, args=[d["id"]])
        return f"Inline comment on «{frag}»: thread {thread}, message {msg}"

    if name == "eva_search":
        lim = int(a.get("limit") or 20)
        r = eva("CmfFullSearch.fulltext_search", {
            "fields": ["code", "name", "cmf_modified_at", "parent", "parent_id", "logic_prefix"], "no_analitycs": True,
            "titles_only": bool(a.get("titles_only")), "archived": bool(a.get("archived")), "deleted": False,
            "slice": [0, lim], "recent_projects": []}, args=[a.get("model") or "ANY", "ANY", a["query"]], use_cache=False)
        out = []
        for x in r or []:
            if x.get("_acl_obj") == "deny" or not x.get("id"):
                continue
            cls = x.get("class_name") or x["id"].split(":")[0]
            code = x.get("code")
            url = doc_url(code) if cls == "CmfDocument" else task_url(code) if cls == "CmfTask" else ""
            par = (x.get("parent") or {}).get("name") if isinstance(x.get("parent"), dict) else ""
            out.append(f"{cls.replace('Cmf', '')}  {code}  {x.get('name')}  ({par}, {(x.get('cmf_modified_at') or '')[:10]})  {url}".rstrip())
        return "\n".join(out) or "Nothing found"

    if name == "eva_find_tasks":
        cls = a.get("class_name") or "CmfTask"
        flt, order = to_filter(a["query"], cls, with_order=True)
        lim = int(a.get("limit") or 30)
        total = eva(f"{cls}.count", {"filter": flt}, use_cache=False)
        r = eva(f"{cls}.list", {"filter": flt, "fields": TASK_ROW_FIELDS if cls == "CmfTask" else ["code", "name"],
                                "order_by": order or a.get("order_by") or ["-cmf_modified_at"], "slice": [0, lim * 2]},
                use_cache=False)
        rows_ = task_rows(r, lim) if cls == "CmfTask" else rows(r, lambda d: f"{d['code']}  {d.get('name')}", lim)
        return f"{total} found (filter {json.dumps(flt, ensure_ascii=False)[:300]})\n{rows_}"

    if name == "eva_saved_filters":
        if a.get("code"):
            f = eva("CmfBqlFilter.get", {"filter": ["code", "=", a["code"]], "fields": ["name", "ubql", "bql", "cmf_model_name"]},
                    use_cache=False)
            if not f:
                raise ValueError(f"Filter {a['code']} not found")
            bql = f.get("bql")
            flt = json.loads(bql) if isinstance(bql, str) and bql.strip().startswith("[") else (bql or to_filter(f.get("ubql")))
            cls = f.get("cmf_model_name") or "CmfTask"
            lim = int(a.get("limit") or 30)
            total = eva(f"{cls}.count", {"filter": flt}, use_cache=False)
            r = eva(f"{cls}.list", {"filter": flt, "fields": TASK_ROW_FIELDS if cls == "CmfTask" else ["code", "name"],
                                    "order_by": ["-cmf_modified_at"], "slice": [0, lim * 2]}, use_cache=False)
            return (f"«{f.get('name')}»  ubql: {f.get('ubql')}\n{total} found\n"
                    + (task_rows(r, lim) if cls == "CmfTask" else rows(r, lambda d: f"{d['code']}  {d.get('name')}", lim)))
        q = a.get("query") or ""
        r = eva("CmfBqlFilter.list", {"filter": ["OR", ["name", "ILIKE", f"%{q}%"], ["code", "ILIKE", f"%{q}%"]],
                                      "fields": ["code", "name", "ubql"], "order_by": ["-cmf_modified_at"],
                                      "limit": int(a.get("limit") or 30) * 2}, use_cache=False)
        return rows(r, lambda f: f"{f['code']}  {f.get('name')}  — {f.get('ubql') or ''}", int(a.get("limit") or 30))

    if name == "eva_my_tasks":
        view = a.get("view") or "doing"
        flt = my_tasks_filter(view, a.get("when") or "all")
        lim = int(a.get("limit") or 50)
        total = eva("CmfTask.count", {"filter": flt}, use_cache=False)
        r = eva("CmfTask.list", {"filter": flt, "fields": TASK_ROW_FIELDS, "order_by": ["alarm_date", "-cmf_modified_at"],
                                 "slice": [0, lim * 2]}, use_cache=False)
        return f"{view}: {total}\n{task_rows(r, lim)}"

    if name == "eva_notifications":
        flt = [["person_id", "==", "__G_CURRENT_USER"]]
        if not a.get("all"):
            flt.append(["status", "!=", "closed"])
        lim = int(a.get("limit") or 30)
        total = eva("CmfNotify.count", {"filter": flt}, use_cache=False)
        r = eva("CmfNotify.list", {"filter": flt, "fields": ["name", "text", "status", "cmf_created_at", "parent", "parent_id"],
                                   "order_by": ["-cmf_created_at"], "slice": [0, lim]}, use_cache=False) or []
        out = []
        for n in r:
            if n.get("_acl_obj") == "deny":
                continue
            par = n.get("parent") if isinstance(n.get("parent"), dict) else {}
            ref = par.get("code") or n.get("parent_id") or ""
            out.append(f"[{(n.get('cmf_created_at') or '')[:16]}] {n.get('status')}  {n.get('name')} — "
                       f"{html_to_md(n.get('text'))[:200]}  {ref}")
        return f"{total} {'total' if a.get('all') else 'unread'}\n" + ("\n".join(out) or "No notifications")

    if name == "eva_find_person":
        ppl = find_people(a["query"], int(a.get("limit") or 10))
        return "\n".join(f"{p.get('name')}  login: {p.get('login')}  {p.get('email') or ''}  {p['id']}" for p in ppl) \
            or "Nobody found"

    if name == "eva_boards":
        if a.get("code"):
            b = eva("CmfKanbanBoard.get", {"filter": ["code", "=", a["code"]], "fields": ["name", "kanban_bql_filter"]},
                    use_cache=False)
            if not b:
                raise ValueError(f"Board {a['code']} not found")
            cols = eva("CmfKanbanBoardColumn.list", {"filter": ["parent_id", "=", b["id"]],
                                                    "fields": ["name", "orderno", "mapped_status_codes"],
                                                    "order_by": ["orderno"]}, use_cache=False) or []
            fid = (b.get("kanban_bql_filter") or {}).get("id") if isinstance(b.get("kanban_bql_filter"), dict) else None
            f = eva("CmfBqlFilter.get", {"filter": ["id", "=", fid], "fields": ["ubql", "bql"]}, use_cache=False) if fid else {}
            bql = (f or {}).get("bql")
            flt = json.loads(bql) if isinstance(bql, str) and bql.strip().startswith("[") else (bql or [])
            per = int(a.get("cards_per_column") or 15)
            out = [f"«{b.get('name')}»  filter: {(f or {}).get('ubql')}"]
            for c in cols:
                codes = [x.get("code") if isinstance(x, dict) else x for x in (c.get("mapped_status_codes") or [])]
                cflt = [flt, ["status.code", "IN", codes]] if codes else [flt, ["cache_status_type", "=", "__none__"]]
                if not codes:  # columns without mapping in this instance: fall back to the status name
                    cflt = [flt, ["status.name", "=", c.get("name")]]
                try:
                    n = eva("CmfTask.count", {"filter": cflt}, use_cache=False)
                    r = eva("CmfTask.list", {"filter": cflt, "fields": TASK_ROW_FIELDS, "order_by": ["-cmf_modified_at"],
                                             "slice": [0, per * 2]}, use_cache=False)
                    out.append(f"\n## {c.get('name')} ({n})\n{task_rows(r, per)}")
                except Exception as e:
                    out.append(f"\n## {c.get('name')}\n(error: {e})")
            return "\n".join(out)
        conds = []
        if a.get("query"):
            conds.append(["name", "ILIKE", f"%{a['query']}%"])
        if a.get("project"):
            conds.append(["project_id", "=", resolve_project(a["project"])])
        flt = conds[0] if len(conds) == 1 else (["AND", *conds] if conds else ["parent", "==", "__G_CURRENT_USER"])
        r = eva("CmfKanbanBoard.list", {"filter": flt, "fields": ["code", "name"], "limit": 60}, use_cache=False)
        return rows(r, lambda b: f"{b['code']}  {b.get('name')}", 30)

    if name == "eva_sprints":
        lp = "list.release" if a.get("kind") == "release" else "list.agile_sprint"
        if a.get("code"):
            l = eva("CmfList.get", {"filter": ["code", "=", a["code"]], "fields": ["name", "cache_status_type"]}, use_cache=False)
            if not l:
                raise ValueError(f"{a['code']} not found")
            lim = int(a.get("limit") or 30)
            flt = ["lists", "IN", [l["id"]]]
            total = eva("CmfTask.count", {"filter": flt}, use_cache=False)
            r = eva("CmfTask.list", {"filter": flt, "fields": TASK_ROW_FIELDS, "order_by": ["-cmf_modified_at"],
                                     "slice": [0, lim * 2]}, use_cache=False)
            return f"«{l.get('name')}» [{l.get('cache_status_type')}]: {total} tasks\n{task_rows(r, lim)}"
        conds = [["logic_prefix", "=", lp]]
        if a.get("project"):
            conds.append(["project_id", "=", resolve_project(a["project"])])
        if a.get("query"):
            conds.append(["name", "ILIKE", f"%{a['query']}%"])
        if a.get("open_only", True):
            conds.append(["cache_status_type", "!=", "CLOSED"])
        r = eva("CmfList.list", {"filter": conds, "fields": ["code", "name", "cache_status_type", "plan_start_date",
                                                            "plan_end_date", "project"],
                                 "order_by": ["-cmf_created_at"], "limit": int(a.get("limit") or 30) * 2}, use_cache=False)
        return rows(r, lambda l: f"{l['code']}  {l.get('name')}  [{l.get('cache_status_type')}]  "
                                 f"{(l.get('plan_start_date') or '')[:10]}–{(l.get('plan_end_date') or '')[:10]}  "
                                 f"{((l.get('project') or {}).get('name') if isinstance(l.get('project'), dict) else '') or ''}",
                    int(a.get("limit") or 30))

    if name == "eva_task_lists":
        t = eva("CmfTask.get", {"filter": ["code", "=", a["code"]], "fields": ["lists"]}, use_cache=False)
        if not t:
            raise ValueError(f"Task {a['code']} not found")
        l = eva("CmfList.get", {"filter": ["code", "=", a["list"]], "fields": ["name"]}, use_cache=False)
        if not l:
            raise ValueError(f"List {a['list']} not found")
        cur = [x.get("id") if isinstance(x, dict) else x for x in (t.get("lists") or [])]
        new = [x for x in cur if x != l["id"]] + ([l["id"]] if (a.get("action") or "add") == "add" else [])
        eva("CmfTask.lock", {}, top={"filter": ["id", "==", t["id"]]})
        try:
            eva("CmfTask.update", {"lists": new}, args=[t["id"]])
        finally:
            eva("CmfTask.unlock", {}, top={"filter": ["id", "==", t["id"]]})
        return f"{a['code']}: {'added to' if (a.get('action') or 'add') == 'add' else 'removed from'} «{l.get('name')}»"

    if name in ("eva_list_comments", "eva_add_comment"):
        code = a["code"]
        cls = "CmfDocument" if code.upper().startswith("DOC-") else "CmfTask"
        parent = resolve_obj(cls, code)
        if name == "eva_list_comments":
            r = eva("CmfComment.list", {"filter": ["parent_id", "=", parent],
                                        "fields": ["text", "cmf_author", "cmf_created_at", "tree_parent_id"],
                                        "order_by": ["cmf_created_at"], "limit": int(a.get("limit") or 50)}, use_cache=False)
            out = []
            for c in r or []:
                if not c.get("id") or c.get("_acl_obj") == "deny":
                    continue
                who = (c.get("cmf_author") or {}).get("name") if isinstance(c.get("cmf_author"), dict) else c.get("cmf_author_id")
                reply = f" (reply to {c['tree_parent_id']})" if c.get("tree_parent_id") else ""
                out.append(f"[{(c.get('cmf_created_at') or '')[:16]}] {who}{reply} {c['id']}\n{html_to_md(c.get('text'))}")
            return "\n\n".join(out) or "No comments"
        body = a["text"] if (a.get("format") == "html") else md_to_html(a["text"])
        dummy = eva("CmfComment.create_dummy", {"parent_id": parent, "text": "", "tree_parent": a.get("reply_to"),
                                                "private": bool(a.get("private")), "perm_encrypt": False,
                                                "fields": ["id"]}, args=[])
        cid = dummy.get("id") if isinstance(dummy, dict) else dummy
        eva("CmfComment.update", {"is_dummy": False, "text": add_data_ids(body)}, args=[cid])
        return f"Comment {cid} added to {code}"

    if name == "eva_delete_comment":
        eva("CmfComment.delete", {}, args=[a["comment_id"]])
        return f"Comment {a['comment_id']} deleted"

    if name == "eva_upload_attachment":
        code, p = a["code"], os.path.expanduser(a["path"])
        if not os.path.isfile(p):
            raise ValueError(f"File not found: {p}")
        if code.upper().startswith("DOC-"):
            obj_id = find_document(code)["id"]
        else:
            t = eva("CmfTask.get", {"filter": ["code", "=", code], "fields": ["code"]}, use_cache=False)
            if not t:
                raise ValueError(f"{code} not found")
            obj_id = t["id"]
        with open(p, "rb") as f:
            url, _ = upload_file(obj_id, os.path.basename(p), f.read(), mimetypes.guess_type(p)[0] or "application/octet-stream")
        return f"{BASE}{url}"

    if name == "eva_list_documents":
        if a.get("parent"):
            p = eva("CmfDocument.get", {"filter": ["code", "=", a["parent"]], "fields": ["code"]})
            if not p:
                return "Parent not found"
            flt = ["tree_parent_id", "=", p["id"]]  # page tree lives in tree_parent, parent is the project
        elif a.get("project"):
            flt = ["tree_parent_id", "=", resolve_project(a["project"])]
        else:
            raise ValueError("Pass parent or project")
        r = eva("CmfDocument.list", {"filter": flt, "fields": ["code", "name"], "limit": lim(100) * 3})
        return rows(r, lambda d: f"{d['code']}  {d.get('name')}", lim(100))

    if name == "eva_list_projects":
        q = a.get("query") or ""
        flt = ["OR", ["name", "ILIKE", f"%{q}%"], ["code", "ILIKE", f"%{q}%"]]
        r = eva("CmfProject.list", {"filter": flt, "fields": ["code", "name"], "limit": lim(30) * 3})
        return rows(r, lambda p: f"{p['code']}  {p.get('name')}  ({p.get('id')})", lim(30))

    if name == "eva_get_task":
        fields = ["code", "name", "text", "status_id", "cache_status_type", "responsible_id", "cmf_owner_id",
                  "project_id", "parent_id", "cmf_created_at", "cmf_modified_at", "deadline"] + (a.get("fields") or [])
        r = eva("CmfTask.get", {"filter": ["code", "=", a["code"]], "fields": fields})
        if not r:
            return "Not found (or no access)"
        if r.get("text"):
            r["text"] = html_to_md(r["text"])
        r["url"] = task_url(r["code"])
        return dump(r)

    if name == "eva_search_tasks":
        conds = [["name", "ILIKE", f"%{a['query']}%"]]
        pid = resolve_project(a.get("project"))
        if pid:
            conds.append(["project_id", "=", pid])
        if a.get("status_type"):
            conds.append(["cache_status_type", "=", a["status_type"]])
        flt = conds[0] if len(conds) == 1 else ["AND", *conds]
        r = eva("CmfTask.list", {"filter": flt, "fields": ["code", "name", "cache_status_type"],
                                 "order_by": ["-cmf_modified_at"], "limit": lim(20) * 3})
        return rows(r, lambda t: f"{t['code']}  [{t.get('cache_status_type')}]  {t.get('name')}  {task_url(t['code'])}", lim(20))

    if name == "eva_list_classes":
        q = (a.get("query") or "").lower()
        names = [c for c in sorted(CATALOG) if q in c.lower()]
        return f"{len(names)} classes\n" + "\n".join(f"{c} ({len(CATALOG[c])} methods)" for c in names)

    if name == "eva_list_methods":
        cls = a["class_name"]
        if cls not in CATALOG:
            raise ValueError(f"Unknown class {cls}. Use eva_list_classes.")
        q = (a.get("query") or "").lower()
        ms = [m for m in CATALOG[cls] if q in m.lower()]
        lines = []
        for m in ms:
            e, src = proto(cls, m)
            origin = f"plugin {e['plugin']}" if e and e.get("plugin") else ("" if src == cls else src)
            ui = f"  ui{' (' + origin + ')' if origin else ''}: {proto_sig(e)}" if e else ""
            lines.append(f"[{'r' if is_read(m) else 'w'}] {cls}.{m}{ui}")
        known = sum(1 for m in ms if proto(cls, m)[0])
        return (f"{cls}: {len(ms)} methods, {known} with the UI calling convention "
                f"(id = object method, {{...}} = kwargs)\n" + "\n".join(lines))

    if name == "eva_describe_class":
        meta = eva(f"{a['class_name']}.get_meta") or {}
        q = (a.get("query") or "").lower()
        out = []
        for fname, f in (meta.get("fields") or {}).items():
            if not f.get("api_allow", True):
                continue
            cap = f.get("caption") or ""
            if q and q not in fname.lower() and q not in cap.lower():
                continue
            flags = ",".join(x for x in ("readonly" if f.get("readonly") else "",
                                         "custom" if f.get("custom") else "",
                                         "virtual" if f.get("virtual") else "") if x)
            out.append(f"{fname}  [{f.get('widget')}]  {cap}" + (f"  ({flags})" if flags else ""))
        return f"{a['class_name']}: {len(out)} fields\n" + "\n".join(out)

    if name == "eva_call":
        method = a["method"]
        cls, _, m = method.partition(".")
        if CATALOG and (cls not in CATALOG or m not in CATALOG[cls]):
            raise ValueError(f"{method} is not in the API catalog. Use eva_list_methods.")
        if is_write(m) and not a.get("allow_write"):
            raise PermissionError(f"{method} modifies data; call again with allow_write=true if the user asked for it.")
        entry, _ = proto(cls, m)
        args = list(a.get("args") or [])
        if a.get("id"):
            args = [resolve_obj(cls, a["id"])] + args
        elif entry and entry["kind"] == "object" and not args:
            raise ValueError(f"{method} is an object method {proto_sig(entry)}: pass id (object id or code)")
        # wrapper methods go through the UI helper (jsonrpc 2.2 + args); plain ORM calls carry args only if given
        generic_orm = m in ("create", "get", "list", "count", "search")
        send_args = args if (args or (entry and not generic_orm)) else None
        res = eva(method, a.get("kwargs"), send_args, use_cache=is_read(m), top=a.get("top"),
                  endpoint=a.get("endpoint") or "api")
        if isinstance(res, list):
            hidden = sum(1 for x in res if isinstance(x, dict) and x.get("_acl_obj") == "deny")
            if hidden:
                res = [x for x in res if not (isinstance(x, dict) and x.get("_acl_obj") == "deny")]
                return dump(res) + f"\n({hidden} rows hidden: no access)"
        return dump(res)

    if name == "eva_whoami":
        r = eva("CmfPerson.get_current_user", use_cache=False)
        if isinstance(r, dict):
            r = {k: r.get(k) for k in ("id", "name", "login", "email") if k in r}
        return dump(r)

    if name == "eva_refresh_catalog":
        new = refresh_catalog()
        CATALOG.clear()
        CATALOG.update(new)
        return f"Catalog updated: {len(CATALOG)} classes, {sum(map(len, CATALOG.values()))} methods"

    raise ValueError(f"unknown tool {name}")


# ---------- MCP stdio loop ----------

def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            res = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "eva", "version": VERSION}}
        elif method == "tools/list":
            res = {"tools": visible_tools()}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                res = {"content": [{"type": "text", "text": run_tool(p.get("name"), p.get("arguments") or {})}]}
            except Exception as e:
                res = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        elif method == "ping":
            res = {}
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": res})


if __name__ == "__main__":
    if "--refresh-catalog" in sys.argv:
        c = refresh_catalog()
        print(f"{len(c)} classes, {sum(map(len, c.values()))} methods")
    elif "--refresh-protocol" in sys.argv:
        page = _request("/api/docs", timeout=60)  # any unknown /api/ path returns the SPA shell
        js = re.search(r'src="/?(main\.[0-9a-f]+\.js)"', page).group(1)
        # the UI loads /plugins/<name>/static/script.js for every enabled plugin; scripts may load more files
        queue = [f"plugins/{n}/static/script.js" for n in eva("CmfGlobalSettings.enabled_plugin_nclist", {},
                                                                use_cache=False) or []]
        tmpdir = os.path.join(HERE, ".protocol-src")
        os.makedirs(tmpdir, exist_ok=True)
        files, seen = [], set()
        queue.insert(0, js)
        while queue:
            rel = queue.pop(0)
            if rel in seen:
                continue
            seen.add(rel)
            try:
                body = _request("/" + rel, timeout=300)
            except Exception:
                continue
            if body.lstrip().lower().startswith("<!doctype"):
                continue  # not a script (SPA fallback)
            path = os.path.join(tmpdir, rel.replace("/", "_"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            files.append(path)
            if rel.startswith("plugins/"):
                here_dir = rel.rsplit("/", 1)[0] + "/"
                bases = re.findall(r"""['"]/(plugins/[\w-]+/static/)['"]""", body) or [here_dir]
                refs = set(re.findall(r"""['"]/(plugins/[\w-]+/static/[\w.-]+\.js)""", body))
                for name in re.findall(r"""['"]([\w-]+\.js)(?:\?|['"])""", body):
                    refs.update(b + name for b in bases)
                queue.extend(sorted(refs - seen))
        plugins = [f for f in files if "plugins_" in os.path.basename(f)]
        out = subprocess.run([sys.executable, "-I", os.path.join(HERE, "tools", "extract_protocol.py"), *files],
                             capture_output=True, text=True, check=True).stdout
        for path in files:
            os.remove(path)
        os.rmdir(tmpdir)
        with open(PROTOCOL_PATH, "w", encoding="utf-8") as f:
            f.write(out)
        p = json.loads(out)
        n_pl = sum(1 for ms in p.values() for e in ms.values() if "plugin" in e)
        print(f"protocol.json: {len(p)} classes, {sum(map(len, p.values()))} methods "
              f"({n_pl} from {len(plugins)} plugin scripts) from {js}")
    elif "--check" in sys.argv:
        try:
            print(f"EVA: {base_url()}")
        except RuntimeError as e:
            sys.exit(f"Error: {e}")
        print(f"TLS: {'NOT verified (EVA_INSECURE=1)' if INSECURE else 'verified with ' + (CA_BUNDLE or 'system trust store')}")
        print(f"Mode: {'READ-ONLY (EVA_READONLY=1)' if READONLY else 'read/write'}; audit log: {AUDIT_PATH}")
        print(run_tool("eva_whoami", {}))
    else:
        main()
