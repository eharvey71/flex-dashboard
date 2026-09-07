"""Todoist via the REST API.

Chosen over Evernote and Google Keep because it needs only a personal API
token: no OAuth, no app registration, no admin approval, no expiry. Two clicks
in Settings -> Integrations -> Developer.
"""

import datetime, os, sys, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import creds, httpjson

API = "https://api.todoist.com/api/v1"   # v2 was retired; returns 410

# Todoist priority is inverted: 4 is urgent, 1 is none.
PRIORITY = {4: "urgent", 3: "high", 2: "medium", 1: None}


def _auth(cfg):
    return {"Authorization": "Bearer " + creds.get(
        cfg.get("keychain_key", "todoist_api_token"))}


def _all(url, headers, limit=10):
    """The v1 API paginates: {"results": [...], "next_cursor": "..."}. Older
    endpoints returned a bare list, so tolerate both."""
    out, cursor = [], None
    for _ in range(limit):
        u = url
        if cursor:
            u += ("&" if "?" in u else "?") + "cursor=" + urllib.parse.quote(cursor)
        data = httpjson.request(u, headers=headers)
        if isinstance(data, list):
            return data
        out.extend(data.get("results") or [])
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return out


def _projects(cfg, headers):
    return {p["id"]: p.get("name", "")
            for p in _all("%s/projects" % API, headers)}


def fetch(cfg, target_day):
    headers = _auth(cfg)
    names = _projects(cfg, headers)

    only = set(cfg.get("only_projects") or [])
    skip = set(cfg.get("skip_projects") or [])

    out = []
    for t in _all("%s/tasks" % API, headers):
        project = names.get(t.get("project_id"), "")
        if skip and project in skip:
            continue
        if only and project not in only:
            continue

        due = (t.get("due") or {})
        due_date = (due.get("date") or "")[:10] or None
        out.append({
            "source": "todoist",
            "project": project,
            "title": (t.get("content") or "").strip(),
            "notes": (t.get("description") or "").strip()[:300] or None,
            "due": due_date,
            "due_text": due.get("string"),
            "recurring": bool(due.get("is_recurring")),
            "overdue": bool(due_date and due_date < target_day.isoformat()),
            "due_today": due_date == target_day.isoformat(),
            "priority": PRIORITY.get(t.get("priority")),
            "labels": t.get("labels") or [],
            "url": t.get("url"),
        })

    out.sort(key=lambda x: (x["due"] is None, x["due"] or ""))
    return out


def check(cfg):
    headers = _auth(cfg)
    tasks = _all("%s/tasks" % API, headers)
    projects = _projects(cfg, headers)
    return "Todoist OK - %d open tasks across %d projects (%s)" % (
        len(tasks), len(projects), ", ".join(sorted(projects.values())[:6]))
