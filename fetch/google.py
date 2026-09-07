"""Gmail + Google Calendar via REST, using an OAuth refresh token.

One-time setup (see README): create a Google Cloud project, enable the Gmail
and Calendar APIs, make an OAuth client of type "Desktop app", then run

    python3 fetch/google.py auth

which walks the consent screen once and stores the refresh token in the
Keychain. After that the nightly job runs unattended.
"""

import datetime, email.utils, http.server, os, re, sys, urllib.parse, webbrowser
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import creds, httpjson

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/calendar.readonly",
          "https://www.googleapis.com/auth/tasks.readonly"]
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT_PORT = 8731
REDIRECT = "http://127.0.0.1:%d/" % REDIRECT_PORT


# ---------- token handling ----------

def _access_token():
    data = httpjson.request(TOKEN_URL, form=True, data={
        "client_id": creds.get("google_client_id"),
        "client_secret": creds.get("google_client_secret"),
        "refresh_token": creds.get("google_refresh_token"),
        "grant_type": "refresh_token",
    })
    return data["access_token"]


def _auth_headers():
    return {"Authorization": "Bearer " + _access_token()}


# ---------- one-time consent ----------

class _Catch(http.server.BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catch.code = (q.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = _Catch.code is not None
        self.wfile.write(("<h2>%s</h2><p>You can close this tab.</p>" %
                          ("Authorized." if ok else "No code returned.")).encode())

    def log_message(self, *a):
        pass


def authorize():
    client_id = creds.get("google_client_id")
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    })
    url = "%s?%s" % (AUTH_URL, params)
    print("Opening your browser to authorize.\nIf it doesn't open:\n\n  %s\n" % url)
    webbrowser.open(url)

    srv = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _Catch)
    srv.handle_request()
    if not _Catch.code:
        raise SystemExit("Authorization failed - no code returned.")

    tok = httpjson.request(TOKEN_URL, form=True, data={
        "code": _Catch.code,
        "client_id": client_id,
        "client_secret": creds.get("google_client_secret"),
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    })
    if "refresh_token" not in tok:
        raise SystemExit("Google returned no refresh token. Revoke the app's access "
                         "at myaccount.google.com/permissions and run auth again.")
    creds.put("google_refresh_token", tok["refresh_token"])
    print("Refresh token stored in Keychain. Google is set up.")


# ---------- calendar ----------

def _events(cfg, headers, day):
    """Events for the target LOCAL day.

    Two things Google will trip you on: the window has to be built in the
    user's timezone (a UTC window is off by the offset and silently grabs the
    wrong evening), and all-day events carry an EXCLUSIVE end date, so an
    event marked for the 31st comes back as start=31 end=1.
    """
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    lo = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)
    hi = lo + datetime.timedelta(days=1)

    drop_all_day = cfg["google"].get("skip_all_day_events", True)
    ignore = [t.lower() for t in cfg["google"].get("ignore_event_titles", [])]

    out = []
    for cal_id in cfg["google"].get("calendar_ids", ["primary"]):
        url = ("https://www.googleapis.com/calendar/v3/calendars/%s/events"
               "?timeMin=%s&timeMax=%s&singleEvents=true&orderBy=startTime&maxResults=50"
               % (urllib.parse.quote(cal_id, safe=""),
                  urllib.parse.quote(lo.isoformat()), urllib.parse.quote(hi.isoformat())))
        for ev in httpjson.request(url, headers=headers).get("items", []):
            if ev.get("status") == "cancelled":
                continue
            me = next((a for a in ev.get("attendees", []) if a.get("self")), {})
            if me.get("responseStatus") == "declined":
                continue

            s_, e_ = ev.get("start", {}), ev.get("end", {})
            all_day = "date" in s_
            if all_day:
                if drop_all_day:
                    continue
                # Exclusive end: keep only if start <= day < end.
                if not (datetime.date.fromisoformat(s_["date"]) <= day
                        < datetime.date.fromisoformat(e_["date"])):
                    continue

            title = ev.get("summary") or "(no title)"
            if title.lower() in ignore:
                continue

            out.append({
                "source": "calendar",
                "title": title,
                "start": s_.get("dateTime") or s_.get("date"),
                "end": e_.get("dateTime") or e_.get("date"),
                "all_day": all_day,
                "organizer_is_me": (ev.get("organizer") or {}).get("self", False),
                "attendee_count": len(ev.get("attendees", []) or []),
                "location": ev.get("location"),
                "url": ev.get("htmlLink"),
            })

    out.sort(key=lambda x: (not x["all_day"], x["start"] or ""))
    return out


# ---------- google tasks ----------

