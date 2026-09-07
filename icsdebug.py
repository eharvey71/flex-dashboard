"""Structural diagnostic for an ICS feed. Prints shapes and counts only -
no event titles, locations or attendees."""

import collections, datetime, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import ics_cal

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/cal.ics"
text = open(path, encoding="utf-8", errors="replace").read()

lines = ics_cal._unfold(text)
print("unfolded lines : %d" % len(lines))

prop_shapes = collections.Counter()
for ln in lines:
    if ln.startswith("DTSTART"):
        head = ln.split(":", 1)[0]
        prop_shapes[head.split("=")[0] if ";" in head else head] += 1
print("DTSTART shapes :")
for k, v in prop_shapes.most_common(8):
    print("   %-28s %d" % (k, v))

evs = ics_cal._events_raw(text)
print("VEVENTs parsed : %d" % len(evs))

with_rrule = [e for e in evs if ics_cal._rrule(e)]
print("with RRULE     : %d" % len(with_rrule))

freqs = collections.Counter()
for e in with_rrule:
    freqs[(ics_cal._rrule(e) or {}).get("FREQ", "?")] += 1
print("RRULE freqs    : %s" % dict(freqs))

parsed_ok, parse_fail, years = 0, 0, collections.Counter()
for e in evs:
    sp, sv = ics_cal._first(e, "DTSTART")
    if not sv:
        parse_fail += 1
        continue
    d, allday, dt = ics_cal._parse_dt(sv, sp or {})
    if d is None:
        parse_fail += 1
    else:
        parsed_ok += 1
        years[d.year] += 1
print("DTSTART parsed : %d ok, %d FAILED" % (parsed_ok, parse_fail))
print("years present  : %s" % dict(sorted(years.items())))

today = datetime.date.today()
print("\noccurrence check, next 14 days:")
total = 0
for n in range(14):
    day = today + datetime.timedelta(days=n)
    hits = 0
    for e in evs:
        sp, sv = ics_cal._first(e, "DTSTART")
        if not sv:
            continue
        sd, _, _ = ics_cal._parse_dt(sv, sp or {})
        if sd and ics_cal._occurs_on(e, sd, ics_cal._rrule(e), day):
            hits += 1
    total += hits
    print("   %s %s  %d" % (day, day.strftime("%a"), hits))
print("total over 14 days: %d" % total)
