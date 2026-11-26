"""Git in EVA: the built-in git hosting (CmfEvagit*) and the external git integration (CmfGit*, CmfPluginGit).

Calls follow the web UI wrappers in the frontend bundle:
  CmfEvagitRepo.fetch_branches(id) / fetch_repo_tree(id, branch, folder) / fetch_file_content(id, branch, path)
  fetch_commits(id, branch, limit, offset) / fetch_commit(id, sha) / fetch_commit_diff(id, sha_from, sha_to)
  fetch_unmerged_commits(id, branch_from, branch_to) / create_branch(id, from_branch, name) / delete_branch(id, name)
  CmfEvagitMergeRequest.merge(id) / close(id) / fetch_conflicts(id)
  CmfPluginGit.sync(id) / sync_repo_list(id)  — refresh data of the external integration
All object methods go with args=[id, ...] and jsonrpc 2.2 (server.eva does that when args are given).
"""
import json

READ_ACTIONS = ("repos", "branches", "tree", "file", "commits", "commit", "diff", "unmerged", "mrs", "mr",
                "conflicts", "linked")
WRITE_ACTIONS = ("create_branch", "delete_branch", "merge", "close_mr", "sync")

TOOLS_GIT = [
    {"name": "eva_git",
     "description": "Git in EVA, like the UI repository pages. Read: repos (built-in CmfEvagitRepo and external CmfGitRepo), "
                    "branches, tree (folder), file (path), commits (branch), commit (sha), diff (sha_from..sha_to), "
                    "unmerged (branch_from -> branch_to), mrs / mr (merge requests), conflicts (mr), linked (commits, branches, "
                    "MRs linked to a task code). Write (use only when the user asked; not verified on live EVA): "
                    "create_branch (from_branch, name), delete_branch (name), merge / close_mr (mr), sync (refresh external "
                    "integration).",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": list(READ_ACTIONS + WRITE_ACTIONS)},
         "repo": {"type": "string", "description": "repo code (EGR-…/GRP-…), id or url path like group/name"},
         "branch": {"type": "string", "default": "master"}, "path": {"type": "string", "default": ""},
         "sha": {"type": "string"}, "sha_from": {"type": "string"}, "sha_to": {"type": "string"},
         "branch_from": {"type": "string"}, "branch_to": {"type": "string"},
         "from_branch": {"type": "string"}, "name": {"type": "string"},
         "mr": {"type": "string", "description": "merge request code or id"}, "task": {"type": "string"},
         "query": {"type": "string"}, "limit": {"type": "integer", "default": 30}, "offset": {"type": "integer", "default": 0}},
         "required": ["action"]}},
]


def is_write(args):
    return (args or {}).get("action") in WRITE_ACTIONS


def _repo(s, ref):
    if not ref:
        raise ValueError("Pass repo (code, id or group/name)")
    if ref.startswith(("CmfEvagitRepo:", "CmfGitRepo:")):
        return ref
    for cls in ("CmfEvagitRepo", "CmfGitRepo"):
        r = s.eva(f"{cls}.list", {"filter": ["OR", ["code", "=", ref], ["url", "=", ref], ["name", "=", ref]],
                                  "fields": ["code"], "slice": [0, 2]}, use_cache=False) or []
        r = [x for x in r if x.get("id") and x.get("_acl_obj") != "deny"]
        if r:
            return r[0]["id"]
    raise ValueError(f"Repository {ref} not found (or no access)")


def _mr(s, ref):
    if ref and ref.startswith(("CmfEvagitMergeRequest:", "CmfGitMergeRequest:")):
        return ref
    for cls in ("CmfEvagitMergeRequest", "CmfGitMergeRequest"):
        r = s.eva(f"{cls}.get", {"filter": ["code", "=", ref], "fields": ["code"]}, use_cache=False)
        if r and r.get("id"):
            return r["id"]
    raise ValueError(f"Merge request {ref} not found")


def _evagit(repo_id):
    if not repo_id.startswith("CmfEvagitRepo:"):
        raise ValueError("This action works with the built-in EVA git (CmfEvagitRepo). External repos (CmfGitRepo) "
                         "only have synced branches/commits/MRs: use action=linked or eva_call on CmfGitBranch/CmfGitCommit.")
    return repo_id


def _fmt(x):
    return json.dumps(x, ensure_ascii=False, indent=1, default=str)[:60000]


