"""Jira Cloud REST v3. Basic auth: account email + API token."""

import base64, os, sys, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import creds, httpjson

FIELDS = "summary,status,priority,duedate,issuetype,parent,updated,labels,assignee"


def _auth(cfg):
    token = creds.get("jira_api_token")
    pair = "%s:%s" % (cfg["jira"]["account_email"], token)
    return {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}


def _search(cfg, jql, max_results):
    """Board order is Jira's Rank field. Fall back if the instance rejects it."""
    url = "https://%s/rest/api/3/search/jql" % cfg["jira"]["site"]
    body = {"jql": jql, "maxResults": max_results, "fields": FIELDS.split(",")}
    try:
        return httpjson.request(url, headers=_auth(cfg), data=body), True
    except httpjson.HttpError as ex:
        if ex.status == 400 and "rank" in (ex.body or "").lower():
            body["jql"] = jql.split(" ORDER BY ")[0] + " ORDER BY duedate ASC, priority DESC"
            return httpjson.request(url, headers=_auth(cfg), data=body), False
        raise


def fetch(cfg):
    j = cfg["jira"]
    base = j["jql"].split(" ORDER BY ")[0]
    data, ranked = _search(cfg, base + " ORDER BY Rank ASC", j.get("max_issues", 40))

    issues = []
    for it in data.get("issues", []):
        f = it.get("fields") or {}
        status = (f.get("status") or {})
        issues.append({
            "source": "jira",
            "key": it.get("key"),
            "title": f.get("summary") or "",
            "status": status.get("name"),
            "status_category": ((status.get("statusCategory") or {}).get("name")),
            "priority": ((f.get("priority") or {}).get("name")),
            "type": ((f.get("issuetype") or {}).get("name")),
            "due": f.get("duedate"),
            "updated": f.get("updated"),
            "labels": f.get("labels") or [],
            "epic": ((f.get("parent") or {}).get("fields") or {}).get("summary"),
            "url": "https://%s/browse/%s" % (j["site"], it.get("key")),
        })

    # Mark the single issue at the top of the board's To Do column.
    # Board order comes from the Agile API, which applies the board filter and
    # sprint scope - a JQL search ordered by Rank does not and gets this wrong.
    # Deliberately deterministic: the model is told which one, it does not pick.
    todo_names = [n.lower() for n in j.get("todo_status_names", ["to do"])]
    order = None
    try:
        order = board_issues(cfg)
    except httpjson.HttpError as ex:
        print("  note: board order unavailable (%s); falling back to JQL rank"
              % ex.status, file=sys.stderr)

    pos = {k: i for i, k in enumerate(order or [])}
    for i, issue in enumerate(issues):
        issue["in_sprint"] = issue["key"] in pos if order else None
        # Off-board issues get no rank at all, so they sort last. Falling back
        # to the JQL index here would collide with real board positions.
        issue["board_rank"] = pos.get(issue["key"]) if order else (i if ranked else None)
        issue["top_of_todo"] = False

    ordered = ([k for k in order if k in {i["key"] for i in issues}] if order
               else [i["key"] for i in issues] if ranked else [])
    by_key = {i["key"]: i for i in issues}
    for key in ordered:
        if (by_key[key].get("status") or "").lower() in todo_names:
            by_key[key]["top_of_todo"] = True
            break

    issues.sort(key=lambda x: (x["board_rank"] is None, x["board_rank"] or 0))
    return issues


def check(cfg):
    url = "https://%s/rest/api/3/myself" % cfg["jira"]["site"]
    me = httpjson.request(url, headers=_auth(cfg))
    return "Jira OK - %s (%s)" % (me.get("displayName"), me.get("emailAddress"))


# ---------- agile board (the real Kanban order) ----------
#
# A JQL search ordered by Rank is NOT the board's column order: the board
# applies its own filter and sprint scope first. Only the Agile API knows what
# actually sits at the top of the To Do column.

AGILE = "/rest/agile/1.0"


def list_boards(cfg):
    j = cfg["jira"]
    url = "https://%s%s/board" % (j["site"], AGILE)
    if j.get("project_key"):
        url += "?projectKeyOrId=%s" % j["project_key"]
    boards = httpjson.request(url, headers=_auth(cfg)).get("values", [])

    out = []
    for b in boards:
        sprint = None
        if b.get("type") == "scrum":
            try:
                sp = httpjson.request("https://%s%s/board/%d/sprint?state=active"
                                      % (j["site"], AGILE, b["id"]), headers=_auth(cfg))
                vals = sp.get("values", [])
                sprint = vals[0] if vals else None
            except httpjson.HttpError:
                pass
        out.append({"id": b["id"], "name": b.get("name"), "type": b.get("type"),
                    "sprint_id": sprint["id"] if sprint else None,
                    "sprint_name": sprint["name"] if sprint else None})
    return out


def board_issues(cfg):
    """Issues assigned to him, in board rank order, scoped to the board.

    Uses the active sprint when the board has one, otherwise the board itself.
    """
    j = cfg["jira"]
    board_id = j.get("board_id")
    if not board_id:
        return None

    scope = "board/%d" % board_id
    if j.get("sprint_id"):
        scope = "sprint/%d" % j["sprint_id"]
    elif j.get("use_active_sprint", True):
        try:
            sp = httpjson.request("https://%s%s/board/%d/sprint?state=active"
                                  % (j["site"], AGILE, board_id), headers=_auth(cfg))
            vals = sp.get("values", [])
            if vals:
                scope = "sprint/%d" % vals[0]["id"]
        except httpjson.HttpError:
            pass

    url = ("https://%s%s/%s/issue?jql=%s&fields=%s&maxResults=100"
           % (j["site"], AGILE, scope,
              urllib.parse.quote("assignee = currentUser() AND statusCategory != Done"),
              urllib.parse.quote(FIELDS)))
    data = httpjson.request(url, headers=_auth(cfg))
    return [it.get("key") for it in data.get("issues", [])]