def _tasks(cfg, headers, target_day):
    """Open items from Google Tasks - the things he put there himself.

    These differ in kind from Jira and GitHub: nothing inferred them from
    system state, he sat down and decided they mattered. Due dates are
    date-only in practice, even though the API returns RFC3339.
    """
    lists = httpjson.request("https://tasks.googleapis.com/tasks/v1/users/@me/lists",
                             headers=headers).get("items", [])
    wanted = cfg["google"].get("task_lists")  # None = every list

    out = []
    for tl in lists:
        if wanted and tl.get("title") not in wanted:
            continue
        url = ("https://tasks.googleapis.com/tasks/v1/lists/%s/tasks"
               "?showCompleted=false&showHidden=false&maxResults=100" % tl["id"])
        for t in httpjson.request(url, headers=headers).get("items", []):
            if t.get("status") == "completed" or not (t.get("title") or "").strip():
                continue
            due = (t.get("due") or "")[:10] or None
            overdue = bool(due and due < target_day.isoformat())
            out.append({
                "source": "tasks",
                "list": tl.get("title"),
                "title": t["title"].strip(),
                "notes": (t.get("notes") or "").strip()[:300] or None,
                "due": due,
                "overdue": overdue,
                "due_today": due == target_day.isoformat(),
                "is_subtask": bool(t.get("parent")),
                "url": t.get("webViewLink"),
            })

    out.sort(key=lambda x: (x["due"] is None, x["due"] or ""))
    return out


# ---------- gmail ----------

def _messages(cfg, headers):
    g = cfg["google"]
    listing = httpjson.request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=%s&maxResults=%d"
        % (urllib.parse.quote(g["gmail_query"]), g.get("max_messages", 30)),
        headers=headers)

    out = []
    for stub in listing.get("messages", []):
        m = httpjson.request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/%s"
            "?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date"
            % stub["id"], headers=headers)
        hdr = {h["name"].lower(): h["value"]
               for h in (m.get("payload") or {}).get("headers", [])}
        frm = hdr.get("from", "")
        name, addr = email.utils.parseaddr(frm)
        out.append({
            "source": "gmail",
            "from": name or addr,
            "from_addr": addr,
            "subject": hdr.get("subject") or "(no subject)",
            "date": hdr.get("date"),
            "snippet": (m.get("snippet") or "")[:300],
            "labels": m.get("labelIds", []),
            "url": "https://mail.google.com/mail/u/0/#inbox/%s" % stub["id"],
        })
    return out


def fetch(cfg, target_day):
    h = _auth_headers()
    look = max(1, cfg["google"].get("lookahead_days", 5))
    upcoming = []
    for n in range(1, look):
        d = target_day + datetime.timedelta(days=n)
        for ev in _events(cfg, h, d):
            ev["day"] = d.isoformat()
            upcoming.append(ev)

    return {"events": _events(cfg, h, target_day),
            "upcoming": upcoming,
            "mail": classify_mail(_messages(cfg, h), cfg),
            "tasks": _tasks(cfg, h, target_day)}


def check(cfg):
    h = _auth_headers()
    prof = httpjson.request("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=h)
    cals = httpjson.request("https://www.googleapis.com/calendar/v3/users/me/calendarList", headers=h)
    try:
        tl = httpjson.request("https://tasks.googleapis.com/tasks/v1/users/@me/lists", headers=h)
        tasks_note = "%d task lists" % len(tl.get("items", []))
    except httpjson.HttpError as ex:
        body = (ex.body or "").lower()
        if "has not been used in project" in body or "is disabled" in body:
            tasks_note = "TASKS API NOT ENABLED in the Cloud project - enable it, then retry"
        elif "insufficient" in body or "scope" in body:
            tasks_note = "TASKS SCOPE MISSING - re-run `python3 fetch/google.py auth`"
        else:
            tasks_note = "tasks error %s: %s" % (ex.status, (ex.body or "")[:160])
    return "Google OK - %s, %d calendars, %s" % (
        prof.get("emailAddress"), len(cals.get("items", [])), tasks_note)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        authorize()
    else:
        print("usage: python3 fetch/google.py auth")


MENTION_RE = None  # built per-call from the configured handle
GH_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")


def classify_mail(messages, cfg):
    """Separate real asks from channel noise, and link chat to code.

    Rocket Chat stamps "You have been mentioned" on the subject of channel
    traffic he is merely subscribed to. The body is the honest signal: only a
    literal @handle is a request addressed to him. Snippets also carry PR
    links, which is how a chat message joins the GitHub data.
    """
    handle = cfg["google"].get("chat_handle") or cfg["google"]["account"].split("@")[0]
    at_me = re.compile(r"@%s\b" % re.escape(handle), re.I)

    now = datetime.datetime.now(datetime.timezone.utc)
    for m in messages:
        try:
            sent = email.utils.parsedate_to_datetime(m.get("date") or "")
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=datetime.timezone.utc)
            m["age_days"] = (now - sent).days
        except Exception:
            m["age_days"] = None

        blob = "%s %s" % (m.get("subject", ""), m.get("snippet", ""))
        is_chat = "rocketchat" in m.get("from_addr", "").lower()
        m["is_chat"] = is_chat
        m["direct_mention"] = bool(is_chat and at_me.search(m.get("snippet", "")))
        m["pr_numbers"] = sorted({int(n) for n in GH_URL_RE.findall(blob)})
        if is_chat:
            ch = re.search(r"in (#[\w-]+)", m.get("subject", ""))
            m["channel"] = ch.group(1) if ch else None
    return messages
