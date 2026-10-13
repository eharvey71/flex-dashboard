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
            author = (pr.get("user") or {}).get("login")
            out.append({
                "source": "github",
                "kind": kind,                       # review = blocking someone else
                "key": "%s/%s #%d" % (repo[-2], repo[-1], pr["number"]),
                "title": pr.get("title") or "",
                "author": author,
                # Whose PR this is, stated rather than implied. Without it a
                # review request and his own open PR look identical on the page.
                "mine": author == login,
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


MERGE_NOTES = {
    "clean":    "ready to merge",
    "dirty":    "merge conflicts",
    "behind":   "branch is behind base",
    "blocked":  "blocked - required reviews or checks not met",
    # "unstable" means only NON-required checks are failing - GitHub still
    # allows the merge. Reporting it as failing produced false alarms, so it
    # is treated as mergeable. A failing REQUIRED check shows as "blocked".
    "unstable": "ready to merge",
    "draft":    "draft",
    "has_hooks": "ready to merge",
}


def pr_details(cfg, prs):
    """What has ACTUALLY happened on his own open PRs.

    `requested_reviewers` only says who was asked - often auto-assigned by
    CODEOWNERS - and says nothing about whether anyone looked. Reading it as
    "waiting on X's review" put people's names on the page who had never
    touched the PR. So for each PR he wrote, read the submitted reviews and
    GitHub's merge state instead. Two calls per PR, his only.

    Adds `review` to each of his PRs in place:
      approved_by / changes_requested_by / commented_by - latest review per
        person, excluding his own; a dismissed review counts for nothing
      merge_state / merge_note - GitHub's mergeable_state, in plain words.
        "dirty" means conflicts; non-required check failures are ignored.
    Any call that fails leaves that part as None rather than guessing.
    """
    h = _auth()
    me = cfg["github"]["login"]
    for pr in prs:
        if pr.get("author") != me:
            continue
        base = "%s/repos/%s/pulls/%d" % (API, pr["repo"], pr["number"])

        try:
            reviews = httpjson.get_all_pages(base + "/reviews", h, limit=2)
        except Exception as ex:
            print("  note: reviews unavailable for #%d (%s)" % (pr["number"], ex),
                  file=sys.stderr)
            reviews = None

        latest = {}
        for r in reviews or []:           # returned oldest first
            who = (r.get("user") or {}).get("login")
            st = r.get("state")
            if not who or who == me or st == "PENDING":
                continue
            if st == "DISMISSED":
                latest.pop(who, None)
            elif st == "COMMENTED" and latest.get(who) in ("APPROVED", "CHANGES_REQUESTED"):
                continue                  # a later comment does not undo a verdict
            else:
                latest[who] = st

        try:
            detail = httpjson.request(base, headers=h)
            state = detail.get("mergeable_state")
        except Exception:
            state = None

        pick = lambda s: sorted(u for u, v in latest.items() if v == s)
        pr["review"] = {
            "reviews_known": reviews is not None,
            "approved_by": pick("APPROVED"),
            "changes_requested_by": pick("CHANGES_REQUESTED"),
            "commented_by": pick("COMMENTED"),
            "merge_state": state,
            "merge_note": MERGE_NOTES.get(state),
        }
    return prs
