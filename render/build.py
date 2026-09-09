"""Fill the Flex page template from a ranked bundle.

Everything here is presentation. If a field is missing the section degrades to
a quiet line rather than an empty box - a page with a blank EVENTS panel reads
as broken, whereas "nothing scheduled" reads as information.
"""

import datetime, html, os, re, sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
GAP_MIN = 60          # only call out free stretches at least this long

JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
PR_NUM = re.compile(r"#(\d+)")

# Jira status names vary per workflow; map to a stable short label.
STATUS_SHORT = {
    "to do": "Todo", "open": "Todo", "backlog": "Backlog", "selected for development": "Ready",
    "in progress": "In Prog", "in development": "In Prog",
    "in review": "Review", "code review": "Review", "review": "Review",
    "in qa": "QA", "qa": "QA", "testing": "QA",
    "blocked": "Blocked", "done": "Done", "closed": "Done", "resolved": "Done",
}


ORIGIN_ARRAYS = (("jira", "title"), ("github", "title"), ("reminders", "title"),
                 ("todoist", "title"), ("tasks", "title"), ("mail", "subject"),
                 ("mail_signals", "title"))


def origins(bundle):
    """Map each source item's own text back to the array it came from.

    The model writes the `source` chip itself and gets it wrong - a Rocket Chat
    DM in work Gmail came out labelled "reminders". Anything knowable from the
    data should be looked up, not taken on trust.
    """
    out = {}
    for kind, field in ORIGIN_ARRAYS:
        for item in bundle.get(kind) or []:
            text = (item.get(field) or "").strip().lower()
            if len(text) > 8:
                out.setdefault(text, kind)
    return out


def _verify_source(task, origin_map):
    """Prefer the array an item verifiably came from over the model's label."""
    claimed = (task.get("source") or "").strip().lower()
    stated = (task.get("origin") or "").strip().lower()
    if stated and stated in origin_map:
        return origin_map[stated]
    # Fall back to matching the task text against source items.
    text = (task.get("text") or "").strip().lower()
    if len(text) > 12:
        for known, kind in origin_map.items():
            if known in text or text in known:
                return kind
    return claimed or None


def index(bundle):
    """Authoritative lookups. The model chooses what appears; facts come from here."""
    jira = {i["key"]: i for i in bundle.get("jira", []) if i.get("key")}
    prs = {}
    pr_tickets = {}     # PR number -> the ticket(s) it implements
    for l in bundle.get("links", []):
        for pr in l.get("prs", []):
            prs[pr["number"]] = pr
            pr_tickets.setdefault(pr["number"], []).append(l["key"])
    for pr in bundle.get("unlinked_prs", []):
        prs.setdefault(pr["number"], pr)
    for pr in bundle.get("github", []):
        n = PR_NUM.search(pr.get("key") or "")
        if n:
            prs.setdefault(int(n.group(1)), {"kind": pr.get("kind")})
    return {"jira": jira, "prs": prs, "pr_tickets": pr_tickets}


def _chips(task, idx, me):
    """Status pill for Jira, ownership pill for PRs - both from source data."""
    out = []
    blob = "%s %s" % (task.get("key") or "", task.get("text") or "")

    n = PR_NUM.search(blob)
    k = JIRA_KEY.search(blob)

    # A PR-shaped task carries no ticket key in its text, but correlate.py knows
    # which issue it implements. Show it - the PR number alone means nothing
    # when you are reconstructing what a line was about the next morning.
    ticket_keys = []
    if k:
        ticket_keys = [k.group(1)]
    elif n:
        ticket_keys = (idx.get("pr_tickets") or {}).get(int(n.group(1)), [])[:2]
    seen = set()
    for tk in ticket_keys:
        if tk not in seen:
            seen.add(tk)
            out.append('<span class="key ticket">%s</span>' % e(tk))

    if n:
        num = "#%s" % n.group(1)
        if num not in blob.replace(n.group(0), "", 1) or True:
            out.append('<span class="key">%s</span>' % e(num))

    for tk in ticket_keys[:1]:
        st = (idx["jira"].get(tk) or {}).get("status") or ""
        if st:
            out.append('<span class="status">%s</span>'
                       % e(STATUS_SHORT.get(st.lower().strip(), st)))

    if n:
        pr = idx["prs"].get(int(n.group(1)))
        if pr:
            author = pr.get("author")
            if author == me or pr.get("kind") == "mine":
                out.append('<span class="owner mine">yours</span>')
            elif author:
                out.append('<span class="owner">@%s</span>' % e(author))
            elif pr.get("kind") == "review":
                out.append('<span class="owner">review req</span>')
            revs = [r for r in (pr.get("reviewers") or []) if r != me]
            if revs and (author == me or pr.get("kind") == "mine"):
                out.append('<span class="owner">waiting: @%s</span>' % e(revs[0]))
    return out