def run(name, a):
    if name != "eva_git":
        return None
    import server as s
    act = a.get("action")
    lim = int(a.get("limit") or 30)
    if act in WRITE_ACTIONS and s.READONLY:
        raise s.ReadOnlyError(f"EVA_READONLY is on: eva_git action={act} is disabled")

    if act == "repos":
        q = a.get("query") or ""
        out = []
        for cls, kind in (("CmfEvagitRepo", "EVA git"), ("CmfGitRepo", "external")):
            flt = ["OR", ["name", "ILIKE", f"%{q}%"], ["code", "ILIKE", f"%{q}%"]] if q else ["id", "!=", None]
            r = s.eva(f"{cls}.list", {"filter": flt, "fields": ["code", "name", "url"], "slice": [0, lim]},
                      use_cache=False) or []
            out += [f"{x.get('code')}  {x.get('name')}  ({kind}{', ' + x['url'] if x.get('url') else ''})"
                    for x in r if x.get("code") and x.get("_acl_obj") != "deny"]
        return "\n".join(out) or "No repositories visible"

    if act == "linked":
        t = s.eva("CmfTask.get", {"filter": ["code", "=", a["task"]], "fields": ["code"]}, use_cache=False)
        if not t:
            raise ValueError(f"Task {a.get('task')} not found")
        out = []
        for cls in ("CmfGitBranch", "CmfGitCommit", "CmfGitMergeRequest", "CmfEvagitBranch", "CmfEvagitCommit",
                    "CmfEvagitMergeRequest"):
            try:
                r = s.eva(f"{cls}.list", {"filter": ["tasks", "IN", [t["id"]]], "fields": ["code", "name"],
                                          "slice": [0, lim]}, use_cache=False) or []
            except Exception:
                continue
            out += [f"{cls.replace('Cmf', '')}  {x.get('code')}  {x.get('name')}" for x in r
                    if x.get("id") and x.get("_acl_obj") != "deny"]
        return "\n".join(out) or f"Nothing linked to {a['task']}"

    if act in ("mrs", "mr", "conflicts", "merge", "close_mr"):
        if act == "mrs":
            out = []
            rid = _repo(s, a["repo"]) if a.get("repo") else None
            for cls in ("CmfEvagitMergeRequest", "CmfGitMergeRequest"):
                flt = ["parent_id", "=", rid] if rid else ["id", "!=", None]
                r = s.eva(f"{cls}.list", {"filter": flt, "fields": ["code", "name", "cache_status_type"],
                                          "order_by": ["-cmf_created_at"], "slice": [0, lim]}, use_cache=False) or []
                out += [f"{x.get('code')}  [{x.get('cache_status_type')}]  {x.get('name')}" for x in r
                        if x.get("code") and x.get("_acl_obj") != "deny"]
            return "\n".join(out) or "No merge requests"
        mid = _mr(s, a.get("mr"))
        cls = mid.split(":")[0]
        if act == "mr":
            return _fmt(s.eva(f"{cls}.get", {"filter": ["id", "=", mid], "fields": []}, use_cache=False))
        method = {"conflicts": "fetch_conflicts", "merge": "merge", "close_mr": "close"}[act]
        return _fmt(s.eva(f"CmfEvagitMergeRequest.{method}", {}, args=[mid]))

    if act == "sync":
        r = s.eva("CmfPluginGit.list", {"filter": ["id", "!=", None], "fields": ["code"]}, use_cache=False) or []
        if not r:
            raise ValueError("No git integration (CmfPluginGit) configured")
        pid = r[0]["id"]
        res = s.eva("CmfPluginGit.sync", {}, args=[pid])
        s.eva("CmfPluginGit.sync_repo_list", {}, args=[pid])
        return f"Sync started for {pid}\n" + _fmt(res)

    rid = _evagit(_repo(s, a.get("repo")))
    branch = a.get("branch") or "master"
    if act == "branches":
        return _fmt(s.eva("CmfEvagitRepo.fetch_branches", {}, args=[rid]))
    if act == "tree":
        return _fmt(s.eva("CmfEvagitRepo.fetch_repo_tree", {}, args=[rid, branch, a.get("path") or ""]))
    if act == "file":
        res = s.eva("CmfEvagitRepo.fetch_file_content", {}, args=[rid, branch, a.get("path") or ""])
        return res if isinstance(res, str) else _fmt(res)
    if act == "commits":
        return _fmt(s.eva("CmfEvagitRepo.fetch_commits", {}, args=[rid, branch, lim, int(a.get("offset") or 0)]))
    if act == "commit":
        return _fmt(s.eva("CmfEvagitRepo.fetch_commit", {}, args=[rid, a["sha"]]))
    if act == "diff":
        return _fmt(s.eva("CmfEvagitRepo.fetch_commit_diff", {}, args=[rid, a["sha_from"], a["sha_to"]]))
    if act == "unmerged":
        return _fmt(s.eva("CmfEvagitRepo.fetch_unmerged_commits", {}, args=[rid, a["branch_from"], a["branch_to"]]))
    if act == "create_branch":
        return _fmt(s.eva("CmfEvagitRepo.create_branch", {}, args=[rid, a.get("from_branch") or branch, a["name"]]))
    if act == "delete_branch":
        return _fmt(s.eva("CmfEvagitRepo.delete_branch", {}, args=[rid, a["name"]]))
    raise ValueError(f"Unknown action {act}")
