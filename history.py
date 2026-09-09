"""What the previous days' bundles say that today's cannot.

Every source reports what is currently open. That makes the system blind to
change: it cannot tell a task raised this morning from one that has sat there
for a fortnight, and it cannot see that something got done. Both facts change
what belongs on the page.

The rule here is to hand the model computed FACTS, not raw history and not its
own previous conclusions. Feeding back yesterday's output invites it to confirm
yesterday's mistakes; feeding back yesterday's data doubles the prompt for
little gain. So this walks the stored bundles and emits a small digest.
"""

import datetime, glob, json, os, re

import config

# How an item is recognised as "the same thing" across days.
def _identity(item, kind):
    if kind == "jira":
        return "jira:%s" % item.get("key")
    if kind == "github":
        return "pr:%s" % item.get("key")
    if kind in ("reminders", "todoist", "tasks"):
        return "%s:%s" % (kind, (item.get("title") or "").strip().lower())
    if kind == "mail":
        return "mail:%s|%s" % ((item.get("from_addr") or "").lower(),
                               (item.get("subject") or "").strip().lower()[:80])
    return None


TRACKED = ("jira", "github", "reminders", "todoist", "tasks", "mail")


def _index(bundle):
    seen = {}
    for kind in TRACKED:
        for item in bundle.get(kind) or []:
            key = _identity(item, kind)
            if key:
                seen[key] = {"kind": kind,
                             "label": item.get("key") or item.get("title")
                                      or item.get("subject")}
    return seen


def _load_previous(cfg, target, back=14):
    """Bundles for the days before the target, newest first."""
    d = config.path(cfg.get("output", {}).get("data_dir", "data"))
    out = []
    for n in range(1, back + 1):
        day = (target - datetime.timedelta(days=n)).isoformat()
        path = os.path.join(d, "%s.json" % day)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                out.append((day, json.load(f)))
        except (ValueError, OSError):
            continue
    return out


def build(bundle, cfg, target):
    prior = _load_previous(cfg, target)
    if not prior:
        return {"available": False}

    today = _index(bundle)
    yesterday_day, yesterday = prior[0]
    prev = _index(yesterday)

    # Consecutive days an item has been in the input, today included.
    runs = {}
    for key in today:
        n = 1
        for _, older in prior:
            if key in _index(older):
                n += 1
            else:
                break
        if n > 1:
            runs[key] = n

    # A source returning nothing where it returned plenty is a failed fetch,
    # not a day in which everything got completed. Guard before inferring.
    suspect = set()
    for kind in TRACKED:
        now, before = len(bundle.get(kind) or []), len(yesterday.get(kind) or [])
        if before >= 3 and now == 0:
            suspect.add(kind)

    gone = []
    for key, meta in prev.items():
        if key in today or meta["kind"] in suspect:
            continue
        gone.append({"item": meta["label"], "kind": meta["kind"],
                     "last_seen": yesterday_day})

    notes_recent = []
    for day, older in prior[:3]:
        try:
            with open(config.data_path(cfg, day, "ranked.json")) as f:
                notes_recent.extend(json.load(f).get("notes") or [])
        except (OSError, ValueError):
            continue

    return {
        "available": True,
        "compared_with": yesterday_day,
        "days_of_history": len(prior),
        "carried_over": sorted(
            [{"item": today[k]["label"], "kind": today[k]["kind"], "days": n}
             for k, n in runs.items()],
            key=lambda x: -x["days"])[:25],
        "disappeared_since_yesterday": gone[:25],
        "sources_that_returned_nothing": sorted(suspect),
        "notes_said_in_last_3_days": notes_recent[:12],
    }
