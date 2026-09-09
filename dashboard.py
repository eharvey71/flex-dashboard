#!/usr/bin/env python3
"""Flex Dashboard - nightly next-day planner page.

    python3 dashboard.py check    verify every credential, one line each
    python3 dashboard.py fetch    pull all four sources into data/<date>.json
"""

import datetime, json, os, re, sys

import config, correlate, deliver, history, rank as ranker

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import jira, github, google  # noqa: E402
from render import build as renderer  # noqa: E402
from fetch import imap_mail, local_mac, ics_cal, todoist  # noqa: E402


OVERRIDE_DAY = None   # set by --date=YYYY-MM-DD
NO_EMAIL = False      # set by --no-email, for test runs


def target_day(cfg):
    """The day the page is FOR. Run in the evening, so that's tomorrow."""
    if OVERRIDE_DAY:
        return OVERRIDE_DAY
    return datetime.date.today() + datetime.timedelta(days=1)


def cmd_check(cfg):
    """Check exactly the sources this profile declares, in the order fetch runs."""
    checks = []
    if cfg.get("jira"):
        checks.append(("Jira", jira.check))
    if cfg.get("github"):
        checks.append(("GitHub", github.check))
    if cfg.get("google"):
        checks.append(("Google", google.check))
    if cfg.get("mail"):
        checks.append(("Mail", lambda c: imap_mail.check(c["mail"])))
    if cfg.get("calendar", {}).get("enabled"):
        checks.append(("Calendar", lambda c: ics_cal.check(c["calendar"], target_day(c))
                       if c["calendar"].get("source", "ics") == "ics"
                       else "Calendar OK - %d events tomorrow"
                       % len(local_mac.calendar_events(target_day(c), c["calendar"]))))
    if cfg.get("reminders", {}).get("enabled"):
        checks.append(("Reminders", lambda c: "Reminders OK - %d open"
                       % len(local_mac.reminders(c["reminders"]))))
    if cfg.get("todoist", {}).get("enabled"):
        checks.append(("Todoist", lambda c: todoist.check(c["todoist"])))

    failures = 0
    for name, fn in checks:
        try:
            print("  " + fn(cfg))
        except Exception as e:
            failures += 1
            print("  %s FAILED - %s" % (name, e))
    try:
        import creds
        creds.get("anthropic_api_key")
        print("  Anthropic key present")
    except Exception as e:
        failures += 1
        print("  Anthropic FAILED - %s" % e)

    print()
    print("All %d sources ready." % len(checks) if not failures
          else "%d source(s) still need setup." % failures)
    return 1 if failures else 0


def _optional(label, fn, default, retries=1):
    """Run a source; on failure log it, keep its duration, and carry on.

    Times every source because a twelve-minute run with no breakdown is not
    diagnosable. Retries once on timeout: the local AppleScript sources are
    slow rather than broken, and a single retry costs less than losing the
    whole source for the night.
    """
    for attempt in range(retries + 1):
        started = datetime.datetime.now()
        try:
            got = fn()
            secs = (datetime.datetime.now() - started).total_seconds()
            TIMINGS.append((label, secs, "ok"))
            return got
        except Exception as e:
            secs = (datetime.datetime.now() - started).total_seconds()
            transient = "timed out" in str(e).lower() or "timeout" in str(e).lower()
            if transient and attempt < retries:
                print("  %s timed out after %.0fs, retrying once" % (label, secs),
                      file=sys.stderr)
                continue
            TIMINGS.append((label, secs, "FAILED"))
            print("  %s unavailable after %.0fs: %s" % (label, secs, e), file=sys.stderr)
            return default


TIMINGS = []


def _report_timings():
    if not TIMINGS:
        return
    total = sum(s for _, s, _ in TIMINGS)
    print("  timings (%.0fs in sources):" % total)
    for label, secs, status in sorted(TIMINGS, key=lambda x: -x[1]):
        if secs >= 1 or status != "ok":
            print("    %-24s %6.1fs  %s" % (label, secs, status))


def _calendar(cfg, day, kind="calendar"):
    """ICS by default. Calendar.app is kept as a fallback but is flaky and
    needs a TCC grant tied to the calling binary, which launchd does not share.

    `kind` splits real appointments from signal feeds - all-day markers an
    automation writes when a matching email arrives. Those are mail, not
    meetings, and putting them on the timeline would be a lie.
    """
    cal = cfg["calendar"]
    if cal.get("source", "ics") == "ics":
        return [e for e in ics_cal.fetch(cal, day) if e.get("kind", "calendar") == kind]
    return local_mac.calendar_events(day, cal) if kind == "calendar" else []


