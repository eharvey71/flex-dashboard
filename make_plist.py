#!/usr/bin/env python3
"""Generate a LaunchAgent plist for a profile.

The plists are not committed: they hold an absolute home directory and a
profile name, and profile names are people's names. Generating them keeps the
paths correct on any machine and keeps the repo free of both.

    python3 make_plist.py work     21:00 --days sun-thu
    python3 make_plist.py personal 21:05
    python3 make_plist.py <profile> 21:10 --install
"""

import argparse, os, plistlib, subprocess, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def weekdays(spec):
    if not spec or spec == "daily":
        return None
    if "-" in spec:
        a, b = spec.split("-", 1)
        lo, hi = DAYS[a.lower()], DAYS[b.lower()]
        return list(range(lo, hi + 1)) if lo <= hi else \
            list(range(lo, 7)) + list(range(0, hi + 1))
    return [DAYS[d.strip().lower()] for d in spec.split(",")]


def build(profile, at, days, label_prefix):
    hour, minute = (int(x) for x in at.split(":"))
    args = ["/usr/bin/python3", os.path.join(ROOT, "dashboard.py"), "run"]
    if profile != "work":
        args.append(profile)

    when = {"Hour": hour, "Minute": minute}
    schedule = [dict(when, Weekday=d) for d in days] if days else when

    label = "%s.flex-dashboard%s" % (label_prefix, "" if profile == "work" else "-" + profile)
    log = os.path.join(ROOT, "logs", "run%s.log" % ("" if profile == "work" else "-" + profile))

    return label, {
        "Label": label,
        "ProgramArguments": args,
        "WorkingDirectory": ROOT,
        "StartCalendarInterval": schedule,
        "RunAtLoad": False,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        # launchd starts with a bare environment; python3 needs a usable PATH.
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("at", help="24h time, e.g. 21:00")
    ap.add_argument("--days", default="daily",
                    help="'daily', a range like sun-thu, or a list like mon,wed,fri")
    ap.add_argument("--prefix", default="com." + os.environ.get("USER", "local"))
    ap.add_argument("--install", action="store_true",
                    help="copy into ~/Library/LaunchAgents and bootstrap it")
    a = ap.parse_args()

    label, doc = build(a.profile, a.at, weekdays(a.days), a.prefix)
    out = os.path.join(ROOT, label + ".plist")
    with open(out, "wb") as f:
        plistlib.dump(doc, f)
    print("wrote %s" % out)
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)

    if a.install:
        dest = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label)
        subprocess.run(["cp", out, dest], check=True)
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)],
                       capture_output=True)
        subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, dest], check=True)
        print("installed and bootstrapped %s" % label)
