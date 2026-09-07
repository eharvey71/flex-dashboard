#!/usr/bin/env python3
"""Flex Dashboard - nightly next-day planner page.

    python3 dashboard.py check    verify every credential, one line each
    python3 dashboard.py fetch    pull all four sources into data/<date>.json
"""

import datetime, json, os, re, sys

import config, correlate, deliver, rank as ranker

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import jira, github, google  # noqa: E402
from render import build as renderer  # noqa: E402
from fetch import imap_mail, local_mac, ics_cal, todoist  # noqa: E402


OVERRIDE_DAY = None   # set by --date=YYYY-MM-DD


def target_day(cfg):
    """The day the page is FOR. Run in the evening, so that's tomorrow."""
    if OVERRIDE_DAY:
        return OVERRIDE_DAY
    return datetime.date.today() + datetime.timedelta(days=1)


def is_personal(cfg):
    """Which pipeline a profile uses is decided by what its config declares,
    not by its name - otherwise every new person needs a code change."""
    return not cfg.get("jira")


def cmd_check(cfg):
    failures = 0
    if is_personal(cfg):
        checks = [("Mail", lambda c: imap_mail.check(c["mail"]))]
        if cfg.get("reminders", {}).get("enabled"):
            checks.append(("Reminders", lambda c: "Reminders OK - %d open"
                           % len(local_mac.reminders(c["reminders"]))))
        if cfg.get("todoist", {}).get("enabled"):
            checks.append(("Todoist", lambda c: todoist.check(c["todoist"])))
        if cfg.get("calendar", {}).get("enabled"):
            checks.append(("Calendar", lambda c: ics_cal.check(c["calendar"], target_day(c))
                           if c["calendar"].get("source", "ics") == "ics"
                           else "Calendar OK - %d events tomorrow"
                           % len(local_mac.calendar_events(target_day(c), c["calendar"]))))
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
        print("Personal sources ready." if not failures
              else "%d source(s) still need setup." % failures)
        return 1 if failures else 0

    for name, fn in (("Jira", jira.check), ("GitHub", github.check), ("Google", google.check)):
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
    print("All four sources ready." if not failures else "%d source(s) still need setup." % failures)
    return 1 if failures else 0


def _optional(label, fn, default):
    """A slow or blocked personal source must not cost the whole page."""
    try:
        return fn()
    except Exception as e:
        print("  %s unavailable: %s" % (label, e), file=sys.stderr)
        return default


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


def cmd_fetch_personal(cfg):
    day = target_day(cfg)
    bundle = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "target_date": day.isoformat(),
        "profile": "personal",
        "mail": _optional("mail", lambda: imap_mail.fetch(
            cfg["mail"], since_days=cfg["mail"].get("since_days", 1)), []),
        "reminders": _optional("reminders", lambda: local_mac.reminders(cfg["reminders"]), [])
                     if cfg.get("reminders", {}).get("enabled") else [],
        "todoist": _optional("todoist", lambda: todoist.fetch(cfg["todoist"], day), [])
                   if cfg.get("todoist", {}).get("enabled") else [],
        "events": _optional("calendar", lambda: _calendar(cfg, day), [])
                  if cfg.get("calendar", {}).get("enabled") else [],
        "upcoming": _optional("lookahead", lambda: _lookahead(cfg, day), [])
                    if cfg.get("calendar", {}).get("enabled") else [],
        "mail_signals": _optional("signals", lambda: _signals(cfg, day), [])
                        if cfg.get("calendar", {}).get("enabled") else [],
    }
    out = config.data_path(cfg, day.isoformat())
    with open(out, "w") as f:
        json.dump(bundle, f, indent=2)

    vips = [m for m in bundle["mail"] if m.get("vip")]
    print("Wrote %s" % out)
    print("  %d mail (%d from people), %d reminders, %d todoist, %d events, %d ahead, %d signals"
          % (len(bundle["mail"]), len(vips), len(bundle["reminders"]), len(bundle["todoist"]),
             len(bundle["events"]), len(bundle["upcoming"]), len(bundle["mail_signals"])))
    return 0


def cmd_fetch(cfg):
    if is_personal(cfg):
        return cmd_fetch_personal(cfg)
    day = target_day(cfg)
    g = google.fetch(cfg, day)
    issues = jira.fetch(cfg)
    prs, branches = github.repo_state(cfg)

    bundle = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "target_date": day.isoformat(),
        "jira": issues,
        "github": github.fetch(cfg),
        "events": g["events"],
        "upcoming": g.get("upcoming", []),
        "mail": g["mail"],
        "tasks": g["tasks"],
        "reminders": _optional("reminders", lambda: local_mac.reminders(cfg["reminders"]), [])
                     if cfg.get("reminders", {}).get("enabled") else [],
    }
    bundle.update(correlate.build(
        issues, prs, branches,
        stale_days=cfg["github"].get("stale_days", 2),
        me=cfg["github"]["login"]))
    os.makedirs(config.path("data"), exist_ok=True)
    out = config.path("data", "%s.json" % day.isoformat())
    with open(out, "w") as f:
        json.dump(bundle, f, indent=2)

    print("Wrote %s" % out)
    print("  %d Jira, %d PRs, %d events, %d ahead, %d mail, %d tasks, %d reminders"
          % (len(bundle["jira"]), len(bundle["github"]), len(bundle["events"]),
             len(bundle["upcoming"]), len(bundle["mail"]),
             len(bundle["tasks"]), len(bundle["reminders"])))
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
    if pdf_path:
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
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    cmd = args[0] if args else "check"
    profile = args[1] if len(args) > 1 else None
    if cmd not in COMMANDS:
        raise SystemExit("usage: python3 dashboard.py {%s} [profile]"
                         % "|".join(COMMANDS))
    sys.exit(COMMANDS[cmd](config.load(profile)))
