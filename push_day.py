"""Build one day's compact document for the Flex Daily artifact.

The nightly LaunchAgent writes the page; this turns the day it just built into
the shape the phone view reads, and prints it for the scheduled push. Nothing
here talks to the artifact: the push is done by Claude, which is the only side
holding credentials for the artifact's store.

  push_day.py            newest day, printed as one JSON object
  push_day.py 2026-11-13 that day
  push_day.py --last 10  a JSON array of the last N days
"""

import datetime, glob, json, os, re, sys
from zoneinfo import ZoneInfo

# The repo root is wherever this file sits, so the same script works from the
# Mac (~/dev/flex-dashboard) and from the Linux VM (~/mnt/dev/flex-dashboard).
ROOT = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("America/New_York")
LOG = os.path.join(ROOT, "logs/run.log")

def log_blocks():
    """Per-target-date build facts, parsed from the run log."""
    try:
        lines = open(LOG, errors="replace").read().splitlines()
    except OSError:
        return {}
    out, cur = {}, None
    for ln in lines:
        m = re.search(r"Wrote .*/data/(\d{4}-\d{2}-\d{2})\.json", ln)
        if m:
            cur = {"date": m.group(1), "timings": [], "failed": [], "empty": []}
            out[m.group(1)] = cur
            continue
        if cur is None:
            continue
        m = re.match(r"\s{4}([a-z][\w: ]+?)\s+([\d.]+)s\s+(ok|FAILED)\s*$", ln)
        if m:
            cur["timings"].append({"name": m.group(1).strip(),
                                   "seconds": float(m.group(2)),
                                   "status": m.group(3).lower()})
            if m.group(3) == "FAILED":
                cur["failed"].append(m.group(1).strip())
        m = re.search(r"one page at ([\d.]+)pt", ln)
        if m:
            cur["fit_pt"] = float(m.group(1))
        m = re.search(r"Wrote .*/out/(\d{4}-\d{2}-\d{2})-\w+\.html \(([\d.]+)s\)", ln)
        if m and m.group(1) == cur["date"]:
            cur["duration_s"] = float(m.group(2))
    return out

def mins(a, b):
    try:
        s = datetime.datetime.fromisoformat(a); e = datetime.datetime.fromisoformat(b)
    except Exception:
        return None
    n = int((e - s).total_seconds() // 60)
    return None if n <= 0 else (("%dh %02dm" % divmod(n, 60)) if n >= 60 else "%dm" % n)

def clock(iso):
    try:
        d = datetime.datetime.fromisoformat(iso)
    except Exception:
        return ""
    if d.tzinfo is not None:
        d = d.astimezone(TZ)
    h = d.strftime("%I").lstrip("0") or "12"
    return h + (":" + d.strftime("%M") if d.minute else "") + " " + d.strftime("%p")

def build(date):
    rp = os.path.join(ROOT, "data", date + ".ranked.json")
    bp = os.path.join(ROOT, "data", date + ".json")
    if not os.path.exists(rp):
        return None
    r = json.load(open(rp))
    bundle = json.load(open(bp)) if os.path.exists(bp) else {}
    me = _github_login()

    prs = {}
    for l in bundle.get("links", []):
        for p in l.get("prs", []):
            prs[p["number"]] = p
    for p in bundle.get("unlinked_prs", []):
        prs.setdefault(p["number"], p)
    for g in bundle.get("github", []):
        n = re.search(r"#(\d+)", g.get("key") or "")
        if n:
            prs.setdefault(int(n.group(1)), {}).update(
                {k: v for k, v in g.items() if v is not None})
    ticket_prs = {}
    for l in bundle.get("links", []):
        ticket_prs[l["key"]] = [p["number"] for p in l.get("prs", [])]

    def mine(t):
        blob = " ".join(str(t.get(k) or "") for k in ("key", "text", "meta"))
        nums = {int(x) for x in re.findall(r"#(\d+)", blob)}
        k = re.search(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b", blob)
        if k:
            nums.update(ticket_prs.get(k.group(1), []))
        return any((prs.get(n) or {}).get("author") == me
                   or (prs.get(n) or {}).get("mine") for n in nums)

    tasks = [{"text": t.get("text"), "source": t.get("source"), "key": t.get("key"),
              "flag": t.get("flag"), "meta": t.get("meta"), "mine": mine(t)}
             for t in r.get("tasks", [])]

    events = []
    for e in r.get("events", []):
        events.append({"title": e.get("title"), "all_day": bool(e.get("all_day")),
                       "time": "" if e.get("all_day") else clock(e.get("start") or ""),
                       "length": None if e.get("all_day") else mins(e.get("start"), e.get("end")),
                       "detail": e.get("location") or None})

    hist = (bundle.get("history") or {})
    # A bare ticket key says nothing. Carry-over is only worth a line for items
    # actually ON the page, named the way the page names them, so the entry
    # reads as the work rather than as an id.
    runs = {c.get("item"): c.get("days") for c in (hist.get("carried_over") or [])}
    window = hist.get("days_of_history") or 14
    jira_titles = {i.get("key"): i.get("title") for i in bundle.get("jira", [])}
    carried = []
    for t in r.get("tasks", []):
        k = t.get("key")
        n = runs.get(k)
        if not k or not n or n < 3:
            continue
        carried.append({"key": k, "item": t.get("text") or jira_titles.get(k) or k,
                        "days": n, "at_least": bool(n >= window)})
    carried.sort(key=lambda c: -c["days"])
    carried = carried[:10]

    info = log_blocks().get(date, {})
    gen = bundle.get("generated_at")
    doc = {
        "date": date,
        "built_at": clock(gen) if gen else None,
        "goals": [{"horizon": g.get("horizon"), "text": g.get("text")} for g in r.get("goals", [])],
        "priorities": [{"text": p.get("text"), "why": p.get("why")} for p in r.get("priorities", [])],
        "tasks": tasks,
        "events": events,
        "notes": r.get("notes", []),
        "carried": carried,
        "dropped": r.get("dropped") or None,
        "build": {"duration_s": info.get("duration_s"), "fit_pt": info.get("fit_pt"),
                  "timings": info.get("timings", []), "failed": info.get("failed", []),
                  "empty_sources": hist.get("sources_that_returned_nothing") or []},
    }
    return doc

dates = sorted(os.path.basename(p)[:10]
               for p in glob.glob(os.path.join(ROOT, "data", "*.ranked.json")))

args = sys.argv[1:]
if args and args[0] == "--last":
    want = dates[-int(args[1]):]
    print(json.dumps([d for d in (build(x) for x in want) if d], separators=(",", ":")))
else:
    day = args[0] if args else (dates[-1] if dates else None)
    doc = build(day) if day else None
    if not doc:
        sys.exit("No ranked page for %s" % day)
    print(json.dumps(doc, separators=(",", ":")))
