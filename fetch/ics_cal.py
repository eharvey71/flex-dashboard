"""Google Calendar via its private iCal ("secret address") URL.

Chosen over reading Calendar.app: that route needs a TCC grant tied to the
calling binary, took 30s on a good run and timed out at 120s on the next, and
would likely be denied under launchd where the caller is not Terminal. This is
one HTTP GET with no permissions and no app involved.

The cost is that Google serves raw RRULEs rather than expanded occurrences, so
recurrence has to be handled here. That is only tractable because the question
is narrow: not "expand this calendar" but "does this event occur on ONE given
day". Supported: FREQ DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL, COUNT, UNTIL,
BYDAY, plus EXDATE cancellations and RECURRENCE-ID overrides. Anything more
exotic (BYSETPOS, BYMONTHDAY lists) is not expanded and will be missed - the
tradeoff accepted for a personal calendar.
"""

import datetime, html, re, time, urllib.request

_CACHE = {}          # url -> (fetched_at, text)
CACHE_SECONDS = 300  # one run asks for several days; the feed cannot
                     # meaningfully change between them, and it is 3MB

DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _unfold(text):
    """RFC 5545 folds long lines with a leading space or tab on continuations."""
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


# Mail gateways prepend an external-sender warning to every inbound message.
# It is identical on all of them, carries no information, and would otherwise
# occupy most of the captured text.
BANNER = re.compile(
    r"^\s*CAUTION\s*:?.{0,200}?(?:content is safe\.?|know the content is safe\.?)\s*",
    re.I | re.S)
EXTERNAL = re.compile(
    r"^\s*(?:\[?EXTERNAL\]?|EXTERNAL EMAIL|\*\*\*\s*EXTERNAL)\s*[:\-]?\s*", re.I)


def _clean_detail(text, limit=600):
    """DESCRIPTION may hold a whole HTML email. Reduce it to readable text."""
    if not text:
        return None
    t = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", text)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    t = _unescape(t)
    t = BANNER.sub("", t)
    t = EXTERNAL.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] or None


def _unescape(text):
    return (text.replace("\\n", " ").replace("\\N", " ")
                .replace("\\,", ",").replace("\\;", ";")
                .replace("\\\\", "\\")).strip()


