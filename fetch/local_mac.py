"""Personal sources read from the Mac's own apps - no OAuth, no cloud project.

Reminders, Calendar and Mail already sync to this machine. Reading them locally
sidesteps the whole personal-Google-account problem: an Internal OAuth app
cannot serve a personal account, and an External one caps refresh tokens at
seven days in Testing mode, which would break a nightly job every week.

Uses JXA (JavaScript for Automation) rather than AppleScript, because JXA can
return real JSON instead of string-munged tab output.

TCC WARNING: the first access to each app raises a macOS permission dialog.
Under launchd there is no one to click it and the job hangs forever - the same
trap as the Keychain ACL. Run `python3 fetch/local_mac.py test` interactively
and grant all three BEFORE the nightly job ever needs them.
"""

import json, os, subprocess, sys

TIMEOUT = 120


class LocalSourceError(Exception):
    pass


def _jxa(script, timeout=TIMEOUT):
    try:
        p = subprocess.run(["osascript", "-l", "JavaScript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LocalSourceError(
            "timed out after %ds - if this is the first run, a permission dialog "
            "is probably waiting on screen" % timeout)
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        if "-1743" in err or "not allowed" in err.lower():
            raise LocalSourceError(
                "permission denied by macOS. Grant access in System Settings > "
                "Privacy & Security, then re-run. (%s)" % err[:120])
        raise LocalSourceError(err[:300] or "osascript failed")
    out = (p.stdout or "").strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise LocalSourceError("unparseable JXA output: %s" % out[:200])


# ---------- reminders ----------
#
# JXA reads one property at a time, which means an Apple Event per property per
# reminder - thousands of round-trips and a two-minute timeout. AppleScript's
# PLURAL accessors return a whole column in a single event, so this is two
# events per list regardless of how many reminders it holds.

REMINDERS_AS = """
tell application "Reminders"
  set outText to ""
  repeat with l in lists
    set lname to name of l
    set ns to name of (every reminder in l whose completed is false)
    set ds to due date of (every reminder in l whose completed is false)
    if (count of ns) > 0 then
      repeat with i from 1 to count of ns
        set n to item i of ns
        set d to item i of ds
        if d is missing value then
          set dtxt to ""
        else
          set dtxt to ((year of d) as string) & "-" & ¬
            text -2 thru -1 of ("0" & ((month of d) as integer as string)) & "-" & ¬
            text -2 thru -1 of ("0" & ((day of d) as string))
        end if
        set outText to outText & lname & tab & n & tab & dtxt & linefeed
      end repeat
    end if
  end repeat
  return outText
end tell
"""


def _osascript(script, timeout):
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LocalSourceError("timed out after %ds" % timeout)
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        if "-1743" in err or "not allowed" in err.lower():
            raise LocalSourceError("permission denied by macOS (%s)" % err[:120])
        raise LocalSourceError(err[:300] or "osascript failed")
    return p.stdout


def reminders(cfg=None):
    cfg = cfg or {}
    raw = _osascript(REMINDERS_AS, cfg.get("timeout", 45))
    skip = set(cfg.get("skip_lists", []))
    only = set(cfg.get("only_lists") or [])

    out = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        lname, title, due = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not title or lname in skip or (only and lname not in only):
            continue
        out.append({"source": "reminders", "list": lname,
                    "title": title, "due": due or None})

    out.sort(key=lambda x: (x["due"] is None, x["due"] or ""))
    return out


# ---------- calendar ----------

CALENDAR_JS = """
var app = Application("Calendar");
var start = new Date("%(start)s");
var end = new Date("%(end)s");
var out = [];
var cals = app.calendars();
for (var i = 0; i < cals.length; i++) {
  var evs = cals[i].events.whose({
    _and: [{startDate: {_greaterThan: start}}, {startDate: {_lessThan: end}}]
  })();
  for (var j = 0; j < evs.length; j++) {
    var e = evs[j];
    out.push({
      calendar: cals[i].name(),
      title: e.summary(),
      start: e.startDate().toISOString(),
      end: e.endDate().toISOString(),
      all_day: e.alldayEvent(),
      location: e.location() || null
    });
  }
}
JSON.stringify(out);
"""


def calendar_events(day, cfg=None):
    import datetime
    start = datetime.datetime.combine(day, datetime.time.min)
    end = start + datetime.timedelta(days=1)
    js = CALENDAR_JS % {"start": start.isoformat(), "end": end.isoformat()}
    skip = set((cfg or {}).get("skip_calendars", []))
    out = [e for e in _jxa(js) if e.get("calendar") not in skip]
    for e in out:
        e["source"] = "calendar"
    out.sort(key=lambda x: x.get("start") or "")
    return out


# ---------- mail ----------

MAIL_JS = """
var app = Application("Mail");
var out = [];
var boxes = app.inbox.mailboxes();
var msgs = app.inbox.messages.whose({readStatus: false})();
var limit = Math.min(msgs.length, %(limit)d);
for (var i = 0; i < limit; i++) {
  var m = msgs[i];
  out.push({
    subject: m.subject(),
    sender: m.sender(),
    date: m.dateReceived().toISOString(),
    flagged: m.flaggedStatus()
  });
}
JSON.stringify(out);
"""


def mail(cfg=None):
    limit = (cfg or {}).get("max_messages", 25)
    out = _jxa(MAIL_JS % {"limit": limit})
    for m in out:
        m["source"] = "mail"
    return out


# ---------- interactive permission check ----------

if __name__ == "__main__":
    import datetime
    day = datetime.date.today() + datetime.timedelta(days=1)
    checks = [
        ("Reminders", lambda: reminders()),
        ("Calendar", lambda: calendar_events(day)),
        ("Mail", lambda: mail()),
    ]
    failed = 0
    for name, fn in checks:
        sys.stdout.write("  %-10s ... " % name)
        sys.stdout.flush()
        started = datetime.datetime.now()
        try:
            items = fn()
            secs = (datetime.datetime.now() - started).total_seconds()
            print("OK  %3d items  %5.1fs" % (len(items), secs))
            for it in items[:3]:
                label = it.get("title") or it.get("subject") or "?"
                print("       - %s" % str(label)[:64])
        except LocalSourceError as ex:
            failed += 1
            print("FAILED\n       %s" % ex)
    print()
    print("All three readable." if not failed else
          "%d source(s) blocked - grant access and re-run BEFORE scheduling." % failed)
    sys.exit(1 if failed else 0)