def e(s):
    return html.escape(str(s), quote=True) if s is not None else ""


def _dt(iso, tz=None):
    """Parse an ICS/API timestamp, always returning an aware datetime.

    Feeds mix forms: Google publishes UTC with a Z, Outlook publishes local
    times with a TZID and no offset. Comparing one against the other raises,
    and it took a crash on a real page to surface it. A naive timestamp from a
    calendar feed is local time by definition, so it gets the reader's zone.
    """
    if not iso or len(iso) <= 10:
        return None
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None and tz is not None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _clock(dt, tz=None):
    """Feed times arrive in UTC. Print them where the reader actually lives,
    on a 12-hour clock, and drop a bare ":00" so the column stays narrow."""
    if tz is not None and dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    out = dt.strftime("%I:%M %p").lstrip("0")
    return out.replace(":00 ", " ")


# ---------- sections ----------

def goals(items):
    if not items:
        return '<div class="empty">No goals carried forward.</div>'
    return "".join(
        '<div class="goal"><span class="horizon">%s</span><span>%s</span></div>'
        % (e(g.get("horizon") or "Goal"), e(g.get("text")))
        for g in items)


def priorities(items):
    if not items:
        return '<li><div class="empty">Nothing critical tomorrow.</div></li>'
    return "".join(
        '<li><div>%s<span class="why">%s</span></div></li>'
        % (e(p.get("text")), e(p.get("why")))
        for p in items)


def tasks(items, idx=None, me=None, capacity=14, origin_map=None):
    if not items:
        return '<div class="empty">Nothing worth a checkbox.</div>'
    idx = idx or {"jira": {}, "prs": {}, "pr_tickets": {}}
    out = []
    for t in items:
        flag = t.get("flag")
        meta = []
        src = _verify_source(t, origin_map or {})
        if src:
            meta.append('<span class="src">%s</span>' % e(src))
        meta.extend(_chips(t, idx, me))
        if t.get("meta"):
            cls = "age hot" if flag == "overdue" else "age"
            meta.append('<span class="%s">%s</span>' % (cls, e(t["meta"])))
        out.append(
            '<div class="task%s"><div class="checkbox"></div><div class="body">'
            '<span class="what">%s</span><div class="meta">%s</div></div></div>'
            % (" flag-%s" % e(flag) if flag else "", e(t.get("text")), "".join(meta)))

    # The paper page has fourteen printed checkboxes whether or not they are all
    # used. Draw the unused ones: they are where he writes his own items in, and
    # without them the column just looks like dead space.
    for _ in range(max(0, capacity - len(items))):
        out.append('<div class="task blank"><div class="checkbox"></div>'
                   '<div class="body"><span class="what">&nbsp;</span></div></div>')
    return "".join(out)


