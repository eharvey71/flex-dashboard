"""GitHub REST. Bearer auth with a fine-grained or classic PAT."""

import datetime, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import creds, httpjson

API = "https://api.github.com"


def _auth():
    return {"Authorization": "Bearer " + creds.get("github_token"),
            "X-GitHub-Api-Version": "2022-11-28"}


def _age_days(iso):
    if not iso:
        return None
    then = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    return (datetime.datetime.utcnow() - then).days


def _search(query, headers):
    data = httpjson.request("%s/search/issues?q=%s&per_page=50"
                            % (API, query.replace(" ", "+")), headers=headers)
    return data.get("items", [])


def fetch(cfg):
    g = cfg["github"]
    h = _auth()
    login = g["login"]
    scope = (" org:%s" % g["org"]) if g.get("org") else ""

    seen, out = set(), []
    queries = []
    if g.get("include_review_requests", True):
        queries.append(("review", "is:open is:pr review-requested:%s%s" % (login, scope)))
    if g.get("include_assigned_prs", True):
        queries.append(("mine", "is:open is:pr author:%s%s" % (login, scope)))

    for kind, q in queries:
        for pr in _search(q, h):
            if pr["html_url"] in seen:
                continue
            seen.add(pr["html_url"])
            repo = pr["repository_url"].rsplit("/", 2)
            out.append({
                "source": "github",
                "kind": kind,                       # review = blocking someone else
                "key": "%s/%s #%d" % (repo[-2], repo[-1], pr["number"]),
                "title": pr.get("title") or "",
                "draft": pr.get("draft", False),
                "age_days": _age_days(pr.get("created_at")),
                "idle_days": _age_days(pr.get("updated_at")),
                "url": pr["html_url"],
            })
    return out


def check(cfg):
    me = httpjson.request("%s/user" % API, headers=_auth())
    return "GitHub OK - %s" % me.get("login")


def repo_state(cfg):
    """Open PRs (with head branch refs) and branch names for the watched repos.

    The /search/issues endpoint used above does not return a PR's head ref,
    which is where ticket keys usually live. This pulls the real thing.
    """
    h = _auth()
    g = cfg["github"]
    org = g.get("org")
    prs, branches = [], []

    for name in g.get("watch_repos") or []:
        full = name if "/" in name else "%s/%s" % (org, name)

        for pr in httpjson.get_all_pages("%s/repos/%s/pulls?state=open" % (API, full), h):
            prs.append({
                "repo": full,
                "number": pr["number"],
                "title": pr.get("title") or "",
                "body": (pr.get("body") or "")[:600],
                "head": (pr.get("head") or {}).get("ref", ""),
                "author": ((pr.get("user") or {}).get("login")),
                "draft": pr.get("draft", False),
                "age_days": _age_days(pr.get("created_at")),
                "idle_days": _age_days(pr.get("updated_at")),
                "reviewers": [r.get("login") for r in (pr.get("requested_reviewers") or [])],
                "url": pr.get("html_url"),
            })

        # Branches need Contents:read, which PR-only tokens lack. Degrade
        # rather than fail: without branches the correlation still works, it
        # just cannot tell "branch exists, no PR yet" from "nothing started".
        try:
            for b in httpjson.get_all_pages("%s/repos/%s/branches" % (API, full), h):
                branches.append({"repo": full, "name": b.get("name", "")})
        except httpjson.HttpError as e:
            if e.status == 403:
                print("  note: no branch access on %s (needs Contents:read) - "
                      "branch signals disabled" % full, file=sys.stderr)
            else:
                raise

    return prs, branches