def _parse_dt(value, params):
    """Returns (date, is_all_day, datetime_or_None)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and "T" not in value):
        d = datetime.datetime.strptime(value, "%Y%m%d").date()
        return d, True, None
    raw = value[:-1] if value.endswith("Z") else value
    try:
        dt = datetime.datetime.strptime(raw, "%Y%m%dT%H%M%S")
    except ValueError:
        return None, False, None
    if value.endswith("Z"):
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.date(), False, dt


def _events_raw(text):
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"_raw": {}}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            bits = head.split(";")
            key = bits[0].upper()
            params = {}
            for b in bits[1:]:
                if "=" in b:
                    k, v = b.split("=", 1)
                    params[k.upper()] = v
            cur["_raw"].setdefault(key, []).append((params, value))
    return events


def _first(ev, key):
    got = ev["_raw"].get(key)
    return got[0] if got else (None, None)


def _rrule(ev):
    params, value = _first(ev, "RRULE")
    if not value:
        return None
    out = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v
    return out


def _excluded(ev, day):
    for params, value in ev["_raw"].get("EXDATE", []):
        for chunk in value.split(","):
            d, _, _ = _parse_dt(chunk, params)
            if d == day:
                return True
    return False


def _occurs_on(ev, start_date, rule, day):
    """Does this event have an occurrence on `day`? Narrow by design."""
    if day < start_date:
        return False
    if start_date == day and not rule:
        return True
    if not rule:
        return False

    interval = int(rule.get("INTERVAL", 1) or 1)
    freq = rule.get("FREQ", "").upper()

    until = rule.get("UNTIL")
    if until:
        u, _, _ = _parse_dt(until, {})
        if u and day > u:
            return False

    delta_days = (day - start_date).days
    n = None
    if freq == "DAILY":
        if delta_days % interval:
            return False
        n = delta_days // interval
    elif freq == "WEEKLY":
        byday = [DAYS[d] for d in rule.get("BYDAY", "").split(",") if d in DAYS]
        if byday and day.weekday() not in byday:
            return False
        weeks = (day - start_date).days // 7
        if weeks % interval:
            return False
        n = weeks // interval
        if not byday and day.weekday() != start_date.weekday():
            return False
    elif freq == "MONTHLY":
        months = (day.year - start_date.year) * 12 + (day.month - start_date.month)
        if months < 0 or months % interval or day.day != start_date.day:
            return False
        n = months // interval
    elif freq == "YEARLY":
        years = day.year - start_date.year
        if years % interval or (day.month, day.day) != (start_date.month, start_date.day):
            return False
        n = years // interval
    else:
        return False

    count = rule.get("COUNT")
    if count and n is not None and n >= int(count):
        return False
    return True


def _feed(url):
    """Download and parse once per run. Queried per-day, the naive version
    re-fetched megabytes for every day in the lookahead window."""
    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    with urllib.request.urlopen(url, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    parsed = _events_raw(text)
    _CACHE[url] = (now, parsed)
    return parsed


def _feeds(cfg):
    """Each entry is a URL string, or {url, label, kind}.

    `kind` matters: a feed can be a real calendar, or a signal feed whose
    "events" are markers written by an automation (an Outlook Power Automate
    flow posting an all-day entry when a matching email arrives). The second
    kind must never be rendered as an appointment - it is mail wearing a
    calendar costume.
    """
    out = []
    for entry in cfg.get("ics_urls") or []:
        if isinstance(entry, str):
            out.append({"url": entry, "label": None, "kind": "calendar"})
        else:
            out.append({"url": entry["url"],
                        "label": entry.get("label"),
                        "kind": entry.get("kind", "calendar")})
    return out


def fetch(cfg, day):
    out, overridden = [], set()

    for feed in _feeds(cfg):
        url = feed["url"]
        raw = _feed(url)

        # A modified single occurrence appears as its own VEVENT carrying
        # RECURRENCE-ID; it must replace the generated one, not double it.
        for ev in raw:
            params, value = _first(ev, "RECURRENCE-ID")
            if value:
                d, _, _ = _parse_dt(value, params or {})
                uid = (_first(ev, "UID")[1] or "")
                if d:
                    overridden.add((uid, d))

        for ev in raw:
            if (_first(ev, "STATUS")[1] or "").upper() == "CANCELLED":
                continue
            sp, sv = _first(ev, "DTSTART")
            if not sv:
                continue
            start_date, all_day, start_dt = _parse_dt(sv, sp or {})
            if not start_date:
                continue

            rid = _first(ev, "RECURRENCE-ID")[1]
            uid = _first(ev, "UID")[1] or ""
            rule = _rrule(ev)

            if rid:
                d, _, _ = _parse_dt(rid, _first(ev, "RECURRENCE-ID")[0] or {})
                if start_date != day:
                    continue
            else:
                if (uid, day) in overridden:
                    continue
                if _excluded(ev, day):
                    continue
                if not _occurs_on(ev, start_date, rule, day):
                    continue

            ep, evv = _first(ev, "DTEND")
            _, _, end_dt = _parse_dt(evv, ep or {}) if evv else (None, False, None)

            if start_dt and end_dt:
                shift = day - start_date
                s_iso = (start_dt + shift).isoformat()
                e_iso = (end_dt + shift).isoformat()
            else:
                s_iso, e_iso = day.isoformat(), day.isoformat()

            out.append({
                "source": "mail_signal" if feed["kind"] == "signal" else "calendar",
                "feed": feed["label"],
                "kind": feed["kind"],
                "title": (_first(ev, "SUMMARY")[1] or "(no title)").replace("\\,", ","),
                "start": s_iso,
                "end": e_iso,
                "all_day": all_day,
                "location": (_first(ev, "LOCATION")[1] or None),
                # Signal feeds carry the email's preview text here. ICS escapes
                # commas, semicolons and newlines, so unescape before use.
                "detail": _clean_detail(_first(ev, "DESCRIPTION")[1] or ""),
                "recurring": bool(rule),
            })

    out.sort(key=lambda x: (not x["all_day"], x["start"]))
    return out


def check(cfg, day=None):
    day = day or (datetime.date.today() + datetime.timedelta(days=1))
    n = len(cfg.get("ics_urls") or [])
    if not n:
        return "Calendar SKIPPED - no ics_urls configured"
    return "Calendar OK - %d feed(s), %d events on %s" % (n, len(fetch(cfg, day)), day)