def events(items, tz=None):
    if not items:
        return '<div class="empty">Nothing scheduled.</div>'
    out, prev_end = [], None
    for ev in items:
        start, end = _dt(ev.get("start"), tz), _dt(ev.get("end"), tz)

        if prev_end and start and (start - prev_end).total_seconds() / 60 >= GAP_MIN:
            out.append('<div class="gap-row">%s &ndash; %s &middot; clear</div>'
                       % (_clock(prev_end, tz), _clock(start, tz)))

        if start and end:
            mins = int((end - start).total_seconds() // 60)
            when = ('<span class="time">%s <span class="dur">%dm</span></span>'
                    % (_clock(start, tz), mins))
            prev_end = end
        else:
            when = '<span class="time">all day</span>'

        with_ = []
        if ev.get("organizer_is_me") and (ev.get("attendee_count") or 0) > 1:
            with_.append("you are hosting")
        elif ev.get("attendee_count"):
            with_.append("%d attending" % ev["attendee_count"])
        if ev.get("location"):
            with_.append(e(ev["location"]))

        out.append('<div class="event">%s<span class="what">%s%s</span></div>'
                   % (when, e(ev.get("title")),
                      ('<span class="with">%s</span>' % " &middot; ".join(with_)) if with_ else ""))
    return "".join(out)


def upcoming(items, target_day, days=5, tz=None):
    """Days 2..N as one line each. Deliberately compact: the point is a heads-up
    that Thursday is busy, not a second timeline competing with tomorrow's."""
    if days <= 1:
        return ""

    by_day = {}
    for ev in items or []:
        by_day.setdefault(ev.get("day") or (ev.get("start") or "")[:10], []).append(ev)

    rows = []
    for n in range(1, days):
        d = target_day + datetime.timedelta(days=n)
        evs = sorted(by_day.get(d.isoformat(), []),
                     key=lambda x: (not x.get("all_day"), x.get("start") or ""))
        if evs:
            bits = []
            for ev in evs[:3]:
                start = _dt(ev.get("start"), tz)
                at = ('<span class="at">%s</span> ' % _clock(start, tz)) if start else ""
                bits.append("%s%s" % (at, e(ev.get("title"))))
            if len(evs) > 3:
                bits.append('<span class="at">+%d more</span>' % (len(evs) - 3))
            rows.append('<div class="ahead-day"><span class="dow">%s</span>'
                        '<span class="items">%s</span></div>'
                        % (d.strftime("%a"), " &middot; ".join(bits)))
        else:
            rows.append('<div class="ahead-day free"><span class="dow">%s</span>'
                        '<span class="items">clear</span></div>' % d.strftime("%a"))

    if not rows:
        return ""
    return ('<div class="ahead"><div class="ahead-label">Next %d days</div>%s</div>'
            % (days - 1, "".join(rows)))


def notes(items):
    if not items:
        return '<div class="note"><span>Quiet day. Nothing needs flagging.</span></div>'
    return "".join('<div class="note"><span>%s</span></div>' % e(n) for n in items)


def overflow(dropped):
    if not dropped or not dropped.get("count"):
        return ""
    return ('<p class="overflow"><strong>%s</strong> more open items didn\'t fit &mdash; %s</p>'
            % (e(dropped["count"]), e(dropped.get("summary") or "lower urgency.")))


# ---------- assembly ----------

def build(ranked, cfg, bundle=None, trim=0):
    day = datetime.date.fromisoformat(ranked["target_date"])
    idx = index(bundle or {})
    origin_map = origins(bundle or {})
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    me = (cfg.get("github") or {}).get("login")
    evs = ranked.get("events") or []
    booked = sum(
        int((_dt(x["end"], tz) - _dt(x["start"], tz)).total_seconds() // 60)
        for x in evs if _dt(x.get("start"), tz) and _dt(x.get("end"), tz))

    fields = {
        "DATE_LONG": day.strftime("%A, %-d %B %Y"),
        "DATE_SHORT": day.strftime("%a %-d %b"),
        "STAMP": "Built %s" % datetime.datetime.now().strftime("%H:%M %a"),
        "GOALS": goals(ranked.get("goals")),
        "PRIORITIES": priorities(ranked.get("priorities")),
        "TASKS": tasks(ranked.get("tasks"), idx, me,
                       0 if trim >= 2 else cfg["ranking"].get("max_tasks", 14),
                       origin_map),
        "TASK_COUNT": "%d of %d lines" % (len(ranked.get("tasks") or []),
                                          cfg["ranking"].get("max_tasks", 14)),
        "EVENTS": events(evs, tz),
        "EVENT_COUNT": ("%d &middot; %dh %02dm booked" % (len(evs), booked // 60, booked % 60))
                       if evs else "clear",
        # trim 1 drops the lookahead strip, trim 2 also drops the blank rows.
        "UPCOMING": "" if trim >= 1 else upcoming(
            ranked.get("upcoming"), day,
            (cfg.get("google") or cfg.get("calendar") or {}).get("lookahead_days", 5), tz),
        "NOTES": notes(ranked.get("notes")),
        "OVERFLOW": overflow(ranked.get("dropped")),
        "SOURCES": "Jira &middot; GitHub &middot; Gmail &middot; Calendar",
    }

    page = open(TEMPLATE).read()
    for k, v in fields.items():
        page = page.replace("{{%s}}" % k, v)
    return page


def write(ranked, cfg, bundle=None):
    page = build(ranked, cfg, bundle)
    out = cfg["output"]
    day = ranked["target_date"]

    # Placeholders: {date} {dow} {profile} {d} {m} {y}
    dt = datetime.date.fromisoformat(day)
    stem = out.get("filename", "{date}").format(
        date=day, dow=dt.strftime("%a"), profile=cfg.get("profile", "work"),
        d=dt.strftime("%d"), m=dt.strftime("%b"), y=dt.strftime("%Y"))

    out_dir = config.path(out.get("dir", "out"))
    os.makedirs(out_dir, exist_ok=True)
    dated = os.path.join(out_dir, "%s.html" % stem)
    with open(dated, "w") as f:
        f.write(page)
    # Stable local address; deliberately NOT in the publish folder, since a file
    # that changes every night would create a duplicate note on every sync.
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write(page)

    # publish/ holds exactly one dated file per format - this is what the
    # Evernote sync folder watches.
    pub_dir = config.path(out.get("publish_dir", "publish"))
    os.makedirs(pub_dir, exist_ok=True)

    if out.get("publish_html", True):
        with open(os.path.join(pub_dir, "%s.html" % stem), "w") as f:
            f.write(page)

    pdf_path = None
    if out.get("pdf", True):
        from render import pdf as pdfrender
        target = os.path.join(pub_dir, "%s.pdf" % stem)
        fitcfg = out.get("fit") or {}
        TRIMS = {0: None, 1: "dropped the 4-day lookahead",
                 2: "dropped the lookahead and the blank rows"}
        try:
            result = None
            for level in (0, 1, 2):
                if level:
                    # Re-render the page with less on it, then try to fit again.
                    with open(dated, "w") as f:
                        f.write(build(ranked, cfg, bundle, trim=level))
                result = pdfrender.fit(
                    dated, target,
                    start=fitcfg.get("start_pt", 9.0),
                    floor=fitcfg.get("floor_pt", 7.5),
                    step=fitcfg.get("step_pt", 0.5))
                if result["fitted"]:
                    break
            pdf_path = target
            tries = len(result["attempts"])
            note = "" if tries == 1 else " (%d renders)" % tries
            if result["fitted"]:
                extra = (" - %s" % TRIMS[level]) if level else ""
                print("  PDF: %s - one page at %.1fpt%s%s"
                      % (target, result["pt"], note, extra))
            else:
                print("  PDF: %s - STILL %d PAGES at the %.1fpt floor with everything "
                      "trimmed%s" % (target, result["pages"], result["pt"], note),
                      file=sys.stderr)
                print("       the day is genuinely too full; the ranking should cut harder",
                      file=sys.stderr)
        except Exception as ex:
            # Never let a missing PDF cost the page.
            print("  PDF skipped: %s" % ex, file=sys.stderr)

    return dated, pdf_path
