"""Personal inbox over IMAP, authenticated with a Gmail app password.

Chosen over both the Gmail API and Mail.app scripting:
  - The Gmail API needs an OAuth app, and a personal account cannot use the
    Internal app that serves work. An External one caps refresh tokens at seven
    days in Testing, which breaks a nightly job weekly.
  - Mail.app via JXA is slow and blocks while the app is busy indexing.
imaplib is standard library, talks to the server directly, and takes about a
second. Requires 2-Step Verification on the account to mint an app password.
"""

import datetime, email, email.header, email.utils, imaplib, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import creds  # noqa: E402

HOST = "imap.gmail.com"   # default; per-account override in config
BODY_CHARS = 400
DEADLINE_CHARS = 1200   # deadline mail buries the date under boilerplate


def _decode(raw):
    if not raw:
        return ""
    out = []
    for chunk, enc in email.header.decode_header(raw):
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", "replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _body_snippet(msg):
    """First text/plain part, collapsed. HTML-only mail returns a stripped tag soup."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    text = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
                break
    else:
        try:
            text = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            text = ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:BODY_CHARS]


def _vip(addr, name, cfg):
    """People. Deterministic on purpose - mail from family must never depend
    on a model's opinion of whether the subject line looked important."""
    addr = (addr or "").lower()
    name = (name or "").lower()
    for entry in cfg.get("vip", []):
        e = entry.lower().strip()
        if not e:
            continue
        if e.startswith("@") and addr.endswith(e):
            return entry
        if e == addr or (" " in e and e in name):
            return entry
    return None


def _watch(addr, subject, cfg):
    """Services worth noticing, split by whether the deadline is recoverable.

    An auction closing or a payment due date passes and is gone. A shipping
    notice can be read whenever. Those deserve different treatment on the page,
    so the tag carries the kind, not just the fact of a match.
    """
    addr = (addr or "").lower()
    subject = (subject or "").lower()

    for word in cfg.get("urgent_topics", []):
        if word.lower() in subject:
            return {"tag": word, "kind": "deadline"}
    for word in cfg.get("info_topics", []):
        if word.lower() in subject:
            return {"tag": word, "kind": "informational"}
    for dom in cfg.get("watch_senders", []):
        if addr.endswith(dom.lower().strip()):
            return {"tag": dom.lstrip("@").split(".")[0], "kind": "unclassified"}
    for word in cfg.get("watch_topics", []):
        if word.lower() in subject:
            return {"tag": word, "kind": "unclassified"}
    return None


def _search(conn, query):
    """Gmail's own search syntax over IMAP. Far more selective than raw IMAP."""
    typ, data = conn.search(None, "X-GM-RAW", '"%s"' % query)
    if typ != "OK":
        return []
    return (data[0] or b"").split()


