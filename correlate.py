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


# Most actionable first: the state that decides what he does next.
PR_PRIORITY = ["merge_conflict", "changes_requested", "approved", "draft",
               "commented", "no_review_yet", "pr_open"]


def pr_signal(pr):
    """One word for where a PR really stands, from what happened on it.

    Built from submitted reviews and GitHub's merge state (github.pr_details),
    never from who was merely requested. A PR with no review data - someone
    else's PR on his ticket, or a failed fetch - stays the neutral "pr_open".
    """
    if pr.get("draft"):
        return "draft"
    rv = pr.get("review")
    if not rv:
        return "pr_open"
    if rv.get("merge_state") == "dirty":
        return "merge_conflict"
    if rv.get("changes_requested_by"):
        return "changes_requested"
    if rv.get("approved_by"):
        return "approved"
    if rv.get("commented_by"):
        return "commented"
    if rv.get("reviews_known"):
        return "no_review_yet"
    return "pr_open"


def _worst(signals):
    for s in PR_PRIORITY:
        if s in signals:
            return s
    return "pr_open"


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

        if cat == "done" and my_prs:
            signal = "done_but_pr_open"
        elif my_prs:
            signal = _worst([pr_signal(p) for p in my_prs])
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
            # author/mine must survive the join: the page needs to say whose
            # code each line is, and dropping them here made that impossible.
            # `review` is what actually happened on the PR. The requested-
            # reviewer list is deliberately NOT passed on: it records who was
            # asked, often by CODEOWNERS, and was being read as who is reviewing.
            "prs": [{"repo": p["repo"], "number": p["number"], "draft": p["draft"],
                     "idle_days": p["idle_days"], "author": p.get("author"),
                     "pr_signal": pr_signal(p), "review": p.get("review"),
                     "url": p["url"]} for p in my_prs],
            "branches": my_brs,
        })

    # "Untracked work" means a PR carrying no ticket reference at all - and only
    # one HE WROTE. A PR naming a colleague's ticket is tracked, just not on
    # his board; a stranger's PR is not his problem either. Being a requested
    # reviewer used to qualify here; it no longer does - he does not want the
    # incoming review queue on this page at all.
    orphans = [{"repo": pr["repo"], "number": pr["number"], "title": pr["title"],
                "head": pr["head"], "author": pr["author"], "draft": pr["draft"],
                "idle_days": pr["idle_days"], "url": pr["url"],
                "pr_signal": pr_signal(pr), "review": pr.get("review")}
               for pr, ks in pr_keys
               if not ks and (me is None or pr["author"] == me)]

    order = {"claimed_but_nothing_exists": 0, "changes_requested": 1,
             "merge_conflict": 2, "approved": 3, "done_but_pr_open": 4,
             "draft": 5, "no_review_yet": 6, "commented": 7,
             "branch_only": 8, "pr_open": 9}
    links.sort(key=lambda l: (order.get(l["signal"], 9), l["key"]))

    return {"links": links, "unlinked_prs": orphans}