def _signals(cfg, day):
    """Signal markers land on the day the EMAIL arrived, but the page is built
    the evening before and read the next morning. Looking only at the target
    day would therefore miss everything that came in today - which is most of
    what she needs to know about.
    """
    back = max(1, cfg["calendar"].get("signal_days", 2))
    out = []
    for n in range(back):
        d = day - datetime.timedelta(days=n)
        for ev in _calendar(cfg, d, "signal"):
            ev["arrived"] = d.isoformat()
            ev["days_ago"] = (day - d).days
            out.append(ev)
    return out


def _lookahead(cfg, day):
    """Days 2..N, flat, each event tagged with its day."""
    look = max(1, cfg["calendar"].get("lookahead_days", 5))
    out = []
    for n in range(1, look):
        d = day + datetime.timedelta(days=n)
        for ev in _calendar(cfg, d):
            ev["day"] = d.isoformat()
            out.append(ev)
    return out


def cmd_fetch(cfg):
    """Every source is optional and runs only if the config declares it.

    This used to branch into a "work" pipeline and a "local" one, so a profile
    could have Jira or a personal inbox but never both. One page covering a
    whole day needs both: the shape of the config decides what runs.
    """
    day = target_day(cfg)
    bundle = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "target_date": day.isoformat(),
        "profile": cfg.get("profile", "work"),
        "jira": [], "github": [], "links": [], "unlinked_prs": [],
        "tasks": [], "reminders": [], "todoist": [], "mail_signals": [],
    }
    events, upcoming, mail = [], [], []

    if cfg.get("google"):
        g = _optional("google", lambda: google.fetch(cfg, day),
                      {"events": [], "upcoming": [], "mail": [], "tasks": []})
        events += g.get("events", [])
        upcoming += g.get("upcoming", [])
        for m in g.get("mail", []):
            m.setdefault("account", "work")
        mail += g.get("mail", [])
        bundle["tasks"] = g.get("tasks", [])

    if cfg.get("mail"):
        mc = cfg["mail"]
        got = _optional("imap:%s" % mc.get("label", "mail"),
                        lambda: imap_mail.fetch(mc, since_days=mc.get("since_days", 1)), [])
        for m in got:
            m.setdefault("account", mc.get("label", "personal"))
        mail += got

    if cfg.get("calendar", {}).get("enabled"):
        events += _optional("calendar", lambda: _calendar(cfg, day), [])
        upcoming += _optional("lookahead", lambda: _lookahead(cfg, day), [])
        bundle["mail_signals"] = _optional("signals", lambda: _signals(cfg, day), [])

    events.sort(key=lambda x: (not x.get("all_day"), x.get("start") or ""))
    upcoming.sort(key=lambda x: (x.get("day") or "", x.get("start") or ""))
    mail.sort(key=lambda m: (m.get("vip") is None, m.get("watch") is None, m.get("date") or ""))
    bundle["events"], bundle["upcoming"], bundle["mail"] = events, upcoming, mail

    completed = []
    if cfg.get("reminders", {}).get("enabled"):
        bundle["reminders"] = _optional(
            "reminders", lambda: local_mac.reminders(cfg["reminders"], day), [])
        completed += _optional(
            "completed reminders",
            lambda: local_mac.completed_recently(cfg["reminders"]), [])
    if cfg.get("todoist", {}).get("enabled"):
        bundle["todoist"] = _optional(
            "todoist", lambda: todoist.fetch(cfg["todoist"], day), [])
        completed += _optional(
            "completed todoist",
            lambda: todoist.completed_recently(cfg["todoist"]), [])
    bundle["completed_recently"] = completed

    if cfg.get("jira"):
        bundle["jira"] = _optional("jira", lambda: jira.fetch(cfg), [])
    if cfg.get("github"):
        bundle["github"] = _optional("github", lambda: github.fetch(cfg), [])
        prs, branches = _optional("github repo state",
                                  lambda: github.repo_state(cfg), ([], []))
        bundle.update(correlate.build(
            bundle["jira"], prs, branches,
            stale_days=cfg["github"].get("stale_days", 2),
            me=cfg["github"].get("login")))

    bundle["history"] = _optional(
        "history", lambda: history.build(bundle, cfg, day), {"available": False})

    out = config.data_path(cfg, day.isoformat())
    with open(out, "w") as f:
        json.dump(bundle, f, indent=2)

    print("Wrote %s" % out)
    counts = [("Jira", len(bundle["jira"])), ("PRs", len(bundle["github"])),
              ("mail", len(bundle["mail"])),
              ("vip", len([m for m in bundle["mail"] if m.get("vip")])),
              ("events", len(bundle["events"])), ("ahead", len(bundle["upcoming"])),
              ("signals", len(bundle["mail_signals"])),
              ("reminders", len(bundle["reminders"])),
              ("todoist", len(bundle["todoist"])), ("gtasks", len(bundle["tasks"])),
              ("completed recently", len(bundle["completed_recently"]))]
    print("  " + ", ".join("%d %s" % (n, label) for label, n in counts if n))
    _report_timings()
    h = bundle.get("history") or {}
    if h.get("available"):
        print("  history: %d days, %d carried over, %d disappeared since %s"
              % (h["days_of_history"], len(h["carried_over"]),
                 len(h["disappeared_since_yesterday"]), h["compared_with"]))
        if h.get("sources_that_returned_nothing"):
            print("    NOTE: %s returned nothing today - treated as a failed fetch, "
                  "not completions" % ", ".join(h["sources_that_returned_nothing"]))
    if bundle["links"]:
        print("  %d ticket<->code links, %d PRs with no ticket"
              % (len(bundle["links"]), len(bundle["unlinked_prs"])))
        for l in bundle["links"][:6]:
            print("    %-28s %s" % (l["signal"], l["key"]))
    return 0