def _headers(conn, ids):
    """Headers only. Pulling RFC822 for every message drags in attachments and
    costs ~20s for a day's inbox; this is about a second."""
    if not ids:
        return {}
    typ, data = conn.fetch(b",".join(ids),
                           "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
    out, idx = {}, 0
    for part in data:
        if not isinstance(part, tuple):
            continue
        out[ids[idx]] = email.message_from_bytes(part[1])
        idx += 1
        if idx >= len(ids):
            break
    return out


def _snippet(conn, mid, limit=BODY_CHARS):
    """Body text for the few messages that earned a line.

    BODY.PEEK[1] grabs the FIRST MIME part, which in HTML marketing mail is a
    stylesheet - the model was being handed CSS and judging on subject alone.
    Fetch the whole message for these few and walk it for real text instead.
    """
    typ, data = conn.fetch(mid, "(RFC822)")
    for part in data or []:
        if not (isinstance(part, tuple) and part[1]):
            continue
        msg = email.message_from_bytes(part[1])

        plain, html = "", ""
        for sub in (msg.walk() if msg.is_multipart() else [msg]):
            ctype = sub.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                text = sub.get_payload(decode=True).decode(
                    sub.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text

        text = plain or html
        if not plain and html:
            # Drop style and script bodies wholesale before stripping tags,
            # otherwise their contents survive as text.
            text = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", html)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"&[a-z]+;|&#\d+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:limit]
    return ""


def _plain_search(conn, since_days):
    """Standard IMAP SEARCH. Yahoo and most servers have no equivalent of
    Gmail's X-GM-RAW, so there is no category filter to lean on - all the
    noise reduction falls to the VIP list and the ranking pass."""
    since = datetime.date.today() - datetime.timedelta(days=max(1, since_days))
    typ, data = conn.search(None, '(SINCE "%s")' % since.strftime("%d-%b-%Y"))
    return (data[0] or b"").split() if typ == "OK" else []


def fetch(cfg, since_days=1):
    account = cfg["account"]
    host = cfg.get("host", HOST)
    # Keychain key comes from config so more than one person can have a page.
    password = creds.get(cfg.get("keychain_key", "personal_mail_app_password"))
    gmail = "gmail" in host
    window = "newer_than:%dd" % max(1, since_days)

    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(account, password)
        conn.select("INBOX", readonly=True)

        if gmail:
            # Let Gmail drop promotions and social first - it classifies them
            # better than a keyword list would, and saves fetching them at all.
            ids = _search(conn, "in:inbox %s -category:promotions -category:social" % window)

            # Then add anyone on the VIP list back unconditionally, in case
            # Gmail filed a person's mail under a category. People are never noise.
            addrs = [v for v in cfg.get("vip", []) if "@" in v]
            if addrs:
                vip_q = "in:inbox %s (%s)" % (window, " OR ".join("from:%s" % a for a in addrs))
                for mid in _search(conn, vip_q):
                    if mid not in ids:
                        ids.append(mid)
        else:
            ids = _plain_search(conn, since_days)

        ids = ids[-cfg.get("max_messages", 60):]
        headers = _headers(conn, ids)

        out = []
        for mid in ids:
            msg = headers.get(mid)
            if msg is None:
                continue

            frm = _decode(msg.get("From"))
            name, addr = email.utils.parseaddr(frm)
            subject = _decode(msg.get("Subject")) or "(no subject)"
            try:
                sent = email.utils.parsedate_to_datetime(msg.get("Date"))
                age_h = int((datetime.datetime.now(datetime.timezone.utc)
                             - sent.astimezone(datetime.timezone.utc)).total_seconds() // 3600)
                when = sent.isoformat()
            except Exception:
                age_h, when = None, None

            vip = _vip(addr, name, cfg)
            watch = _watch(addr, subject, cfg)

            out.append({
                "source": "mail",
                "from": name or addr,
                "from_addr": addr,
                "subject": subject,
                "date": when,
                "age_hours": age_h,
                "vip": vip,
                "watch": watch,
                # Only the messages that matter get a body fetch.
                "snippet": _snippet(
                    conn, mid,
                    DEADLINE_CHARS if (watch or {}).get("kind") == "deadline"
                    else BODY_CHARS) if (vip or watch) else "",
                "to_me_directly": account.lower() in (msg.get("To") or "").lower(),
            })

        out.sort(key=lambda m: (m["vip"] is None, m["watch"] is None, m["date"] or ""))
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def check(cfg):
    conn = imaplib.IMAP4_SSL(cfg.get("host", HOST))
    try:
        conn.login(cfg["account"],
                   creds.get(cfg.get("keychain_key", "personal_mail_app_password")))
        typ, data = conn.select("INBOX", readonly=True)
        return "Personal mail OK - %s, %s messages in inbox" % (
            cfg["account"], (data[0] or b"?").decode())
    finally:
        try:
            conn.logout()
        except Exception:
            pass


if __name__ == "__main__":
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = json.load(open(os.path.join(root, "config.personal.json")))["mail"]

    # Warm the Keychain OUTSIDE the timer. A first-time read raises a macOS
    # dialog, and waiting for a human to click it is not fetch latency.
    creds.get(cfg.get("keychain_key", "personal_mail_app_password"))

    started = datetime.datetime.now()
    msgs = fetch(cfg, since_days=cfg.get("since_days", 1))
    secs = (datetime.datetime.now() - started).total_seconds()

    vips = [m for m in msgs if m["vip"]]
    watch = [m for m in msgs if m["watch"] and not m["vip"]]
    rest = [m for m in msgs if not m["vip"] and not m["watch"]]

    print("%d messages in %.1fs  |  %d people, %d watched, %d other\n"
          % (len(msgs), secs, len(vips), len(watch), len(rest)))

    for label, group in (("PEOPLE", vips), ("WATCHED", watch)):
        print("--- %s ---" % label)
        for m in group:
            w = m["watch"]
            tag = m["vip"] or (("%s/%s" % (w["tag"], w["kind"])) if w else "")
            print("  [%s] %s" % (tag, m["subject"][:60]))
            print("      from %s <%s>  %sh ago" % (m["from"][:26], m["from_addr"][:34], m["age_hours"]))
        if not group:
            print("  (none)")
        print()

    print("--- OTHER (%d) ---" % len(rest))
    for m in rest[:15]:
        print("  %-30s %s" % (m["from"][:30], m["subject"][:52]))
