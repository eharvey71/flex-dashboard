"""Join Jira issues to the PRs and branches that implement them.

Neither system knows about the other, but the ticket key is almost always
written into the branch name, the PR title, or the PR body. Matching on that
turns two flat lists into something that can answer the question the board
cannot: is this ticket actually moving, and if not, who is it waiting on?
"""

import re

KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


def _keys(*texts):
    found = set()
    for t in texts:
        if t:
            found.update(KEY.findall(t))
    return found


def build(jira_issues, prs, branches, stale_days=2, me=None):
    by_key = {i["key"]: i for i in jira_issues if i.get("key")}
    known = set(by_key)

    pr_keys = [(pr, _keys(pr["head"], pr["title"], pr["body"])) for pr in prs]
    br_keys = [(b, _keys(b["name"])) for b in branches]

    links, matched_prs = [], set()
    for key, issue in by_key.items():
        my_prs = [pr for pr, ks in pr_keys if key in ks]
        my_brs = [b["name"] for b, ks in br_keys if key in ks]
        for pr in my_prs:
            matched_prs.add((pr["repo"], pr["number"]))

        cat = (issue.get("status_category") or "").lower()
        idle = max([p["idle_days"] or 0 for p in my_prs], default=None)

        if cat == "done" and my_prs:
            signal = "done_but_pr_open"
        elif my_prs and idle is not None and idle >= stale_days:
            signal = "waiting_on_review"
        elif my_prs:
            signal = "pr_open"
        elif my_brs:
            signal = "branch_only"
        elif "progress" in cat:
            signal = "claimed_but_nothing_exists"
        else:
            signal = "not_started"

        if signal in ("not_started",) and not my_prs and not my_brs:
            continue  # a plain backlog ticket is not a correlation finding

        links.append({
            "key": key,
            "title": issue.get("title"),
            "status": issue.get("status"),
            "signal": signal,
            "prs": [{"repo": p["repo"], "number": p["number"], "draft": p["draft"],
                     "idle_days": p["idle_days"], "reviewers": p["reviewers"],
                     "url": p["url"]} for p in my_prs],
            "branches": my_brs,
        })

    # "Untracked work" means a PR carrying no ticket reference at all - and only
    # one that is his. A PR naming a colleague's ticket is tracked, just not on
    # his board; a stranger's PR is not his problem either.
    orphans = [{"repo": pr["repo"], "number": pr["number"], "title": pr["title"],
                "head": pr["head"], "author": pr["author"], "draft": pr["draft"],
                "idle_days": pr["idle_days"], "url": pr["url"]}
               for pr, ks in pr_keys
               if not ks and (me is None or pr["author"] == me
                              or me in (pr.get("reviewers") or []))]

    order = {"claimed_but_nothing_exists": 0, "waiting_on_review": 1,
             "done_but_pr_open": 2, "branch_only": 3, "pr_open": 4}
    links.sort(key=lambda l: (order.get(l["signal"], 9), l["key"]))

    return {"links": links, "unlinked_prs": orphans}
