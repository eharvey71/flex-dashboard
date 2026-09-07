"""Rank the raw pile down to what fits on one paper page.

The Flex dashboard page has hard capacity: 3 priority slots and 14 task
checkboxes. So this is a cutting problem, not a listing problem. The model's
job is to decide what earns a line and what gets dropped.
"""

import datetime, json, os, sys
from zoneinfo import ZoneInfo

import config, creds, httpjson

API = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"

def system_prompt(cfg):
    """Work and personal reason about entirely different things, so each has
    its own prompt file. Edit prompts/*.md rather than this module."""
    # Prompt files are versioned in git; profile names are not, because they are
    # people's names. Let config map one to the other.
    name = (cfg.get("ranking") or {}).get("prompt") or cfg.get("profile", "work")
    path = config.path("prompts", "%s.md" % name)
    if not os.path.exists(path):
        raise SystemExit("No prompt file for profile '%s' (expected %s)" % (name, path))
    with open(path) as f:
        return f.read()


DATE_FIELDS = ("start", "end", "due", "day", "arrived", "date")


def _stamp(value, target, tz):
    """Turn an ISO date or datetime into labels the model can quote verbatim."""
    if not value or not isinstance(value, str):
        return None
    try:
        d = datetime.date.fromisoformat(value[:10])
    except ValueError:
        return None
    delta = (d - target).days
    when = {0: "the target day", 1: "the day after", -1: "the day before"}.get(delta)
    if when is None:
        when = "%d days %s the target day" % (abs(delta), "after" if delta > 0 else "before")
    label = {"weekday": d.strftime("%A"), "date_label": d.strftime("%a %-d %b %Y"),
             "days_from_target": delta, "relative": when}
    if len(value) > 10 and "T" in value:
        try:
            t = datetime.datetime.fromisoformat(value)
            # ICS feeds publish UTC. Formatting that as-is is how a 3pm meeting
            # becomes a 7pm meeting on the page.
            if t.tzinfo is not None:
                t = t.astimezone(tz)
                d2 = t.date()
                if d2 != d:
                    delta2 = (d2 - target).days
                    label["weekday"] = t.strftime("%A")
                    label["date_label"] = t.strftime("%a %-d %b %Y")
                    label["days_from_target"] = delta2
            label["time_label"] = t.strftime("%-I:%M%p").lower().replace(":00", "")
        except ValueError:
            pass
    return label


def _label_dates(obj, target, tz):
    """Walk the bundle and attach a *_when block beside every date field, so
    the model never has to work out what weekday something falls on. It gets
    this wrong, and a wrong weekday on a planner page is worse than no page."""
    if isinstance(obj, list):
        for item in obj:
            _label_dates(item, target, tz)
    elif isinstance(obj, dict):
        for key in list(obj):
            if key in DATE_FIELDS:
                lab = _stamp(obj[key], target, tz)
                if lab:
                    obj[key + "_when"] = lab
            else:
                _label_dates(obj[key], target, tz)
    return obj


def _payload(bundle, cfg):
    r = cfg["ranking"]
    day = datetime.date.fromisoformat(bundle["target_date"])
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    bundle = _label_dates(bundle, day, tz)
    user = ("Target day: %s, a %s.\n"
            "Every dated item below carries a *_when block with the weekday and date "
            "already worked out. Use those; do not calculate dates yourself.\n\n"
            "Cap tasks at %d, priorities at %d.\n\n"
            "RAW SOURCE DATA:\n%s"
            % (day.isoformat(), day.strftime("%A"),
               r.get("max_tasks", 14), r.get("max_priorities", 3),
               json.dumps(bundle, indent=1)[:120000]))
    return {"model": r.get("model", "claude-sonnet-4-5"),
            "max_tokens": 4000,
            "system": system_prompt(cfg),
            "messages": [{"role": "user", "content": user}]}


def _parse(text):
    """Models occasionally wrap JSON in a fence despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(t)


def rank(bundle, cfg):
    headers = {"x-api-key": creds.get("anthropic_api_key"), "anthropic-version": VERSION}
    resp = httpjson.request(API, headers=headers, data=_payload(bundle, cfg))
    text = "".join(b.get("text", "") for b in resp.get("content", []))
    try:
        return _parse(text)
    except json.JSONDecodeError as e:
        raise SystemExit("Model returned unparseable JSON (%s).\nFirst 500 chars:\n%s"
                         % (e, text[:500]))


def load_bundle(date_str, cfg):
    p = config.data_path(cfg, date_str)
    if not os.path.exists(p):
        raise SystemExit("No fetched data at %s - run `dashboard.py fetch` first." % p)
    with open(p) as f:
        return json.load(f)


def run(cfg, date_str):
    bundle = load_bundle(date_str, cfg)
    result = rank(bundle, cfg)
    result["target_date"] = date_str
    result["events"] = bundle["events"]          # calendar passes through untouched
    result["upcoming"] = bundle.get("upcoming", [])
    out = config.data_path(cfg, date_str, "ranked.json")
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    return result, out


if __name__ == "__main__":
    cfg = config.load()
    date_str = sys.argv[1] if len(sys.argv) > 1 else \
        (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    result, out = run(cfg, date_str)
    print(json.dumps(result, indent=2))
    print("\nWrote %s" % out, file=sys.stderr)