def cmd_rank(cfg):
    _, out = ranker.run(cfg, target_day(cfg).isoformat())
    print("Wrote %s" % out)
    return 0


def cmd_render(cfg):
    """Rebuild the page from the last ranked file - no API calls, no tokens."""
    day = target_day(cfg).isoformat()
    ranked = json.load(open(config.data_path(cfg, day, "ranked.json")))
    bundle = json.load(open(config.data_path(cfg, day)))
    html_path, _ = renderer.write(ranked, cfg, bundle)
    print("Wrote %s" % html_path)
    return 0


def cmd_run(cfg):
    """The whole pipeline. This is what launchd calls."""
    started = datetime.datetime.now()
    cmd_fetch(cfg)
    ranked, _ = ranker.run(cfg, target_day(cfg).isoformat())
    bundle = json.load(open(config.data_path(cfg, target_day(cfg).isoformat())))
    path, pdf_path = renderer.write(ranked, cfg, bundle)
    _prune(cfg)
    print("Wrote %s (%.1fs)" % (path, (datetime.datetime.now() - started).total_seconds()))

    # Delivery is separate from generation on purpose: a failed send must not
    # look like a failed build, and the page is already on disk either way.
    if pdf_path and NO_EMAIL:
        print("  email suppressed (--no-email)")
    elif pdf_path:
        try:
            status = deliver.send(cfg, pdf_path, target_day(cfg).isoformat())
            if status:
                print("  %s" % status)
        except Exception as ex:
            print("  EMAIL FAILED: %s" % ex, file=sys.stderr)
    return 0


def _prune(cfg):
    """Keep the output and data directories from growing without limit."""
    keep = cfg["output"].get("keep_days", 30)
    cutoff = datetime.date.today() - datetime.timedelta(days=keep)
    o = cfg.get("output", {})
    for sub in (o.get("dir", "out"), o.get("data_dir", "data"),
                o.get("publish_dir", "publish")):
        d = config.path(sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            # Filenames are templated now, so find the date rather than assume
            # it is the whole stem - otherwise pruning silently stops working.
            m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
            if not m:
                continue
            try:
                if datetime.date.fromisoformat(m.group(1)) < cutoff:
                    os.remove(os.path.join(d, name))
            except ValueError:
                continue


def cmd_boards(cfg):
    """List Jira boards so the right one can go in config.json."""
    for b in jira.list_boards(cfg):
        sprint = (" | active sprint %s (id %s)" % (b["sprint_name"], b["sprint_id"])) \
                 if b["sprint_id"] else ""
        print("  id %-6s %-10s %s%s" % (b["id"], b["type"], b["name"], sprint))
    return 0


COMMANDS = {"boards": cmd_boards, "check": cmd_check, "fetch": cmd_fetch, "rank": cmd_rank,
            "render": cmd_render, "run": cmd_run}

if __name__ == "__main__":
    for a in sys.argv[1:]:
        if a.startswith("--date="):
            OVERRIDE_DAY = datetime.date.fromisoformat(a.split("=", 1)[1])
        elif a == "--no-email":
            NO_EMAIL = True
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    cmd = args[0] if args else "check"
    profile = args[1] if len(args) > 1 else None
    if cmd not in COMMANDS:
        raise SystemExit("usage: python3 dashboard.py {%s} [profile]"
                         % "|".join(COMMANDS))
    sys.exit(COMMANDS[cmd](config.load(profile)))
