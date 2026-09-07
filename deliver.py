"""Email the finished page as an attachment.

For anyone whose pages are produced on someone else's machine, a shared folder
or a sync client is one more thing to fail quietly. Mail arrives on the phone,
needs no account setup, and fails loudly.

Standard library only: smtplib and email both ship with Python.
"""

import datetime, mimetypes, os, smtplib, ssl
from email.message import EmailMessage

import creds


def send(cfg, pdf_path, day):
    """Returns a status line, or None when delivery is not configured."""
    conf = (cfg.get("output") or {}).get("email") or {}
    if not conf.get("enabled"):
        return None
    if not os.path.exists(pdf_path):
        raise SystemExit("nothing to send: %s missing" % pdf_path)

    sender = conf["from"]
    recipients = conf["to"] if isinstance(conf["to"], list) else [conf["to"]]
    password = creds.get(conf.get("keychain_key", "personal_mail_app_password"))

    d = datetime.date.fromisoformat(day)
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = conf.get("subject", "Planner page for {dow} {date}").format(
        date=d.strftime("%-d %b"), dow=d.strftime("%A"), iso=day)
    msg.set_content(conf.get("body", "Attached: your page for %s.")
                    % d.strftime("%A %-d %B"))

    ctype, _ = mimetypes.guess_type(pdf_path)
    maintype, subtype = (ctype or "application/pdf").split("/", 1)
    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                           filename=os.path.basename(pdf_path))

    host = conf.get("host", "smtp.mail.yahoo.com")
    port = conf.get("port", 465)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(sender, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(sender, password)
            s.send_message(msg)

    return "emailed %s to %s" % (os.path.basename(pdf_path), ", ".join(recipients))
